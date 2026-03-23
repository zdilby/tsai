from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from account import get_current_admin
from backend.db import (
    get_all_users_with_stats, get_user_by_id,
    get_user_sessions_with_stats, get_user_daily_tokens,
    get_session_messages_detail, get_session_daily_tokens,
    get_session_files, get_session_info, update_user_max_tokens,
    update_user_max_file_size,
)

admin_router = APIRouter()
templates = Jinja2Templates(directory="templates")


@admin_router.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request, admin=Depends(get_current_admin)):
    users = await get_all_users_with_stats()
    return templates.TemplateResponse("admin/dashboard.html", {
        "request": request, "users": users, "admin": admin,
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
