from fastapi import FastAPI, Request, Form, Query, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google import genai
from google.genai import types
from pgvector.asyncpg import register_vector, Vector
import uuid
from settings import settings, client
from account import router as account_router, get_current_user
from backend.db import database, init_db, save_message, get_context, session_exists, add_knowledge, get_user_today_tokens
from backend.rag import get_embedding, query_rag
from midware.tools import fetch_from_web
from midware.upload import router as upload_router, upload_file
from admin import admin_router

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(account_router, prefix="/account", tags=["account"])
app.include_router(upload_router, prefix="/upload", tags=["upload"])
app.include_router(admin_router, prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
async def startup():
    await database.connect()
    # await init_db()
    async with database._backend._pool.acquire() as conn:
        await register_vector(conn)


@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return RedirectResponse("/account/login")
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, session_id: str = Query(None), user=Depends(get_current_user)):
    if not session_id:
        session_id = str(uuid.uuid4())
        await new_null_session(session_id, user["id"])
        session_ex = False
    elif await session_exists(session_id):
        session_ex = True
    else:
        session_ex = False
    return templates.TemplateResponse("chat.html", {"request": request, "session_id": session_id, "session_exists": session_ex, "user": user["username"]})


@app.get("/ping")
async def ping():
    return {"status": "ok"}


@app.post("/chat")
async def chat(session_id: str = Form(...), message: str = Form(...),
               source_files: str = Form(""), user=Depends(get_current_user)):
    # 检查每日 Token 配额
    max_tokens = user["max_daily_tokens"] or 0
    if max_tokens > 0:
        today_used = await get_user_today_tokens(user["id"])
        if today_used >= max_tokens:
            raise HTTPException(status_code=429, detail=f"今日 Token 配额已用完（上限 {max_tokens}）")

    # 保存用户消息
    await save_message(session_id, "user", message)
    # print('message: ', message)

    # 获取历史上下文
    context = await get_context(session_id)
    context_text = "\n".join([f"{c['role']}: {c['content']}" for c in context])

    # RAG 查询（如果前端指定了书籍则只查指定书）
    source_list = [s.strip() for s in source_files.split(',') if s.strip()] if source_files else None
    query_embedding = await get_embedding(client, message)
    rag_results = await query_rag(query_embedding, session_id=session_id, source_files=source_list)
    rag_text = "\n".join([r["content"] for r in rag_results])
    rag_citations = [
        {
            "source": r["source_file"],
            "chunk": r["chunk_index"],
            "score": round(1 - r["distance"], 3),
            "snippet": (r.get("original_content") or "")[:200].strip(),
        }
        for r in rag_results if r.get("source_file")
    ]

    # 执行 Google 搜索获取最新网络信息（不再写入 knowledge_base，避免污染 RAG 向量空间）
    web_info = await fetch_from_web(message)

    # 构建提示词 prompt
    rag_section = (
        f"Relevant info from uploaded documents:\n{rag_text}\n\n"
        if rag_results else
        "Relevant info from uploaded documents:\n（当前问题在知识库中未找到相关文档内容）\n\n"
    )
    web_section = f"Latest info from web:\n{web_info}\n\n" if web_info else ""
    prompt = (
        f"Context:\n{context_text}\n\n"
        f"{rag_section}"
        f"{web_section}"
        f"如果回答引用了上传文档的原文或观点，请在该句末尾用括号标注来源，格式为（来源：文件名，第N段）。直接引用原文时请加引号。\n"
        f"User: {message}\nAI:"
    )
    # print('prompt: ', prompt)

    # 设置前置的Grounding with Google Search
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(tools=[grounding_tool])

    # 调用 Gemini 生成回答
    chat = client.aio.chats.create(model=settings.generation_model, config=config)
    resp = await chat.send_message(prompt)
    answer = resp.text
    usage = resp.usage_metadata
    tokens_in  = getattr(usage, "prompt_token_count",     0) or 0
    tokens_out = getattr(usage, "candidates_token_count", 0) or 0
    tokens_total = getattr(usage, "total_token_count",    0) or 0
    await save_message(session_id, "assistant", answer,
                       tokens_in=tokens_in, tokens_out=tokens_out, tokens_total=tokens_total)
    return JSONResponse({"answer": answer, "citations": rag_citations})


@app.post("/new_session")
async def new_session(name: str = Form(None), user=Depends(get_current_user)):
    session_id = str(uuid.uuid4())
    if not name:
        name = "未命名对话"
    query = "INSERT INTO sessions (id, name, user_id) VALUES (:id, :name, :user_id)"
    try:
        await database.execute(query, values={"id": session_id, "name": name, "user_id": user["id"]})
        return {"id": session_id, "name": name, "success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/change_session")
async def change_session(session_id: str = Form(...), name: str = Form(...), user=Depends(get_current_user)):
    query = "UPDATE sessions SET name = :name WHERE id = :id AND user_id = :user_id"
    try:
        await database.execute(query, values={"id": session_id, "name": name, "user_id": user["id"]})
        return {"id": session_id, "name": name, "success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/del_session")
async def del_session(session_id: str = Form(...), user=Depends(get_current_user)):
    query = "DELETE FROM sessions WHERE id = :id AND user_id = :user_id"
    try:
        await database.execute(query, values={"id": session_id, "user_id": user["id"]})
        return {"id": session_id, "success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/save_to_rag")
async def save_to_rag(
    session_id: str = Form(...),
    content: str = Form(...),
    user=Depends(get_current_user)
):
    if not await session_exists(session_id):
        raise HTTPException(status_code=403, detail="仅命名会话可保存到知识库")
    embedding = await get_embedding(client, content)
    await add_knowledge(content, embedding, session_id, source_file="对话摘要")
    return JSONResponse({"success": True})


async def new_null_session(session_id: str, user_id: int):
    query = "INSERT INTO sessions (id, user_id) VALUES (:id, :user_id)"
    await database.execute(query, values={"id": session_id, "user_id": user_id})
    return {"id": session_id}


@app.get("/sessions")
async def get_sessions(user=Depends(get_current_user)):
    query = "SELECT id, name FROM sessions WHERE user_id = :uid AND name IS NOT NULL ORDER BY created_at DESC"
    rows = await database.fetch_all(query, values={"uid": user["id"]})
    return [{"id": r["id"], "name": r["name"]} for r in rows]


@app.get("/messages/{session_id}")
async def get_messages(session_id: str, limit: int = 50, user=Depends(get_current_user)):
    query = "SELECT role, content FROM messages WHERE session_id = :sid ORDER BY created_at DESC LIMIT :limit"
    rows = await database.fetch_all(query, values={"sid": session_id, "limit": limit})
    return list(reversed([{"role": r["role"], "content": r["content"]} for r in rows]))


@app.get("/collections/{session_id}")
async def get_collections(session_id: str, per_page: int = 500, user=Depends(get_current_user)):
    query = "SELECT filename, filepath FROM upload_files WHERE session_id = :sid ORDER BY created_at DESC LIMIT :limit"
    rows = await database.fetch_all(query, values={"sid": session_id, "limit": per_page})
    return list(reversed([{"filename": r["filename"], "filepath": r["filepath"]} for r in rows]))
