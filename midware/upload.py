from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi import UploadFile, File, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
import pdfplumber
import uuid
import shutil
from docx import Document
from settings import settings, client, logger
from account import get_current_user
from backend.db import database, init_db, save_message, get_context, add_knowledge, session_exists, save_file
from backend.rag import get_embedding, query_rag
from midware.tools import extract_text_chunks_path

router = APIRouter()

@router.post("/")
async def upload_file(background_tasks: BackgroundTasks, session_id: str = Form(...), file: UploadFile = File(...), user = Depends(get_current_user)):
    if not await session_exists(session_id):
        raise HTTPException(status_code=403, detail="会话不存在")

    upload_dir: Path = settings.base_dir / "static" / "loads" / user["username"] / session_id
    upload_dir.mkdir(parents=True, exist_ok=True)  # 若目录不存在则创建
    file_path: Path = upload_dir / file.filename
    if file_path.exists():
        return JSONResponse({"status": "success", "message": f"{file.filename} 已存在，无需重复上传"})
    with file_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    relative_file_path: Path = Path("static") / "loads" / user["username"] / session_id / file.filename
    await save_file(session_id, file.filename, str(relative_file_path))
    background_tasks.add_task(process_file_and_insert, file_path, session_id)
    return JSONResponse({"status": "success", "message": f"{file.filename} 上传成功，后台解析约需半小时，请稍后使用", "filename": file.filename, "filepath": str(relative_file_path)})


async def process_file_and_insert(file_path: Path, session_id: str):
    try:
        logger.info("开始处理文件: %s (session_id=%s)", file_path.name, session_id)
        chunks = await extract_text_chunks_path(file_path)
        for chunk in chunks:
            embedding = await get_embedding(client, chunk)
            await add_knowledge(chunk, embedding, session_id=session_id)
        logger.info("文件处理完成: %s", file_path.name)
    except Exception as e:
        logger.exception("处理文件 %s 出错: %s", file_path.name, e)
