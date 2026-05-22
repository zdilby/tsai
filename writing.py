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
    get_writing_sections,
    get_writing_section,
    upsert_writing_sections,
    update_writing_section,
    delete_writing_section,
    update_style_skills,
    save_writing_evaluation,
    get_latest_evaluation,
)
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


def _codex_format_sync(chunk: str) -> str:
    """Call Codex OpenAI-compatible endpoint synchronously. Raises if not configured."""
    import os
    api_key = os.getenv("CODEX_API_KEY", "").strip()
    base_url = os.getenv("CODEX_BASE_URL", "").strip()
    model = os.getenv("CODEX_MODEL", "gpt-4o").strip()
    if not api_key or not base_url:
        raise RuntimeError("CODEX not configured")
    url = base_url.rstrip("/") + "/chat/completions"
    with httpx.Client(timeout=60) as hc:
        resp = hc.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _FORMAT_SYSTEM},
                    {"role": "user", "content": chunk},
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _gemini_format_sync(chunk: str) -> str:
    """Format via project Gemini client synchronously."""
    resp = client.models.generate_content(
        model=settings.generation_model,
        contents=chunk,
        config=gtypes.GenerateContentConfig(
            system_instruction=_FORMAT_SYSTEM,
            max_output_tokens=8192,
        ),
    )
    return resp.text


def _format_markdown_sync(raw: str) -> str:
    """Format each chunk: Codex first, fall back to Gemini, preserve on double failure."""
    chunks = _split_for_format(raw)
    results: list[str] = []
    for chunk in chunks:
        if len(chunk.strip()) < 30:
            results.append(chunk)
            continue
        try:
            results.append(_codex_format_sync(chunk).strip())
        except Exception as codex_exc:
            logger.warning("Codex 排版失败，回退 Gemini: %s", codex_exc)
            try:
                results.append(_gemini_format_sync(chunk).strip())
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


def _parse_toc(toc: str) -> list[str]:
    """Extract plain heading text from a TOC string.

    Supports '## Heading', '1. Heading', '- Heading' formats.
    Returns a list of plain heading strings (no prefix).
    """
    headings = []
    for line in toc.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("## "):
            headings.append(line[3:].strip())
        elif re.match(r'^\d+[.、)]\s+', line):
            headings.append(re.sub(r'^\d+[.、)]\s+', '', line).strip())
        elif re.match(r'^[-*]\s+', line):
            headings.append(re.sub(r'^[-*]\s+', '', line).strip())
    return [h for h in headings if h]


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
    toc: Optional[str] = None
    reference_files: Optional[list[str]] = None


class SaveWritingContentRequest(BaseModel):
    content: str


class UpdateSectionRequest(BaseModel):
    heading: Optional[str] = None
    sub_outline: Optional[str] = None
    content: Optional[str] = None
    word_count_target: Optional[int] = None
    status: Optional[str] = None


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
    if data:
        await update_writing_task(task_id, user["id"], **data)
    # Sync sections when TOC is saved
    if payload.toc is not None:
        task = await get_writing_task(task_id, user["id"])
        headings = _parse_toc(payload.toc)
        if headings:
            wc = (task["word_count"] if task else 0) or 0
            per_sec = wc // len(headings) if wc > 0 else 0
            await upsert_writing_sections(task_id, [
                {"heading": h, "sub_outline": "", "word_count_target": per_sec}
                for h in headings
            ])
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
    task_id: str = Form(None),
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

    # Save source text for later style distillation / comparison
    if task_id and await writing_task_owned_by(task_id, user["id"]):
        await update_writing_task(task_id, user["id"], style_source_text=text)

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
    style_skills = (task.get("style_skills") or "").strip()
    style_block = (
        f"\n【风格技能手册（优先遵照）】\n{style_skills}\n"
        if style_skills else
        f"\n风格：{style_req}\n" if style_req else ""
    )

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
                        f"文章标题：{title}{style_block}内容要求：{content_req}\n"
                        f"完整大纲：\n{outline}\n参考资料：\n{rag_text}\n"
                        f"本章节大纲：\n{sec_outline}\n"
                        f"本章节字数：约{per_section_words}字（第{i+1}/{len(sections)}章）"
                        f"{context_hint}\n直接输出本章节，不加任何额外说明。"
                    )
                else:
                    sec_prompt = (
                        f"请优化以下写作任务指定章节（Markdown，输出含章节标题的完整章节）：\n"
                        f"文章标题：{title}{style_block}内容要求：{content_req}\n"
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
                    f"标题：{title}\n字数：{word_count}字{style_block}"
                    f"内容要求：{content_req}\n内容大纲：\n{outline}\n参考资料：\n{rag_text}\n"
                    f"直接输出文章内容，不要任何额外说明。"
                )
            else:
                prompt = (
                    f"请根据以下设置优化现有文章内容（Markdown 格式，重新输出完整内容）{word_hint}：\n"
                    f"标题：{title}\n字数：{word_count}字{style_block}"
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


@writing_router.post("/tasks/{task_id}/generate_toc")
async def generate_toc(task_id: str, user: dict = Depends(require_write_access)):
    await _ensure_task_owner(task_id, user["id"])
    task = await get_writing_task(task_id, user["id"])
    if not task:
        raise HTTPException(status_code=404, detail="写作任务不存在")

    outline = task.get("outline") or ""
    if outline:
        prompt = (
            f"请根据以下详细内容大纲，提炼出一个简洁的写作目录（TOC）。\n"
            f"要求：仅列出主要章节标题（每行以 ## 开头），5-10 个，每行一个，不加编号，不加说明。\n\n"
            f"内容大纲：\n{outline}"
        )
    else:
        prompt = (
            f"请为以下写作任务生成写作目录（TOC），列出主要章节标题（每行以 ## 开头），5-10 个，每行一个。\n"
            f"文章标题：{task['title']}\n字数要求：{task['word_count']}字\n"
            f"内容要求：{task['content_req']}\n仅输出章节标题列表，不加任何额外说明。"
        )

    async def gen_toc():
        try:
            stream = await client.aio.models.generate_content_stream(
                model=settings.generation_model, contents=prompt
            )
            async for chunk in stream:
                if chunk.text:
                    yield f"data: {chunk.text}\n\n"
        except Exception as e:
            logger.exception("TOC 生成失败: %s", e)
            yield f"data: 生成失败：{e}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen_toc(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@writing_router.post("/tasks/{task_id}/generate_outline_from_toc")
async def generate_outline_from_toc(task_id: str, user: dict = Depends(require_write_access)):
    await _ensure_task_owner(task_id, user["id"])
    task = await get_writing_task(task_id, user["id"])
    if not task:
        raise HTTPException(status_code=404, detail="写作任务不存在")
    if not (task.get("toc") or "").strip():
        raise HTTPException(status_code=400, detail="请先保存写作目录（TOC）")

    prompt = (
        f"请根据以下写作目录，为每个章节生成详细的内容大纲（使用 ## 和 ### Markdown 层级）：\n"
        f"文章标题：{task['title']}\n字数要求：{task['word_count']}字（0 表示不限）\n"
        f"风格要求：{task['style_req']}\n内容要求：{task['content_req']}\n\n"
        f"写作目录：\n{task['toc']}\n\n仅输出完整的 Markdown 大纲，不加任何额外说明。"
    )

    async def gen_outline():
        try:
            stream = await client.aio.models.generate_content_stream(
                model=settings.generation_model, contents=prompt
            )
            async for chunk in stream:
                if chunk.text:
                    yield f"data: {chunk.text}\n\n"
        except Exception as e:
            logger.exception("从 TOC 生成大纲失败: %s", e)
            yield f"data: 生成失败：{e}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen_outline(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@writing_router.get("/tasks/{task_id}/sections")
async def list_sections(task_id: str, user: dict = Depends(require_write_access)):
    await _ensure_task_owner(task_id, user["id"])
    sections = await get_writing_sections(task_id)
    return [_serialize_row(s) for s in sections]


@writing_router.patch("/tasks/{task_id}/sections/{section_id}")
async def patch_section(
    task_id: str,
    section_id: str,
    payload: UpdateSectionRequest,
    user: dict = Depends(require_write_access),
):
    await _ensure_task_owner(task_id, user["id"])
    data = payload.dict(exclude_none=True)
    if not data:
        return {"success": True}
    if not await update_writing_section(section_id, task_id, **data):
        raise HTTPException(status_code=404, detail="段落不存在")
    return {"success": True}


@writing_router.post("/tasks/{task_id}/sections/{section_id}/generate")
async def generate_section_content(
    task_id: str,
    section_id: str,
    user: dict = Depends(require_write_access),
):
    await _ensure_task_owner(task_id, user["id"])
    task = await get_writing_task(task_id, user["id"])
    if not task:
        raise HTTPException(status_code=404, detail="写作任务不存在")
    section = await get_writing_section(section_id, task_id)
    if not section:
        raise HTTPException(status_code=404, detail="段落不存在")

    reference_files = task.get("reference_files") or []
    rag_text = ""
    if reference_files:
        q = f"{task['title']} {section['heading']} {task['content_req']}"
        embedding = await get_embedding(embed_client, q)
        rag_results = await query_rag(
            embedding,
            session_id=str(task["session_id"]),
            source_files=reference_files,
        )
        rag_text = "\n".join(r["content"] for r in rag_results)

    # Previous section's tail for continuity
    all_secs = await get_writing_sections(task_id)
    idx = section["section_index"]
    prev_tail = ""
    for s in all_secs:
        if s["section_index"] < idx and s.get("content"):
            prev_tail = s["content"][-800:]

    wc_target = section.get("word_count_target") or 0
    if wc_target == 0:
        wc = (task.get("word_count") or 0)
        wc_target = (wc // len(all_secs)) if (wc > 0 and all_secs) else 2000

    heading = section["heading"]
    sub_outline = section.get("sub_outline") or ""
    sec_outline = (f"## {heading}\n{sub_outline}").strip() if sub_outline else f"## {heading}"
    context_hint = f"\n前一段落结尾（仅供衔接参考，勿重复）：\n...{prev_tail}" if prev_tail else ""

    _skills = (task.get("style_skills") or "").strip()
    _style_block = (
        f"\n【风格技能手册（优先遵照）】\n{_skills}\n"
        if _skills else
        f"\n风格：{task['style_req']}\n" if task.get("style_req") else ""
    )
    prompt = (
        f"请为以下写作任务创作指定章节内容（Markdown，直接输出含 ## 章节标题的完整章节）：\n"
        f"文章标题：{task['title']}{_style_block}内容要求：{task['content_req']}\n"
        f"完整大纲：\n{task.get('outline', '')}\n参考资料：\n{rag_text}\n"
        f"本章节大纲：\n{sec_outline}\n本章节字数：约{wc_target}字"
        f"{context_hint}\n直接输出本章节完整内容，不加任何额外说明。"
    )
    gen_config = gtypes.GenerateContentConfig(max_output_tokens=65536)
    accumulated: list[str] = []

    async def gen_section():
        try:
            stream = await client.aio.models.generate_content_stream(
                model=settings.generation_model, contents=prompt, config=gen_config,
            )
            async for chunk in stream:
                if chunk.text:
                    accumulated.append(chunk.text)
                    yield f"data: {chunk.text}\n\n"
        except Exception as e:
            logger.exception("章节内容生成失败: %s", e)
            yield f"data: 生成失败：{e}\n\n"
            yield "data: [DONE]\n\n"
            return
        full_content = "".join(accumulated)
        if full_content:
            await update_writing_section(section_id, task_id, content=full_content, status="draft")
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen_section(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@writing_router.post("/tasks/{task_id}/sections/{section_id}/format")
async def format_section_content(
    task_id: str,
    section_id: str,
    user: dict = Depends(require_write_access),
):
    await _ensure_task_owner(task_id, user["id"])
    section = await get_writing_section(section_id, task_id)
    if not section or not section.get("content"):
        raise HTTPException(status_code=404, detail="段落内容不存在")
    try:
        formatted = await asyncio.to_thread(_format_markdown_sync, section["content"])
    except Exception as e:
        logger.exception("段落排版失败: %s", e)
        raise HTTPException(status_code=500, detail=f"排版失败：{e}")
    await update_writing_section(section_id, task_id, content=formatted)
    return {"content": formatted}


@writing_router.post("/tasks/{task_id}/sections/{section_id}/chat")
async def section_chat(
    task_id: str,
    section_id: str,
    message: str = Form(...),
    user: dict = Depends(require_write_access),
):
    await _ensure_task_owner(task_id, user["id"])
    task = await get_writing_task(task_id, user["id"])
    if not task:
        raise HTTPException(status_code=404, detail="写作任务不存在")
    section = await get_writing_section(section_id, task_id)
    if not section:
        raise HTTPException(status_code=404, detail="段落不存在")

    reference_files = task.get("reference_files") or []
    rag_text = ""
    if reference_files:
        embedding = await get_embedding(embed_client, f"{message} {section['heading']}")
        rag_results = await query_rag(
            embedding,
            session_id=str(task["session_id"]),
            source_files=reference_files,
        )
        rag_text = "\n".join(r["content"] for r in rag_results)

    system_instruction = (
        "你是一个专业的写作助手，协助用户创作和改进指定章节。\n"
        "若用户要求修改章节内容，在回复末尾用以下标记输出完整修改后的章节（Markdown，含 ## 标题）：\n"
        "[SECTION_UPDATE_START]\n（完整修改后章节内容）\n[SECTION_UPDATE_END]\n"
        "标记后另起一行用 1-2 句话说明改动要点。\n"
        "若用户只是提问或讨论，正常回复即可，无需输出标记。"
    )

    content_block = (
        f"\n\n## 当前章节内容\n\n{section.get('content', '')}"
        if section.get("content") else "\n\n## 当前章节内容\n\n（尚未生成内容）"
    )
    rag_block = f"\n\n## 参考资料片段\n\n{rag_text}" if rag_text else ""

    prompt = (
        f"## 写作任务\n标题：{task['title']}\n风格：{task['style_req']}\n内容要求：{task['content_req']}\n"
        f"## 当前章节：{section['heading']}\n章节大纲：{section.get('sub_outline', '')}"
        f"{content_block}{rag_block}\n\n## 用户指令\n\n{message}"
    )

    config = gtypes.GenerateContentConfig(system_instruction=system_instruction, max_output_tokens=65536)
    try:
        resp = await client.aio.models.generate_content(
            model=settings.generation_model, contents=prompt, config=config,
        )
        answer = resp.text
    except Exception as e:
        logger.exception("章节对话生成失败: %s", e)
        raise HTTPException(status_code=502, detail="AI 服务暂时不可用")

    return {"answer": answer}


_DISTILL_SYSTEM = """你是专业文体风格分析师。基于提供的材料，提炼出写作Agent可直接调用的「风格技能手册」。
输出 Markdown，结构严格如下（每节 2-3 条，每条一句话，具体可操作）：

## 语气与腔调
- ...

## 句式结构
- ...

## 词汇风格
- ...

## 叙事节奏
- ...

## 过渡与衔接
- ...

## 结构模式
- ...

仅输出手册内容，不加任何说明或前言。"""

_READABILITY_PROMPT_TMPL = """你是专业中文文章阅读体验评审师。请评审以下文章：

{content}

评估五个维度：逻辑连贯性、段落长度（建议150-300字/段）、重复表达、标题内容一致性、整体流畅度。

输出格式（严格遵守，不可增删字段）：
评分：[0-100整数]
问题：
- [具体问题，标注段落位置]
- [...]
总结：[1-2句整体评价]"""

_STYLE_COMPARE_PROMPT_TMPL = """你是风格对照专家。将【待评估文章】与【风格参考资料】进行深度对照分析。

{style_ref}

【待评估文章节选】
{content}

评估四个维度（每项0-100分）：语气腔调匹配度、句式结构相似度、词汇风格一致性、叙事节奏吻合度。

输出格式（严格遵守）：
总分：[四维平均，0-100整数]
语气腔调：[分数] - [一句具体分析]
句式结构：[分数] - [一句具体分析]
词汇风格：[分数] - [一句具体分析]
叙事节奏：[分数] - [一句具体分析]
重点改进：
- [最重要改进建议，附原文改法示例]
- [改进建议2]
- [改进建议3]"""


@writing_router.post("/tasks/{task_id}/distill_style")
async def distill_style(task_id: str, user: dict = Depends(require_write_access)):
    await _ensure_task_owner(task_id, user["id"])
    task = await get_writing_task(task_id, user["id"])
    if not task:
        raise HTTPException(status_code=404, detail="写作任务不存在")

    style_req = (task.get("style_req") or "").strip()
    source_text = (task.get("style_source_text") or "").strip()
    if not style_req and not source_text:
        raise HTTPException(status_code=400, detail="请先设置风格要求或通过 URL/文件生成风格描述")

    parts = []
    if style_req:
        parts.append(f"【风格描述】\n{style_req}")
    if source_text:
        parts.append(f"【参考原文节选】\n{source_text[:4000]}")
    user_content = "\n\n".join(parts)

    config = gtypes.GenerateContentConfig(
        system_instruction=_DISTILL_SYSTEM,
        max_output_tokens=4096,
    )
    accumulated: list[str] = []

    async def gen_distill():
        try:
            stream = await client.aio.models.generate_content_stream(
                model=settings.generation_model, contents=user_content, config=config,
            )
            async for chunk in stream:
                if chunk.text:
                    accumulated.append(chunk.text)
                    yield f"data: {chunk.text}\n\n"
        except Exception as e:
            logger.exception("风格蒸馏失败: %s", e)
            yield f"data: 蒸馏失败：{e}\n\n"
            yield "data: [DONE]\n\n"
            return
        full_skills = "".join(accumulated)
        if full_skills:
            await update_style_skills(task_id, full_skills)
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen_distill(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@writing_router.post("/tasks/{task_id}/evaluate")
async def evaluate_content(task_id: str, user: dict = Depends(require_write_access)):
    await _ensure_task_owner(task_id, user["id"])
    task = await get_writing_task(task_id, user["id"])
    if not task:
        raise HTTPException(status_code=404, detail="写作任务不存在")

    content_data = await get_writing_content(task_id)
    content = (content_data.get("content") or "") if content_data else ""
    if not content.strip():
        raise HTTPException(status_code=400, detail="暂无内容可评估")

    style_skills = (task.get("style_skills") or "").strip()
    style_req = (task.get("style_req") or "").strip()
    style_source = (task.get("style_source_text") or "").strip()
    reference_files = task.get("reference_files") or []

    rag_text = ""
    if reference_files and (style_skills or style_req or style_source):
        try:
            embedding = await get_embedding(embed_client, "写作风格 语气 句式 词汇 叙述")
            rag_results = await query_rag(
                embedding,
                session_id=str(task["session_id"]),
                source_files=reference_files,
            )
            rag_text = "\n".join(r["content"] for r in rag_results)
        except Exception:
            pass

    async def gen_eval():
        import json as _json

        # --- Stage 1: Readability ---
        yield f"data: {_json.dumps({'type':'stage','stage':'readability','status':'running'}, ensure_ascii=False)}\n\n"
        readability_score, readability_report = 0, ""
        try:
            prompt = _READABILITY_PROMPT_TMPL.format(content=content[:8000])
            resp = await client.aio.models.generate_content(
                model=settings.generation_model,
                contents=prompt,
                config=gtypes.GenerateContentConfig(max_output_tokens=2048),
            )
            readability_report = resp.text or ""
            m = re.search(r'评分[：:]\s*(\d+)', readability_report)
            readability_score = max(0, min(100, int(m.group(1)))) if m else 70
        except Exception as e:
            readability_report = f"评估失败：{e}"
            readability_score = 0
        yield f"data: {_json.dumps({'type':'stage','stage':'readability','status':'done','score':readability_score,'report':readability_report}, ensure_ascii=False)}\n\n"

        # --- Stage 2: Style Comparison ---
        style_score, style_report = None, ""
        has_style_ref = bool(style_skills or style_req or style_source)
        if has_style_ref:
            yield f"data: {_json.dumps({'type':'stage','stage':'style','status':'running'}, ensure_ascii=False)}\n\n"
            ref_parts = []
            if style_skills:
                ref_parts.append(f"【风格技能手册】\n{style_skills}")
            elif style_req:
                ref_parts.append(f"【风格要求描述】\n{style_req}")
            if style_source:
                ref_parts.append(f"【参考原文节选】\n{style_source[:2000]}")
            if rag_text:
                ref_parts.append(f"【参考资料片段】\n{rag_text[:1500]}")
            try:
                prompt = _STYLE_COMPARE_PROMPT_TMPL.format(
                    style_ref="\n\n".join(ref_parts),
                    content=content[:6000],
                )
                resp = await client.aio.models.generate_content(
                    model=settings.generation_model,
                    contents=prompt,
                    config=gtypes.GenerateContentConfig(max_output_tokens=2048),
                )
                style_report = resp.text or ""
                m = re.search(r'总分[：:]\s*(\d+)', style_report)
                style_score = max(0, min(100, int(m.group(1)))) if m else 70
            except Exception as e:
                style_report = f"评估失败：{e}"
                style_score = 0
            yield f"data: {_json.dumps({'type':'stage','stage':'style','status':'done','score':style_score,'report':style_report}, ensure_ascii=False)}\n\n"

        overall = ((readability_score + (style_score or 0)) // 2) if style_score is not None else readability_score
        try:
            await save_writing_evaluation(
                task_id,
                readability_score=readability_score,
                readability_report=readability_report,
                style_score=style_score or 0,
                style_report=style_report,
                overall_score=overall,
            )
        except Exception as e:
            logger.warning("保存评估结果失败: %s", e)

        yield f"data: {_json.dumps({'type':'complete','overall_score':overall,'has_style':has_style_ref}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen_eval(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@writing_router.get("/tasks/{task_id}/evaluations/latest")
async def get_evaluation_latest(task_id: str, user: dict = Depends(require_write_access)):
    await _ensure_task_owner(task_id, user["id"])
    ev = await get_latest_evaluation(task_id)
    if not ev:
        return {"found": False}
    return {"found": True, **_serialize_row(ev)}


@writing_router.get("/tasks/{task_id}/full_content")
async def get_full_content(task_id: str, user: dict = Depends(require_write_access)):
    """Assemble draft/confirmed section contents into a single document."""
    await _ensure_task_owner(task_id, user["id"])
    sections = await get_writing_sections(task_id)
    parts = [s["content"] for s in sections if s.get("content") and s.get("status") in ("draft", "confirmed")]
    return {"content": "\n\n".join(parts), "section_count": len(parts)}


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
