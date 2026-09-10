## **Project Overview**

**TSAI** is a multi-module async app built on FastAPI + Google Gemini + PostgreSQL/pgvector. It has grown from a single chat+RAG tool into four user-facing modules, switchable per session via `_module_switch.html`:

1. **Chat** — general chat with RAG (Retrieval-Augmented Generation): fetches web results via Google Custom Search, embeds query/content with Gemini embeddings, stores in pgvector, and grounds responses with similarity search + Google Search grounding.
2. **Writing** — sectioned long-form writing (TOC generation, per-section drafting/typesetting/confirmation, multi-agent style/quality evaluation pipeline).
3. **Drawing** — AI image generation via a third-party relay (`gpt-image-1` through `CODEX_BASE_URL`), with style presets and async/background generation + polling.
4. **Map** — map-based module (see `ARCHITECTURE.md` for details; design notes originally in `MAP-MODULE-DESIGN.md`).

It also includes user registration (invite-code gated), login, profile management, an admin panel, and file upload for session knowledge bases.

## **Features**

- **User Authentication**: Users can sign up (invite-code gated), log in, and manage their profiles.
- **Chat + RAG**: General questions answered by Gemini, grounded with Google Search and a per-session pgvector knowledge base.
- **Upload System**: Users can upload personal materials (PDF/DOCX/DOC/TXT) to enhance a session's knowledge base.
- **Writing Module**: TOC-driven sectioned writing with per-section generation, layout, and a multi-agent quality-check pipeline.
- **Drawing Module**: Prompt-driven AI image generation with style presets, run as background tasks.
- **Map Module**: See `ARCHITECTURE.md` (map section) for current scope.
- **Admin Panel**: User/invite management (`admin.py`, `templates/admin/`).
- **Chat History**: Conversations are stored in the database, and users can view past interactions.

## **Project Structure**

```bash
tsai/
│
├── backend/
│   ├── __init__.py
│   ├── db.py
│   ├── rag.py
│   ├── image_gen.py      # drawing module: third-party image-gen relay
│   ├── bot.py / agent_b.py / agent_c.py / agent_chat.py / tasks.py / celery_app.py
├── logs/
├── midware/
│   ├── __init__.py
│   ├── tools.py
│   ├── upload.py
├── static/
│   ├── css/
│   ├── images/
│   ├── js/
│   ├── loads/
│   ├── materialize/
├── templates
│   ├── account/
│   │   ├── login.html
│   │   ├── register.html
│   ├── admin/
│   ├── agent/
│   ├── _module_switch.html
│   ├── chat.html
│   ├── writing.html
│   ├── drawing.html
│   ├── map.html
├── account.py
├── admin.py
├── writing.py
├── drawing.py
├── map.py
├── main.py
├── settings.py
└── requirement.txt
```

See `ARCHITECTURE.md` for a detailed, section-by-section breakdown of every module. `CLAUDE.md` has dev/run instructions.

## **License**

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

### **Contact**

- **Author**: [zdilby](https://github.com/zdilby)
- **Project Link**: [TSAI](https://github.com/zdilby/tsai)

---
