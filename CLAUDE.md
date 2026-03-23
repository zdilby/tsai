# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TSAI is an async chat application built on **FastAPI** + **Google Gemini** + **PostgreSQL with pgvector**. Each chat session has its own RAG (Retrieval-Augmented Generation) knowledge base. On every user message, the system: fetches web results via Google Custom Search, embeds both the query and fetched content with Gemini embeddings, stores results in pgvector, runs a similarity search against the session's knowledge base, and sends the assembled prompt to Gemini (with Google Search grounding enabled).

## Running the Application

```bash
# Development
uvicorn main:app --reload

# Production (must run from project root)
gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```

## Database Setup

The DB schema is defined in `backend/db.py:init_db()`. It is **not** called automatically on startup (the call is commented out in `main.py`). Run it manually once:

```python
# From a Python shell in the project root:
import asyncio
from backend.db import init_db
asyncio.run(init_db())
```

Requires PostgreSQL with the `pgvector` extension. The `DATABASE_URL` must use the `postgresql+asyncpg://` scheme.

## Generating Invite Codes

Registration requires an invite code. Generate one with:

```bash
python -m scripts.generate_invite
```

## Required Environment Variables (`.env`)

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (`postgresql+asyncpg://...`) |
| `GEMINI_API_KEY` | Google Gemini API key |
| `GOOGLE_API_KEY` | Google Custom Search API key |
| `GOOGLE_CX` | Google Custom Search Engine ID |
| `SECRET_KEY` | JWT signing secret |
| `GEMINI_TEXT_MODEL` | Defaults to `gemini-2.5-flash` |
| `GEMINI_EMBED_MODEL` | Defaults to `text-embedding-004` |
| `EMBEDDING_DIM` | Defaults to `768` |

## Architecture

### Request Flow for `/chat`

1. Save user message → `backend/db.py:save_message()`
2. Fetch recent history → `backend/db.py:get_context()`
3. Embed query → `backend/rag.py:get_embedding()` (wraps Gemini embed in `asyncio.to_thread`)
4. RAG similarity search → `backend/rag.py:query_rag()` (pgvector `<->` cosine distance, filtered by `session_id`)
5. Google Custom Search → `midware/tools.py:fetch_from_web()`
6. Store web results into knowledge base → `backend/db.py:add_knowledge()`
7. Build prompt and call Gemini with Google Search grounding tool → return response

### Key Files

- `main.py` — All FastAPI routes; startup/shutdown connects/disconnects the async DB pool and registers the pgvector codec
- `settings.py` — Pydantic `Settings` model loaded from `.env`; creates the global `client` (Gemini) and `logger`
- `account.py` — JWT auth via HTTP-only cookies; invite-code–gated registration
- `backend/db.py` — All raw SQL via the `databases` library; direct asyncpg pool access for pgvector operations
- `backend/rag.py` — Embedding and vector similarity query
- `midware/tools.py` — Google Custom Search fetch; file text extraction and chunking (PDF, DOCX, DOC, TXT)
- `midware/upload.py` — File upload endpoint; stores file to `static/loads/<username>/<session_id>/`, runs `process_file_and_insert` as a FastAPI `BackgroundTask`
- `scripts/generate_invite.py` — CLI utility to insert an invite code into the DB

### Sessions

Sessions have two states: **null** (no `name`, created automatically on `GET /`) and **named** (user-created via `POST /new_session`). `session_exists()` checks for `name IS NOT NULL`. RAG/upload only work for named sessions.

### pgvector Access Pattern

The `databases` library does not support pgvector natively. Raw asyncpg connection is acquired from `database._backend._pool` and `register_vector(conn)` is called before every vector read/write.
