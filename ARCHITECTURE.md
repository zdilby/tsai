# TSAI 项目架构文档

> 本文档由 Claude Code 自动生成并维护，随代码变动同步更新。
> 最后更新：2026-04-28

---

## 一、项目概述

TSAI 是一个**多用户 AI 异步对话系统**，集成了 RAG（检索增强生成）、文档知识库、网络搜索增强等能力。每个对话 Session 拥有独立的知识库，用户可上传文档，系统自动分块、向量化并在对话中检索相关内容。

**技术栈一览：**

| 层次 | 技术 |
|---|---|
| 后端框架 | FastAPI |
| AI 模型 | Google Gemini（`gemini-2.5-flash` + `gemini-embedding-exp-03-07`） |
| 数据库 | PostgreSQL + pgvector（向量相似搜索） |
| 认证 | JWT（HTTP-only Cookie）+ bcrypt |
| 文档解析 | pdfplumber / pytesseract / ebooklib / python-docx |
| 前端 | Jinja2 模板 + Materialize CSS + jQuery + Marked.js |
| 网络搜索 | Google Custom Search API |

---

## 二、目录结构

```
tsai/
├── main.py               # 核心路由：聊天、Session 管理
├── account.py            # 认证路由：登录、注册、改密
├── admin.py              # 管理员路由：用户管理、邀请码
├── settings.py           # 配置加载（.env）、全局 logger
├── backend/
│   ├── db.py             # 全部 SQL 操作与数据库 Schema
│   └── rag.py            # 向量检索、Embedding 生成
├── midware/
│   ├── tools.py          # 文档解析、分块、网络搜索
│   └── upload.py         # 文件上传与后台处理
├── templates/            # Jinja2 HTML 模板
│   ├── chat.html         # 主聊天界面
│   ├── account/          # 登录/注册页
│   └── admin/            # 管理员后台页
├── static/               # CSS、JS、用户上传文件
│   └── loads/            # 用户上传文件：loads/{username}/{session_id}/
├── scripts/              # 运维脚本（建管理员、生成邀请码等）
└── logs/process.log      # 应用日志
```

---

## 三、API 路由一览

### 核心聊天路由（`main.py`）

| 方法 | 路径 | 功能 |
|---|---|---|
| `GET` | `/` | 主界面，自动初始化 null Session |
| `GET` | `/ping` | 健康检查 |
| `POST` | `/chat` | 核心对话接口（含 RAG + 网络搜索） |
| `POST` | `/new_session` | 创建命名 Session |
| `POST` | `/change_session` | 重命名 Session |
| `POST` | `/del_session` | 删除 Session |
| `GET` | `/sessions` | 获取用户全部命名 Session |
| `GET` | `/messages/{session_id}` | 获取 Session 历史消息 |
| `GET` | `/collections/{session_id}` | 获取 Session 上传文件列表 |
| `GET` | `/session_persona/{session_id}` | 获取 Session 角色人格 |
| `POST` | `/session_persona` | 设置 Session 角色人格（AI 处理，后台任务） |
| `POST` | `/save_to_rag` | 将对话摘要存入知识库 |

### 认证路由（`account.py`）

| 方法 | 路径 | 功能 |
|---|---|---|
| `GET` | `/account/login` | 登录页 |
| `GET` | `/account/invite` | 注册页 |
| `POST` | `/account/register` | 注册接口（需邀请码） |
| `POST` | `/account/token` | 登录接口（OAuth2 表单） |
| `POST` | `/account/logout` | 登出（清除 Cookie） |
| `POST` | `/account/change_password` | 修改密码 |

### 文件上传路由（`midware/upload.py`）

| 方法 | 路径 | 功能 |
|---|---|---|
| `POST` | `/upload/` | 上传文件（异步后台处理） |
| `GET` | `/upload/status/{session_id}` | 查询文件处理状态 |
| `POST` | `/upload/reprocess` | 重新处理失败文件 |

### 管理员路由（`admin.py`）

| 方法 | 路径 | 功能 |
|---|---|---|
| `GET` | `/admin/` | 管理员总览（用户 + 邀请码） |
| `GET` | `/admin/user/{id}` | 用户详情页 |
| `GET` | `/admin/session/{id}` | Session 详情页 |
| `POST` | `/admin/user/{id}/max_tokens` | 设置每日 Token 配额 |
| `POST` | `/admin/user/{id}/max_file_size` | 设置最大文件大小 |
| `POST` | `/admin/user/{id}/reset_password` | 强制重置密码 |
| `POST` | `/admin/invite/generate` | 生成邀请码 |

---

## 四、数据库 Schema

### 表结构关系

```
users              ← 用户账户（含配额）
  ↓ 1:N
sessions           ← 对话 Session（含人格）
  ↓ 1:N             ↓ 1:N             ↓ 1:N
messages           knowledge_base     upload_files
（消息 + token      （RAG 知识库       （文件上传状态
 统计 + 向量索引）    向量分块）          + 处理进度）

invite_codes       ← 邀请码（独立表）
```

### `users`

```sql
id              SERIAL PRIMARY KEY
username        TEXT UNIQUE NOT NULL
password_hash   TEXT NOT NULL
is_admin        BOOLEAN DEFAULT FALSE
max_daily_tokens  INTEGER DEFAULT 100000   -- 0 = 不限
max_file_size_mb  INTEGER DEFAULT 10       -- 0 = 不限
created_at      TIMESTAMP DEFAULT NOW()
```

### `invite_codes`

```sql
code        UUID PRIMARY KEY
used_by     TEXT              -- NULL = 未使用
created_at  TIMESTAMP DEFAULT NOW()
used_at     TIMESTAMP
```

### `sessions`

```sql
id                        UUID PRIMARY KEY
user_id                   INTEGER REFERENCES users(id) ON DELETE CASCADE
name                      TEXT          -- NULL = 未命名（null session）
persona                   TEXT          -- 已废弃
system_instruction_origin TEXT          -- 用户原始人格输入
system_instruction        TEXT          -- AI 处理后的系统指令
created_at                TIMESTAMP DEFAULT NOW()
```

索引：`idx_sessions_user_id` on `user_id`

### `messages`

```sql
id          SERIAL PRIMARY KEY
session_id  UUID
role        TEXT              -- 'user' | 'assistant'
content     TEXT
tokens_in   INTEGER DEFAULT 0
tokens_out  INTEGER DEFAULT 0
tokens_total INTEGER DEFAULT 0
embedding   vector(768)       -- 用于历史语义检索
created_at  TIMESTAMP DEFAULT NOW()
```

索引：
- `idx_messages_session_id` on `session_id`
- `idx_messages_embedding`（HNSW，余弦距离）

### `upload_files`

```sql
id                SERIAL PRIMARY KEY
session_id        UUID
filename          TEXT
filepath          TEXT    -- static/loads/{username}/{session_id}/{filename}
status            TEXT DEFAULT 'pending'   -- pending|processing|done|failed
total_chunks      INTEGER DEFAULT 0
processed_chunks  INTEGER DEFAULT 0
error_msg         TEXT
created_at        TIMESTAMP DEFAULT NOW()
```

### `knowledge_base`

```sql
id               SERIAL PRIMARY KEY
session_id       UUID REFERENCES sessions(id) ON DELETE CASCADE
content          TEXT    -- 加上下文头的富化文本（用于向量化）
original_content TEXT    -- 原始分块文本
source_file      TEXT    -- 文件名 或 "对话摘要"
chunk_index      INTEGER DEFAULT 0
embedding        vector(768)
```

索引：
- `idx_knowledge_base_session_id` on `session_id`
- `idx_knowledge_base_hnsw`（HNSW，cosine_ops）

### `prompt_versions`（Phase 3a）

版本化的 system prompt 存储，支持 Agent B 自动改 + 手动回滚。

```sql
id           SERIAL PRIMARY KEY
name         TEXT NOT NULL                    -- e.g. "agent_tool_rules"
content      TEXT NOT NULL
version      INTEGER NOT NULL
is_active    BOOLEAN DEFAULT FALSE             -- 同 name 下仅一行为 TRUE
created_at   TIMESTAMP DEFAULT NOW()
created_by   TEXT DEFAULT 'manual'             -- "manual" | "bootstrap" | "agent_b"
reason       TEXT
UNIQUE(name, version)
```

索引：`idx_prompt_versions_active`（partial，only `is_active = TRUE`）

### `agent_traces`（Phase 3a）

每次 `/chat` 完成后异步落盘的完整调用 trace。Agent B 据此分析。

```sql
id                  SERIAL PRIMARY KEY
session_id          UUID
user_id             INTEGER REFERENCES users(id)
message_id          INTEGER REFERENCES messages(id)
query               TEXT
route               TEXT                       -- "rag" | "agent" | "full_context" | "empty_kb"
tools_called        JSONB                      -- [{round, tool, args, result_preview}, ...]
iterations          INTEGER DEFAULT 1
citations           JSONB
tokens_in           INTEGER
tokens_out          INTEGER
duration_ms         INTEGER
prompt_version_id   INTEGER REFERENCES prompt_versions(id)
hallucination_rate  FLOAT                       -- NULL until verified by Agent B
analyzed_at         TIMESTAMP                   -- NULL = 尚未被 Agent B 分析
created_at          TIMESTAMP DEFAULT NOW()
```

索引：
- `idx_agent_traces_pending`（partial，`analyzed_at IS NULL`）—— Agent B 拉新数据用
- `idx_agent_traces_route` on `(route, created_at DESC)`

### `subsystem_status`（Phase 3a）

机器人 / Agent B / Agent C 的启停状态 + 心跳。

```sql
component       TEXT PRIMARY KEY                -- "bot" | "agent_b" | "agent_c"
enabled         BOOLEAN DEFAULT FALSE
last_heartbeat  TIMESTAMP
last_action     TEXT
status_msg      TEXT
updated_at      TIMESTAMP DEFAULT NOW()
```

---

## 五、核心处理流程

### 5.1 聊天请求（`POST /chat`）

```
1. 验证 JWT Cookie → 获取用户信息
2. 检查今日 Token 配额（超限返回 429）
3. 保存用户消息到 messages 表
4. 估算 session 总语料 token 数（启发式：len(text)/2.5）+ 路由决策：
   ├── < FULL_CONTEXT_THRESHOLD (默认 300_000) → 全量上下文路径（5a）
   ├── ≥ THRESHOLD + AGENT_CHAT_ENABLED + needs_agent(query) → Agent 路径（5c）
   └── 其余 → RAG 路径（5b，含空知识库）

   ┌─ 5a 全量上下文（小语料）：
   │    ├── get_all_session_chunks(session_id) 拉取全部 chunk
   │    ├── 按 (source_file, chunk_index) 排序，加文件头分组
   │    └── Prompt 段落："All uploaded documents (full content)"
   │
   ├─ 5b RAG（大语料 / 简单查询 / 空知识库）：
   │    ├── 并发：Gemini Query Embedding + Google CSE 网页抓取
   │    ├── 检测回忆触发词 → 语义检索历史消息（query_history）
   │    ├── pgvector 相似度检索 knowledge_base（< 0.40，最多 20 条）
   │    └── 动态 Top-K 选择（Margin + Gap 策略）
   │
   └─ 5c Agent ReAct 循环（大语料 + 复杂查询）：
        ├── 预抓 web_info，作为 Agent 免费上下文
        ├── 调用 run_agent_chat()，最多 AGENT_MAX_ITERATIONS=6 轮
        │    每轮：Gemini 决定调哪些 tool（search_kb / read_document /
        │           list_documents / web_search / search_history）
        │           → asyncio.gather 并行执行 → 喂回结果
        │    提前退出条件：search_kb 命中 distance<0.3 时 prompt 鼓励直接作答
        └── 累计 token + citations + agent_trace 一并返回

5. 拼装 Prompt（5a/5b 路径）：[最近 12 轮] + [历史相关] + [文档段] + [网络信息]
6. 调用 Gemini（附 Google Search grounding + 角色人格）
7. 保存 AI 回复 + Token 计数到 messages 表
8. 后台任务：计算回复 Embedding，写回 messages.embedding
```

> **路径选择日志**：每次 /chat 都会输出 `tokens≈N threshold=M → FULL_CONTEXT|AGENT|RAG|EMPTY_KB`，便于观察实际触发情况。

### 5.1.1 Agent 智能路由（`needs_agent` 启发式）

只有以下任一信号触发时才进 Agent，其余复用 RAG 路径以保延迟：

- **对比类**：包含"对比 / 区别 / 比较 / vs / 差异"
- **回忆类**：匹配"还记得 / 之前 / 上次 / 我们聊过 / 你说过"等
- **开放类**：包含"分析 / 总结 / 概括 / 评价 / 怎么看 / 为什么 / 原因"
- **列举类**：包含"有哪些 / 都有什么 / 列出"
- **多问句**：句中包含 ≥ 2 个问号

且 query 长度 ≥ 15 字符（短问题大概率单次 RAG 够）。

实测预期：60-70% 大语料 query 仍走 RAG 路径，延迟与 Phase 1 一致。

### 5.2 文件上传与 RAG 索引

```
1. POST /upload/ → 校验 Session 归属 + 文件大小配额
2. 保存至 static/loads/{username}/{session_id}/
3. 写入 upload_files（status=pending）→ 立即返回 202
4. 后台任务 process_file_and_insert()：
   ├── PDF:  pdfplumber 提取文本；若质量不足则 OCR（pytesseract，中英文）
   ├── EPUB: ebooklib 解析 HTML → Markdown
   └── TXT/DOCX/DOC: 直接读取
5. 文本分块（按 ## 标题或段落，最大 800 字/块）
6. 为每块添加上下文头："[来源：xxx.pdf。开头：...。位置：第N段/共M段]"
7. 批量 Embedding（每批 50 条，遇 429 指数退避重试：30s→60s→120s→...）
8. 批量插入 knowledge_base（含 pgvector 向量）
9. 更新 upload_files.status → done
```

### 5.3 认证流程

```
注册：邀请码验证 → bcrypt 哈希密码 → 写 users → 标记邀请码已用 → 签发 JWT
登录：查 users → bcrypt 验证 → 签发 JWT → 写 HttpOnly Cookie（12 小时有效）
请求：读 Cookie → 解析 JWT → 查 users → 注入 user 依赖（含配额信息）
```

Cookie 安全属性：`httponly=True`，`secure=True`，`samesite="lax"`

---

## 六、RAG 检索细节

### 向量索引

- 类型：pgvector HNSW 索引
- 距离度量：余弦距离（`<=>` 操作符）
- `hnsw_ef_search`：100（查询时 HNSW 参数，越大越准但越慢）

### 动态 Top-K 选择算法（`rag.py:13-34`）

从最多 20 个候选（距离 < 0.40）中动态确定返回数量：

- **Margin 策略**：返回所有距离在最优值 + 0.07 范围内的结果
- **Gap 策略**：从第 4 条开始，找到距离突变最大的位置做截断
- 最终取两种策略的最大值，结果数量范围 `[4, 20]`

### 历史语义检索（`rag.py:73-104`）

- 触发条件：消息匹配"你还记得/上次/之前/我们聊过"等正则模式
- 检索范围：本 Session 内全部 assistant 消息的 embedding
- 阈值：0.55（比 RAG 更宽松）
- 排除最近上下文窗口内的消息（避免重复）

---

## 七、文档解析与分块

### 支持格式

| 格式 | 解析方式 |
|---|---|
| PDF | pdfplumber 文本提取；质量不足时 pytesseract OCR（中英文，150 DPI） |
| EPUB | ebooklib 解析 HTML，提取段落转 Markdown |
| DOCX | python-docx 逐段提取 |
| DOC | docx2txt 通过临时文件转换 |
| TXT | UTF-8 直接读取 |

### 分块策略

- **PDF/EPUB**：按 `## 标题` 分段，单段超 800 字则按段落再分；最终按 800 字/块合并
- **TXT/DOCX**：按段落分割，再合并至最大 800 字/块

### 上下文富化

每块向量化前追加头部信息：

```
[来源文件：{filename}。文档开头：{前300字}。位置：第{i+1}段，共{total}段。]

{原始分块内容}
```

---

## 八、管理员功能

`/admin/` 是板块选择页，分两个板块：

- **`/admin/users` 用户管理**
  - 查看 Token 用量统计、调整每日配额和文件大小限制、强制重置密码
  - 邀请码：生成新邀请码（UUID 格式）、查看使用状态
- **`/admin/perf` 性能调优**（Phase 3a 上线）
  - 子系统状态：bot / agent_b / agent_c 的启停 + 心跳
  - 近期 trace 摘要（最新 50 条 `/chat` 调用，含路径、轮数、耗时、tokens）
  - Prompt 版本历史（含 active 标记、创建者、变更原因）
  - **Phase 3b/3c 上线后**：bot 启停、Agent B 分析记录、prompt 回滚按钮等
- **Session 审查**：查看任意用户的对话内容、Token 明细、文件处理状态
- **运维脚本**（`scripts/`）：

| 脚本 | 用途 |
|---|---|
| `create_admin.py` | 创建第一个管理员账户 |
| `update_admin.py` | 提升/撤销管理员权限 |
| `generate_invite.py` | 生成邀请码 |
| `clear_failed_uploads.py` | 清理失败的上传记录 |
| `clear_knowledge_base.py` | 清空指定 Session 的 RAG 向量 |
| `show_file_errors.py` | 查看文件处理错误 |
| `reset_stuck_processing.py` | 重置卡住的处理任务 |
| `migrate.py` | 执行数据库迁移 |
| `list_models.py` | 测试 Gemini 可用模型 |

---

## 九、关键配置项（`.env`）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | — | PostgreSQL 连接串（`postgresql+asyncpg://...`） |
| `GEMINI_API_KEY` | — | Gemini API 密钥 |
| `GOOGLE_API_KEY` | — | Google Custom Search API 密钥 |
| `GOOGLE_CX` | — | 自定义搜索引擎 ID |
| `SECRET_KEY` | — | JWT 签名密钥（≥32 字符） |
| `GEMINI_TEXT_MODEL` | `gemini-2.5-flash` | 生成模型 |
| `GEMINI_EMBED_MODEL` | `gemini-embedding-exp-03-07` | 嵌入模型 |
| `EMBEDDING_DIM` | `768` | 向量维度 |
| `RAG_DISTANCE_THRESHOLD` | `0.40` | RAG 余弦距离阈值 |
| `TOP_K` | `4` | RAG 最少返回条数 |
| `TOP_K_MAX` | `20` | RAG 最多候选条数 |
| `TOP_K_MARGIN` | `0.07` | Margin 策略容差 |
| `TOP_K_GAP` | `0.05` | Gap 策略突变阈值 |
| `HNSW_EF_SEARCH` | `100` | HNSW 查询精度参数 |
| `MAX_HISTORY_TURNS` | `12` | Prompt 中携带的历史轮数 |
| `FULL_CONTEXT_THRESHOLD` | `300000` | session 总语料 token 数低于此阈值时走全量上下文路径（跳过 RAG 检索） |
| `AGENT_CHAT_ENABLED` | `true` | Phase 2 Agent 循环开关，仅在大语料 + 复杂查询时激活 |
| `AGENT_MAX_ITERATIONS` | `6` | Agent 单次对话最多调用工具数（含 LLM 决策轮）|
| `HTTP_PROXY` | — | 可选 HTTP 代理 |
| `ANTHROPIC_API_KEY` | — | Claude API 密钥（仅 `agent_system/` 子系统使用） |

> `agent_system/llm.py` 在导入时自动加载项目根 `.env`（通过 `python-dotenv`），与 `settings.py` 的 Pydantic Settings 加载模式一致。

---

## 十、Session 状态说明

Session 有两种状态：

- **null session**：`name IS NULL`，访问 `/` 时自动创建，用于匿名浏览，不支持 RAG 和文件上传
- **named session**：用户通过 `POST /new_session` 创建，支持 RAG、文件上传、角色人格设置

`session_exists()` 通过 `name IS NOT NULL` 判断是否为命名 Session。

---

## 十一、Agent 子系统（`agent_system/`）

**目标**：自动读取、修改、测试、迭代 TSAI 项目代码——完整的 plan→act→observe→reflect→repeat 闭环（Harness 模式），并分阶段输出带时间戳的进度反馈。

使用 Claude API（Anthropic），不依赖也不修改 TSAI 主体业务逻辑以外的部分。

### 结构

```
agent_system/
├── __init__.py
├── main.py          # CLI 入口：--root 指定项目根目录
├── llm.py           # LLM facade：转发到选中的 provider
├── orchestrator.py  # 薄层 Orchestrator（项目上下文 + 异常级回滚 + 摘要 + memory）
├── harness.py       # TaskHarness：plan→act→observe→reflect→repeat 主循环
├── memory.py        # MemoryManager（跨 session JSON 持久化）
├── workspace.py     # Workspace（文件备份 / diff / 回滚）
├── tools.py         # TOOL_DEFINITIONS + ToolExecutor（文件和 Shell 工具）
├── memory.json      # 运行时生成：跨 session 记忆（已加入 .gitignore）
├── requirements.txt # anthropic / google-genai / openai / python-dotenv
├── providers/                      # 多后端 LLM 适配层
│   ├── __init__.py                 # get_provider() 工厂，按 LLM_PROVIDER 选择
│   ├── base.py                     # LLMProvider ABC + LLMError
│   ├── anthropic_provider.py       # Claude（adaptive thinking + cache_control）
│   ├── gemini_provider.py          # Gemini（thinking_config 自动预算）
│   └── openai_provider.py          # OpenAI（gpt-4o，无 thinking）
└── agents/
    ├── coding_agent.py  # ACT：tool-use 循环直接编辑项目文件
    ├── test_agent.py    # OBSERVE：collect_observation（py_compile / pytest / ruff / import）
    ├── review_agent.py  # REVIEW：分析 diff 的安全漏洞和代码质量
    └── reflect_agent.py # REFLECT：聚合 observation+review，产出 Reflection
```

### LLM Provider 切换

通过 `.env` 选择后端，**默认 `gemini`**（性价比最高，且 TSAI 已有 `GEMINI_API_KEY`）：

```bash
# .env
LLM_PROVIDER=gemini        # gemini | anthropic | openai
# LLM_MODEL=               # 可选：覆盖默认模型
GEMINI_API_KEY=...         # 已有
# ANTHROPIC_API_KEY=...    # 切到 anthropic 时填
# OPENAI_API_KEY=...       # 切到 openai 时填
```

| Provider | 默认 Model | 流式 | Tool-use | Adaptive Thinking | Prompt Cache |
|---|---|---|---|---|---|
| gemini（默认） | `gemini-2.5-flash` | ✅ | ✅（协议转换） | ✅ `thinking_config(budget=-1)` | ⚠️ 隐式 |
| anthropic | `claude-opus-4-7` | ✅ | ✅ 原生 | ✅ 原生 | ✅ 显式 cache_control |
| openai | `gpt-4o` | ✅ | ✅（协议转换） | ❌（gpt-4o 无 thinking） | ⚠️ 隐式 |

`tools.py` 的 `TOOL_DEFINITIONS` 维持 Anthropic 格式作为权威格式，gemini/openai provider 在内部做协议转换。

### 运行方式

```bash
pip install -r agent_system/requirements.txt   # 装齐三家 SDK；用哪家就只需要哪家的 key
# .env 中 LLM_PROVIDER=gemini 即可，无需额外 export

# 从项目根目录运行（project_root 自动推断为 tsai/）
python -m agent_system.main "为 /chat 接口增加输入长度校验"
python -m agent_system.main "重构 backend/rag.py 使 embedding 调用改为异步"

# 或显式指定项目根目录
python -m agent_system.main --root /path/to/tsai "your goal"
```

### Harness 主循环

每轮迭代由四个阶段组成；OBSERVE 与 REVIEW 通过 `asyncio.gather` 并行执行（前者纯 shell、后者 LLM，相互独立）。

```
用户输入 goal
  └─ [Orchestrator] 注入项目上下文（文件树 + ARCHITECTURE.md + 历史 memory）
      └─ Workspace（文件备份基线）+ ToolExecutor
          └─ [TaskHarness.run]
              ├─ PLANNING：[Claude] 生成具体编码任务 plan
              └─ 主循环 × MAX_ITERATIONS=6
                  ├─ ACT：[CodingAgent] tool-use 循环（最多 40 轮）
                  │     ├─ read_file / edit_file / write_file / run_shell / git_diff
                  │     └─ 完成后输出变更摘要
                  ├─ OBSERVE + REVIEW（asyncio.gather 并行）
                  │     ├─ collect_observation：py_compile / pytest / ruff / import 检查
                  │     └─ ReviewAgent：diff 安全/质量分析 → JSON
                  ├─ REFLECT：[ReflectAgent] 聚合 observation+review+history
                  │     → Reflection { status, assessment, next_steps, remaining_issues }
                  └─ Status 分发：
                      ├─ done    → 退出循环（成功）
                      ├─ stuck   → workspace.restore_all() + 退出
                      ├─ replan  → workspace.restore_all() + 重新生成 plan（最多 1 次）
                      └─ continue→ next_steps 反馈进入下一轮 ACT
              └─ TaskState（plan + iterations[] + status）
      └─ [Claude] 生成交付摘要
          └─ [MemoryManager] record_session（status→verdict 映射）
```

### 阶段性进度反馈

每个阶段开始和结束时打印带时间戳的状态行：

```
[15:32:01] ── PLANNING ────────────────────────────────────────
<Claude 流式输出>
[15:32:08] ✓ PLANNING (7s)

[15:32:08] ── ACT [iter 1/6] ──────────────────────────────────
── CodingAgent [round 1] ──
  → read_file(['backend/db.py'])
  → edit_file(['backend/db.py', ...])
  [run_shell] python -m py_compile backend/db.py
[15:34:22] ✓ ACT (134s) · 2 file(s) modified/created

[15:34:22] ── OBSERVE + REVIEW [iter 1] ───────────────────────
  [observe] python -m py_compile "backend/db.py"
  [observe] python -m pytest --tb=short -q
[15:34:52] ✓ OBSERVE + REVIEW (30s) · review=warn

[15:34:52] ── REFLECT [iter 1] ────────────────────────────────
[15:34:55] ✓ REFLECT (3s) · [DONE] Implemented length check; tests skipped (no suite)

[15:34:55] ── SUMMARY ─────────────────────────────────────────
[15:34:58] ✓ SUMMARY (3s)

[15:34:58] ══ Pipeline complete (177s) — DONE after 1 iteration(s) ══
```

### 数据结构（`harness.py`）

```python
@dataclass
class Iteration:
    number: int
    act_summary: str
    observation: str
    review: dict
    reflection: Reflection

@dataclass
class TaskState:
    goal: str
    plan: str
    iterations: list[Iteration]
    status: str          # running | done | stuck | replan | failed
    replanned: bool
```

### Reflection schema（`agents/reflect_agent.py`）

```python
@dataclass
class Reflection:
    status: str              # continue | done | replan | stuck
    assessment: str          # 1-2 句迭代结论
    next_steps: list[str]    # 下一轮 ACT 的具体步骤（文件 + 函数级）
    remaining_issues: list[str]
```

`status` 触发规则：

| 值 | 含义 | Harness 行为 |
|---|---|---|
| `done` | 目标达成；syntax OK；tests pass 或合理跳过；diff 合理 | 退出循环 |
| `continue` | 有进展但未完成 | next_steps 注入下一轮 ACT |
| `replan` | 当前方案根本错了 | 回滚 workspace + 重新规划（仅一次，第二次升级为 stuck） |
| `stuck` | 同一错误连续两轮 / "No changes made" 连续两轮 | 回滚 workspace + 退出 |

### 文件工具（`tools.py`）

| 工具 | 用途 |
|---|---|
| `read_file` | 读取项目文件（上限 20 000 chars） |
| `write_file` | 创建新文件 |
| `edit_file` | 精确字符串替换（old_string 必须唯一） |
| `list_files` | Glob 匹配枚举文件 |
| `run_shell` | 在项目根目录运行 shell 命令（默认 60s timeout） |
| `git_diff` | 返回本次 session 的 unified diff |

安全限制：路径使用 `Path.relative_to()` 校验（修复了 startswith 的路径逃逸漏洞）；`git push` 系列命令被屏蔽。

### Workspace（`workspace.py`）

首次写入文件前自动快照原始内容。提供：

- `get_diff()` → unified diff（供 ReviewAgent 和输出展示）
- `restore_all()` → 回滚所有修改
- `changed_files()` → 已修改文件列表（供 OBSERVE 针对性验证）
- `has_changes()` / `stats()` → 元信息

回滚触发条件：

- **stuck**：ReflectAgent 判定卡死时，Harness 内部 `restore_all()`
- **replan**：重新规划前先 `restore_all()`，让新 plan 从干净状态出发
- **异常保护**：Pipeline 任意位置发生未捕获异常时，Orchestrator 触发 emergency rollback

### OBSERVE 验证链（`agents/test_agent.py:collect_observation`）

实际运行以下命令并汇总输出（无 LLM 调用，结果交给 ReflectAgent）：

1. **diff stats**：变更行数（diff 为空时使用确切短语 `"No changes made"` 以触发 stuck 检测）
2. `python -m py_compile <file>` — 每个被修改的 `.py` 文件
3. `pytest --tb=short -q` — 如果项目有 `tests/` 或 `test_*.py`（否则跳过）
4. `ruff check <files>` — 如果 ruff 已安装（否则跳过）
5. `python -c 'import sys; sys.path.insert(0, "."); import <module>'` — 导入检查（cwd 添加 `.` 到 sys.path，TSAI 模块可正确导入）

### ReviewAgent

接收 Workspace diff（非全文件）进行安全和质量分析，聚焦 diff 引入的变更，输出 `{verdict, summary, security_issues, quality_issues, suggestions}`。

### Agent 接口签名

| Agent | 类 / 函数 | 签名 | 输出 |
|---|---|---|---|
| Orchestrator | `Orchestrator(project_root, memory_path)` | `async run(goal) -> str` | 完整格式化结果 |
| TaskHarness | `TaskHarness()` | `async run(*, goal, project_context, workspace, executor, memory_context) -> TaskState` | TaskState |
| CodingAgent | `CodingAgent` | `run(task, context, executor, max_rounds=40) -> str` | 变更摘要文本 |
| OBSERVE | `collect_observation` | `(project_root, changed_files, diff) -> str` | 拼接报告 |
| ReviewAgent | `ReviewAgent` | `run(diff, task) -> dict` | JSON 报告 |
| ReflectAgent | `ReflectAgent` | `run(*, goal, plan, history, observation, review, iteration) -> Reflection` | Reflection |

### LLM 封装（`llm.py` + `providers/`）

`llm.py` 是薄壳，导入时由 `providers.get_provider()` 工厂根据 `LLM_PROVIDER` 选好后端，对外仍暴露 `complete()` / `complete_with_tools()` 两个函数：

| 函数 | 用途 | 特性 |
|---|---|---|
| `complete()` | 单轮调用（流式） | `verbose=True` 流式打印；JSON-output agents 设 `verbose=False` |
| `complete_with_tools()` | 多轮 tool-use 循环 | 非流式，每轮打印工具调用 |

各 provider 内部差异：

| Provider | thinking | 系统提示词缓存 | tool 协议 |
|---|---|---|---|
| anthropic | `thinking={"type":"adaptive"}` | 显式 `cache_control: ephemeral` | 原生 Anthropic 格式 |
| gemini | `ThinkingConfig(thinking_budget=-1)`（仅 2.5+） | 隐式 | 转 `Tool(function_declarations=...)` |
| openai | 无（gpt-4o 不支持） | 隐式 | 转 `{type:"function", function:{...}}` |

任何后端的失败都抛 `LLMError`（`agent_system.providers.base.LLMError`，`llm.py` 重新导出）。

### MemoryManager（`memory.py`）

跨 session 的 JSON 文件持久化（`agent_system/memory.json`）：

- `context_for_prompt()` → 在 Planning 步骤注入最近 3 次 session 的 goal + verdict + issues
- `record_session()` → 最后持久化，保留最多 20 条 session，滚动更新 `project_context`

Verdict 映射（`orchestrator._status_to_verdict`）：

| TaskState.status | review verdict | 写入 memory |
|---|---|---|
| `done` | `pass` | `pass` |
| `done` | `warn` | `warn` |
| `stuck` / `failed` | * | `fail` |

---

## 十二、pgvector 特殊访问方式

`databases` 库不支持 pgvector 原生类型，每次向量读写前需手动从连接池获取 asyncpg 原始连接并注册 codec：

```python
conn = await database._backend._pool.acquire()
await register_vector(conn)
# ... 执行向量操作 ...
```

相关代码位于 `backend/db.py` 中所有涉及 `embedding` 列的函数。

---

## 十三、自主调优子系统（Phase 3）

### 目标

让 TSAI 的 Agent prompt 能"自己优化自己"——每天机器人跑测试 query → trace 落盘 → Agent B 周期性分析失败模式 → 自动修改 prompt → Agent C 验证效果（坏就回滚）。

### 三阶段渐进上线

| Phase | 范围 | 状态 |
|---|---|---|
| **3a** | 基础设施：DB 三表、prompt 搬到 DB、trace 自动落盘、admin 页面拆板块、Celery+Redis 骨架 | ✅ 已上线 |
| 3b | 机器人用户：复刻"天书"的 session、每日 5 个 query、性能调优页展示数据 | ⏳ 待开发 |
| 3c | Agent B（半自动 → 自动）：扫 `analyzed_at IS NULL` 的 trace、Gemini 分析、改 prompt | ⏳ 待开发 |
| 3d | Agent C：prompt 改后跑验证集、计算 score、自动回滚 | ⏳ 待开发 |

### 评分公式（Agent C 用）

```
score = -iterations - 5 * hallucination_rate - 0.001 * latency_ms
```

prompt 变更前后跑同样验证集，新版分数显著低于旧版 → 自动 rollback。

### Prompt 加载机制

`backend/agent_chat.py` 不再硬编码 `_AGENT_TOOL_RULES`，改为：

```python
async def build_system_prompt(persona: str | None) -> tuple[str, int]:
    rules, version_id = await _get_cached_rules()  # 30s in-memory cache
    return f"{identity}\n\n{rules}", version_id
```

- `_get_cached_rules()` 从 `prompt_versions WHERE is_active=TRUE` 拉取
- 内存缓存 30 秒，避免每次 /chat 都查 DB
- Agent B 改完后调用 `invalidate_prompt_cache()` 让所有进程下次请求重新拉
- DB 里没有任何版本时（首次启动）→ 自动从代码兜底常量种入 v1

### 调度框架（Celery + Redis）

```
backend/celery_app.py    Celery 实例 + Redis broker/backend 配置
backend/tasks.py         任务定义（3a 仅 ping）

启动 worker：
    celery -A backend.celery_app worker --loglevel=info

启动 beat（周期任务，3b 后才需要）：
    celery -A backend.celery_app beat --loglevel=info
```

`.env` 配置：`REDIS_URL=redis://localhost:6379/0`
