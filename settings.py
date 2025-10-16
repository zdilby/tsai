from pydantic import BaseModel
from dotenv import load_dotenv
from pathlib import Path
from google import genai
from google.genai import types
import os
import logging

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "process.log"

# 设置日志格式
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
logging.basicConfig(
    level=logging.INFO,                  # 默认日志等级
    format=LOG_FORMAT,
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),  # 写入文件
        logging.StreamHandler()                           # 同时输出到控制台
    ]
)

class Settings(BaseModel):
    database_url: str = os.getenv("DATABASE_URL")
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY")
    google_api_key: str | None = os.getenv("GOOGLE_API_KEY")
    google_cx: str | None = os.getenv("GOOGLE_CX")
    generation_model: str = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
    embedding_model: str = os.getenv("GEMINI_EMBED_MODEL", "text-embedding-004")
    embedding_dim: int = int(os.getenv("EMBEDDING_DIM", "768"))
    top_k: int = int(os.getenv("TOP_K", "4"))
    max_history_turns: int = int(os.getenv("MAX_HISTORY_TURNS", "12"))
    secret_key: str = os.getenv("SECRET_KEY")
    base_dir: Path = BASE_DIR

settings = Settings()
GEMINI_API_KEY = settings.gemini_api_key
client = genai.Client(api_key=GEMINI_API_KEY)
logger = logging.getLogger("TSAI")
