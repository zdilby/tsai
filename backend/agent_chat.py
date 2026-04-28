"""
Phase 2 — Agent 循环（ReAct / Plan-Solve）

入口：run_agent_chat(query, session_id, persona, history_text, ...) → dict

设计要点：
  • 5 个工具：search_kb / read_document / list_documents / web_search / search_history
  • 智能路由（needs_agent）：60-70% 的 query 不进 Agent，直接走 RAG 路径
  • 提前退出：system prompt 鼓励 Agent 一旦拿到强证据就立即作答
  • 并行 tool 调用：同一轮内多 function_call 用 asyncio.gather 同时执行
  • 完整 trace：每次工具调用都记录到返回值的 agent_trace 字段，便于调教

输出：
  {
    "answer": "...",                  # 最终回答
    "citations": [...],               # 兼容现有 /chat 响应（来自工具结果）
    "agent_trace": [                  # 给前端 / 调试用
      {"round": 1, "tool": "search_kb", "args": {...}, "result_preview": "..."},
      ...
    ],
    "tokens_in":  int,                # 累计输入 token
    "tokens_out": int,                # 累计输出 token
    "iterations": int,                # 实际循环轮数
  }
"""
import asyncio
import re
from typing import Any

from google.genai import types

from settings import client, embed_client, logger, settings
from .rag import (
    get_embedding,
    query_rag,
    query_history,
    list_session_documents,
    get_full_document,
)


# ── Smart Router（优化 A）────────────────────────────────────────────────────

# 复用主路由里的回忆触发词正则
_RECALL_PATTERNS = re.compile(
    r'(你还记得|还记得|你记得|记得吗|之前(我们|你|咱们)?|上次(你|我们)?|'
    r'我们(之前|以前|上次)?聊过|你(之前|以前|上次)?提到|我(之前|以前)?问过|'
    r'我们讨论过|你说过|你提过|前面(你|我们)?)',
    re.UNICODE,
)

_COMPARISON_KEYS  = ("对比", "区别", "比较", " vs ", "vs.", "差异")
_OPEN_ENDED_KEYS  = ("分析", "总结", "概括", "评价", "怎么看", "为什么", "原因")
_LIST_KEYS        = ("有哪些", "都有什么", "列出", "列表")


def needs_agent(query: str) -> bool:
    """
    判断该 query 是否值得进 Agent 循环。

    分层策略：
      • 强信号（对比/回忆/列举/多问句）→ 无视长度直接进 Agent
      • 弱信号（开放性提问，如"分析/总结/为什么"）→ 长度 ≥ 8 字符才进
      • 无信号 / 太短 → 沿用 RAG 路径以保延迟
    """
    has_comparison = any(k in query for k in _COMPARISON_KEYS)
    has_recall     = bool(_RECALL_PATTERNS.search(query))
    has_open_ended = any(k in query for k in _OPEN_ENDED_KEYS)
    has_list       = any(k in query for k in _LIST_KEYS)
    has_multi_part = (query.count("？") + query.count("?")) >= 2

    if has_comparison or has_recall or has_list or has_multi_part:
        return True
    if has_open_ended and len(query) >= 8:
        return True
    return False


# ── System Prompt ─────────────────────────────────────────────────────────────

_AGENT_TOOL_RULES = """\
你的目标：用工具检索证据，给出准确、可溯源的回答。

# 工具

## 1. search_kb(query, top_k)
向量检索本会话上传文档，返回最相关的片段。这是你的首选工具。
查询写法：不要照抄用户原话，要拆成关键词组合。
  ❌ search_kb("这个方案有什么风险")
  ✓ search_kb("方案 风险 隐患 漏洞")

## 2. read_document(filename)
读取整篇文档原文。何时用：
  - search_kb 命中某文档但需要更完整上下文
  - 用户要求"总结/概括某份文件"
  - 需跨段落对比、计数、列举
不要漫无目的地浏览——必须有明确目标文档。

## 3. list_documents()
列出会话内所有文件名。一次对话最多调 1 次。
何时用：用户问"我有哪些文件"，或你不确定文件名。

## 4. web_search(query)
Google 搜索外部信息。何时用：
  - 用户问的是新闻、最新动态、外部公开知识
  - search_kb 已确认没有相关内容
不要默认从 web_search 开始——永远先试 search_kb。
查询语言：中文问题用中文查，英文术语用英文查。

## 5. search_history(query)
语义检索本会话历史 AI 回复。
何时用：用户用了"还记得/之前/上次/我们聊过/你说过"等回忆触发词。
不要用来检索通用知识——那是 search_kb 的职责。

# 决策优先级

1. 回忆类提问（含触发词）→ search_history
2. 内容可能在上传文档里 → search_kb（绝大多数情况）
3. search_kb 召回不理想 → 换关键词重试 1 次
4. 用户问"我有哪些文件" → list_documents
5. 锁定具体文档要细看 → read_document
6. 仅外部信息能解决 → web_search

# 查询改写参考（6 个典型场景）

| 用户原话 | ❌ 不要 | ✓ 这样 |
|---|---|---|
| 这个方案的风险？ | search_kb("这个方案的风险") | search_kb("方案 风险 隐患 漏洞") |
| 总结第三章 | search_kb("总结第三章") | read_document(target) + search_kb("第三章") |
| 主角性格怎么样 | search_kb("主角性格怎么样") | search_kb("主角 性格 描写 心理") |
| 我们之前聊的 X 有关吗 | search_kb("X 有关") | search_history("X") + search_kb("X 关联") |
| A 和 B 有什么区别 | search_kb("A 和 B 区别") | search_kb("A 定义 特征") + search_kb("B 定义 特征") |
| 最近 OpenAI 发布了什么 | search_kb("OpenAI 最近发布") | web_search("OpenAI 最新发布 2026") |

# 重试策略

- 一次 search_kb 拉空 → 换关键词角度再试 1 次（同义词 / 上下位概念）
- 累计 3 次 search_kb 仍无果 → 切 web_search 或如实告知"资料里没找到"
- 相同工具相同参数不得调用第二次

# 提前退出（重要！延迟优化）

如果 search_kb 返回了 distance < 0.3 的强匹配证据，**立即给答案**，不要继续调工具。
信息已经充分时不要为求"更完整"而多调工具——延迟会显著增加。

# 预算

- 整个对话最多 6 次工具调用
- 接近上限时优先给答案，不再扩展搜索
- 信息不全也要回答——明确说"已尽力检索，但..."

# 引用纪律（硬性规则）

每个事实性论断必须标注来源：

| 来源 | 标注 |
|---|---|
| KB 文档 | （来源：<filename>，第 <chunk_index> 段） |
| 网页 | （来源：网络） |
| 历史对话 | （来源：本次会话历史） |
| 训练知识兜底 | （基于一般知识，未在资料中找到） |

- filename 和 chunk_index 必须真实出现在工具返回结果中——不得编造
- 直接引用原文必须加引号
- 多个事实分别标注，不要笼统挂一个引用

# 禁止

❌ 不调任何工具就回答"这是常识/根据一般规则..."
   例外：纯寒暄、问候、致谢、闲聊（"你好"/"谢谢"/"在吗"等）可直接回应，无需调工具
❌ 用户原话直接当 search_kb 查询
❌ 没有依据就编造文件名
❌ 引用工具结果里不存在的内容
❌ 同一工具同样参数调用 2 次

# 收尾

判定信息充分（或预算用完）→ 停止调工具，直接给答案并标注来源。
"""


def build_system_prompt(persona: str | None) -> str:
    """拼接 persona（若有）+ 工具使用规则。"""
    identity = persona.strip() if persona and persona.strip() else \
        "你是 TSAI 智能助手，能基于用户在本会话上传的文档作答。"
    return f"{identity}\n\n{_AGENT_TOOL_RULES}"


# ── Tool Definitions（Gemini function_declarations 格式）──────────────────────

_TOOLS = [
    types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="search_kb",
            description=(
                "向量语义检索本会话上传的文档，返回最相关的若干文本片段。"
                "查询请用关键词组合，不要照抄用户原话。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键词查询（推荐 2-5 个关键词）"},
                    "top_k": {"type": "integer", "description": "返回片段数（默认 4）"},
                },
                "required": ["query"],
            },
        ),
        types.FunctionDeclaration(
            name="read_document",
            description="读取指定文件名的完整文档原文（最多 30000 字符）。",
            parameters={
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文档文件名（必须是真实存在的）"},
                },
                "required": ["filename"],
            },
        ),
        types.FunctionDeclaration(
            name="list_documents",
            description="列出当前会话内所有上传文档及其 chunk 数。",
            parameters={"type": "object", "properties": {}, "required": []},
        ),
        types.FunctionDeclaration(
            name="web_search",
            description="使用 Google 自定义搜索查询外部网页信息（仅当 KB 内无相关内容时使用）。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索查询（中文问题用中文查）"},
                },
                "required": ["query"],
            },
        ),
        types.FunctionDeclaration(
            name="search_history",
            description="语义检索本会话历史 AI 回复（仅用于回忆类问题）。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "话题关键词"},
                },
                "required": ["query"],
            },
        ),
    ]),
]


# ── Tool Dispatcher ───────────────────────────────────────────────────────────

async def _dispatch_tool(name: str, args: dict, session_id: str) -> tuple[str, list]:
    """
    执行一次工具调用，返回 (text_for_LLM, citations_added)。
    citations_added 用于累积到响应的 citations 字段，前端高亮 chunk 用。
    """
    try:
        if name == "search_kb":
            from midware.tools import fetch_from_web  # noqa: F401（避免循环）
            query = args.get("query", "")
            top_k = int(args.get("top_k", 4))
            embedding = await get_embedding(embed_client, query)
            results = await query_rag(embedding, session_id=session_id)
            if top_k and len(results) > top_k:
                results = results[:top_k]
            if not results:
                return "（无匹配片段）", []
            text = "\n\n".join(
                f"[{r['source_file']} 第{r['chunk_index']}段, distance={round(r['distance'], 3)}]\n{r['content']}"
                for r in results
            )
            cites = [
                {
                    "source": r["source_file"],
                    "chunk": r["chunk_index"],
                    "score": round(1 - r["distance"], 3),
                    "snippet": (r.get("original_content") or r["content"])[:200].strip(),
                }
                for r in results
            ]
            return text, cites

        if name == "read_document":
            filename = args.get("filename", "")
            content = await get_full_document(session_id, filename)
            if not content:
                return f"（未找到文件：{filename}）", []
            return f"[{filename} 全文]\n{content}", []

        if name == "list_documents":
            docs = await list_session_documents(session_id)
            if not docs:
                return "（本会话尚未上传任何文档）", []
            lines = [
                f"- {d['source_file']}（{d['chunk_count']} 段，约 {d['total_chars']} 字符）"
                for d in docs
            ]
            return "本会话已上传：\n" + "\n".join(lines), []

        if name == "web_search":
            from midware.tools import fetch_from_web
            query = args.get("query", "")
            text = await fetch_from_web(query)
            return text or "（网络搜索无结果）", []

        if name == "search_history":
            query = args.get("query", "")
            embedding = await get_embedding(embed_client, query)
            results = await query_history(embedding, session_id=session_id, threshold=0.55)
            if not results:
                return "（历史对话中无相关内容）", []
            return "\n\n".join(
                f"[{r['created_at']}] {r['snippet']}{'…' if len(r['content']) > 300 else ''}"
                for r in results
            ), []

        return f"[ToolError] 未知工具: {name}", []

    except Exception as exc:
        logger.exception("Agent tool '%s' 调用失败", name)
        return f"[ToolError] {name}: {exc}", []


# ── Main Loop ─────────────────────────────────────────────────────────────────

async def run_agent_chat(
    *,
    query: str,
    session_id: str,
    persona: str | None,
    history_text: str,
    web_info: str = "",
) -> dict:
    """
    Agent 主循环。返回结构化结果，由 main.py 调用方组装最终响应。

    流程：
      1. 构造 system_instruction（persona 拼接 + 工具规则）
      2. 启动 contents = [user_message]，每轮：
         - 调 Gemini，拿到 function_calls 或 final answer
         - 如有 function_calls → asyncio.gather 并行执行 → 喂回
         - 否则 → 回答完成，break
      3. 累计 tokens、citations、trace
    """
    system_prompt = build_system_prompt(persona)

    user_content = (
        f"对话历史：\n{history_text}\n\n"
        f"用户问题：{query}\n\n"
        + (f"参考网页搜索（已为你预先抓取）：\n{web_info}\n\n" if web_info else "")
        + "请根据需要调用工具检索证据，最后用准确的引用回答。"
    )

    contents: list = [
        types.Content(role="user", parts=[types.Part.from_text(text=user_content)])
    ]
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=_TOOLS,
        # 显式关闭 SDK 的自动函数调用——我们走的是手动循环，
        # 否则 SDK 会在每次调用时打 "AFC is enabled" 噪音日志
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    trace: list = []
    citations: list = []
    tokens_in = 0
    tokens_out = 0

    final_answer = ""
    iterations = 0

    for round_num in range(1, settings.agent_max_iterations + 1):
        iterations = round_num
        try:
            response = await client.aio.models.generate_content(
                model=settings.generation_model,
                contents=contents,
                config=config,
            )
        except Exception as e:
            logger.exception("Agent LLM 调用失败 (round %d): %s", round_num, e)
            final_answer = f"（Agent 调用失败：{e}）"
            break

        usage = response.usage_metadata
        if usage:
            tokens_in  += getattr(usage, "prompt_token_count",     0) or 0
            tokens_out += getattr(usage, "candidates_token_count", 0) or 0

        candidate = response.candidates[0]
        contents.append(candidate.content)
        parts = candidate.content.parts or []

        function_calls = [p.function_call for p in parts if p.function_call]
        text_parts     = [p.text for p in parts if p.text]

        if not function_calls:
            # 模型给出最终回答
            final_answer = "\n".join(text_parts).strip() or "（模型未输出文本）"
            logger.info("Agent round %d → final answer (%d chars)", round_num, len(final_answer))
            break

        # 优化 E：并行执行所有 function_call
        logger.info(
            "Agent round %d → %d tool call(s): %s",
            round_num,
            len(function_calls),
            [fc.name for fc in function_calls],
        )
        results = await asyncio.gather(*[
            _dispatch_tool(fc.name, dict(fc.args) if fc.args else {}, session_id)
            for fc in function_calls
        ])

        tool_response_parts = []
        for fc, (result_text, cites) in zip(function_calls, results):
            citations.extend(cites)
            args_dict = dict(fc.args) if fc.args else {}
            trace.append({
                "round": round_num,
                "tool": fc.name,
                "args": args_dict,
                "result_preview": result_text[:200],
            })
            tool_response_parts.append(
                types.Part.from_function_response(
                    name=fc.name,
                    response={"result": result_text},
                )
            )

        contents.append(types.Content(role="user", parts=tool_response_parts))

    else:
        # 循环耗尽未给最终答案：用最后一轮的 text_parts 兜底
        final_answer = (
            "\n".join(text_parts).strip()
            if text_parts else "（已达最大工具调用上限，未能完整回答）"
        )

    # 引用去重（按 source + chunk）
    seen: set = set()
    deduped: list = []
    for c in citations:
        key = (c.get("source"), c.get("chunk"))
        if key not in seen:
            seen.add(key)
            deduped.append(c)

    return {
        "answer": final_answer,
        "citations": deduped,
        "agent_trace": trace,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "iterations": iterations,
    }
