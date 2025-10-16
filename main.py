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
from backend.db import database, init_db, save_message, get_context, add_knowledge, session_exists
from backend.rag import get_embedding, query_rag
from midware.tools import fetch_from_web
from midware.upload import router as upload_router, upload_file

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(account_router, prefix="/account", tags=["account"])
app.include_router(upload_router, prefix="/upload", tags=["upload"])
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
async def startup():
    await database.connect()
    await init_db()
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
async def index(request: Request, session_id: str=Query(None), user = Depends(get_current_user)):
    if not session_id:
        session_id = str(uuid.uuid4())
        await new_null_session(session_id, user["id"])
        session_ex = False
    elif await session_exists(session_id):
        session_ex = True
    return templates.TemplateResponse("chat.html", {"request": request, "session_id": session_id, "session_exists": session_ex, "user": user["username"]})


@app.post("/chat")
async def chat(session_id: str = Form(...), message: str = Form(...), user = Depends(get_current_user)):
    # 保存用户消息
    await save_message(session_id, "user", message)
    # print('message: ', message)

    # 获取历史上下文
    context = await get_context(session_id)
    context_text = "\n".join([f"{c['role']}: {c['content']}" for c in context])

    # RAG 查询
    query_embedding = await get_embedding(client, message)
    rag_docs = await query_rag(query_embedding, session_id=session_id)
    rag_text = "\n".join(rag_docs)

    # 执行 Google 搜索获取最新网络信息
    web_info = await fetch_from_web(message)
    if web_info:
        # 将新抓取到的内容存进知识库
        if await session_exists(session_id):
            await add_knowledge(web_info, await get_embedding(client, web_info), session_id=session_id)

    # 构建提示词 prompt
    prompt = f"Context:\n{context_text}\n\nRelevant info from RAG:\n{rag_text}\n\nLatest info from web:\n{web_info}\n\nUser: {message}\nAI:"
    # print('prompt: ', prompt)

    # 设置前置的Grounding with Google Search
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(tools=[grounding_tool])

    # 调用 Gemini 生成回答
    chat = client.aio.chats.create(model=settings.generation_model, config=config)
    resp = await chat.send_message(prompt)
    answer = resp.text
    # print('answer: ', answer)
    await save_message(session_id, "assistant", answer)
    return JSONResponse({"answer": answer})


@app.post("/new_session")
async def new_session(name: str = Form(None), user = Depends(get_current_user)):
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
async def change_session(session_id: str = Form(...), name: str = Form(...), user = Depends(get_current_user)):
    query = "UPDATE sessions SET name = :name WHERE id = :id AND user_id = :user_id"
    try:
        await database.execute(query, values={"id": session_id, "name": name, "user_id": user["id"]})
        return {"id": session_id, "name": name, "success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/del_session")
async def del_session(session_id: str = Form(...), user = Depends(get_current_user)):
    query = "DELETE FROM sessions WHERE id = :id AND user_id = :user_id"
    try:
        await database.execute(query, values={"id": session_id, "user_id": user["id"]})
        return {"id": session_id, "success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def new_null_session(session_id: str, user_id: int):
    query = "INSERT INTO sessions (id, user_id) VALUES (:id, :user_id)"
    await database.execute(query, values={"id": session_id, "user_id": user_id})
    return {"id": session_id}


@app.get("/sessions")
async def get_sessions(user = Depends(get_current_user)):
    query = "SELECT id, name FROM sessions WHERE user_id = :uid AND name IS NOT NULL ORDER BY created_at DESC"
    rows = await database.fetch_all(query, values={"uid": user["id"]})
    return [{"id": r["id"], "name": r["name"]} for r in rows]


@app.get("/messages/{session_id}")
async def get_messages(session_id: str, limit: int = 50, user = Depends(get_current_user)):
    query = "SELECT role, content FROM messages WHERE session_id = :sid ORDER BY created_at DESC LIMIT :limit"
    rows = await database.fetch_all(query, values={"sid": session_id, "limit": limit})
    return list(reversed([{"role": r["role"], "content": r["content"]} for r in rows]))


@app.get("/collections/{session_id}")
async def get_collections(session_id: str, per_page: int = 500, user = Depends(get_current_user)):
    query = "SELECT filename, filepath FROM upload_files WHERE session_id = :sid ORDER BY created_at DESC LIMIT :limit"
    rows = await database.fetch_all(query, values={"sid": session_id, "limit": per_page})
    return list(reversed([{"filename": r["filename"], "filepath": r["filepath"]} for r in rows]))
