"""
Phase 3b — 机器人用户：复刻"天书"的 session、每日自动跑 5 条测试 query。

设计要点：
  • 一次性 snapshot：把"天书"所有 named session 的 (sessions + knowledge_base)
    复制给机器人用户。messages 不复制（机器人需要干净的对话历史）。
  • 每日任务从 10 个 query 模板中按日期轮选 5 个，每天针对 1 个 bot session 跑。
  • 机器人调 /chat 不走 HTTP——直接复用 main.py 的 chat handler 业务逻辑会引入
    auth 耦合，这里改成"内部直跑模式"：复刻 chat handler 的核心调用顺序，绕过 JWT。
  • 所有 bot trace 自然落进 agent_traces 表（user_id = bot.id）。
"""
import asyncio
import random
import time
import uuid
from datetime import datetime
from typing import Any

from pgvector.asyncpg import register_vector
from passlib.context import CryptContext

from settings import settings, logger, embed_client
from .db import (
    database,
    get_context,
    save_message,
    update_message_embedding,
    record_trace,
    set_subsystem_enabled,
    heartbeat_subsystem,
)
from .agent_chat import needs_agent, run_agent_chat
from .rag import (
    estimate_session_tokens,
    get_embedding,
    query_rag,
    query_history,
    get_all_session_chunks,
)


pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── 用户管理 ────────────────────────────────────────────────────────────────

async def ensure_bot_user() -> dict:
    """如果机器人用户不存在则创建（密码用一个随机串，永远不会用 password 登录）。"""
    name = settings.bot_username
    row = await database.fetch_one(
        "SELECT id, username, max_daily_tokens, max_file_size_mb FROM users WHERE username = :n",
        values={"n": name},
    )
    if row:
        return dict(row)

    # 随机 32 字节密码（机器人不用密码登录，只通过内部任务调用）
    pwd_hash = pwd_ctx.hash(uuid.uuid4().hex)
    await database.execute(
        "INSERT INTO users (username, password_hash, is_admin, max_daily_tokens, max_file_size_mb) "
        "VALUES (:u, :p, FALSE, 0, 0)",
        values={"u": name, "p": pwd_hash},
    )
    row = await database.fetch_one(
        "SELECT id, username, max_daily_tokens, max_file_size_mb FROM users WHERE username = :n",
        values={"n": name},
    )
    logger.info("Bot user created: id=%d username=%r", row["id"], name)
    return dict(row)


async def get_user_by_username(username: str) -> dict | None:
    row = await database.fetch_one(
        "SELECT id, username FROM users WHERE username = :n",
        values={"n": username},
    )
    return dict(row) if row else None


# ── Session Snapshot ─────────────────────────────────────────────────────────

async def snapshot_user_sessions(source_username: str, target_user_id: int) -> dict:
    """
    把 source_username 的所有 named session 复刻给 target_user_id。
    复制 sessions 行 + knowledge_base 行（含 pgvector embedding）。
    messages 不复制。

    实现关键：所有操作在**单个原生 asyncpg 连接**内完成，注册 vector codec 后
    SELECT 和 INSERT 对 embedding 列的处理保持一致——避免 databases 库（无 codec）
    和 raw asyncpg（有 codec）混用导致的类型不匹配。

    幂等性：每次调用都**追加**新副本。重复调用会产生重复 session。
    """
    src = await get_user_by_username(source_username)
    if not src:
        raise ValueError(f"源用户不存在：{source_username!r}")

    sessions_copied = 0
    chunks_copied = 0

    async with database._backend._pool.acquire() as conn:
        await register_vector(conn)

        src_sessions = await conn.fetch(
            "SELECT id, name, persona, system_instruction_origin, system_instruction "
            "FROM sessions WHERE user_id = $1 AND name IS NOT NULL "
            "ORDER BY created_at",
            src["id"],
        )

        if not src_sessions:
            logger.warning("源用户 %r 没有 named session，snapshot 跳过", source_username)
            return {"sessions_copied": 0, "chunks_copied": 0}

        async with conn.transaction():
            for src_sess in src_sessions:
                new_sid = uuid.uuid4()
                new_name = f"[bot] {src_sess['name']}"
                await conn.execute(
                    """INSERT INTO sessions (id, user_id, name, persona,
                                             system_instruction_origin, system_instruction)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    new_sid, target_user_id, new_name,
                    src_sess["persona"],
                    src_sess["system_instruction_origin"],
                    src_sess["system_instruction"],
                )
                sessions_copied += 1

                chunk_rows = await conn.fetch(
                    "SELECT content, original_content, source_file, chunk_index, embedding "
                    "FROM knowledge_base WHERE session_id = $1",
                    src_sess["id"],
                )
                for c in chunk_rows:
                    await conn.execute(
                        """INSERT INTO knowledge_base
                             (content, original_content, source_file, chunk_index,
                              embedding, session_id)
                           VALUES ($1, $2, $3, $4, $5, $6)""",
                        c["content"], c["original_content"], c["source_file"],
                        c["chunk_index"], c["embedding"], new_sid,
                    )
                chunks_copied += len(chunk_rows)
                logger.info(
                    "Snapshot: %r → %r (%d chunks)",
                    src_sess["name"], new_name, len(chunk_rows),
                )

    return {"sessions_copied": sessions_copied, "chunks_copied": chunks_copied}


async def list_bot_sessions(bot_user_id: int) -> list[dict]:
    """机器人当前持有的所有 named session。"""
    rows = await database.fetch_all(
        "SELECT id, name FROM sessions WHERE user_id = :uid AND name IS NOT NULL "
        "ORDER BY created_at",
        values={"uid": bot_user_id},
    )
    return [dict(r) for r in rows]


async def list_bot_non_empty_sessions(bot_user_id: int) -> list[dict]:
    """只返回 KB 里有 chunk 的 bot session。空 session 测不出 RAG/AGENT 路径。"""
    rows = await database.fetch_all(
        """SELECT s.id, s.name
           FROM sessions s
           WHERE s.user_id = :uid AND s.name IS NOT NULL
             AND EXISTS (
                 SELECT 1 FROM knowledge_base kb
                 WHERE kb.session_id = s.id AND kb.source_file IS NOT NULL
             )
           ORDER BY s.created_at""",
        values={"uid": bot_user_id},
    )
    return [dict(r) for r in rows]


# ── Query 生成 ──────────────────────────────────────────────────────────────

# 10 个 query 模板，对应早先讨论的测试用例
# 标 needs_kb_filename=True 的需要从 session 知识库挑一个真实文件名填入
_QUERY_TEMPLATES: list[dict] = [
    # 路由层（不该进 Agent）
    {"id": "greeting",       "tpl": "你好",                                                     "kind": "small_talk"},
    {"id": "thanks",         "tpl": "谢谢",                                                     "kind": "small_talk"},
    {"id": "short_factual",  "tpl": "这份资料的标题是什么",                                     "kind": "rag"},
    # 工具层（应进 Agent）
    {"id": "list_docs",      "tpl": "我都上传了哪些文档？",                                      "kind": "agent"},
    {"id": "summary",        "tpl": "请总结一下《{filename}》这份资料的核心内容", "kind": "agent", "needs_filename": True},
    {"id": "compare",        "tpl": "这份资料和一般同类资料有什么区别？",                        "kind": "agent"},
    {"id": "recall",         "tpl": "你还记得我们之前聊过的内容吗？现在情况怎么样了？",            "kind": "agent"},
    {"id": "open_ended",     "tpl": "请分析一下这份资料的写作风格和组织结构",                     "kind": "agent"},
    {"id": "multi_question", "tpl": "这份资料的主要观点是什么？又是怎么论证的？",                  "kind": "agent"},
    # 反幻觉
    {"id": "hallucination",  "tpl": "「天体粒子物理量子重力学方程」在你的资料里是怎么解释的？",     "kind": "agent"},
]


async def select_daily_query_specs(bot_session_id: str) -> list[dict]:
    """
    每次调用都重新抽样 5 个模板（不复用 day_seed —— 同一天多次"立即跑一次"
    会拿到不同的 query 组合，便于产生更多样的 trace 数据供 Agent B 分析）。
    需要文件名的模板会从该 session KB 里随机选文件。
    返回 [{"template_id", "kind", "query"}] 5 条。
    """
    rng = random.Random()  # 不种子 = 每次抽样都重新随机
    picked = rng.sample(_QUERY_TEMPLATES, k=5)

    # 拿一份 session 的所有文件名，供需要的模板填空
    files_rows = await database.fetch_all(
        "SELECT DISTINCT source_file FROM knowledge_base "
        "WHERE session_id = :sid AND source_file IS NOT NULL "
        "ORDER BY source_file",
        values={"sid": bot_session_id},
    )
    filenames = [r["source_file"] for r in files_rows]

    out: list[dict] = []
    for tpl in picked:
        text = tpl["tpl"]
        if tpl.get("needs_filename"):
            if not filenames:
                # 该 session 没有文件，把"summary"模板降级成通用问题
                text = "请总结一下当前 session 内的所有上传资料"
            else:
                text = text.format(filename=rng.choice(filenames))
        out.append({"template_id": tpl["id"], "kind": tpl["kind"], "query": text})
    return out


# ── 内部直跑模式：复用 chat handler 的核心调用顺序 ─────────────────────────────

# 复用 main.py 里的回忆触发词
import re as _re
_RECALL_PATTERNS = _re.compile(
    r'(你还记得|还记得|你记得|记得吗|之前(我们|你|咱们)?|上次(你|我们)?|'
    r'我们(之前|以前|上次)?聊过|你(之前|以前|上次)?提到|我(之前|以前)?问过|'
    r'我们讨论过|你说过|你提过|前面(你|我们)?)[^，。？！,.?!]*[，。？！,.?!]?',
    _re.UNICODE
)


async def run_query_internally(
    *, query: str, session_id: str, user_id: int, template_id: str | None = None,
) -> dict:
    """
    机器人内部触发的 /chat 调用。复刻 main.py:chat 的核心流程但绕开 HTTP/JWT。
    最终也走 record_trace（与真人 query 进同一张表，可统一分析）。

    返回 {answer, route, iterations, duration_ms}
    """
    from settings import client          # 延迟导入避免循环
    from google.genai import types
    from midware.tools import fetch_from_web
    from .db import (
        get_session_persona,
    )

    t0 = time.monotonic()

    # 1. 保存 user 消息（机器人也算"用户"，需要走 messages 流程）
    await save_message(session_id, "user", query)

    # 2. 历史
    context = await get_context(session_id, limit=settings.max_history_turns)
    context_text = "\n".join(f"{c['role']}: {c['content']}" for c in context)

    # 3. 路由判定
    session_tokens = await estimate_session_tokens(session_id)
    use_full_context = 0 < session_tokens < settings.full_context_threshold
    use_agent = (
        not use_full_context
        and session_tokens > 0
        and settings.agent_chat_enabled
        and needs_agent(query)
    )
    route = (
        "full_context" if use_full_context
        else "agent" if use_agent
        else ("rag" if session_tokens > 0 else "empty_kb")
    )
    logger.info(
        "[bot] /chat session=%s tokens≈%d → %s (template=%s)",
        session_id, session_tokens, route.upper(), template_id,
    )

    # 4. Agent 路径
    if use_agent:
        persona = await get_session_persona(session_id)
        web_info = await fetch_from_web(query)
        result = await run_agent_chat(
            query=query, session_id=session_id, persona=persona,
            history_text=context_text, web_info=web_info,
        )
        answer = result["answer"]
        msg_id = await save_message(
            session_id, "assistant", answer,
            tokens_in=result["tokens_in"], tokens_out=result["tokens_out"],
            tokens_total=result["tokens_in"] + result["tokens_out"],
        )
        # 后台存 embedding（这里直接 await，机器人任务不在乎多 200ms）
        try:
            emb = await get_embedding(embed_client, answer[:2000])
            await update_message_embedding(msg_id, emb)
        except Exception as e:
            logger.warning("[bot] history embedding failed: %s", e)

        await record_trace(
            session_id=session_id, user_id=user_id, message_id=msg_id,
            query=query, route="agent",
            tools_called=result["agent_trace"], iterations=result["iterations"],
            citations=result["citations"],
            tokens_in=result["tokens_in"], tokens_out=result["tokens_out"],
            duration_ms=int((time.monotonic() - t0) * 1000),
            prompt_version_id=result.get("prompt_version_id"),
        )
        return {"answer": answer, "route": "agent",
                "iterations": result["iterations"],
                "duration_ms": int((time.monotonic() - t0) * 1000)}

    # 5. Phase 1 路径（rag / full_context / empty_kb）
    query_embedding, web_info = await asyncio.gather(
        get_embedding(embed_client, query),
        fetch_from_web(query),
    )
    oldest_recent_id = context[0]["id"] if context else None
    recall_q = _RECALL_PATTERNS.sub('', query).strip()
    is_recall = bool(recall_q and recall_q != query and len(recall_q) >= 4)
    history_embedding = (
        await get_embedding(embed_client, recall_q) if is_recall else query_embedding
    )
    history_threshold = 0.55 if is_recall else 0.4

    if use_full_context:
        all_chunks, history_results = await asyncio.gather(
            get_all_session_chunks(session_id),
            query_history(history_embedding, session_id=session_id,
                          before_id=oldest_recent_id, threshold=history_threshold),
        )
        by_file: dict[str, list[str]] = {}
        for c in all_chunks:
            by_file.setdefault(c["source_file"], []).append(c["content"])
        rag_text = "\n\n".join(f"=== 文件：{src} ===\n" + "\n".join(parts)
                               for src, parts in by_file.items())
        rag_citations = [{"source": src, "chunk": None, "score": 1.0, "snippet": ""}
                         for src in by_file]
        has_kb = bool(by_file)
    else:
        rag_results, history_results = await asyncio.gather(
            query_rag(query_embedding, session_id=session_id),
            query_history(history_embedding, session_id=session_id,
                          before_id=oldest_recent_id, threshold=history_threshold),
        )
        rag_text = "\n".join(r["content"] for r in rag_results)
        rag_citations = [
            {"source": r["source_file"], "chunk": r["chunk_index"],
             "score": round(1 - r["distance"], 3),
             "snippet": (r.get("original_content") or "")[:200].strip()}
            for r in rag_results
        ]
        has_kb = bool(rag_results)

    rag_section = (
        f"All uploaded documents in this session (full content):\n{rag_text}\n\n"
        if (use_full_context and has_kb)
        else f"Relevant info from uploaded documents:\n{rag_text}\n\n"
        if has_kb
        else "Relevant info from uploaded documents:\n（当前问题在知识库中未找到相关文档内容）\n\n"
    )
    web_section = f"Latest info from web:\n{web_info}\n\n" if web_info else ""
    history_section = ""
    if history_results:
        items = "\n".join(
            f"[{r['created_at'].strftime('%Y-%m-%d') if hasattr(r['created_at'], 'strftime') else str(r['created_at'])[:10]}] "
            f"{r['snippet']}{'…' if len(r['content']) > 300 else ''}"
            for r in history_results
        )
        history_section = f"Relevant excerpts from past conversation in this session:\n{items}\n\n"

    prompt = (
        f"Context:\n{context_text}\n\n"
        f"{history_section}{rag_section}{web_section}"
        f"如果回答引用了上传文档的原文或观点，请在该句末尾用括号标注来源，格式为（来源：文件名，第N段）。直接引用原文时请加引号。\n"
        f"User: {query}\nAI:"
    )

    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    persona = await get_session_persona(session_id)
    config = types.GenerateContentConfig(
        tools=[grounding_tool],
        system_instruction=persona if persona else None,
    )
    chat = client.aio.chats.create(model=settings.generation_model, config=config)
    resp = await chat.send_message(prompt)
    answer = resp.text or ""
    usage = resp.usage_metadata
    tokens_in = getattr(usage, "prompt_token_count", 0) or 0
    tokens_out = getattr(usage, "candidates_token_count", 0) or 0

    msg_id = await save_message(
        session_id, "assistant", answer,
        tokens_in=tokens_in, tokens_out=tokens_out, tokens_total=tokens_in + tokens_out,
    )
    try:
        emb = await get_embedding(embed_client, answer[:2000])
        await update_message_embedding(msg_id, emb)
    except Exception as e:
        logger.warning("[bot] history embedding failed: %s", e)

    await record_trace(
        session_id=session_id, user_id=user_id, message_id=msg_id,
        query=query, route=route,
        tools_called=[], iterations=1,
        citations=rag_citations,
        tokens_in=tokens_in, tokens_out=tokens_out,
        duration_ms=int((time.monotonic() - t0) * 1000),
        prompt_version_id=None,
    )
    return {"answer": answer, "route": route, "iterations": 1,
            "duration_ms": int((time.monotonic() - t0) * 1000)}


# ── 每日入口 ────────────────────────────────────────────────────────────────

async def run_bot_daily() -> dict:
    """
    每日任务入口（由 Celery 调度）。
    顺序：
      1. 检查 subsystem_status.bot.enabled — 不开就跳过
      2. ensure_bot_user
      3. 选 1 个 bot session（按 day_seed 轮换）
      4. 生成 5 个 query
      5. 顺序执行（不并发——避免短时间打爆 Gemini quota）
      6. heartbeat 更新
    """
    bot = await ensure_bot_user()

    # 检查启停状态
    row = await database.fetch_one(
        "SELECT enabled FROM subsystem_status WHERE component = 'bot'"
    )
    if not row or not row["enabled"]:
        logger.info("[bot] subsystem disabled, skipping daily run")
        return {"skipped": True, "reason": "disabled"}

    # 优先选有 chunk 的 session —— 全空就退化到任意 session（结果都会是 empty_kb，
    # 但至少 bot 跑过、产生了路由分布数据）
    non_empty = await list_bot_non_empty_sessions(bot["id"])
    if non_empty:
        sessions = non_empty
    else:
        sessions = await list_bot_sessions(bot["id"])
        if not sessions:
            logger.warning("[bot] no sessions to query (run snapshot first)")
            await heartbeat_subsystem("bot", "skipped: no sessions")
            return {"skipped": True, "reason": "no_sessions"}

    # 随机选 session（替代之前的"按日期轮换"）—— 配合每次随机 5 个 query，
    # 确保连续多次"立即跑一次"产生的交流都是新的：不同 session × 不同 query 组合。
    sess = random.choice(sessions)
    sess_id = str(sess["id"])

    specs = await select_daily_query_specs(sess_id)
    logger.info(
        "[bot] daily run: session=%r → %d queries: %s",
        sess["name"], len(specs), [s["template_id"] for s in specs],
    )

    results: list[dict] = []
    for spec in specs:
        try:
            res = await run_query_internally(
                query=spec["query"], session_id=sess_id, user_id=bot["id"],
                template_id=spec["template_id"],
            )
            results.append({"template_id": spec["template_id"], **res})
        except Exception as e:
            logger.exception("[bot] query failed (template=%s): %s", spec["template_id"], e)
            results.append({"template_id": spec["template_id"], "error": str(e)})

    await heartbeat_subsystem("bot", f"daily run: {len(results)} queries on {sess['name']}")
    return {
        "session": sess["name"],
        "queries_run": len(results),
        "results": results,
    }
