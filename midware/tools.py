import asyncio
import aiohttp
import os
import io
import re
from pathlib import Path
from fastapi import UploadFile, HTTPException
from docx import Document
import pdfplumber
import docx2txt
from settings import settings
from typing import List


# 使用 Google Search 获取最新网络信息
async def fetch_from_web(query: str):
    search_url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": settings.google_api_key,
        "cx": settings.google_cx,
        "q": query,
        "num": 5  # 获取前5个结果
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(search_url, params=params) as response:
            if response.status == 200:
                data = await response.json()
                results = []
                for item in data.get("items", []):
                    title = item.get("title")
                    snippet = item.get("snippet")
                    link = item.get("link")
                    results.append(f"Title: {title}\nSnippet: {snippet}\nLink: {link}")
                return "\n\n".join(results)
            else:
                return ""


# ── 文档解析 ──────────────────────────────────────────────────

def _parse_document_sync(path: Path) -> str:
    """同步读取并解析文档，在线程池中执行以避免阻塞事件循环。"""
    data = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return data.decode("utf-8", errors="ignore")
    elif suffix == ".docx":
        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)
    elif suffix == ".doc":
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".doc") as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        text = docx2txt.process(tmp_path)
        os.remove(tmp_path)
        return text
    elif suffix == ".pdf":
        pages = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text() or "")
        return "\n".join(pages)
    else:
        raise HTTPException(status_code=400, detail="暂不支持该文件格式")


async def parse_document(path: Path) -> str:
    """异步包装器：在线程池中执行同步文档解析。"""
    return await asyncio.to_thread(_parse_document_sync, path)


# ── 段落感知分块 ──────────────────────────────────────────────

def split_into_paragraphs(text: str) -> List[str]:
    """按双换行拆段，兼容中英文。"""
    parts = re.split(r'\n\s*\n', text)
    return [p.strip() for p in parts if p.strip()]


def group_paragraphs(paragraphs: List[str], max_size: int = 800) -> List[str]:
    """将短段落合并为不超过 max_size 字符的块；超大段落直接单独成块。"""
    chunks, current, current_len = [], [], 0
    for p in paragraphs:
        if current_len + len(p) > max_size and current:
            chunks.append("\n\n".join(current))
            current, current_len = [], 0
        current.append(p)
        current_len += len(p)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


# ── Gemini 上下文增强 ─────────────────────────────────────────

async def enrich_chunks_with_context(client, doc_text: str, filename: str, chunks: List[str]) -> List[str]:
    """
    用 1 次 Gemini 调用生成文档摘要，然后为每个 chunk 拼接上下文头。
    enriched chunk = [来源+摘要+位置头] + 原始 chunk 文本
    """
    summary_prompt = (
        f"请用1-2句话概括以下文档的主要内容和主题，输出简洁精准的描述：\n\n{doc_text[:3000]}"
    )
    resp = await client.aio.models.generate_content(
        model=settings.generation_model,
        contents=summary_prompt
    )
    doc_summary = resp.text.strip()

    enriched = []
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        header = (
            f"[来源文件：{filename}。文档摘要：{doc_summary}。"
            f"位置：第{i + 1}段，共{total}段。]\n\n"
        )
        enriched.append(header + chunk)
    return enriched


# ── 旧版接口（保留兼容性）────────────────────────────────────

def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 100) -> List[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


def parse_text_from_bytes(data: bytes, suffix: str, chunk_size: int = 1000) -> List[str]:
    suffix = suffix.lower()
    if suffix == ".txt":
        text = data.decode("utf-8", errors="ignore")
    elif suffix == ".docx":
        doc = Document(io.BytesIO(data))
        text = "\n".join(p.text for p in doc.paragraphs)
    elif suffix == ".doc":
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".doc") as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        text = docx2txt.process(tmp_path)
        os.remove(tmp_path)
    elif suffix == ".pdf":
        pages = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text() or "")
        text = "\n".join(pages)
    else:
        raise HTTPException(status_code=400, detail="暂不支持该文件格式")
    return chunk_text(text, chunk_size=chunk_size)


async def extract_text_chunks(file: UploadFile) -> List[str]:
    suffix = os.path.splitext(file.filename)[1].lower()
    data = await file.read()
    return parse_text_from_bytes(data, suffix)


async def extract_text_chunks_path(path: Path) -> List[str]:
    suffix = path.suffix.lower()
    data = path.read_bytes()
    return parse_text_from_bytes(data, suffix)
