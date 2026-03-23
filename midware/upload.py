import asyncio
from fastapi import APIRouter, Depends, HTTPException, Form
from fastapi import UploadFile, File, BackgroundTasks
from fastapi.responses import JSONResponse
from pathlib import Path
from settings import settings, client, embed_client, logger
from account import get_current_user
from backend.db import session_exists, save_file, add_knowledge_batch, update_file_status, get_file_statuses
from backend.rag import get_embeddings_batch
from midware.tools import (
    parse_document, split_into_paragraphs, group_paragraphs,
    enrich_chunks_with_context, pdf_to_markdown, epub_to_markdown, split_markdown_chunks,
)

router = APIRouter()


@router.post("/")
async def upload_file(
    background_tasks: BackgroundTasks,
    session_id: str = Form(...),
    file: UploadFile = File(...),
    user=Depends(get_current_user)
):
    if not await session_exists(session_id):
        raise HTTPException(status_code=403, detail="会话不存在")

    upload_dir: Path = settings.base_dir / "static" / "loads" / user["username"] / session_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_path: Path = upload_dir / file.filename
    if file_path.exists():
        return JSONResponse({"status": "success", "message": f"{file.filename} 已存在，无需重复上传"})

    # 异步读取并写入文件，避免阻塞事件循环
    content = await file.read()
    await asyncio.to_thread(file_path.write_bytes, content)

    relative_file_path: Path = Path("static") / "loads" / user["username"] / session_id / file.filename
    await save_file(session_id, file.filename, str(relative_file_path))
    background_tasks.add_task(process_file_and_insert, file_path, session_id)
    return JSONResponse({
        "status": "success",
        "message": f"{file.filename} 上传成功，后台解析中，请稍后查看状态",
        "filename": file.filename,
        "filepath": str(relative_file_path)
    })


async def process_file_and_insert(file_path: Path, session_id: str):
    try:
        await update_file_status(session_id, file_path.name, 'processing')

        suffix = file_path.suffix.lower()

        if suffix == '.pdf':
            # ── PDF：OCR 智能解析 → Markdown ─────────────────────
            logger.info("PDF OCR 解析开始: %s", file_path.name)
            md_text = await pdf_to_markdown(file_path)

            # 将生成的 Markdown 保存到同目录（filename.md）
            md_path = file_path.with_suffix('.md')
            await asyncio.to_thread(md_path.write_text, md_text, 'utf-8')
            logger.info("Markdown 已保存: %s", md_path.name)

            # 按标题边界拆分为语义 chunks
            raw_chunks = split_markdown_chunks(md_text)
            text = md_text  # 用于 Gemini 摘要生成
        elif suffix == '.epub':
            # ── EPUB：章节结构解析 → Markdown ────────────────────
            logger.info("EPUB 解析开始: %s", file_path.name)
            md_text = await epub_to_markdown(file_path)

            md_path = file_path.with_suffix('.md')
            await asyncio.to_thread(md_path.write_text, md_text, 'utf-8')
            logger.info("Markdown 已保存: %s", md_path.name)

            raw_chunks = split_markdown_chunks(md_text)
            text = md_text
        else:
            # ── 其他格式：原有段落分块流程 ────────────────────────
            text = await parse_document(file_path)
            raw_chunks = group_paragraphs(split_into_paragraphs(text))

        await update_file_status(session_id, file_path.name, 'processing', total=len(raw_chunks))

        # Gemini 上下文增强（1次摘要调用）
        enriched_chunks = await enrich_chunks_with_context(client, text, file_path.name, raw_chunks)

        # 批量并发 embedding
        embeddings = await get_embeddings_batch(embed_client, enriched_chunks)

        # 批量写入（单连接，单次 register_vector，executemany）
        items = list(zip(enriched_chunks, raw_chunks, embeddings))
        await add_knowledge_batch(items, session_id, source_file=file_path.name)

        await update_file_status(session_id, file_path.name, 'done', processed=len(raw_chunks))
        logger.info("文件处理完成: %s (%d chunks)", file_path.name, len(raw_chunks))
    except Exception as e:
        await update_file_status(session_id, file_path.name, 'failed', error=str(e))
        logger.exception("处理文件 %s 出错: %s", file_path.name, e)


@router.get("/status/{session_id}")
async def get_upload_status(session_id: str, user=Depends(get_current_user)):
    return await get_file_statuses(session_id)


@router.post("/reprocess")
async def reprocess_file(
    background_tasks: BackgroundTasks,
    session_id: str = Form(...),
    filename: str = Form(...),
    user=Depends(get_current_user),
):
    from backend.db import database
    row = await database.fetch_one(
        "SELECT filepath, status FROM upload_files WHERE session_id = :sid AND filename = :fname",
        values={"sid": session_id, "fname": filename},
    )
    if not row:
        raise HTTPException(status_code=404, detail="文件记录不存在")
    if row["status"] == "processing":
        raise HTTPException(status_code=409, detail="文件正在处理中")

    file_path = settings.base_dir / row["filepath"]
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="磁盘文件不存在，请重新上传")

    # 清除该文件的旧向量并重置状态
    await database.execute(
        "DELETE FROM knowledge_base WHERE session_id = :sid AND source_file = :src",
        values={"sid": session_id, "src": filename},
    )
    await database.execute(
        """UPDATE upload_files
           SET status = 'pending', total_chunks = 0, processed_chunks = 0, error_msg = NULL
           WHERE session_id = :sid AND filename = :fname""",
        values={"sid": session_id, "fname": filename},
    )

    background_tasks.add_task(process_file_and_insert, file_path, session_id)
    return JSONResponse({"success": True})
