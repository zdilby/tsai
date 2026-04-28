import asyncio
from google import genai
from google.genai import types
from .db import database
from settings import settings, logger
from pgvector.asyncpg import register_vector, Vector


# 动态 TOP_K 选择：
#   1. margin 策略：返回所有距最佳匹配在 margin 范围内的候选（处理均匀分布的相关内容）
#   2. gap 策略：若距离出现明显跳变，在跳变处截断（处理相关/不相关的明显分界）
#   两种策略取更大值，结果限定在 [min_k, top_k_max]
def _dynamic_select(candidates: list, min_k: int, max_k: int,
                    margin: float, gap_threshold: float) -> list:
    if len(candidates) <= min_k:
        return candidates
    distances = [r['distance'] for r in candidates]

    # 策略1：margin from best
    best_dist = distances[0]
    margin_count = sum(1 for d in distances if d <= best_dist + margin)
    margin_cut = max(min_k, margin_count)

    # 策略2：gap detection（从 min_k 位置开始找最大跳变）
    best_cut, best_gap = min_k, 0.0
    for i in range(min_k, len(distances)):
        gap = distances[i] - distances[i - 1]
        if gap > best_gap:
            best_gap = gap
            best_cut = i
    gap_cut = best_cut if best_gap >= gap_threshold else min_k

    final_cut = min(max(margin_cut, gap_cut), max_k)
    return candidates[:final_cut]


# 异步查询向量表，返回含溯源信息的 dict 列表
# 先取 top_k_max 候选，再用距离间隔算法动态决定实际返回数量
async def query_rag(query_embedding, session_id: str, source_files: list = None) -> list:
    source_filter = "AND source_file = ANY($5)" if source_files else ""
    query = f"""
        SELECT content, original_content, source_file, chunk_index,
               (embedding <=> $2) AS distance
        FROM knowledge_base
        WHERE session_id = $1
          AND source_file IS NOT NULL
          AND (embedding <=> $2) < $3
          {source_filter}
        ORDER BY embedding <=> $2
        LIMIT $4
    """
    vector = Vector(query_embedding)
    async with database._backend._pool.acquire() as conn:
        await register_vector(conn)
        await conn.execute(f"SET LOCAL hnsw.ef_search = {settings.hnsw_ef_search}")
        args = [session_id, vector, settings.rag_distance_threshold, settings.top_k_max]
        if source_files:
            args.append(source_files)
        rows = await conn.fetch(query, *args)
    candidates = [dict(r) for r in rows]
    selected = _dynamic_select(
        candidates, settings.top_k, settings.top_k_max,
        settings.top_k_margin, settings.top_k_gap
    )
    distances = [round(r['distance'], 3) for r in candidates]
    logger.info("RAG: %d候选%s → 选取%d条 (margin=%.2f, gap=%.2f)",
                len(candidates), distances, len(selected),
                settings.top_k_margin, settings.top_k_gap)
    return selected


# 语义检索历史消息（仅 assistant，用于回答「我们聊过 X 吗」类问题）
async def query_history(query_embedding, session_id: str,
                        limit: int = 3, threshold: float = 0.4,
                        before_id: int = None) -> list:
    before_filter = "AND id < $5" if before_id is not None else ""
    query = f"""
        SELECT id, content, created_at,
               (embedding <=> $2) AS distance
        FROM messages
        WHERE session_id = $1
          AND role = 'assistant'
          AND embedding IS NOT NULL
          AND (embedding <=> $2) < $3
          {before_filter}
        ORDER BY embedding <=> $2
        LIMIT $4
    """
    vector = Vector(query_embedding)
    async with database._backend._pool.acquire() as conn:
        await register_vector(conn)
        args = [session_id, vector, threshold, limit]
        if before_id is not None:
            args.append(before_id)
        rows = await conn.fetch(query, *args)
    return [
        {
            "content": row["content"],
            "snippet": row["content"][:300].strip(),
            "created_at": row["created_at"],
            "distance": round(row["distance"], 3),
        }
        for row in rows
    ]


# ── 全量上下文支持 ─────────────────────────────────────────────────────────────
# 启发式 token 估算：中文 ~2 字符/token，英文 ~4 字符/token，混合内容取 2.5。
# 用于 /chat 路由判断 session 总语料是否能直接放进 Gemini 1M 窗口。
_CHARS_PER_TOKEN = 2.5


def estimate_tokens(text: str) -> int:
    """估算单段文本的 token 数（启发式，零成本）。"""
    return int(len(text) / _CHARS_PER_TOKEN) if text else 0


# 估算 session 全部知识库语料的 token 总量
async def estimate_session_tokens(session_id: str) -> int:
    row = await database.fetch_one(
        "SELECT COALESCE(SUM(LENGTH(COALESCE(original_content, content))), 0) AS total_chars "
        "FROM knowledge_base "
        "WHERE session_id = :sid AND source_file IS NOT NULL",
        {"sid": session_id},
    )
    total_chars = int(row["total_chars"]) if row else 0
    return int(total_chars / _CHARS_PER_TOKEN)


# 拉取 session 全部 chunk，按 (source_file, chunk_index) 排序保留文档原顺序
async def get_all_session_chunks(session_id: str) -> list:
    rows = await database.fetch_all(
        "SELECT source_file, chunk_index, "
        "       COALESCE(original_content, content) AS content "
        "FROM knowledge_base "
        "WHERE session_id = :sid AND source_file IS NOT NULL "
        "ORDER BY source_file, chunk_index",
        {"sid": session_id},
    )
    return [dict(r) for r in rows]


# Agent tool 用：列出 session 内所有上传文档（含 chunk 数）
async def list_session_documents(session_id: str) -> list:
    rows = await database.fetch_all(
        "SELECT source_file, COUNT(*) AS chunk_count, "
        "       SUM(LENGTH(COALESCE(original_content, content))) AS total_chars "
        "FROM knowledge_base "
        "WHERE session_id = :sid AND source_file IS NOT NULL "
        "GROUP BY source_file "
        "ORDER BY source_file",
        {"sid": session_id},
    )
    return [dict(r) for r in rows]


# Agent tool 用：拉取指定文件的全部 chunk 拼成完整文档（chunk_index 升序）
# 返回上限 30_000 字符，超出截断（避免 Agent 单次 read 撑爆上下文）
async def get_full_document(session_id: str, filename: str, max_chars: int = 30_000) -> str:
    rows = await database.fetch_all(
        "SELECT chunk_index, COALESCE(original_content, content) AS content "
        "FROM knowledge_base "
        "WHERE session_id = :sid AND source_file = :fn "
        "ORDER BY chunk_index",
        {"sid": session_id, "fn": filename},
    )
    if not rows:
        return ""
    full = "\n\n".join(r["content"] for r in rows)
    if len(full) > max_chars:
        full = full[:max_chars] + f"\n\n... (truncated at {max_chars} chars)"
    return full


_embed_config = types.EmbedContentConfig(output_dimensionality=settings.embedding_dim)


# 异步获取单条文本嵌入
async def get_embedding(client, text: str):
    resp = await asyncio.to_thread(
            client.models.embed_content,
            model=settings.embedding_model,
            contents=text,
            config=_embed_config,
        )
    return resp.embeddings[0].values


# 顺序批量获取文本嵌入，遇到 429 限流时指数退避重试
async def get_embeddings_batch(client, texts: list, batch_size: int = 50, max_retries: int = 6) -> list:
    batches = [texts[i:i + batch_size] for i in range(0, len(texts), batch_size)]
    all_embeddings = []

    for idx, batch in enumerate(batches):
        for attempt in range(max_retries):
            try:
                resp = await asyncio.to_thread(
                    client.models.embed_content,
                    model=settings.embedding_model,
                    contents=batch,
                    config=_embed_config,
                )
                all_embeddings.extend(e.values for e in resp.embeddings)
                break
            except Exception as e:
                if '429' in str(e) or 'RESOURCE_EXHAUSTED' in str(e):
                    wait = 30 * (2 ** attempt)  # 30s, 60s, 120s, ...
                    logger.warning(
                        "Embedding batch %d/%d 触发限流，%ds 后重试 (第 %d 次)",
                        idx + 1, len(batches), wait, attempt + 1
                    )
                    await asyncio.sleep(wait)
                else:
                    raise
        else:
            raise RuntimeError(f"Embedding batch {idx + 1} 超过最大重试次数 ({max_retries})")

    return all_embeddings
