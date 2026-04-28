from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import asyncio
import uuid

from account import get_current_admin, pwd_context
from backend.db import (
    database,
    get_all_users_with_stats, get_user_by_id,
    get_user_sessions_with_stats, get_user_daily_tokens, get_user_total_tokens,
    get_session_messages_detail, get_session_daily_tokens,
    get_session_files, get_session_info, update_user_max_tokens,
    update_user_max_file_size, update_user_password,
    get_all_invite_codes, create_invite_code,
    get_all_subsystem_status, list_prompt_versions,
)

admin_router = APIRouter()
templates = Jinja2Templates(directory="templates")


@admin_router.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request, admin=Depends(get_current_admin)):
    """板块选择页：用户管理 / 性能调优。"""
    return templates.TemplateResponse("admin/dashboard.html", {
        "request": request, "admin": admin,
    })


@admin_router.get("/users", response_class=HTMLResponse)
async def admin_users(request: Request, admin=Depends(get_current_admin)):
    """用户管理（即原 /admin/ 内容）。"""
    users = await get_all_users_with_stats()
    invite_codes = await get_all_invite_codes()
    return templates.TemplateResponse("admin/users.html", {
        "request": request, "users": users, "invite_codes": invite_codes, "admin": admin,
    })


@admin_router.get("/perf", response_class=HTMLResponse)
async def admin_perf(request: Request, admin=Depends(get_current_admin)):
    """性能调优：子系统状态 + 近期 trace + prompt 版本。"""
    subsystems = await get_all_subsystem_status()
    traces = await database.fetch_all(
        """SELECT t.id, t.session_id, t.user_id, u.username, t.query, t.route,
                  t.iterations, t.duration_ms, t.tokens_in, t.tokens_out, t.created_at
           FROM agent_traces t LEFT JOIN users u ON u.id = t.user_id
           ORDER BY t.created_at DESC LIMIT 50"""
    )
    traces = [dict(r) for r in traces]
    prompt_versions = await list_prompt_versions("agent_tool_rules")
    return templates.TemplateResponse("admin/perf.html", {
        "request": request, "admin": admin,
        "subsystems": subsystems, "traces": traces,
        "prompt_versions": prompt_versions,
        "prompt_name": "agent_tool_rules",
    })


@admin_router.get("/user/{user_id}", response_class=HTMLResponse)
async def user_detail(request: Request, user_id: int, admin=Depends(get_current_admin)):
    profile = await get_user_by_id(user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="用户不存在")
    sessions, daily, total_tokens = await asyncio.gather(
        get_user_sessions_with_stats(user_id),
        get_user_daily_tokens(user_id),
        get_user_total_tokens(user_id),
    )
    return templates.TemplateResponse("admin/user.html", {
        "request": request, "profile": profile,
        "sessions": sessions, "daily": daily,
        "total_tokens": total_tokens, "admin": admin,
    })


@admin_router.get("/session/{session_id}", response_class=HTMLResponse)
async def session_detail(request: Request, session_id: str, admin=Depends(get_current_admin)):
    info = await get_session_info(session_id)
    if not info:
        raise HTTPException(status_code=404, detail="Session 不存在")
    messages = await get_session_messages_detail(session_id)
    daily = await get_session_daily_tokens(session_id)
    files = await get_session_files(session_id)
    return templates.TemplateResponse("admin/session.html", {
        "request": request, "info": info,
        "messages": messages, "daily": daily, "files": files, "admin": admin,
    })


@admin_router.post("/user/{user_id}/max_tokens")
async def set_max_tokens(
    user_id: int,
    max_tokens: int = Form(...),
    admin=Depends(get_current_admin)
):
    await update_user_max_tokens(user_id, max_tokens)
    return JSONResponse({"success": True})


@admin_router.post("/user/{user_id}/max_file_size")
async def set_max_file_size(
    user_id: int,
    max_file_size_mb: int = Form(...),
    admin=Depends(get_current_admin)
):
    await update_user_max_file_size(user_id, max_file_size_mb)
    return JSONResponse({"success": True})


@admin_router.post("/user/{user_id}/reset_password")
async def reset_password(
    user_id: int,
    new_password: str = Form(...),
    admin=Depends(get_current_admin)
):
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="密码至少需要 6 位")
    await update_user_password(user_id, pwd_context.hash(new_password))
    return JSONResponse({"success": True})


@admin_router.post("/invite/generate")
async def generate_invite(admin=Depends(get_current_admin)):
    code = str(uuid.uuid4())
    await create_invite_code(code)
    return JSONResponse({"success": True, "code": code})
