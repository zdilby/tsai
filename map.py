"""地图模块路由（map_router，前缀 /map/）。

与「对话 / 写作 / 作图」并列的第四个模块。参考 MapStage
(https://github.com/hopechen067/MapStage) 及其 Demo。

- 权限门控：`is_admin` 或 `users.can_map=TRUE`（`require_map_access`，逐字镜像
  `drawing.py:require_draw_access`）；HTML 页面路由无权限 302 回首页，API 403。
- 一张地图 = `map_documents` 一行，`preset`(JSONB) = { style:{…}, annotations:{points,links} }。
  `preset` 实时防抖 PATCH 原地更新。
- 版本历史（做法2）：`map_documents.preset` 始终最新；`map_preset_versions` 只存
  「检查点」快照（打开文档补差异版 / 周期 / 手动），留最近 3 版，可回滚。
- **不落盘图片、不调第三方、不调 AI。** 瓦片/DEM/矢量全部由浏览器端直连
  （见 settings.MAP_TILE_CONFIG，经 GET /map/tile-config 下发）。
"""
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from settings import logger, MAP_TILE_CONFIG
from account import get_current_user
from backend.db import (
    create_map_document,
    get_map_documents,
    get_map_document,
    map_document_owned_by,
    update_map_document,
    delete_map_document,
    set_map_thumb,
    list_map_versions,
    get_map_version,
    snapshot_map_version,
    latest_map_version_preset,
)

map_router = APIRouter()
_BASE_DIR = Path(__file__).resolve().parent
_templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

_PAGE_ENDPOINTS = {"map_page", "map_document_page"}


async def require_map_access(request: Request, user: dict = Depends(get_current_user)) -> dict:
    if user["is_admin"]:
        return user
    if not user["can_map"]:
        endpoint_name = getattr(request.scope.get("endpoint"), "__name__", "")
        if endpoint_name in _PAGE_ENDPOINTS and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(url="/", status_code=302)
        raise HTTPException(status_code=403, detail="无地图权限")
    return user


def _serialize_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _serialize_row(row: dict) -> dict:
    return {k: _serialize_value(v) for k, v in row.items()}


async def _ensure_map_owner(map_id: str, user_id: int):
    if not await map_document_owned_by(map_id, user_id):
        raise HTTPException(status_code=403, detail="无权访问该地图")


# ── 默认 preset（新建地图时用，antique parchment v3；与 MapStage schema 对齐）──
DEFAULT_PRESET = {
    "version": 3,
    "style": {
        "view": "map",
        "camera": {"center": [104.0, 35.5], "zoom": 4.2, "pitch": 0, "bearing": 0},
        "basemap": {"satellite": True, "relief": False, "isolate": False, "isolateRegion": "china"},
        "mapstage": {
            "backgroundColor": "#c8c2b4",
            "terrainExaggeration": 1,
            "satellite": {"opacity": 0.7, "saturation": -0.08, "contrast": 0.02,
                          "brightnessMin": 0.08, "brightnessMax": 0.96, "hueRotate": 12},
            "hillshade": {"exaggeration": 0.42, "illuminationDirection": 315,
                          "shadowColor": "#2c2824", "highlightColor": "#f0ece4", "accentColor": "#6a6458"},
            "water": {"lakeFill": "rgba(50,118,138,0.9)", "lakeOutline": "rgba(90,110,105,0.8)",
                      "riverLevel3": "rgba(48,112,128,0.55)", "riverLevel2": "rgba(45,110,125,0.88)",
                      "riverLevel1": "rgba(42,105,120,0.95)", "highlightRiver": "rgba(38,100,118,1)",
                      "riverL3Width": 1.2, "riverL2Width": 2, "riverL1Width": 1.8, "highlightWidth": 2.4},
        },
        "css": {"sepia": 0.08, "saturate": 0.98, "contrast": 1.03, "brightness": 1.0, "hueRotate": -1,
                "warmTintAlpha": 0.04, "warmTintColor": "#9a8868", "vignetteStrength": 0.1, "enabled": True},
        "admin": {"enabled": False, "boundary": True, "place": True, "road": False},
        "ui": {"showWater": True, "showHighlight": True},
    },
    "annotations": {"points": [], "links": [], "groups": [], "order": []},
}


class CreateMapRequest(BaseModel):
    name: str = "未命名地图"


class UpdateMapRequest(BaseModel):
    name: Optional[str] = None
    preset: Optional[dict] = None


class SnapshotRequest(BaseModel):
    preset: dict
    note: str = "manual"


class ThumbRequest(BaseModel):
    data_url: str


# ──────────────────────────────────────────────────────────────────────────────
@map_router.get("/", response_class=HTMLResponse)
async def map_page(request: Request, user: dict = Depends(require_map_access)):
    if isinstance(user, RedirectResponse):
        return user
    docs = await get_map_documents(user["id"])
    if docs:
        return RedirectResponse(url=f"/map/{docs[0]['id']}")
    return _templates.TemplateResponse("map.html", {
        "request": request, "map_id": None, "user": user["username"],
        "can_write": bool(user["can_write"] or user["is_admin"]),
        "can_draw": bool(user["can_draw"] or user["is_admin"]),
        "can_map": bool(user["can_map"] or user["is_admin"]),
    })


@map_router.get("/tile-config")
async def tile_config(user: dict = Depends(require_map_access)):
    """把瓦片端点 + 署名下发给前端。唯一的配置类接口，不碰第三方。"""
    return MAP_TILE_CONFIG


_GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim 使用政策要求带上能标识本应用的 User-Agent（浏览器 fetch 设不了），
# 所以地名搜索走服务端代理这一个接口（其余瓦片仍是浏览器直连）。低频内部工具用途。
_GEOCODE_UA = "TSAI-map-module/1.0 (self-hosted internal tool)"


@map_router.get("/geocode")
async def geocode(q: str = Query(..., min_length=1), user: dict = Depends(require_map_access)):
    """地名搜索（OpenStreetMap Nominatim 代理）。返回候选点列表，前端让用户选一个。"""
    q = q.strip()
    if len(q) < 2:
        return {"results": []}
    try:
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": _GEOCODE_UA}) as hc:
            resp = await hc.get(_GEOCODE_URL, params={
                "q": q, "format": "jsonv2", "limit": 8, "addressdetails": 1,
                "accept-language": "zh-CN,zh,en",
            })
    except httpx.HTTPError as e:
        logger.warning("geocode 请求失败: %s", e)
        raise HTTPException(status_code=502, detail="地名搜索服务暂时不可用，请稍后重试或改用经纬度/地图点击")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"地名搜索服务返回 {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="地名搜索返回内容异常")
    results = []
    for it in data:
        try:
            lat, lng = float(it["lat"]), float(it["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        full = it.get("display_name", "") or ""
        results.append({
            "name": full,
            "short": it.get("name") or (full.split(",")[0].strip() if full else q),
            "lat": lat, "lng": lng,
            "type": it.get("type") or "",
            "category": it.get("category") or it.get("class") or "",
        })
    return {"results": results}


@map_router.post("/documents")
async def create_document(payload: CreateMapRequest, user: dict = Depends(require_map_access)):
    name = (payload.name or "未命名地图").strip() or "未命名地图"
    map_id = await create_map_document(user["id"], name, DEFAULT_PRESET)
    return {"id": map_id, "name": name}


@map_router.get("/documents")
async def list_documents(user: dict = Depends(require_map_access)):
    docs = await get_map_documents(user["id"])
    out = []
    for d in docs:
        row = _serialize_row(d)
        row["thumb_url"] = ("/" + d["thumb_path"]) if d.get("thumb_path") else None
        out.append(row)
    return out


@map_router.get("/documents/{map_id}")
async def get_document(map_id: str, user: dict = Depends(require_map_access)):
    doc = await get_map_document(map_id, user["id"])
    if not doc:
        raise HTTPException(status_code=404, detail="地图不存在")
    row = _serialize_row(doc)
    row.pop("user_id", None)
    row["thumb_url"] = ("/" + doc["thumb_path"]) if doc.get("thumb_path") else None
    if not row.get("preset"):
        row["preset"] = DEFAULT_PRESET
    return row


@map_router.patch("/documents/{map_id}")
async def patch_document(map_id: str, payload: UpdateMapRequest, user: dict = Depends(require_map_access)):
    await _ensure_map_owner(map_id, user["id"])
    ok = await update_map_document(map_id, user["id"], name=payload.name, preset=payload.preset)
    return {"success": ok}


@map_router.delete("/documents/{map_id}")
async def remove_document(map_id: str, user: dict = Depends(require_map_access)):
    deleted = await delete_map_document(map_id, user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="地图不存在")
    thumb = deleted.get("thumb_path")
    if thumb:
        try:
            p = _BASE_DIR / thumb
            if p.is_file():
                p.unlink()
        except Exception as e:  # noqa: BLE001
            logger.warning("删除地图缩略图失败 %s: %s", thumb, e)
    return {"success": True}


# ── 版本历史 ────────────────────────────────────────────────────────────────
@map_router.get("/documents/{map_id}/versions")
async def list_versions(map_id: str, user: dict = Depends(require_map_access)):
    await _ensure_map_owner(map_id, user["id"])
    return [_serialize_row(v) for v in await list_map_versions(map_id)]


@map_router.post("/documents/{map_id}/versions")
async def create_version(map_id: str, payload: SnapshotRequest, user: dict = Depends(require_map_access)):
    await _ensure_map_owner(map_id, user["id"])
    note = payload.note if payload.note in ("checkpoint", "manual", "open-diff") else "manual"
    version = await snapshot_map_version(map_id, payload.preset, note=note)
    return {"version": version, "note": note}


@map_router.post("/documents/{map_id}/versions/{version}/restore")
async def restore_version(map_id: str, version: int, user: dict = Depends(require_map_access)):
    await _ensure_map_owner(map_id, user["id"])
    v = await get_map_version(map_id, version)
    if not v:
        raise HTTPException(status_code=404, detail="版本不存在")
    await update_map_document(map_id, user["id"], preset=v["preset"])
    return {"success": True, "preset": v["preset"]}


# ── 可选：侧栏缩略图（前端 canvas.toDataURL()）─────────────────────────────
@map_router.post("/documents/{map_id}/thumb")
async def save_thumb(map_id: str, payload: ThumbRequest, user: dict = Depends(require_map_access)):
    await _ensure_map_owner(map_id, user["id"])
    import base64
    prefix = "data:image/png;base64,"
    if not payload.data_url.startswith(prefix):
        raise HTTPException(status_code=400, detail="仅支持 PNG data URL")
    try:
        raw = base64.b64decode(payload.data_url[len(prefix):])
    except Exception:
        raise HTTPException(status_code=400, detail="缩略图数据无效")
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="缩略图过大")
    save_dir = _BASE_DIR / "static" / "maps" / user["username"]
    save_dir.mkdir(parents=True, exist_ok=True)
    rel = str(Path("static") / "maps" / user["username"] / f"{map_id}.png")
    (save_dir / f"{map_id}.png").write_bytes(raw)
    await set_map_thumb(map_id, user["id"], rel)
    return {"thumb_url": "/" + rel}


# 必须放在文件最后：/{map_id} 会匹配任意单段路径，声明在前会抢先吃掉
# /documents、/tile-config 等固定路径（FastAPI/Starlette 按注册顺序匹配）。
@map_router.get("/{map_id}", response_class=HTMLResponse)
async def map_document_page(map_id: str, request: Request, user: dict = Depends(require_map_access)):
    if isinstance(user, RedirectResponse):
        return user
    if not await map_document_owned_by(map_id, user["id"]):
        return RedirectResponse(url="/map/")
    return _templates.TemplateResponse("map.html", {
        "request": request, "map_id": map_id, "user": user["username"],
        "can_write": bool(user["can_write"] or user["is_admin"]),
        "can_draw": bool(user["can_draw"] or user["is_admin"]),
        "can_map": bool(user["can_map"] or user["is_admin"]),
    })
