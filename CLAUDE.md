# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TSAI started as an async chat application built on **FastAPI** + **Google Gemini** + **PostgreSQL with pgvector**, and has since grown into a multi-module platform. The original core: each chat session has its own RAG (Retrieval-Augmented Generation) knowledge base. On every user message, the system fetches web results via Google Custom Search, embeds both the query and fetched content with Gemini embeddings, stores results in pgvector, runs a similarity search against the session's knowledge base, and sends the assembled prompt to Gemini (with Google Search grounding enabled). Depending on how much text a session's knowledge base holds and how the query reads, `/chat` actually routes to one of three paths — full-context, RAG, or an Agent ReAct tool-use loop — see ARCHITECTURE.md 五.1/5.1.1 for the routing heuristics.

Beyond the core chat, the app now has three parallel feature modules gated by per-user permission flags (`can_write`/`can_draw`/`can_map`, granted in `/admin/users`):

- **Writing** (`writing.py` + `templates/writing.html`) — AI-assisted long-form writing: full-document or TOC-driven sectional generation, style distillation, a two-agent quality evaluation pipeline. The trickiest part is keeping TOC / outline / per-section content in sync — see ARCHITECTURE.md 十一.4.1 before touching any of that.
- **Drawing** (`drawing.py` + `backend/image_gen.py`) — image generation via a third-party OpenAI-compatible relay, with iterative edit history chains and optional third-party "skill package" prompt compilation.
- **Map** (`map.py` + `templates/map.html`) — a MapLibre-based map viewer/editor with a custom point/line/label annotation system. Zero LLM calls; all third-party tiles are fetched client-side except geocoding, which is proxied server-side.

There's also a self-tuning subsystem (Phase 3: `backend/bot.py`/`agent_b.py`/`agent_c.py` + Celery) that periodically runs test queries, analyzes failures, and auto-adjusts prompts with rollback — and a separate, unrelated dev tool at `agent_system/` (not part of the running app; a plan→act→observe→reflect coding agent for modifying this repo itself, gitignored).

**`ARCHITECTURE.md` is the authoritative, actively-maintained reference for all of the above** (routes, DB schema, request flows, module internals, known gotchas with the reasoning behind each fix) — read it before making non-trivial changes, and update the relevant section after any code change that affects behavior it describes. `PRODUCTION.md` has the pre-deploy checklist (gunicorn/Nginx timeouts, background-task-survives-restart caveats, etc.) — re-read it before any production push.

## Running the Application

```bash
# Development
uvicorn main:app --reload

# Production (must run from project root)
gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```

The port shown above (`8000`) is just an example — bind to whatever port is free/expected on the host (e.g. append `--port 8080` to the dev command, or change the `--bind` port for gunicorn). Nothing in the app logic is tied to a specific port; on this machine local testing commonly uses `8080` or `8000` interchangeably.

## Database Setup

The core schema (users/sessions/messages/knowledge_base/upload_files) is defined in `backend/db.py:init_db()`. It is **not** called automatically on startup (the call is commented out in `main.py`). Run it manually once on a fresh database:

```python
# From a Python shell in the project root:
import asyncio
from backend.db import init_db
asyncio.run(init_db())
```

Every module added since then has its own idempotent (`IF NOT EXISTS`) init function that **does** run automatically on every startup — `init_phase3_tables`, `init_writing_tables`, `init_drawing_tables`, `init_map_tables` (see `main.py`'s `startup` event). On an existing DB missing newer columns, run `python -m scripts.migrate`.

Requires PostgreSQL with the `pgvector` extension. The `DATABASE_URL` must use the `postgresql+asyncpg://` scheme.

## Generating Invite Codes

Registration requires an invite code. Generate one with:

```bash
python -m scripts.generate_invite
```

## Required Environment Variables (`.env`)

Core chat/RAG requires:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (`postgresql+asyncpg://...`) |
| `GEMINI_API_KEY` | Google Gemini API key |
| `GOOGLE_SEARCH_KEY` | Google Custom Search API key (note: env var name differs from the `google_api_key` Settings field) |
| `GOOGLE_CX` | Google Custom Search Engine ID |
| `SECRET_KEY` | JWT signing secret |
| `GEMINI_TEXT_MODEL` | Defaults to `gemini-2.5-flash` |
| `GEMINI_EMBED_MODEL` | Defaults to `text-embedding-004` — **verify this model is still live** with `python -m scripts.list_models` after a long gap; Google has retired at least one experimental embedding model this project depended on before (see git history around the `gemini-embedding-exp-03-07` → `gemini-embedding-001` migration) |
| `EMBEDDING_DIM` | Defaults to `768` |

Writing module's Markdown formatting and the drawing module's image generation both need `CODEX_API_KEY`/`CODEX_BASE_URL` (a third-party OpenAI-compatible relay); the map module needs nothing (all third-party tiles are client-side, unconfigured). The full variable list — RAG tuning knobs, Phase 3 self-tuning (`AGENT_B_*`/`AGENT_C_*`/`BOT_*`/`REDIS_URL`), Codex timeouts — is in ARCHITECTURE.md 九; don't duplicate it here, it drifts.

## Architecture

### Request Flow for `/chat`

Simplified — the real router picks one of three paths per request based on how much text the session's knowledge base holds and how the query reads (full-context / RAG / Agent tool-use loop); see ARCHITECTURE.md 五.1 for the actual branching. The RAG path:

1. Save user message → `backend/db.py:save_message()`
2. Fetch recent history → `backend/db.py:get_context()`
3. Embed query → `backend/rag.py:get_embedding()` (wraps Gemini embed in `asyncio.to_thread`)
4. RAG similarity search → `backend/rag.py:query_rag()` (pgvector `<->` cosine distance, filtered by `session_id`)
5. Google Custom Search → `midware/tools.py:fetch_from_web()`
6. Store web results into knowledge base → `backend/db.py:add_knowledge()`
7. Build prompt and call Gemini with Google Search grounding tool → return response

### Key Files

Core chat/RAG:
- `main.py` — Core chat routes + startup/shutdown (connects/disconnects the async DB pool, registers the pgvector codec, runs every module's idempotent table-init, and — as of the "resource stuck at processing forever" incident — sweeps stale `upload_files` rows left `processing` by a killed worker)
- `settings.py` — Pydantic `Settings` model loaded from `.env`; creates the global `client` (Gemini) and `logger`
- `account.py` — JWT auth via HTTP-only cookies; invite-code–gated registration
- `admin.py` — Admin routes: user management, per-user module permissions, Phase 3 perf dashboard
- `backend/db.py` — All raw SQL via the `databases` library; direct asyncpg pool access for pgvector operations
- `backend/rag.py` — Embedding and vector similarity query. `get_embeddings_batch()` retries both rate-limit (429) and transient network errors, with a per-call timeout — the `genai.Client` itself has no HTTP timeout configured, so this is the only thing preventing a stalled request from hanging a file's processing forever
- `midware/tools.py` — Google Custom Search fetch; file text extraction and chunking (PDF, DOCX, DOC, EPUB, TXT)
- `midware/upload.py` — File upload endpoint; stores file to `static/loads/<username>/<session_id>/`, runs `process_file_and_insert` as a FastAPI `BackgroundTask`. **Caveat**: this background task is tied to the worker process — if the worker restarts mid-processing, the task just vanishes and the DB row is stuck `processing` forever unless something sweeps it (see `main.py` startup and `scripts/reset_stuck_processing.py` / `scripts/check_file_status.py`). The same in-process-`BackgroundTasks` caveat applies to the drawing module's image generation.

Feature modules (each is a substantial subsystem — see ARCHITECTURE.md for routes/schema/internals, don't try to hold it all in this file):
- `writing.py`, `drawing.py` + `backend/image_gen.py`, `map.py` — the three permission-gated modules described above
- `agent_system/` — unrelated dev tool (gitignored), not part of the running app

- `scripts/` — ops CLI utilities (invite codes, admin promotion, DB migration, stuck-file diagnosis/reset). Run with `python -m scripts.<name>` from the project root, using the **same venv the running gunicorn service uses** — production has been seen with a system Python too old for this codebase's `str | None` type hints, so a bare `python` on the box may not work.

### Sessions

Three states: **null** (no `name`, created automatically on `GET /`), **named** (user-created via `POST /new_session`), and **writing** (`is_writing_session = TRUE`, auto-created by the writing module, hidden from the session list). `session_exists()` checks for `name IS NOT NULL` (true for both named and writing sessions). RAG/upload only work for non-null sessions.

### pgvector Access Pattern

The `databases` library does not support pgvector natively. Raw asyncpg connection is acquired from `database._backend._pool` and `register_vector(conn)` is called before every vector read/write.

## Browser Verification (claude-in-chrome)

When using the `claude-in-chrome` tool to visually verify changes in this project, cap retry attempts at 3. If 3 consecutive attempts fail (extension not connected, dialog-blocked renderer, safety-classifier timeouts, etc.), stop calling the tool and fall back to other verification methods (curl, direct DB checks, reading source) rather than continuing to retry.
