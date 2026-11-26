"""Writing-panel chat providers; RAG and persistence stay in the route layer."""
import asyncio
import json

import httpx
from google.genai import types as gtypes

from settings import client, logger, settings
from midware.tools import fetch_from_web


_WEB_SEARCH = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "搜索互联网以核实最新信息，返回标题、摘要和来源链接。",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


async def _codex_reply(prompt: str, system_instruction: str) -> str:
    if not all((settings.codex_api_key, settings.codex_base_url, settings.codex_model)):
        raise ValueError("Writing chat provider is not configured")
    messages = [
        {"role": "system", "content": system_instruction +
         "\n需要联网核实时调用 web_search；搜索无结果时说明无法核实，不编造来源。"},
        {"role": "user", "content": prompt},
    ]
    # Bound the entire tool loop, leaving time for fallback within the 360s proxy timeout.
    async with asyncio.timeout(180), httpx.AsyncClient(
        timeout=httpx.Timeout(120, connect=15)
    ) as hc:
        for turn in range(4):
            body = {
                "model": settings.codex_model,
                "reasoning_effort": "high",
                "messages": messages,
                "max_completion_tokens": 65536,
            }
            if turn < 3:
                body["tools"] = [_WEB_SEARCH]
            resp = await hc.post(
                settings.codex_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {settings.codex_api_key}"},
                json=body,
            )
            resp.raise_for_status()
            choice = resp.json()["choices"][0]
            message = choice["message"]
            calls = message.get("tool_calls") or []
            if calls:
                if turn == 3 or len(calls) > 5:
                    raise ValueError("Writing chat exceeded search budget")
                messages.append({"role": "assistant", "content": message.get("content"),
                                 "tool_calls": calls})
                for call in calls:
                    fn = call["function"]
                    args = json.loads(fn["arguments"])
                    query = args.get("query")
                    if fn["name"] != "web_search" or not isinstance(query, str) or not query.strip():
                        raise ValueError("Invalid writing chat search call")
                    result = await fetch_from_web(query)
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": result or "搜索无结果或暂不可用，请明确告知无法核实。"})
                continue
            answer = message.get("content")
            if choice.get("finish_reason") != "stop" or not isinstance(answer, str) or not answer.strip():
                # Never apply a truncated edit to an article.
                raise ValueError("Incomplete writing chat response")
            return answer
    raise ValueError("Writing chat returned no answer")


async def generate_writing_chat_reply(prompt: str, system_instruction: str) -> str:
    try:
        return await _codex_reply(prompt, system_instruction)
    except Exception as exc:
        # Do not log exception bodies/URLs: relays may echo credentials or article content.
        logger.warning("写作对话首选模型失败（%s），回退 Gemini", type(exc).__name__)
    async with asyncio.timeout(120):
        resp = await client.aio.models.generate_content(
            model=settings.generation_model,
            contents=prompt,
            config=gtypes.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=[gtypes.Tool(google_search=gtypes.GoogleSearch())],
                max_output_tokens=65536,
            ),
        )
        if not resp.text or not resp.text.strip():
            raise ValueError("Empty Gemini writing chat response")
        return resp.text
