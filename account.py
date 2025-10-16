from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
from databases import Database
from typing import Optional
import uuid

from backend.db import database
from settings import settings

router = APIRouter()
templates = Jinja2Templates(directory="templates")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET_KEY = settings.secret_key

# ---------------- 工具函数 ----------------
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(hours=12))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm="HS256")

async def get_user(username: str):
    query = "SELECT * FROM users WHERE username = :u"
    return await database.fetch_one(query, values={"u": username})

async def verify_invite(code: str) -> bool:
    query = "SELECT * FROM invite_codes WHERE code = :c AND used_by IS NULL"
    return await database.fetch_one(query, values={"c": code}) is not None

# ---------------- 登录页 ----------------
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("account/login.html", {"request": request})

# ---------------- 邀请注册页 ----------------
@router.get("/invite", response_class=HTMLResponse)
async def invite_page(request: Request):
    return templates.TemplateResponse("account/register.html", {"request": request})

# ---------------- 注册（需要邀请码） ----------------
@router.post("/register")
async def register(username: str = Form(...), password: str = Form(...), invite_code: str = Form(...)):
    if not await verify_invite(invite_code):
        raise HTTPException(status_code=400, detail="无效或已使用的邀请码")

    hashed = pwd_context.hash(password)
    try:
        await database.execute(
            "INSERT INTO users (username, password_hash) VALUES (:u,:p)",
            values={"u": username, "p": hashed}
        )
        await database.execute(
            "UPDATE invite_codes SET used_by=:u, used_at=NOW() WHERE code=:c",
            values={"u": username, "c": invite_code}
        )
        token = create_access_token({"sub": username})
        resp = JSONResponse({"msg": "ok"})
        resp.set_cookie(
            key="access_token",
            value=token,
            httponly=True,   # JS 无法读取，防止 XSS
            secure=True,     # 生产环境必须 https
            samesite="lax"   # 或 'strict'
        )
        return resp
    except Exception:
        raise HTTPException(status_code=400, detail="注册失败，用户名可能已存在")

# ---------------- 登录获取 JWT ----------------
@router.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = await get_user(form_data.username)
    if not user or not pwd_context.verify(form_data.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = create_access_token({"sub": user["username"]})
    resp = JSONResponse({"msg": "ok"})
    resp.set_cookie(
        key="access_token",
        value=token,
        httponly=True,   # JS 无法读取，防止 XSS
        secure=True,     # 生产环境必须 https
        samesite="lax"   # 或 'strict'
    )
    return resp

# ---------------- 生成邀请码（未启用） ----------------
# @router.post("/generate_invite")
async def generate_invite():
    code = str(uuid.uuid4())
    await database.execute(
        "INSERT INTO invite_codes (code) VALUES (:c)",
        values={"c": code}
    )
    return {"invite_code": code}

# ---------------- 依赖：验证当前用户 ----------------
async def get_current_user(request: Request = None):
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="无效令牌")
        # return RedirectResponse("/account/login")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="无效令牌")
            # return RedirectResponse("/account/login")
    except JWTError:
        raise HTTPException(status_code=401, detail="无效令牌")
        # return RedirectResponse("/account/login")
    user = await get_user(username)
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
        # return RedirectResponse("/account/login")
    return user
