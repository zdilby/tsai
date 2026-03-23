from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uuid

from account import get_current_admin, pwd_context
from backend.db import (
    get_all_users_with_stats, get_user_by_id,
    get_user_sessions_with_stats, get_user_daily_tokens,
    get_session_messages_detail, get_session_daily_tokens,
    get_session_files, get_session_info, update_user_max_tokens,
    update_user_max_file_size, update_user_password,
    get_all_invite_codes, create_invite_code,
)

admin_router = APIRouter()
templates = Jinja2Templates(directory="templates")


@admin_router.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request, admin=Depends(get_current_admin)):
    users = await get_all_users_with_stats()
    invite_codes = await get_all_invite_codes()
    return templates.TemplateResponse("admin/dashboard.html", {
        "request": request, "users": users, "invite_codes": invite_codes, "admin": admin,
    })


@admin_router.get("/user/{user_id}", response_class=HTMLResponse)
async def user_detail(request: Request, user_id: int, admin=Depends(get_current_admin)):
    profile = await get_user_by_id(user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="用户不存在")
    sessions = await get_user_sessions_with_stats(user_id)
    daily = await get_user_daily_tokens(user_id)
    return templates.TemplateResponse("admin/user.html", {
        "request": request, "profile": profile,
        "sessions": sessions, "daily": daily, "admin": admin,
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
