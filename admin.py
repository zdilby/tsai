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
    list_agent_b_runs,
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
    """性能调优：子系统状态 + 近期 trace + prompt 版本 + 机器人控制。"""
    from settings import settings as _settings
    subsystems = await get_all_subsystem_status()
    traces = await database.fetch_all(
        """SELECT t.id, t.session_id, t.user_id, u.username, t.query, t.route,
                  t.iterations, t.duration_ms, t.tokens_in, t.tokens_out, t.created_at
           FROM agent_traces t LEFT JOIN users u ON u.id = t.user_id
           ORDER BY t.created_at DESC LIMIT 50"""
    )
    traces = [dict(r) for r in traces]
    prompt_versions = await list_prompt_versions("agent_tool_rules")

    # 机器人专属信息
    bot_recent = await database.fetch_all(
        """SELECT t.query, t.route, t.iterations, t.duration_ms,
                  s.name AS session_name, t.created_at
           FROM agent_traces t
           JOIN users u ON u.id = t.user_id
           LEFT JOIN sessions s ON s.id = t.session_id
           WHERE u.username = :bot
           ORDER BY t.created_at DESC LIMIT 20""",
        values={"bot": _settings.bot_username},
    )
    bot_session_count = await database.fetch_val(
        """SELECT COUNT(*) FROM sessions s
           JOIN users u ON u.id = s.user_id
           WHERE u.username = :bot AND s.name IS NOT NULL""",
        values={"bot": _settings.bot_username},
    )

    agent_b_runs = await list_agent_b_runs(limit=20)

    return templates.TemplateResponse("admin/perf.html", {
        "request": request, "admin": admin,
        "subsystems": subsystems, "traces": traces,
        "prompt_versions": prompt_versions,
        "prompt_name": "agent_tool_rules",
        "bot_recent": [dict(r) for r in bot_recent],
        "bot_session_count": bot_session_count or 0,
        "bot_username": _settings.bot_username,
        "bot_source_username": _settings.bot_source_username,
        "agent_b_runs": agent_b_runs,
        "agent_b_run_hours": _settings.agent_b_run_hours,
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


# ── Phase 3b — 机器人控制端点 ────────────────────────────────────────────────

@admin_router.post("/bot/start")
async def bot_start(admin=Depends(get_current_admin)):
    """启用机器人每日自动跑 query。"""
    from backend.db import set_subsystem_enabled
    await set_subsystem_enabled("bot", True, status_msg="enabled by admin")
    return JSONResponse({"success": True, "enabled": True})


@admin_router.post("/bot/stop")
async def bot_stop(admin=Depends(get_current_admin)):
    """停用机器人。已在跑的任务不会被中断，但下次 beat 触发时会跳过。"""
    from backend.db import set_subsystem_enabled
    await set_subsystem_enabled("bot", False, status_msg="disabled by admin")
    return JSONResponse({"success": True, "enabled": False})


@admin_router.post("/bot/snapshot")
async def bot_snapshot(admin=Depends(get_current_admin)):
    """
    一次性 snapshot：把"天书"（settings.bot_source_username）的所有 named session
    复刻给机器人用户。注意：重复调用会产生重复副本。
    """
    from backend.bot import ensure_bot_user, snapshot_user_sessions
    from settings import settings as _settings
    bot = await ensure_bot_user()
    try:
        result = await snapshot_user_sessions(_settings.bot_source_username, bot["id"])
    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    return JSONResponse({"success": True, **result})


@admin_router.post("/bot/run_now")
async def bot_run_now(admin=Depends(get_current_admin)):
    """
    立即触发一次机器人每日任务（不等 03:00 beat）。
    通过 Celery 异步调度，立刻返回 task_id；前端可轮询结果或看 perf 页面 trace。
    """
    from backend.tasks import bot_run_daily_queries
    async_result = bot_run_daily_queries.delay()
    return JSONResponse({"success": True, "task_id": async_result.id})


@admin_router.get("/bot/recent_queries")
async def bot_recent_queries(admin=Depends(get_current_admin)):
    """返回机器人最近 20 条自动 query 的 trace。"""
    from settings import settings as _settings
    rows = await database.fetch_all(
        """SELECT t.id, t.session_id, s.name AS session_name,
                  t.query, t.route, t.iterations, t.duration_ms,
                  t.tokens_in, t.tokens_out, t.created_at
           FROM agent_traces t
           JOIN users u ON u.id = t.user_id
           LEFT JOIN sessions s ON s.id = t.session_id
           WHERE u.username = :bot
           ORDER BY t.created_at DESC LIMIT 20""",
        values={"bot": _settings.bot_username},
    )
    return JSONResponse({"queries": [dict(r) for r in rows]}, headers={"Cache-Control": "no-store"})


# ── Phase 3c — Agent B 控制 ────────────────────────────────────────────────

@admin_router.post("/agent_b/start")
async def agent_b_start(admin=Depends(get_current_admin)):
    from backend.db import set_subsystem_enabled
    await set_subsystem_enabled("agent_b", True, status_msg="enabled by admin")
    return JSONResponse({"success": True, "enabled": True})


@admin_router.post("/agent_b/stop")
async def agent_b_stop(admin=Depends(get_current_admin)):
    from backend.db import set_subsystem_enabled
    await set_subsystem_enabled("agent_b", False, status_msg="disabled by admin")
    return JSONResponse({"success": True, "enabled": False})


@admin_router.post("/agent_b/run_now")
async def agent_b_run_now(admin=Depends(get_current_admin)):
    """立即触发 Agent B 一次分析（不等 12 小时 beat）。"""
    from backend.tasks import agent_b_analyze_pending_traces
    async_result = agent_b_analyze_pending_traces.delay()
    return JSONResponse({"success": True, "task_id": async_result.id})


# ── Phase 3c — Prompt 版本回滚 ────────────────────────────────────────────

@admin_router.post("/prompt/rollback/{version_id}")
async def prompt_rollback(version_id: int, admin=Depends(get_current_admin)):
    """
    把指定 version_id 设为 active，其余 demote。立即失效缓存让所有进程拉新版。
    用于 Agent B 改坏了的紧急回退。
    """
    from backend.db import activate_prompt_version
    from backend.agent_chat import invalidate_prompt_cache
    ok = await activate_prompt_version(version_id)
    if not ok:
        return JSONResponse({"success": False, "error": "version not found"}, status_code=404)
    invalidate_prompt_cache()
    return JSONResponse({"success": True, "active_version_id": version_id})
