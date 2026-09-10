from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from google.genai import types as gtypes
from pydantic import BaseModel

from settings import settings, logger, client
from account import get_current_user
from backend.db import (
    create_drawing_style,
    get_drawing_styles,
    get_drawing_style,
    drawing_style_owned_by,
    update_drawing_style,
    delete_drawing_style,
    create_drawing_prompt,
    get_drawing_prompts,
    delete_drawing_prompt,
    update_drawing_prompt,
    create_drawing_skill_package,
    get_drawing_skill_packages,
    get_drawing_skill_package,
    delete_drawing_skill_package,
    update_drawing_skill_package,
    create_drawing_generation,
    update_drawing_generation_result,
    get_drawing_generations,
    get_drawing_generation,
    get_drawing_generation_tips,
    get_drawing_generation_lineage,
    delete_drawing_generation_step,
    delete_drawing_generation_lineage,
)
from backend.image_gen import generate_image, edit_image, ImageGenError

drawing_router = APIRouter()
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


async def require_draw_access(request: Request, user: dict = Depends(get_current_user)) -> dict:
    if user["is_admin"]:
        return user
    if not user["can_draw"]:
        endpoint_name = getattr(request.scope.get("endpoint"), "__name__", "")
        if (
            endpoint_name in {"drawing_page", "drawing_style_page"}
            and "text/html" in request.headers.get("accept", "")
        ):
            return RedirectResponse(url="/", status_code=302)
        raise HTTPException(status_code=403, detail="无作图权限")
    return user


def _serialize_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _serialize_row(row: dict) -> dict:
    return {k: _serialize_value(v) for k, v in row.items()}


async def _ensure_style_owner(style_id: str, user_id: int):
    if not await drawing_style_owned_by(style_id, user_id):
        raise HTTPException(status_code=403, detail="无权访问该作图风格")


@drawing_router.get("/", response_class=HTMLResponse)
async def drawing_page(request: Request, user: dict = Depends(require_draw_access)):
    if isinstance(user, RedirectResponse):
        return user
    styles = await get_drawing_styles(user["id"])
    if styles:
        latest_style_id = styles[0]["id"]
        return RedirectResponse(url=f"/drawing/{latest_style_id}")
    return _templates.TemplateResponse(
        "drawing.html",
        {
            "request": request, "style_id": None, "style": None, "user": user["username"],
            "can_write": bool(user["can_write"] or user["is_admin"]),
            "can_draw": bool(user["can_draw"] or user["is_admin"]),
            "can_map": bool(user["can_map"] or user["is_admin"]),
        },
    )


class CreateStyleRequest(BaseModel):
    name: str = "未命名风格"
    prompt_ids: list[str] = []


class UpdateStyleRequest(BaseModel):
    name: Optional[str] = None
    prompt_ids: Optional[list[str]] = None
    # "" 表示显式解绑；None（即字段缺失）表示不改动；真实 id 表示绑定。
    # exclude_none=True 会丢弃 None，但保留 ""，所以三种状态互不冲突。
    skill_package_id: Optional[str] = None


class CreatePromptRequest(BaseModel):
    name: str
    content: str = ""


class UpdatePromptRequest(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None


class CreateSkillPackageRequest(BaseModel):
    name: str
    source_url: str = ""
    instructions: str


class UpdateSkillPackageRequest(BaseModel):
    name: Optional[str] = None
    source_url: Optional[str] = None
    instructions: Optional[str] = None


class FetchGithubSkillRequest(BaseModel):
    repo_url: str


class GenerateRequest(BaseModel):
    user_input: str
    parent_generation_id: Optional[str] = None


async def _compose_edit_instruction(
    style_context: str, initial_prompt: str, previous_prompt: str, new_instruction: str
) -> tuple[str, bool]:
    """用 Gemini 把风格设定 + 历史上下文 + 本次新指令合成为一条可直接执行的图像编辑指令。

    图片本身的最新视觉状态才是 images/edits 调用里最重要的输入（图片文件本身携带），
    这里只负责把文字层面的意图理清楚，避免简单字符串拼接导致多轮编辑后指令混乱、风格漂移。

    返回 (指令文本, 是否发生了合成降级)。降级 = Gemini 调用失败或返回空文本，此时回退为
    用户原始输入直接使用；调用方需要把这个信号透出给前端，否则用户完全看不出这次编辑
    有没有真的用上风格设定和历史上下文。
    """
    prompt = (
        "你是图像编辑指令合成助手。任务：结合风格设定与本次编辑上下文，"
        "输出一条清晰、单一、可以直接交给图像编辑模型执行的修改指令。\n"
        f"风格设定：{style_context or '（无）'}\n"
        f"最初创作意图：{initial_prompt or '（无）'}\n"
        f"上一轮编辑指令：{previous_prompt or '（无）'}\n"
        f"用户本次新的修改要求：{new_instruction}\n"
        "只输出最终指令文本本身，不要解释、不要加引号、不要输出多余内容。"
    )
    try:
        resp = await client.aio.models.generate_content(model=settings.generation_model, contents=prompt)
        composed = (resp.text or "").strip()
        if composed:
            return composed, False
        logger.warning("编辑指令合成返回空文本，回退为原始输入")
        return new_instruction, True
    except Exception as e:
        logger.warning("编辑指令合成失败，回退为原始输入: %s", e)
        return new_instruction, True


async def _compile_with_skill_package(
    *, skill_instructions: str, style_context: str,
    user_input: str, base_image_bytes: bytes | None = None,
    initial_prompt: str = "", previous_prompt: str = "",
) -> tuple[str, bool]:
    """用 Gemini 按第三方 skill 包的方法论编译出最终 image-gen prompt。

    skill 包（SKILL.md + references/*.md 的快照全文）定义了一套完整的"如何把任意主题转译成
    符合某种视觉体系的图"的规则；style_context（风格勾选的 Prompt 库条目拼接）是这个风格在
    skill 规则允许范围内的补充倾向，不是与 skill 平级的另一套指令——两者都交给 LLM 在编译这
    一步做融合仲裁，产出一条内部自洽的最终 prompt，避免简单拼接导致相互矛盾。

    返回 (最终 prompt, 是否发生了编译降级)。降级 = Gemini 调用失败或返回空文本，此时回退为
    style_context+user_input 直接拼接、完全没有用上 skill 规则；调用方需要把这个信号透出给
    前端，否则用户会以为图片是按 skill 生成的，实际上只是原始输入直接发给了图像模型。
    """
    parts = [
        "以下是一套图像生成方法论（skill），严格遵循其中的规则、术语和结构来产出最终 image-gen prompt：",
        skill_instructions,
    ]
    if style_context:
        parts.append(f"这个风格配置的补充要求（在不违反上面 skill 规则的前提下尽量满足）：{style_context}")
    if initial_prompt:
        parts.append(f"这条创作历史最初的意图：{initial_prompt}")
    if previous_prompt:
        parts.append(f"上一轮的编译结果：{previous_prompt}")
    parts.append(f"本次用户的创作/修改要求：{user_input}")
    parts.append("只输出最终可以直接交给图像生成模型执行的一段 prompt 文本本身，不要解释、不要加引号、不要输出选择过程。")
    prompt_text = "\n\n".join(parts)

    try:
        contents = [prompt_text]
        if base_image_bytes:
            contents.append(gtypes.Part.from_bytes(data=base_image_bytes, mime_type="image/png"))
        resp = await client.aio.models.generate_content(model=settings.generation_model, contents=contents)
        composed = (resp.text or "").strip()
        if composed:
            return composed, False
        logger.warning("skill 编译返回空文本，回退为 style_context+user_input 拼接")
        return "\n\n".join([p for p in [style_context, user_input] if p]), True
    except Exception as e:
        logger.warning("skill 编译失败，回退为 style_context+user_input 拼接: %s", e)
        return "\n\n".join([p for p in [style_context, user_input] if p]), True


@drawing_router.post("/styles")
async def create_style(payload: CreateStyleRequest, user: dict = Depends(require_draw_access)):
    name = payload.name or "未命名风格"
    style_id = await create_drawing_style(user["id"], name, payload.prompt_ids)
    return {"id": style_id, "name": name}


@drawing_router.get("/styles")
async def list_styles(user: dict = Depends(require_draw_access)):
    styles = await get_drawing_styles(user["id"])
    return [_serialize_row(s) for s in styles]


@drawing_router.get("/styles/{style_id}")
async def get_style(style_id: str, user: dict = Depends(require_draw_access)):
    await _ensure_style_owner(style_id, user["id"])
    style = await get_drawing_style(style_id, user["id"])
    if not style:
        raise HTTPException(status_code=404, detail="作图风格不存在")
    return _serialize_row(style)


@drawing_router.patch("/styles/{style_id}")
async def patch_style(style_id: str, payload: UpdateStyleRequest, user: dict = Depends(require_draw_access)):
    await _ensure_style_owner(style_id, user["id"])
    data = payload.dict(exclude_none=True)
    if "skill_package_id" in data and data["skill_package_id"] == "":
        data["skill_package_id"] = None  # 空字符串 = 显式解绑
    if data:
        await update_drawing_style(style_id, user["id"], **data)
    return {"success": True}


@drawing_router.delete("/styles/{style_id}")
async def remove_style(style_id: str, user: dict = Depends(require_draw_access)):
    await _ensure_style_owner(style_id, user["id"])
    generations = await get_drawing_generations(style_id, limit=1000)
    await delete_drawing_style(style_id, user["id"])
    for gen in generations:
        _delete_image_file(gen.get("image_path"))
    return {"success": True}


@drawing_router.get("/prompts")
async def list_prompts(user: dict = Depends(require_draw_access)):
    prompts = await get_drawing_prompts()
    return [_serialize_row(p) for p in prompts]


@drawing_router.post("/prompts")
async def create_prompt(payload: CreatePromptRequest, user: dict = Depends(require_draw_access)):
    prompt_id = await create_drawing_prompt(payload.name, payload.content, user["id"])
    return {"id": prompt_id, "name": payload.name, "content": payload.content}


@drawing_router.patch("/prompts/{prompt_id}")
async def update_prompt(prompt_id: str, payload: UpdatePromptRequest, user: dict = Depends(require_draw_access)):
    data = payload.dict(exclude_none=True)
    if data:
        await update_drawing_prompt(prompt_id, **data)
    return {"success": True}


@drawing_router.delete("/prompts/{prompt_id}")
async def remove_prompt(prompt_id: str, user: dict = Depends(require_draw_access)):
    await delete_drawing_prompt(prompt_id)
    return {"success": True}


async def _fetch_skill_package_from_github(repo_url: str) -> str:
    """抓取一个 GitHub 仓库的 SKILL.md + references/*.md，拼接成一份 skill 包全文快照。

    存的是仓库某一时刻的快照，不依赖第三方仓库长期存活；调用方应在保存前让用户预览/编辑这段文本。
    """
    parsed = urlparse(repo_url.strip())
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.netloc not in ("github.com", "www.github.com") or len(parts) < 2:
        raise HTTPException(status_code=400, detail="请提供形如 https://github.com/owner/repo 的仓库地址")
    owner, repo = parts[0], parts[1]
    headers = {"User-Agent": "tsai-drawing-skill-fetch", "Accept": "application/vnd.github+json"}

    async with httpx.AsyncClient(timeout=30, headers=headers) as hc:
        try:
            repo_resp = await hc.get(f"https://api.github.com/repos/{owner}/{repo}")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"访问 GitHub 失败：{e}")
        if repo_resp.status_code != 200:
            raise HTTPException(status_code=400, detail=f"无法访问仓库 {owner}/{repo}（{repo_resp.status_code}）")
        branch = repo_resp.json().get("default_branch", "main")

        skill_resp = await hc.get(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/SKILL.md")
        if skill_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="该仓库根目录下没有找到 SKILL.md，不是一个标准 skill 仓库")
        sections = [f"# SKILL.md\n\n{skill_resp.text}"]

        refs_resp = await hc.get(f"https://api.github.com/repos/{owner}/{repo}/contents/references")
        if refs_resp.status_code == 200:
            for item in refs_resp.json():
                if item.get("type") == "file" and item.get("name", "").endswith(".md"):
                    file_resp = await hc.get(
                        f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/references/{item['name']}"
                    )
                    if file_resp.status_code == 200:
                        sections.append(f"# references/{item['name']}\n\n{file_resp.text}")

        return "\n\n---\n\n".join(sections)


@drawing_router.get("/skill-packages")
async def list_skill_packages(user: dict = Depends(require_draw_access)):
    packages = await get_drawing_skill_packages()
    return [_serialize_row(p) for p in packages]


@drawing_router.post("/skill-packages")
async def create_skill_package(payload: CreateSkillPackageRequest, user: dict = Depends(require_draw_access)):
    if not payload.instructions.strip():
        raise HTTPException(status_code=400, detail="skill 内容不能为空")
    package_id = await create_drawing_skill_package(
        payload.name, payload.source_url, payload.instructions, user["id"]
    )
    return {"id": package_id, "name": payload.name}


@drawing_router.post("/skill-packages/fetch-github")
async def fetch_skill_package_preview(payload: FetchGithubSkillRequest, user: dict = Depends(require_draw_access)):
    instructions = await _fetch_skill_package_from_github(payload.repo_url)
    return {"instructions": instructions}


@drawing_router.get("/skill-packages/{package_id}")
async def get_skill_package(package_id: str, user: dict = Depends(require_draw_access)):
    package = await get_drawing_skill_package(package_id)
    if not package:
        raise HTTPException(status_code=404, detail="skill 包不存在")
    return _serialize_row(package)


@drawing_router.patch("/skill-packages/{package_id}")
async def update_skill_package(package_id: str, payload: UpdateSkillPackageRequest, user: dict = Depends(require_draw_access)):
    data = payload.dict(exclude_none=True)
    if data:
        await update_drawing_skill_package(package_id, **data)
    return {"success": True}


@drawing_router.delete("/skill-packages/{package_id}")
async def remove_skill_package(package_id: str, user: dict = Depends(require_draw_access)):
    await delete_drawing_skill_package(package_id)
    return {"success": True}


def _delete_image_file(relative_path: str | None) -> None:
    if not relative_path:
        return
    try:
        abs_path = settings.base_dir / relative_path
        if abs_path.is_file():
            abs_path.unlink()
    except Exception as e:
        logger.warning("删除作图文件失败 %s: %s", relative_path, e)


def _resolve_prompt_texts(style: dict, all_prompts: dict) -> list[str]:
    return [
        all_prompts[pid]["content"]
        for pid in style["prompt_ids"]
        if pid in all_prompts and all_prompts[pid]["content"]
    ]


def _save_generation_image(
    image_bytes: bytes, username: str, style_id: str, generation_id: str, ext: str = "png"
) -> str:
    save_dir = settings.base_dir / "static" / "images" / username / style_id
    save_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{generation_id}.{ext}"
    (save_dir / filename).write_bytes(image_bytes)
    return str(Path("static") / "images" / username / style_id / filename)


_UPLOAD_CONTENT_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_UPLOAD_LABEL = "[来自用户上传图片]"


async def _run_generation_job(
    *, generation_id: str, style_id: str, username: str,
    full_prompt: str, base_image_bytes: bytes | None,
) -> None:
    """后台执行真正的出图调用。出图经第三方中转，常见 1-3 分钟、偶尔更久，
    不能占着 HTTP 请求。结果（成功图片路径 / 失败原因）写回 drawing_generations，
    前端按 generation_id 轮询 /generations/{id}/status 拿最终状态。"""
    try:
        if base_image_bytes:
            image_bytes_out = await edit_image(base_image_bytes, full_prompt)
        else:
            image_bytes_out = await generate_image(full_prompt)
    except ImageGenError as e:
        logger.warning("作图生成失败 gen=%s kind=%s: %s", generation_id, e.kind, e)
        await update_drawing_generation_result(generation_id, status="failed", error_msg=str(e))
        return
    except Exception as e:  # noqa: BLE001 — 后台任务里任何异常都得落库，否则永远卡 processing
        logger.exception("作图生成未知错误 gen=%s: %s", generation_id, e)
        await update_drawing_generation_result(
            generation_id, status="failed",
            error_msg="生成图片时发生未知错误，请稍后重试；若持续出现请联系管理员。",
        )
        return

    try:
        relative_path = _save_generation_image(image_bytes_out, username, style_id, generation_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("作图保存失败 gen=%s: %s", generation_id, e)
        await update_drawing_generation_result(
            generation_id, status="failed", error_msg=f"图片已生成但保存到本地失败：{e}",
        )
        return

    await update_drawing_generation_result(
        generation_id, status="done", image_path=relative_path, model=settings.codex_image_model,
    )


@drawing_router.post("/styles/{style_id}/generate")
async def generate(
    style_id: str, payload: GenerateRequest, background_tasks: BackgroundTasks,
    user: dict = Depends(require_draw_access),
):
    await _ensure_style_owner(style_id, user["id"])
    style = await get_drawing_style(style_id, user["id"])
    if not style:
        raise HTTPException(status_code=404, detail="作图风格不存在")

    # asyncpg 返回的 UUID 列是 uuid.UUID 对象，而 prompt_ids（来自 jsonb）是字符串——
    # 两侧都转成字符串再做映射，否则查找总是落空但不报错。
    all_prompts = {str(p["id"]): p for p in await get_drawing_prompts()}
    prompt_texts = _resolve_prompt_texts(style, all_prompts)
    style_context = "\n\n".join(prompt_texts)

    # 先统一算出"是否编辑 + 编辑相关上下文"，再决定走 skill 编译还是现状拼接——
    # 两条路径共用同一套编辑上下文，绑没绑 skill 只影响 full_prompt 怎么来。
    base_image_bytes: bytes | None = None
    initial_prompt = ""
    previous_prompt = ""

    if payload.parent_generation_id:
        parent = await get_drawing_generation(payload.parent_generation_id)
        if (
            not parent
            or parent["user_id"] != user["id"]
            or str(parent["style_id"]) != style_id
            or parent["status"] != "done"
            or not parent["image_path"]
        ):
            raise HTTPException(status_code=404, detail="要编辑的图片不存在或尚未生成完成")
        if not payload.user_input.strip():
            raise HTTPException(status_code=400, detail="请输入修改要求")

        chain = await get_drawing_generation_lineage(payload.parent_generation_id, user["id"])
        initial_prompt = chain[0]["full_prompt"] if chain else ""
        previous_prompt = parent["full_prompt"]
        try:
            base_image_bytes = (settings.base_dir / parent["image_path"]).read_bytes()
        except OSError as e:
            raise HTTPException(status_code=404, detail=f"读取原图失败：{e}")

    compile_degraded = False
    if style.get("skill_package_id"):
        package = await get_drawing_skill_package(str(style["skill_package_id"]))
        if not package:
            raise HTTPException(status_code=404, detail="绑定的 skill 包不存在")
        full_prompt, compile_degraded = await _compile_with_skill_package(
            skill_instructions=package["instructions"],
            style_context=style_context,
            user_input=payload.user_input,
            base_image_bytes=base_image_bytes,
            initial_prompt=initial_prompt,
            previous_prompt=previous_prompt,
        )
    elif payload.parent_generation_id:
        full_prompt, compile_degraded = await _compose_edit_instruction(
            style_context, initial_prompt, previous_prompt, payload.user_input
        )
    else:
        full_prompt = "\n\n".join([p for p in [style_context, payload.user_input] if p and p.strip()])

    if not full_prompt.strip():
        raise HTTPException(status_code=400, detail="prompt 为空，无法生成")

    generation_id = await create_drawing_generation(
        style_id, user["id"], payload.user_input, full_prompt,
        parent_generation_id=payload.parent_generation_id,
    )
    await update_drawing_generation_result(generation_id, status="processing")

    # 真正的出图调用放后台，接口立即返回 processing。前端轮询
    # GET /drawing/generations/{id}/status 直到 done / failed；用户切走再回来也能续上。
    background_tasks.add_task(
        _run_generation_job,
        generation_id=generation_id,
        style_id=style_id,
        username=user["username"],
        full_prompt=full_prompt,
        base_image_bytes=base_image_bytes,
    )

    return {
        "id": generation_id,
        "full_prompt": full_prompt,
        "user_input": payload.user_input,
        "parent_generation_id": payload.parent_generation_id,
        "status": "processing",
        "compile_degraded": compile_degraded,
    }


@drawing_router.post("/styles/{style_id}/upload")
async def upload_generation_image(
    style_id: str, file: UploadFile = File(...), user: dict = Depends(require_draw_access)
):
    await _ensure_style_owner(style_id, user["id"])

    ext = _UPLOAD_CONTENT_TYPES.get(file.content_type)
    if not ext:
        raise HTTPException(status_code=400, detail="仅支持上传 PNG/JPEG/WEBP 图片")

    content = await file.read()
    max_mb = user["max_file_size_mb"] if user["max_file_size_mb"] is not None else 10
    if max_mb > 0 and len(content) > max_mb * 1024 * 1024:
        size_mb = round(len(content) / 1024 / 1024, 1)
        raise HTTPException(status_code=413, detail=f"文件大小 {size_mb}MB 超过上限 {max_mb}MB")

    generation_id = await create_drawing_generation(style_id, user["id"], _UPLOAD_LABEL, _UPLOAD_LABEL)
    relative_path = _save_generation_image(content, user["username"], style_id, generation_id, ext=ext)
    await update_drawing_generation_result(generation_id, status="done", image_path=relative_path, model="upload")

    return {
        "id": generation_id,
        "image_url": "/" + relative_path,
        "full_prompt": _UPLOAD_LABEL,
        "user_input": _UPLOAD_LABEL,
        "parent_generation_id": None,
        "status": "done",
    }


@drawing_router.get("/styles/{style_id}/generations")
async def list_generations(style_id: str, user: dict = Depends(require_draw_access)):
    await _ensure_style_owner(style_id, user["id"])
    tips = await get_drawing_generation_tips(style_id)
    result = []
    for g in tips:
        row = _serialize_row(g)
        row["image_url"] = ("/" + row["image_path"]) if row.get("image_path") else None
        result.append(row)
    return result


@drawing_router.get("/generations/{generation_id}/status")
async def generation_status(generation_id: str, user: dict = Depends(require_draw_access)):
    """前端轮询单条生成的状态：processing / done / failed。"""
    gen = await get_drawing_generation(generation_id)
    if not gen or gen["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="生成记录不存在")
    return {
        "id": str(gen["id"]),
        "status": gen["status"],
        "image_url": ("/" + gen["image_path"]) if gen["image_path"] else None,
        "error_msg": gen["error_msg"] or "",
        "user_input": gen["user_input"],
    }


@drawing_router.get("/generations/{generation_id}/history")
async def get_generation_history(generation_id: str, user: dict = Depends(require_draw_access)):
    chain = await get_drawing_generation_lineage(generation_id, user["id"])
    if not chain:
        raise HTTPException(status_code=404, detail="生成记录不存在")
    result = []
    for g in chain:
        row = _serialize_row(g)
        row["image_url"] = ("/" + row["image_path"]) if row.get("image_path") else None
        result.append(row)
    return result


@drawing_router.delete("/generations/{generation_id}/step")
async def remove_generation_step(generation_id: str, user: dict = Depends(require_draw_access)):
    deleted = await delete_drawing_generation_step(generation_id, user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="生成记录不存在")
    _delete_image_file(deleted.get("image_path"))
    return {"success": True}


@drawing_router.delete("/generations/{generation_id}")
async def remove_generation(generation_id: str, user: dict = Depends(require_draw_access)):
    image_paths = await delete_drawing_generation_lineage(generation_id, user["id"])
    if image_paths is None:
        raise HTTPException(status_code=404, detail="生成记录不存在")
    for path in image_paths:
        _delete_image_file(path)
    return {"success": True}


# 必须放在文件最后：/{style_id} 会匹配任意单段路径，若声明在前会抢先吃掉
# /styles、/skills 等固定路径的请求（FastAPI/Starlette 按注册顺序匹配路由）。
@drawing_router.get("/{style_id}", response_class=HTMLResponse)
async def drawing_style_page(style_id: str, request: Request, user: dict = Depends(require_draw_access)):
    if isinstance(user, RedirectResponse):
        return user
    if not await drawing_style_owned_by(style_id, user["id"]):
        return RedirectResponse(url="/drawing/")
    style = await get_drawing_style(style_id, user["id"])
    return _templates.TemplateResponse(
        "drawing.html",
        {
            "request": request, "style_id": style_id, "style": style, "user": user["username"],
            "can_write": bool(user["can_write"] or user["is_admin"]),
            "can_draw": bool(user["can_draw"] or user["is_admin"]),
            "can_map": bool(user["can_map"] or user["is_admin"]),
        },
    )
