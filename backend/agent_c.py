"""
Phase 3d — Agent C：验证 Agent B 应用的 prompt 改动是否真的更好；不好就自动回滚。

每 4 小时由 Celery beat 触发。流程：
  1. Redis SETNX("agent_c:lock") 防并发
  2. 拿当前 active 版本 v_new + 它的前一个版本 v_old
  3. 仅当 v_new 是 agent_b 创建时才验证（manual / bootstrap 不动）
  4. 拉两个版本各自的 route='agent' trace
  5. 对每条 trace 反查 KB 计算 hallucination_rate（NULL 时补算）
  6. 用公式：score = -iterations - 5*hallucination_rate - 0.001*latency_ms
     算两版的均分
  7. delta = new_avg - old_avg
     delta < 0 → activate_prompt_version(v_old) + invalidate_prompt_cache
     delta ≥ 0 → 保留 v_new
  8. agent_c_runs 落盘 + heartbeat

不调 Gemini —— 全部基于 trace 已有数据 + KB 反查，零成本。
"""
import asyncio
import json
import re

from redis import Redis

from settings import settings, logger
from .db import (
    activate_prompt_version,
    create_agent_c_run,
    fetch_active_prompt_version_id,
    fetch_previous_prompt_version_id,
    fetch_prompt_version_by_id,
    fetch_traces_by_version,
    get_message_content,
    get_subsystem_status,
    heartbeat_subsystem,
    update_agent_c_run,
    update_trace_hallucination_rate,
)
from .agent_chat import PROMPT_NAME, invalidate_prompt_cache


# 答案文本中内联引用的正则：（来源：文件名，第N段）
# 同时兼容全角和半角括号、冒号、逗号
_CITE_RE = re.compile(r'[（(]来源[：:](.+?)[，,]第\s*(\d+)\s*段[）)]')
# read_document 结果的文件名提取
_READ_DOC_RE = re.compile(r'^\[(.+?)\s+全文\]')


# ── Redis 锁 ────────────────────────────────────────────────────────────────

_LOCK_KEY = "agent_c:lock"
_LOCK_TTL = 600


def _redis() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def _try_acquire_lock() -> bool:
    return bool(_redis().set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL))


def _release_lock() -> None:
    try:
        _redis().delete(_LOCK_KEY)
    except Exception as e:
        logger.warning("[agent_c] release_lock failed: %s", e)


# ── Hallucination 检测（answer 内联引用 vs. 实际 tool 结果交叉验证）─────────

async def _compute_hallucination_rate(trace: dict) -> float:
    """
    真实幻觉检测：answer 文本里标注的 （来源：X，第N段） 是否真的来自本轮工具调用。

    逻辑：
    1. retrieved_pairs = trace["citations"] 中所有 (source, chunk)（search_kb 实际返回的）
    2. files_fully_read = read_document 调用过的文件名（全文可信，不限 chunk）
    3. answer 文本 → 正则抽取内联引用 → 对照 retrieved_pairs / files_fully_read
    4. 出现在 answer 但不在工具结果里 → 幻觉
    """
    # ── 1. 已检索到的 (source, chunk) 集合 ──────────────────────────────────
    citations = trace.get("citations") or []
    if isinstance(citations, str):
        try:
            citations = json.loads(citations)
        except json.JSONDecodeError:
            citations = []

    retrieved_pairs: set[tuple[str, int]] = set()
    for c in citations:
        src = c.get("source")
        chk = c.get("chunk")
        if src is not None and chk is not None:
            retrieved_pairs.add((src, int(chk)))

    # ── 2. 通过 read_document 完整读取的文件（该文件所有 chunk 引用均合法）──
    tools_called = trace.get("tools_called") or []
    if isinstance(tools_called, str):
        try:
            tools_called = json.loads(tools_called)
        except json.JSONDecodeError:
            tools_called = []

    files_fully_read: set[str] = set()
    for call in tools_called:
        if call.get("tool") == "read_document":
            m = _READ_DOC_RE.match(call.get("result_preview", ""))
            if m:
                files_fully_read.add(m.group(1).strip())

    # 没有任何 KB 访问 → 幻觉检测不适用
    if not retrieved_pairs and not files_fully_read:
        return 0.0

    # ── 3. 取 answer 文本 ────────────────────────────────────────────────────
    message_id = trace.get("message_id")
    if not message_id:
        return 0.0

    answer = await get_message_content(message_id)
    if not answer:
        return 0.0

    # ── 4. 解析 answer 中的内联 KB 引用 ─────────────────────────────────────
    cited_pairs = [
        (m.group(1).strip(), int(m.group(2)))
        for m in _CITE_RE.finditer(answer)
    ]
    if not cited_pairs:
        return 0.0

    fake = sum(
        1 for src, chk in cited_pairs
        if (src, chk) not in retrieved_pairs and src not in files_fully_read
    )
    return fake / len(cited_pairs)


async def _backfill_hallucination_rates(traces: list[dict]) -> None:
    """对 hallucination_rate IS NULL 的 trace 补算并写入。"""
    for t in traces:
        if t.get("hallucination_rate") is not None:
            continue
        rate = await _compute_hallucination_rate(t)
        await update_trace_hallucination_rate(t["id"], rate)
        t["hallucination_rate"] = rate


# ── 评分公式 ───────────────────────────────────────────────────────────────

def _score(trace: dict) -> float:
    """score = -iterations - 5*hallucination_rate - 0.001*latency_ms"""
    iter_ = trace.get("iterations") or 1
    halluc = trace.get("hallucination_rate") or 0.0
    ms = trace.get("duration_ms") or 0
    return -float(iter_) - 5.0 * float(halluc) - 0.001 * float(ms)


def _avg_score(traces: list[dict]) -> float:
    if not traces:
        return float("-inf")
    return sum(_score(t) for t in traces) / len(traces)


# ── 主入口 ─────────────────────────────────────────────────────────────────

async def run_agent_c_verification() -> dict:
    """Agent C 一次完整运行。"""
    if not _try_acquire_lock():
        logger.info("[agent_c] another instance running, skipping")
        return {"skipped": "lock_held"}

    run_id: int | None = None
    try:
        # 1. 启停门控
        status = await get_subsystem_status("agent_c")
        if not status or not status["enabled"]:
            logger.info("[agent_c] subsystem disabled, skipping")
            return {"skipped": "disabled"}

        # 2. 当前 active 版本
        active_id = await fetch_active_prompt_version_id(PROMPT_NAME)
        if active_id is None:
            return {"skipped": "no_active_version"}
        active_meta = await fetch_prompt_version_by_id(active_id)
        if active_meta is None:
            return {"skipped": "active_version_not_found"}

        # 3. 仅验证 agent_b 应用的版本
        if active_meta["created_by"] != "agent_b":
            msg = f"active v{active_meta['version']} created_by={active_meta['created_by']!r}, not agent_b"
            logger.info("[agent_c] %s, skipping", msg)
            return {"skipped": msg}

        # 4. 前一个版本
        old_id = await fetch_previous_prompt_version_id(PROMPT_NAME, active_id)
        if old_id is None:
            return {"skipped": "no_previous_version"}

        run_id = await create_agent_c_run(new_version_id=active_id, old_version_id=old_id)

        # 5. 拉两侧 trace
        new_traces = await fetch_traces_by_version(active_id, route="agent")
        old_traces = await fetch_traces_by_version(old_id, route="agent")

        new_n = len(new_traces)
        old_n = len(old_traces)

        if new_n < settings.agent_c_min_traces:
            msg = f"v_new traces={new_n} < {settings.agent_c_min_traces}"
            await update_agent_c_run(
                run_id,
                new_traces_count=new_n, old_traces_count=old_n,
                decision="insufficient_data", decision_reason=msg,
                finished=True,
            )
            logger.info("[agent_c] %s, skipping", msg)
            return {"skipped": msg, "run_id": run_id}

        if old_n < settings.agent_c_min_traces:
            msg = f"v_old traces={old_n} < {settings.agent_c_min_traces}"
            await update_agent_c_run(
                run_id,
                new_traces_count=new_n, old_traces_count=old_n,
                decision="insufficient_data", decision_reason=msg,
                finished=True,
            )
            logger.info("[agent_c] %s, skipping", msg)
            return {"skipped": msg, "run_id": run_id}

        # 6. 补算 hallucination_rate
        logger.info(
            "[agent_c] backfilling hallucination_rate (v_new=%d traces, v_old=%d traces)",
            new_n, old_n,
        )
        await _backfill_hallucination_rates(new_traces)
        await _backfill_hallucination_rates(old_traces)

        # 7. 算分
        new_avg = _avg_score(new_traces)
        old_avg = _avg_score(old_traces)
        delta = new_avg - old_avg

        # 8. 决策 & 执行
        if delta < 0:
            ok = await activate_prompt_version(old_id)
            if not ok:
                msg = f"rollback failed: activate_prompt_version({old_id}) returned false"
                await update_agent_c_run(
                    run_id,
                    new_traces_count=new_n, old_traces_count=old_n,
                    new_avg_score=new_avg, old_avg_score=old_avg, score_delta=delta,
                    decision="error", error_message=msg,
                    finished=True,
                )
                return {"error": msg, "run_id": run_id}
            invalidate_prompt_cache()
            decision = "rolled_back"
            reason = (
                f"v_new score {new_avg:+.3f} < v_old score {old_avg:+.3f} "
                f"(delta={delta:+.3f}); rolled back to v_old"
            )
        else:
            decision = "kept"
            reason = (
                f"v_new score {new_avg:+.3f} >= v_old score {old_avg:+.3f} "
                f"(delta={delta:+.3f}); keeping v_new"
            )

        await update_agent_c_run(
            run_id,
            new_traces_count=new_n, old_traces_count=old_n,
            new_avg_score=new_avg, old_avg_score=old_avg, score_delta=delta,
            decision=decision, decision_reason=reason,
            finished=True,
        )
        await heartbeat_subsystem("agent_c", f"{decision}: delta={delta:+.3f}")
        logger.info("[agent_c] %s — %s", decision.upper(), reason)

        return {
            "run_id": run_id,
            "decision": decision,
            "new_avg_score": new_avg,
            "old_avg_score": old_avg,
            "delta": delta,
            "new_traces_count": new_n,
            "old_traces_count": old_n,
        }

    except Exception as e:
        logger.exception("[agent_c] unexpected failure")
        if run_id is not None:
            try:
                await update_agent_c_run(
                    run_id, error_message=str(e)[:500], finished=True,
                )
            except Exception:
                pass
        return {"error": str(e), "run_id": run_id}
    finally:
        _release_lock()
