"""
Phase 3c — Agent B：分析 agent_traces，自动改 prompt。

每 12 小时由 Celery beat 触发。完整流程：
  1. Redis SETNX 锁防并发
  2. 拉 N 条 route='agent' 且 analyzed_at IS NULL 的 trace（按时间升序，老的先分析）
  3. 喂给 gemini-2.5-pro，按"6 类失败模式"标签返回结构化 JSON
  4. 24h 频率门控 → 通过
     5 项护栏校验 → 全通过
     → upsert_prompt_version + invalidate_prompt_cache
  5. 标记这批 trace 的 analyzed_at = NOW()
  6. 一行 agent_b_runs 落盘（含失败原因）
  7. 释放锁

不验证效果 —— 那是 Phase 3d 的活儿。这里只产生候选并应用。
"""
import json
import re
import time
from typing import Any

from google import genai
from google.genai import types
from redis import Redis

from settings import settings, logger
from .db import (
    database,
    create_agent_b_run,
    update_agent_b_run,
    fetch_pending_agent_traces,
    has_recent_agent_b_change,
    heartbeat_subsystem,
    mark_traces_analyzed,
    get_active_prompt,
    upsert_prompt_version,
    get_subsystem_status,
)
from .agent_chat import PROMPT_NAME, invalidate_prompt_cache


# ── 系统 Prompt（喂给 Gemini，告诉它怎么分析）─────────────────────────────────

_AGENT_B_SYSTEM_PROMPT = """\
你是 Agent B，TSAI 的 prompt 调优 agent。
你的任务：阅读最近一批 production agent 的调用 trace，识别 prompt 引导失败的模式，
必要时提出**针对性的 patch**改进当前 prompt。

# 输入

你会收到：
1. 当前生效的 agent system prompt（叫 _AGENT_TOOL_RULES，~2000 字符）
2. N 条 trace 记录，每条含：
   - trace_id（int）
   - query（用户原话）
   - tools_called（tool 调用序列）
   - iterations（LLM 轮数）
   - citations（agent 引用的 KB chunk）
   - duration_ms

# 6 类失败模式（必须用这些标签）

| category | 触发条件 |
|---|---|
| `hallucination` | citations 里的 source/chunk 在 tools_called 的 result_preview 中**找不到** |
| `wrong_tool` | 该用 search_kb 时用了 web_search（或反过来）|
| `over_search` | iterations ≥ 5 且最终答案没明显比 2-3 轮版本更好 |
| `under_search` | iterations = 1 且 tools_called 为空，但 query 显然需要检索 KB |
| `verbatim_query` | search_kb 的 query 跟用户原话 ≥ 80% 相似（没改写关键词）|
| `repeated_call` | 同一 tool 同样参数被调 ≥ 2 次 |

# 决策规则（重要）

- 只有当**某类失败在 ≥ 25% 的 trace 中出现**时才考虑改 prompt（频率 < 25% = 噪音，不动）
- 改 prompt 必须 patch 现有 section（不可整体重写）
- 改动应"加强 / 收紧"既有规则，不是引入新概念
- 一次只改一个 section 一类问题

# 输出格式（ONLY a single JSON object，no prose, no markdown fences）

{
  "issues_found": [
    {"category": "hallucination", "frequency": 5, "trace_ids": [42, 47, 51, 58, 60]},
    {"category": "wrong_tool",    "frequency": 2, "trace_ids": [44, 52]}
  ],
  "should_change_prompt": true,
  "patch": {
    "strategy": "tighten_section",
    "target_section": "# 引用纪律（硬性规则）",
    "new_section_content": "# 引用纪律（硬性规则）\\n...\\n（改进的全文）",
    "reasoning": "5/20 trace 出现 hallucination，引用纪律 section 缺乏 verification 步骤..."
  }
}

如果 should_change_prompt=false，patch 字段可以为 null 或省略。
target_section 必须是当前 prompt 中真实存在的 section 标题（含 #）。
new_section_content 必须以 target_section 开头（保留 # 标题行）。
"""


# ── Redis 锁 ──────────────────────────────────────────────────────────────

_LOCK_KEY = "agent_b:lock"
_LOCK_TTL = 600  # 10 分钟，超过这个时间锁自动释放（防进程崩溃）


def _redis_client() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def _try_acquire_lock() -> bool:
    return bool(_redis_client().set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL))


def _release_lock() -> None:
    try:
        _redis_client().delete(_LOCK_KEY)
    except Exception as e:
        logger.warning("[agent_b] release_lock failed: %s", e)


# ── JSON 提取（容错，应对 Gemini 偶尔加 markdown 围栏）─────────────────────

def _parse_json(text: str) -> dict | None:
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    if start != -1:
        depth, end = 0, -1
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end != -1:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    return None


# ── Patch 应用器：替换或追加 prompt 中的某一 section ──────────────────────

_SECTION_HEADER_RE = re.compile(r"^#\s+.+?$", re.MULTILINE)


def _replace_section(prompt: str, target_header: str, new_content: str) -> str:
    """
    替换以 target_header 开头的 section（直到下一个 '# ' 或文末）。
    target_header 必须是完整的 '# xxx' 行内容。new_content 必须以 target_header 开头。
    """
    norm_target = target_header.strip()
    headers = list(_SECTION_HEADER_RE.finditer(prompt))
    if not headers:
        raise ValueError("当前 prompt 不包含任何 '# ' 标题行")

    target_match = None
    for i, h in enumerate(headers):
        if h.group(0).strip() == norm_target:
            target_match = (i, h)
            break
    if target_match is None:
        raise ValueError(f"target_section {target_header!r} 在当前 prompt 中不存在")

    i, h = target_match
    section_start = h.start()
    section_end = headers[i + 1].start() if i + 1 < len(headers) else len(prompt)
    return prompt[:section_start] + new_content.rstrip() + "\n\n" + prompt[section_end:]


def _apply_patch(current: str, patch: dict) -> str:
    strategy = patch.get("strategy", "tighten_section")
    target = patch.get("target_section", "").strip()
    new_content = patch.get("new_section_content", "").strip()
    if not target or not new_content:
        raise ValueError("patch 缺少 target_section 或 new_section_content")

    if strategy in ("tighten_section", "rewrite_section"):
        return _replace_section(current, target, new_content)
    if strategy == "additive":
        return current.rstrip() + "\n\n" + new_content + "\n"
    raise ValueError(f"未知 strategy: {strategy!r}")


# ── 5 项安全护栏 ─────────────────────────────────────────────────────────

_REQUIRED_SECTIONS = ["# 工具", "# 决策优先级", "# 引用纪律", "# 禁止"]
_REQUIRED_TOOLS = ["search_kb", "read_document", "list_documents", "web_search", "search_history"]
_MIN_LEN, _MAX_LEN = 500, 5000


def _validate_new_prompt(new: str, current: str) -> tuple[bool, str]:
    """
    返回 (ok, reason)。reason 在 ok=False 时说明被拒原因。
    """
    if not (_MIN_LEN <= len(new) <= _MAX_LEN):
        return False, f"长度 {len(new)} 越界 [{_MIN_LEN}, {_MAX_LEN}]"

    for s in _REQUIRED_SECTIONS:
        if s not in new:
            return False, f"缺少必备 section：{s!r}"

    for t in _REQUIRED_TOOLS:
        if t not in new:
            return False, f"缺少必备 tool 名：{t!r}"

    diff_size = abs(len(new) - len(current))
    if diff_size > len(current):
        return False, f"diff 过大：{diff_size}/{len(current)}（>100%）"

    return True, "OK"


# ── Trace 序列化（喂给 Gemini）───────────────────────────────────────────

def _format_traces_for_llm(traces: list[dict]) -> str:
    """把 trace 列表打包成 LLM 可读的紧凑格式。"""
    chunks: list[str] = []
    for t in traces:
        tools = t.get("tools_called", [])
        if isinstance(tools, str):
            try:
                tools = json.loads(tools)
            except json.JSONDecodeError:
                tools = []
        cites = t.get("citations", [])
        if isinstance(cites, str):
            try:
                cites = json.loads(cites)
            except json.JSONDecodeError:
                cites = []

        tools_brief = "\n".join(
            f"  round={x.get('round')} tool={x.get('tool')} args={list((x.get('args') or {}).keys())} "
            f"result_preview={(x.get('result_preview') or '')[:120]!r}"
            for x in tools
        ) or "  (no tool calls)"

        cites_brief = "\n".join(
            f"  source={c.get('source')!r} chunk={c.get('chunk')} score={c.get('score')}"
            for c in cites
        ) or "  (no citations)"

        chunks.append(
            f"--- trace_id={t['id']} ---\n"
            f"query: {t['query']!r}\n"
            f"iterations: {t.get('iterations')}, duration_ms: {t.get('duration_ms')}\n"
            f"tools_called:\n{tools_brief}\n"
            f"citations:\n{cites_brief}"
        )
    return "\n\n".join(chunks)


# ── Gemini 调用 ──────────────────────────────────────────────────────────

def _gemini_client() -> "genai.Client":
    return genai.Client(api_key=settings.gemini_api_key)


async def _call_gemini_for_analysis(current_prompt: str, traces: list[dict]) -> dict | None:
    """
    返回结构化分析 JSON dict，解析失败返回 None。
    """
    client = _gemini_client()
    user_content = (
        f"# 当前生效的 agent system prompt（_AGENT_TOOL_RULES）\n\n"
        f"{current_prompt}\n\n"
        f"# 待分析的 {len(traces)} 条 trace\n\n"
        f"{_format_traces_for_llm(traces)}\n\n"
        "请按 JSON 格式输出分析结果。"
    )
    config = types.GenerateContentConfig(
        system_instruction=_AGENT_B_SYSTEM_PROMPT,
        # Pro 模型支持 thinking_config，开启可显著提高分析质量
        thinking_config=types.ThinkingConfig(thinking_budget=-1),
        # 显式禁用 AFC（我们没注册任何 callable tool）
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_content)])]

    try:
        resp = await client.aio.models.generate_content(
            model=settings.agent_b_model,
            contents=contents,
            config=config,
        )
    except Exception as e:
        logger.exception("[agent_b] Gemini call failed")
        raise

    raw = (resp.text or "").strip()
    parsed = _parse_json(raw)
    if parsed is None:
        logger.warning("[agent_b] failed to parse Gemini response: %r", raw[:300])
    return parsed


# ── 主入口 ───────────────────────────────────────────────────────────────

async def run_agent_b_analysis() -> dict:
    """
    Agent B 一次完整运行。返回结果字典（包含跳过原因 / applied=true|false 等）。
    """
    if not _try_acquire_lock():
        logger.info("[agent_b] another instance running, skipping")
        return {"skipped": "lock_held"}

    run_id: int | None = None
    try:
        # 1. 启停门控
        status = await get_subsystem_status("agent_b")
        if not status or not status["enabled"]:
            logger.info("[agent_b] subsystem disabled, skipping")
            return {"skipped": "disabled"}

        # 2. 启 run（先建行，所有失败都记进同一行）
        run_id = await create_agent_b_run()

        # 3. 拉 trace
        traces = await fetch_pending_agent_traces(limit=settings.agent_b_batch_size)
        if len(traces) < settings.agent_b_min_traces:
            msg = f"only {len(traces)} traces, need >= {settings.agent_b_min_traces}"
            logger.info("[agent_b] %s", msg)
            await update_agent_b_run(
                run_id, traces_analyzed=len(traces), error_message=msg, finished=True,
            )
            await heartbeat_subsystem("agent_b", f"skipped: {msg}")
            return {"skipped": msg, "run_id": run_id}

        # 4. 拿当前 prompt
        cur = await get_active_prompt(PROMPT_NAME)
        if cur is None:
            msg = "no active prompt to analyze; bootstrap a v1 first"
            await update_agent_b_run(run_id, error_message=msg, finished=True)
            return {"skipped": msg, "run_id": run_id}
        current_content, _ = cur

        # 5. 调 Gemini 分析
        logger.info("[agent_b] analyzing %d traces with %s",
                    len(traces), settings.agent_b_model)
        analysis = await _call_gemini_for_analysis(current_content, traces)
        if analysis is None:
            msg = "Gemini response not parseable as JSON"
            await update_agent_b_run(
                run_id, traces_analyzed=len(traces),
                error_message=msg, finished=True,
            )
            # trace 仍标记 analyzed —— 否则下次会重复分析同一批失败
            await mark_traces_analyzed([t["id"] for t in traces])
            return {"error": msg, "run_id": run_id}

        issues = analysis.get("issues_found", [])
        proposed = bool(analysis.get("should_change_prompt"))

        await update_agent_b_run(
            run_id,
            traces_analyzed=len(traces),
            issues_found=issues,
            proposed_change=proposed,
        )

        # 6. 分支：是否真的应用 patch
        if not proposed:
            logger.info("[agent_b] analysis: no change proposed (issues=%s)",
                        [(i.get('category'), i.get('frequency')) for i in issues])
            await mark_traces_analyzed([t["id"] for t in traces])
            await update_agent_b_run(run_id, applied=False, finished=True)
            await heartbeat_subsystem("agent_b", f"analyzed {len(traces)}, no change")
            return {"run_id": run_id, "traces_analyzed": len(traces),
                    "applied": False, "issues_found": issues}

        # 7. 24h 频率门控
        if await has_recent_agent_b_change(hours=24):
            msg = "24h frequency cap: another agent_b change already applied within 24h"
            logger.info("[agent_b] %s", msg)
            await update_agent_b_run(run_id, applied=False, error_message=msg, finished=True)
            await mark_traces_analyzed([t["id"] for t in traces])
            return {"run_id": run_id, "traces_analyzed": len(traces),
                    "applied": False, "skipped_reason": msg}

        # 8. 应用 patch
        patch = analysis.get("patch") or {}
        try:
            new_content = _apply_patch(current_content, patch)
        except Exception as e:
            msg = f"patch application failed: {e}"
            logger.warning("[agent_b] %s", msg)
            await update_agent_b_run(run_id, applied=False, error_message=msg, finished=True)
            await mark_traces_analyzed([t["id"] for t in traces])
            return {"run_id": run_id, "applied": False, "error": msg}

        # 9. 5 项安全护栏校验
        ok, reason = _validate_new_prompt(new_content, current_content)
        if not ok:
            msg = f"validation failed: {reason}"
            logger.warning("[agent_b] %s", msg)
            await update_agent_b_run(run_id, applied=False, error_message=msg, finished=True)
            await mark_traces_analyzed([t["id"] for t in traces])
            return {"run_id": run_id, "applied": False, "error": msg}

        # 10. 写入新版本 + 失效缓存
        new_version_id = await upsert_prompt_version(
            PROMPT_NAME, new_content,
            created_by="agent_b",
            reason=patch.get("reasoning", "agent_b auto-tuned")[:500],
        )
        invalidate_prompt_cache()

        await update_agent_b_run(
            run_id,
            applied=True,
            new_prompt_version_id=new_version_id,
            finished=True,
        )
        await mark_traces_analyzed([t["id"] for t in traces])
        await heartbeat_subsystem(
            "agent_b",
            f"applied prompt v{new_version_id}: {patch.get('target_section', '')[:40]}",
        )
        logger.info(
            "[agent_b] APPLIED new prompt v_id=%d, target=%r",
            new_version_id, patch.get("target_section"),
        )
        return {
            "run_id": run_id,
            "traces_analyzed": len(traces),
            "applied": True,
            "new_prompt_version_id": new_version_id,
            "issues_found": issues,
        }

    except Exception as e:
        logger.exception("[agent_b] unexpected failure")
        if run_id is not None:
            try:
                await update_agent_b_run(run_id, error_message=str(e)[:500], finished=True)
            except Exception:
                pass
        return {"error": str(e), "run_id": run_id}
    finally:
        _release_lock()
