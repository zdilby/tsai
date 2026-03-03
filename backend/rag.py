import asyncio
from google import genai
from .db import database
from settings import settings
from pgvector.asyncpg import register_vector, Vector


# 异步查询向量表，返回含溯源信息的 dict 列表
async def query_rag(query_embedding, session_id: str) -> list:
    query = """
        SELECT content, original_content, source_file, chunk_index,
               (embedding <=> $2) AS distance
        FROM knowledge_base
        WHERE session_id = $1
        ORDER BY embedding <=> $2
        LIMIT $3
    """
    vector = Vector(query_embedding)
    async with database._backend._pool.acquire() as conn:
        await register_vector(conn)
        rows = await conn.fetch(query, session_id, vector, settings.top_k)
    return [dict(r) for r in rows]


# 异步获取单条文本嵌入
async def get_embedding(client, text: str):
    resp = await asyncio.to_thread(
            client.models.embed_content,
            model=settings.embedding_model,
            contents=text
        )
    return resp.embeddings[0].values


# 并发批量获取文本嵌入
async def get_embeddings_batch(client, texts: list, batch_size: int = 50) -> list:
    batches = [texts[i:i + batch_size] for i in range(0, len(texts), batch_size)]

    async def _embed_one_batch(batch):
        resp = await asyncio.to_thread(
            client.models.embed_content,
            model=settings.embedding_model,
            contents=batch
        )
        return [e.values for e in resp.embeddings]

    results = await asyncio.gather(*[_embed_one_batch(b) for b in batches])
    return [emb for batch in results for emb in batch]
