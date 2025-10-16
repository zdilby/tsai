import asyncio
from google import genai
from .db import database
from settings import settings
from sqlalchemy import text
from pgvector.asyncpg import register_vector, Vector


# 异步查询向量表
async def query_rag(query_embedding, session_id: str):
    query = """
        SELECT content FROM knowledge_base
        WHERE session_id = $1
        ORDER BY embedding <-> $2
        LIMIT $3
    """
    vector = Vector(query_embedding)
    # rows = await database.fetch_all(query, values={"embedding": vector, "top_k": settings.top_k})
    async with database._backend._pool.acquire() as conn:
        await register_vector(conn)
        rows = await conn.fetch(query, session_id, vector, settings.top_k)
    return [r["content"] for r in rows]


# 异步获取文本嵌入 
async def get_embedding(client, text: str):
    resp = await asyncio.to_thread(
            client.models.embed_content,
            model=settings.embedding_model,
            contents=text
        )
    return resp.embeddings[0].values
