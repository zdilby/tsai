import asyncio
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from google.genai import types as gtypes
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
from agent_system.llm import complete_coding, complete as complete_default
from backend.rag import get_embedding, query_rag

_FORMAT_SYSTEM = (
    "你是一个专业的中文文章 Markdown 排版助手。\n"
    "任务：将输入文本转换为规范的 Markdown 格式，严格遵守：\n"
    "1. 段落之间必须有空行（两个换行符）\n"
    "2. 章节标题（## 或 ###）必须独占一行，前后各有一个空行\n"
    "3. 完整保留原文所有文字，不增删任何内容\n"
    "4. 超过 150 字的长段落按叙事逻辑适当分段\n"
    "5. 直接输出排版后的 Markdown，不加任何说明或注释"
)
_FORMAT_CHUNK_SIZE = 2200


def _split_for_format(text: str) -> list[str]:
    """Split text into chunks suitable for one Codex formatting call each."""
    # Prefer splitting at ## section boundaries
    raw = re.split(r'(?=^## )', text, flags=re.MULTILINE)
    chunks: list[str] = []
    for seg in raw:
        seg = seg.strip()
        if not seg:
            continue
        if len(seg) <= _FORMAT_CHUNK_SIZE:
            chunks.append(seg)
            continue
        # Sub-chunk: break at sentence ends near the size limit
        start = 0
        while start < len(seg):
            end = min(start + _FORMAT_CHUNK_SIZE, len(seg))
            if end < len(seg):
                for i in range(end, max(start + _FORMAT_CHUNK_SIZE // 2, start), -1):
                    if seg[i] in '。？！…\n':
                        end = i + 1
                        break
            chunks.append(seg[start:end].strip())
            start = end
    return chunks or [text]


def _format_markdown_sync(raw: str) -> str:
    """Run Codex formatting on each chunk and reassemble. Synchronous."""
    chunks = _split_for_format(raw)
    results: list[str] = []
    for chunk in chunks:
        if len(chunk.strip()) < 30:
            results.append(chunk)
            continue
        try:
            formatted = complete_coding(
                system=_FORMAT_SYSTEM,
                messages=[{"role": "user", "content": chunk}],
                label=f"format_md_codex[{len(chunk)}c]",
                verbose=False,
            )
            results.append(formatted.strip())
        except Exception as codex_exc:
            logger.warning("Codex 排版失败，回退 Gemini: %s", codex_exc)
            try:
                formatted = complete_default(
                    system=_FORMAT_SYSTEM,
                    messages=[{"role": "user", "content": chunk}],
                    label=f"format_md_gemini[{len(chunk)}c]",
                    verbose=False,
                )
                results.append(formatted.strip())
            except Exception as gemini_exc:
                logger.warning("Gemini 排版也失败，保留原文: %s", gemini_exc)
                results.append(chunk)
    return "\n\n".join(results)


writing_router = APIRouter()


def _parse_outline_sections(outline: str) -> list[tuple[str, str]]:
    """Split outline by ## headings → [(heading_line, sub_content), ...]."""
    sections: list[tuple[str, str]] = []
    current_heading: str | None = None
    current_lines: list[str] = []
    for line in outline.splitlines():
        if line.startswith("## "):
            if current_heading is not None:
                sections.append((current_heading, "\n".join(current_lines).strip()))
            current_heading = line
            current_lines = []
        elif current_heading is not None:
            current_lines.append(line)
    if current_heading is not None:
        sections.append((current_heading, "\n".join(current_lines).strip()))
    return sections
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


async def require_write_access(request: Request, user: dict = Depends(get_current_user)) -> dict:
    if user["is_admin"]:
        return user
    if not user["can_write"]:
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
    word_count: int = 0
    style_req: str = ""
    content_req: str = ""


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
    task_id, session_id = await create_writing_task(
        user["id"],
        title,
        payload.word_count,
        payload.style_req,
        payload.content_req,
    )
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


@writing_router.post("/generate_style")
async def generate_style(
    url: str = Form(None),
    file: UploadFile = File(None),
    user: dict = Depends(require_write_access),
):
    if not url and (not file or not file.filename):
        raise HTTPException(status_code=422, detail="请提供 URL 或上传文档")

    if url:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as hc:
                resp = await hc.get(url, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                html = resp.text
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"URL 获取失败：{e}")
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
    else:
        import os
        from midware.tools import parse_text_from_bytes
        data = await file.read()
        suffix = os.path.splitext(file.filename)[1].lower()
        try:
            chunks = parse_text_from_bytes(data, suffix)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"文件解析失败：{e}")
        text = " ".join(chunks)

    text = text[:6000]
    if not text.strip():
        raise HTTPException(status_code=400, detail="未能提取到有效文本内容")

    prompt = (
        "请分析以下文本的写作风格，输出 2-4 句话的风格描述，"
        "涵盖：行文语气（正式/口语/学术等）、用词特点、句式习惯、叙述节奏等，"
        "仅输出风格描述，不引用原文，不加任何额外说明。\n\n"
        f"文本内容：\n{text}"
    )

    async def generator():
        try:
            stream = await client.aio.models.generate_content_stream(
                model=settings.generation_model, contents=prompt
            )
            async for chunk in stream:
                if chunk.text:
                    yield f"data: {chunk.text}\n\n"
        except Exception as e:
            logger.exception("风格生成失败: %s", e)
            yield f"data: 生成失败：{e}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
            stream = await client.aio.models.generate_content_stream(
                model=settings.generation_model, contents=prompt
            )
            async for chunk in stream:
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

    sections = _parse_outline_sections(outline) if outline else []
    use_sectional = len(sections) >= 2 and (word_count == 0 or word_count >= 3000)
    gen_config = gtypes.GenerateContentConfig(max_output_tokens=65536)

    async def generator():
        if use_sectional:
            per_section_words = max(1000, word_count // len(sections)) if word_count > 0 else 2000
            accumulated = ""
            for i, (sec_heading, sec_body) in enumerate(sections):
                context_hint = (
                    f"\n已完成内容（仅供风格参考，勿重复）：\n...{accumulated[-1200:]}"
                    if accumulated else ""
                )
                sec_outline = f"{sec_heading}\n{sec_body}".strip()
                if not current_content:
                    sec_prompt = (
                        f"请为以下写作任务创作指定章节内容（Markdown，直接输出含章节标题的完整章节）：\n"
                        f"文章标题：{title}\n风格：{style_req}\n内容要求：{content_req}\n"
                        f"完整大纲：\n{outline}\n参考资料：\n{rag_text}\n"
                        f"本章节大纲：\n{sec_outline}\n"
                        f"本章节字数：约{per_section_words}字（第{i+1}/{len(sections)}章）"
                        f"{context_hint}\n直接输出本章节，不加任何额外说明。"
                    )
                else:
                    sec_prompt = (
                        f"请优化以下写作任务指定章节（Markdown，输出含章节标题的完整章节）：\n"
                        f"文章标题：{title}\n风格：{style_req}\n内容要求：{content_req}\n"
                        f"完整大纲：\n{outline}\n参考资料：\n{rag_text}\n"
                        f"本章节大纲：\n{sec_outline}\n"
                        f"本章节字数：约{per_section_words}字（第{i+1}/{len(sections)}章）"
                        f"{context_hint}\n直接输出本章节完整内容，不加任何额外说明。"
                    )
                try:
                    stream = await client.aio.models.generate_content_stream(
                        model=settings.generation_model, contents=sec_prompt, config=gen_config,
                    )
                    async for chunk in stream:
                        if chunk.text:
                            accumulated += chunk.text
                            yield f"data: {chunk.text}\n\n"
                    accumulated += "\n\n"
                except Exception as e:
                    logger.exception("章节内容生成失败 (section %d): %s", i, e)
                    yield f"data: [章节{i+1}生成失败：{e}]\n\n"
                    return
        else:
            word_hint = f"（必须达到约{word_count}字，不得提前结束）" if word_count > 0 else "（内容尽量详尽充实）"
            if not current_content:
                prompt = (
                    f"请根据以下设置创作一篇完整的文章（Markdown 格式）{word_hint}：\n"
                    f"标题：{title}\n字数：{word_count}字\n风格：{style_req}\n"
                    f"内容要求：{content_req}\n内容大纲：\n{outline}\n参考资料：\n{rag_text}\n"
                    f"直接输出文章内容，不要任何额外说明。"
                )
            else:
                prompt = (
                    f"请根据以下设置优化现有文章内容（Markdown 格式，重新输出完整内容）{word_hint}：\n"
                    f"标题：{title}\n字数：{word_count}字\n风格：{style_req}\n"
                    f"内容要求：{content_req}\n内容大纲：\n{outline}\n参考资料：\n{rag_text}\n"
                    f"当前内容（仅供参考，优化时可改动）：\n{current_content[:3000]}\n"
                    f"直接输出完整优化后的文章，不要任何额外说明。"
                )
            try:
                stream = await client.aio.models.generate_content_stream(
                    model=settings.generation_model, contents=prompt, config=gen_config,
                )
                async for chunk in stream:
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


@writing_router.post("/tasks/{task_id}/chat")
async def writing_task_chat(
    task_id: str,
    message: str = Form(...),
    user: dict = Depends(require_write_access),
):
    await _ensure_task_owner(task_id, user["id"])
    task = await get_writing_task(task_id, user["id"])
    if not task:
        raise HTTPException(status_code=404, detail="写作任务不存在")

    writing_content = await get_writing_content(task_id)
    current_content = writing_content["content"] if writing_content else ""

    reference_files = task.get("reference_files") or []
    rag_text = ""
    if reference_files:
        query_text = f"{message} {task['title']}"
        embedding = await get_embedding(embed_client, query_text)
        rag_results = await query_rag(
            embedding,
            session_id=str(task["session_id"]),
            source_files=reference_files,
        )
        rag_text = "\n".join(r["content"] for r in rag_results)

    system_instruction = (
        "你是一个专业的写作助手，协助用户创作和改进文章。\n"
        "规则：\n"
        "1. 若用户要求修改/改进/调整文章内容，请在回复末尾用以下标记输出完整修改后的文章（Markdown 格式）："
        "标记之后另起一行用 2-4 句话说明改动要点：\n"
        "[WRITING_UPDATE_START]\n"
        "（完整的修改后文章，保留原有结构，仅修改用户要求的部分，字数不得少于原文）\n"
        "[WRITING_UPDATE_END]\n"
        "改动说明...\n"
        "2. 若用户只是提问、讨论或请求意见，不需要修改文章，正常回复即可。\n"
        "3. 修改文章时，务必保持整体字数规模，不可随意删减段落。"
    )

    content_block = (
        f"\n\n## 当前文章内容（共约 {len(current_content)} 字）\n\n{current_content}"
        if current_content else "\n\n## 当前文章内容\n\n（尚未生成内容）"
    )
    rag_block = f"\n\n## 参考资料片段\n\n{rag_text}" if rag_text else ""

    prompt = (
        f"## 写作任务\n"
        f"标题：{task['title']}\n"
        f"字数要求：{task['word_count']}字\n"
        f"风格：{task['style_req']}\n"
        f"内容要求：{task['content_req']}\n"
        f"大纲：\n{task['outline']}"
        f"{content_block}"
        f"{rag_block}"
        f"\n\n## 用户指令\n\n{message}"
    )

    config = gtypes.GenerateContentConfig(
        system_instruction=system_instruction,
        max_output_tokens=65536,
    )

    try:
        resp = await client.aio.models.generate_content(
            model=settings.generation_model,
            contents=prompt,
            config=config,
        )
        answer = resp.text
    except Exception as e:
        logger.exception("写作对话生成失败: %s", e)
        raise HTTPException(status_code=502, detail="AI 服务暂时不可用")

    return {"answer": answer}


@writing_router.post("/tasks/{task_id}/format_content")
async def format_content(task_id: str, user: dict = Depends(require_write_access)):
    await _ensure_task_owner(task_id, user["id"])
    writing_content = await get_writing_content(task_id)
    if not writing_content or not writing_content.get("content"):
        raise HTTPException(status_code=404, detail="暂无内容可排版")
    raw = writing_content["content"]
    try:
        formatted = await asyncio.to_thread(_format_markdown_sync, raw)
    except Exception as e:
        logger.exception("排版失败: %s", e)
        raise HTTPException(status_code=500, detail=f"排版失败：{e}")
    version = await save_writing_content(task_id, formatted)
    return {"content": formatted, "version": version}


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
