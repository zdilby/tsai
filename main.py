from fastapi import FastAPI, Request, Form, Query, Depends, HTTPException, BackgroundTasks
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google import genai
from google.genai import types
from pgvector.asyncpg import register_vector, Vector
import asyncio
import re
import time
import uuid

# 回忆触发词：检测到则剥离语气词，用核心话题做历史检索
_RECALL_PATTERNS = re.compile(
    r'(你还记得|还记得|你记得|记得吗|之前(我们|你|咱们)?|上次(你|我们)?|'
    r'我们(之前|以前|上次)?聊过|你(之前|以前|上次)?提到|我(之前|以前)?问过|'
    r'我们讨论过|你说过|你提过|前面(你|我们)?)[^，。？！,.?!]*[，。？！,.?!]?',
    re.UNICODE
)
from settings import settings, client, embed_client, logger
from account import router as account_router, get_current_user
from backend.db import database, init_db, save_message, update_message_embedding, get_context, session_exists, session_owned_by, add_knowledge, get_user_today_tokens, get_session_persona, get_session_persona_origin, update_session_persona, save_persona_origin, update_session_instruction, record_trace
from backend.rag import get_embedding, query_rag, query_history, estimate_session_tokens, get_all_session_chunks
from backend.agent_chat import needs_agent, run_agent_chat
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
    # Phase 3a 表是幂等的（IF NOT EXISTS），可安全在每次启动时执行
    from backend.db import init_phase3_tables
    await init_phase3_tables()
    async with database._backend._pool.acquire() as conn:
        await register_vector(conn)


@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()


@app.exception_handler(StarletteHTTPException)
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return RedirectResponse("/account/login")
    if exc.status_code == 404 and "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, session_id: str = Query(None), user=Depends(get_current_user)):
    if not session_id:
        session_id = str(uuid.uuid4())
        await new_null_session(session_id, user["id"])
        session_ex = False
    elif await session_exists(session_id) and await session_owned_by(session_id, user["id"]):
        session_ex = True
    else:
        # session 不存在或不属于当前用户，创建新的匿名 session
        session_id = str(uuid.uuid4())
        await new_null_session(session_id, user["id"])
        session_ex = False
    max_file_mb = user["max_file_size_mb"] if user["max_file_size_mb"] is not None else 10
    return templates.TemplateResponse("chat.html", {
        "request": request, "session_id": session_id,
        "session_exists": session_ex, "user": user["username"],
        "max_file_size_mb": max_file_mb,
    })


@app.get("/ping")
async def ping():
    return {"status": "ok"}


@app.post("/chat")
async def chat(background_tasks: BackgroundTasks,
               session_id: str = Form(...), message: str = Form(...),
               source_files: str = Form(""), user=Depends(get_current_user)):
    if not await session_owned_by(session_id, user["id"]):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    _chat_t0 = time.monotonic()
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
    context = await get_context(session_id, limit=settings.max_history_turns)
    context_text = "\n".join([f"{c['role']}: {c['content']}" for c in context])

    # 判断 session 语料规模：小语料走全量上下文路径，大语料走 RAG 路径
    session_tokens = await estimate_session_tokens(session_id)
    use_full_context = 0 < session_tokens < settings.full_context_threshold

    # Phase 2 Agent 路由：大语料 + 开关开启 + 复杂查询 → Agent 循环
    use_agent = (
        not use_full_context
        and session_tokens > 0
        and settings.agent_chat_enabled
        and needs_agent(message)
    )

    logger.info(
        "/chat session=%s tokens≈%d threshold=%d → %s",
        session_id, session_tokens, settings.full_context_threshold,
        "FULL_CONTEXT" if use_full_context
        else "AGENT" if use_agent
        else ("RAG" if session_tokens > 0 else "EMPTY_KB"),
    )

    # ── Phase 2 Agent 分支：独立完整流程，提前 return ─────────────────────────
    if use_agent:
        persona = await get_session_persona(session_id)
        # 预抓 web 信息，作为 Agent 的免费上下文（减少 web_search 工具调用）
        web_info = await fetch_from_web(message)
        agent_result = await run_agent_chat(
            query=message,
            session_id=session_id,
            persona=persona,
            history_text=context_text,
            web_info=web_info,
        )
        answer = agent_result["answer"]
        a_tokens_in   = agent_result["tokens_in"]
        a_tokens_out  = agent_result["tokens_out"]
        a_tokens_total = a_tokens_in + a_tokens_out
        msg_id = await save_message(
            session_id, "assistant", answer,
            tokens_in=a_tokens_in, tokens_out=a_tokens_out, tokens_total=a_tokens_total,
        )

        async def _agent_save_embedding():
            try:
                emb = await get_embedding(embed_client, answer[:2000])
                await update_message_embedding(msg_id, emb)
            except Exception as e:
                logger.warning("历史消息 embedding 存储失败 (id=%s): %s", msg_id, e)

        background_tasks.add_task(_agent_save_embedding)

        # Phase 3a — 落盘 trace（异步，不阻塞响应）
        background_tasks.add_task(
            record_trace,
            session_id=session_id, user_id=user["id"], message_id=msg_id,
            query=message, route="agent",
            tools_called=agent_result["agent_trace"],
            iterations=agent_result["iterations"],
            citations=agent_result["citations"],
            tokens_in=a_tokens_in, tokens_out=a_tokens_out,
            duration_ms=int((time.monotonic() - _chat_t0) * 1000),
            prompt_version_id=agent_result.get("prompt_version_id"),
        )

        return JSONResponse({
            "answer": answer,
            "citations": agent_result["citations"],
            "agent_trace": agent_result["agent_trace"],
            "iterations": agent_result["iterations"],
        })

    # ── 否则继续 Phase 1 既有路径 ────────────────────────────────────────────
    # embedding、RAG 查询、Web 搜索并发执行
    source_list = [s.strip() for s in source_files.split(',') if s.strip()] if source_files else None
    query_embedding, web_info = await asyncio.gather(
        get_embedding(embed_client, message),
        fetch_from_web(message),
    )

    # 近期消息中最旧的 ID，历史检索排除这些消息（避免重复）
    oldest_recent_id = context[0]["id"] if context else None

    # 检测是否为主动回忆型提问，是则剥离语气词取核心话题并放宽检索阈值
    recall_query = _RECALL_PATTERNS.sub('', message).strip()
    is_recall = bool(recall_query and recall_query != message and len(recall_query) >= 4)
    history_embedding = await get_embedding(embed_client, recall_query) if is_recall else query_embedding
    history_threshold = 0.55 if is_recall else 0.4

    if use_full_context:
        # 小语料：全量加载所有 chunk，按文档分组带文件头
        all_chunks, history_results = await asyncio.gather(
            get_all_session_chunks(session_id),
            query_history(history_embedding, session_id=session_id,
                          before_id=oldest_recent_id, threshold=history_threshold),
        )
        by_file: dict[str, list[str]] = {}
        for c in all_chunks:
            by_file.setdefault(c["source_file"], []).append(c["content"])
        rag_text = "\n\n".join(
            f"=== 文件：{src} ===\n" + "\n".join(parts)
            for src, parts in by_file.items()
        )
        rag_citations = [
            {"source": src, "chunk": None, "score": 1.0, "snippet": ""}
            for src in by_file
        ]
        has_kb = bool(by_file)
    else:
        # 大语料 / 空知识库：走 RAG 检索
        rag_results, history_results = await asyncio.gather(
            query_rag(query_embedding, session_id=session_id, source_files=source_list),
            query_history(history_embedding, session_id=session_id,
                          before_id=oldest_recent_id, threshold=history_threshold),
        )
        rag_text = "\n".join([r["content"] for r in rag_results])
        rag_citations = [
            {
                "source": r["source_file"],
                "chunk": r["chunk_index"],
                "score": round(1 - r["distance"], 3),
                "snippet": (r.get("original_content") or "")[:200].strip(),
            }
            for r in rag_results
        ]
        has_kb = bool(rag_results)

    # 构建提示词 prompt
    if use_full_context and has_kb:
        rag_section = f"All uploaded documents in this session (full content):\n{rag_text}\n\n"
    elif has_kb:
        rag_section = f"Relevant info from uploaded documents:\n{rag_text}\n\n"
    else:
        rag_section = "Relevant info from uploaded documents:\n（当前问题在知识库中未找到相关文档内容）\n\n"
    web_section = f"Latest info from web:\n{web_info}\n\n" if web_info else ""
    if history_results:
        history_items = "\n".join([
            f"[{r['created_at'].strftime('%Y-%m-%d') if hasattr(r['created_at'], 'strftime') else str(r['created_at'])[:10]}] "
            f"{r['snippet']}{'…' if len(r['content']) > 300 else ''}"
            for r in history_results
        ])
        history_section = f"Relevant excerpts from past conversation in this session:\n{history_items}\n\n"
    else:
        history_section = ""
    prompt = (
        f"Context:\n{context_text}\n\n"
        f"{history_section}"
        f"{rag_section}"
        f"{web_section}"
        f"如果回答引用了上传文档的原文或观点，请在该句末尾用括号标注来源，格式为（来源：文件名，第N段）。直接引用原文时请加引号。\n"
        f"User: {message}\nAI:"
    )
    # print('prompt: ', prompt)

    # 设置前置的Grounding with Google Search
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    persona = await get_session_persona(session_id)
    config = types.GenerateContentConfig(
        tools=[grounding_tool],
        system_instruction=persona if persona else None,
    )

    # 调用 Gemini 生成回答
    chat = client.aio.chats.create(model=settings.generation_model, config=config)
    try:
        resp = await chat.send_message(prompt)
    except Exception as e:
        logger.exception("Gemini API 调用失败: %s", e)
        raise HTTPException(status_code=502, detail="AI 服务暂时不可用，请稍后重试")
    answer = resp.text
    usage = resp.usage_metadata
    tokens_in  = getattr(usage, "prompt_token_count",     0) or 0
    tokens_out = getattr(usage, "candidates_token_count", 0) or 0
    tokens_total = getattr(usage, "total_token_count",    0) or 0
    msg_id = await save_message(session_id, "assistant", answer,
                                tokens_in=tokens_in, tokens_out=tokens_out, tokens_total=tokens_total)

    # 后台异步为该条消息计算并存储 embedding，供历史检索使用
    async def _save_embedding():
        try:
            emb = await get_embedding(embed_client, answer[:2000])
            await update_message_embedding(msg_id, emb)
        except Exception as e:
            logger.warning("历史消息 embedding 存储失败 (id=%s): %s", msg_id, e)

    background_tasks.add_task(_save_embedding)

    # Phase 3a — 落盘 trace（异步，不阻塞响应）
    _route = "full_context" if use_full_context else ("rag" if has_kb else "empty_kb")
    background_tasks.add_task(
        record_trace,
        session_id=session_id, user_id=user["id"], message_id=msg_id,
        query=message, route=_route,
        tools_called=[], iterations=1,
        citations=rag_citations,
        tokens_in=tokens_in, tokens_out=tokens_out,
        duration_ms=int((time.monotonic() - _chat_t0) * 1000),
        prompt_version_id=None,
    )

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


_PERSONA_PROCESS_PROMPT = (
    "你是一个AI助手配置验证器。用户为AI角色提供了以下性格描述：\n\n"
    "\"\"\"\n{raw}\n\"\"\"\n\n"
    "任务：判断此内容是否是合理的AI角色性格/风格描述（描述AI的说话方式、专业领域、性格特点等）。\n"
    "如果是，将其整理为简洁、明确的system instruction（中文，不超过200字）。\n"
    "如果不是（乱码、无意义内容、与AI性格无关等），只输出空字符串。\n"
    "只输出整理结果或空字符串，不要任何解释或其他内容。"
)


async def _process_persona(raw: str) -> str:
    """调用 Gemini 验证并提取有效的 system instruction，无效内容返回空字符串。"""
    try:
        resp = await client.aio.models.generate_content(
            model=settings.generation_model,
            contents=_PERSONA_PROCESS_PROMPT.format(raw=raw),
        )
        return (resp.text or "").strip()
    except Exception as e:
        logger.warning("Persona AI 处理失败，降级使用原始输入: %s", e)
        return raw.strip()


@app.get("/session_persona/{session_id}")
async def get_persona(session_id: str, user=Depends(get_current_user)):
    if not await session_owned_by(session_id, user["id"]):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    # 返回原始输入供前端编辑回显
    return JSONResponse({"persona": await get_session_persona_origin(session_id)})


@app.post("/session_persona")
async def set_persona(background_tasks: BackgroundTasks,
                      session_id: str = Form(...), persona: str = Form(""),
                      user=Depends(get_current_user)):
    if not await session_owned_by(session_id, user["id"]):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    raw = persona.strip()
    if len(raw) > 500:
        raise HTTPException(status_code=400, detail="性格描述不能超过500字")
    # 立即保存原始输入
    await save_persona_origin(session_id, user["id"], raw)
    # AI 提取放后台，不阻塞响应
    async def _extract():
        processed = await _process_persona(raw) if raw else ""
        await update_session_instruction(session_id, processed)
    background_tasks.add_task(_extract)
    return JSONResponse({"success": True})


@app.post("/save_to_rag")
async def save_to_rag(
    session_id: str = Form(...),
    content: str = Form(...),
    user=Depends(get_current_user)
):
    if not await session_exists(session_id):
        raise HTTPException(status_code=403, detail="仅命名会话可保存到知识库")
    embedding = await get_embedding(embed_client, content)
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
    if not await session_owned_by(session_id, user["id"]):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    query = "SELECT role, content FROM messages WHERE session_id = :sid ORDER BY created_at DESC LIMIT :limit"
    rows = await database.fetch_all(query, values={"sid": session_id, "limit": limit})
    return list(reversed([{"role": r["role"], "content": r["content"]} for r in rows]))


@app.get("/collections/{session_id}")
async def get_collections(session_id: str, per_page: int = 500, user=Depends(get_current_user)):
    if not await session_owned_by(session_id, user["id"]):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    query = "SELECT filename, filepath FROM upload_files WHERE session_id = :sid ORDER BY created_at DESC LIMIT :limit"
    rows = await database.fetch_all(query, values={"sid": session_id, "limit": per_page})
    return list(reversed([{"filename": r["filename"], "filepath": r["filepath"]} for r in rows]))
