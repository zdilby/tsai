from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from settings import client, embed_client, settings, logger
from account import get_current_user
from backend.db import (
    database,
    update_session_instruction,
    create_writing_task,
    get_writing_tasks,
    get_writing_task,
    writing_task_owned_by,
    update_writing_task,
    delete_writing_task,
    get_writing_content,
    save_writing_content,
    get_user_processed_files,
)
from backend.rag import get_embedding, query_rag

writing_router = APIRouter()
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


async def require_write_access(request: Request, user: dict = Depends(get_current_user)) -> dict:
    if user.get("is_admin"):
        return user
    if not user.get("can_write"):
        endpoint_name = getattr(request.scope.get("endpoint"), "__name__", "")
        if (
            endpoint_name in {"writing_page", "writing_task_page"}
            and "text/html" in request.headers.get("accept", "")
        ):
            return RedirectResponse(url="/", status_code=302)
        raise HTTPException(status_code=403, detail="无写作权限")
    return user


@writing_router.get("/", response_class=HTMLResponse)
async def writing_page(request: Request, user: dict = Depends(require_write_access)):
    if isinstance(user, RedirectResponse):
        return user
    tasks = await get_writing_tasks(user["id"])
    if tasks:
        latest_task_id = tasks[0]["id"]
        return RedirectResponse(url=f"/writing/{latest_task_id}")
    return _templates.TemplateResponse(
        "writing.html",
        {"request": request, "task_id": None, "task": None, "user": user["username"]},
    )


class CreateWritingTaskRequest(BaseModel):
    title: str = "未命名写作"


class UpdateWritingTaskRequest(BaseModel):
    title: Optional[str] = None
    word_count: Optional[int] = None
    style_req: Optional[str] = None
    content_req: Optional[str] = None
    outline: Optional[str] = None
    reference_files: Optional[list[str]] = None


class SaveWritingContentRequest(BaseModel):
    content: str


def _serialize_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _serialize_row(row: dict) -> dict:
    return {k: _serialize_value(v) for k, v in row.items()}


async def _ensure_task_owner(task_id: str, user_id: int):
    if not await writing_task_owned_by(task_id, user_id):
        raise HTTPException(status_code=403, detail="无权访问该写作任务")


@writing_router.post("/tasks")
async def create_task(payload: CreateWritingTaskRequest, user: dict = Depends(require_write_access)):
    title = payload.title or "未命名写作"
    task_id, session_id = await create_writing_task(user["id"], title)
    return {"id": task_id, "title": title, "session_id": session_id}


@writing_router.get("/tasks")
async def list_tasks(user: dict = Depends(require_write_access)):
    tasks = await get_writing_tasks(user["id"])
    return [_serialize_row(t) for t in tasks]


@writing_router.get("/tasks/{task_id}")
async def get_task(task_id: str, user: dict = Depends(require_write_access)):
    await _ensure_task_owner(task_id, user["id"])
    task = await get_writing_task(task_id, user["id"])
    if not task:
        raise HTTPException(status_code=404, detail="写作任务不存在")
    return _serialize_row(task)


@writing_router.patch("/tasks/{task_id}")
async def patch_task(task_id: str, payload: UpdateWritingTaskRequest, user: dict = Depends(require_write_access)):
    await _ensure_task_owner(task_id, user["id"])
    data = payload.dict(exclude_none=True)
    await update_writing_task(task_id, user["id"], **data)
    return {"success": True}


@writing_router.delete("/tasks/{task_id}")
async def remove_task(task_id: str, user: dict = Depends(require_write_access)):
    await _ensure_task_owner(task_id, user["id"])
    await delete_writing_task(task_id, user["id"])
    return {"success": True}


@writing_router.get("/tasks/{task_id}/content")
async def get_task_content(task_id: str, user: dict = Depends(require_write_access)):
    await _ensure_task_owner(task_id, user["id"])
    content = await get_writing_content(task_id)
    if not content:
        return {"content": "", "version": 0}
    return {"content": content["content"], "version": content["version"]}


@writing_router.post("/tasks/{task_id}/content")
async def save_task_content(task_id: str, payload: SaveWritingContentRequest, user: dict = Depends(require_write_access)):
    await _ensure_task_owner(task_id, user["id"])
    version = await save_writing_content(task_id, payload.content)
    return {"version": version}


@writing_router.get("/files")
async def list_files(user: dict = Depends(require_write_access)):
    files = await get_user_processed_files(user["id"])
    return {"files": files}


@writing_router.get("/tasks/{task_id}/generate_outline")
async def generate_outline(task_id: str, user: dict = Depends(require_write_access)):
    await _ensure_task_owner(task_id, user["id"])
    task = await get_writing_task(task_id, user["id"])
    if not task:
        raise HTTPException(status_code=404, detail="写作任务不存在")
    prompt = (
        f"请为以下写作任务生成详细的内容大纲（Markdown 格式，使用 ## 和 ### 层级）：\n"
        f"标题：{task['title']}\n"
        f"字数要求：{task['word_count']}字（0 表示不限）\n"
        f"风格要求：{task['style_req']}\n"
        f"内容要求：{task['content_req']}\n"
        f"只输出大纲，不要任何额外说明。"
    )

    async def generator():
        try:
            async for chunk in client.aio.models.generate_content_stream(
                model=settings.generation_model, contents=prompt
            ):
                if chunk.text:
                    yield f"data: {chunk.text}\n\n"
        except Exception as e:
            logger.exception("写作大纲生成失败: %s", e)
            yield f"data: 生成失败：{e}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@writing_router.post("/tasks/{task_id}/generate_content")
async def generate_content(task_id: str, user: dict = Depends(require_write_access)):
    await _ensure_task_owner(task_id, user["id"])
    task = await get_writing_task(task_id, user["id"])
    if not task:
        raise HTTPException(status_code=404, detail="写作任务不存在")

    writing_content = await get_writing_content(task_id)
    reference_files = task.get("reference_files") or []
    if reference_files:
        query_text = f"{task['title']} {task['content_req']}"
        embedding = await get_embedding(embed_client, query_text)
        rag_results = await query_rag(
            embedding,
            session_id=str(task["session_id"]),
            source_files=reference_files,
        )
        rag_text = "\n".join(r["content"] for r in rag_results)
    else:
        rag_text = ""

    current_content = writing_content["content"] if writing_content else ""
    title = task["title"]
    word_count = task["word_count"]
    style_req = task["style_req"]
    content_req = task["content_req"]
    outline = task["outline"]
    if not current_content:
        prompt = (
            f"请根据以下设置创作一篇完整的文章（Markdown 格式）：\n"
            f"标题：{title}\n"
            f"字数：{word_count}字（0 表示不限）\n"
            f"风格：{style_req}\n"
            f"内容要求：{content_req}\n"
            f"内容大纲：\n{outline}\n"
            f"参考资料：\n{rag_text}\n"
            f"直接输出文章内容，不要任何额外说明。"
        )
    else:
        prompt = (
            f"请根据以下设置优化现有文章内容（Markdown 格式，重新输出完整内容）：\n"
            f"标题：{title}\n"
            f"字数：{word_count}字（0 表示不限）\n"
            f"风格：{style_req}\n"
            f"内容要求：{content_req}\n"
            f"内容大纲：\n{outline}\n"
            f"参考资料：\n{rag_text}\n"
            f"当前内容（仅供参考，优化时可改动）：\n{current_content[:3000]}\n"
            f"直接输出完整优化后的文章，不要任何额外说明。"
        )

    async def generator():
        try:
            async for chunk in client.aio.models.generate_content_stream(
                model=settings.generation_model, contents=prompt
            ):
                if chunk.text:
                    yield f"data: {chunk.text}\n\n"
        except Exception as e:
            logger.exception("写作内容生成失败: %s", e)
            yield f"data: 生成失败：{e}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@writing_router.get("/{task_id}", response_class=HTMLResponse)
async def writing_task_page(task_id: str, request: Request, user: dict = Depends(require_write_access)):
    if isinstance(user, RedirectResponse):
        return user
    if not await writing_task_owned_by(task_id, user["id"]):
        return RedirectResponse(url="/writing/")
    task = await get_writing_task(task_id, user["id"])
    return _templates.TemplateResponse(
        "writing.html",
        {"request": request, "task_id": task_id, "task": task, "user": user["username"]},
    )
