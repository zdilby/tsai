import asyncio
import aiohttp
import os
import io
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


# 文本切分
def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 100) -> List[str]:
    """
    将长文本切分为若干 chunk，以便写入向量数据库。
    chunk_size: 每片最大字符数
    overlap: 前后片的重叠字符数，避免语义割裂
    """
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


# 从字节数据中解析文本
def parse_text_from_bytes(data: bytes, suffix: str, chunk_size: int = 1000) -> str:
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

    # 分片返回
    return chunk_text(text, chunk_size=chunk_size)

# 读取上传文件并返回分片后的文本列表
async def extract_text_chunks(file: UploadFile) -> List[str]:
    suffix = os.path.splitext(file.filename)[1].lower()
    data = await file.read()
    return parse_text_from_bytes(data, suffix)


# 读取在地文件并返回分片后的文本列表
async def extract_text_chunks_path(path: Path) -> List[str]:
    suffix = path.suffix.lower()
    data = path.read_bytes()
    return parse_text_from_bytes(data, suffix)
