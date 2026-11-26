import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import writing
from backend import writing_chat as chat


class WritingChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = patch.multiple(chat.settings, codex_api_key="test-key",
                                     codex_base_url="https://relay.example/v1",
                                     codex_model="gpt-6.1-sol",
                                     generation_model="gemini-3-flash-preview")
        self.config.start()
        self.addCleanup(self.config.stop)

    def response(self, content="answer", finish="stop", calls=None):
        return httpx.Response(200, request=httpx.Request("POST", "https://relay.example"),
                              json={"choices": [{"finish_reason": finish, "message": {
                                  "role": "assistant", "content": content, "tool_calls": calls}}]})

    def mock_http(self, responses):
        context = unittest.mock.MagicMock()
        hc = SimpleNamespace(post=AsyncMock(side_effect=responses))
        context.__aenter__ = AsyncMock(return_value=hc)
        context.__aexit__ = AsyncMock(return_value=False)
        return context, hc

    async def test_primary_preserves_context_and_high_reasoning(self):
        context, hc = self.mock_http([self.response("[SECTION_UPDATE_START]正文[SECTION_UPDATE_END]")])
        with patch.object(chat.httpx, "AsyncClient", return_value=context), \
             patch.object(chat.client.aio.models, "generate_content", new_callable=AsyncMock) as fallback:
            answer = await chat.generate_writing_chat_reply("资料片段及历史", "先确认再修改")
        body = hc.post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "gpt-6.1-sol")
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(body["messages"][1]["content"], "资料片段及历史")
        self.assertIn("先确认再修改", body["messages"][0]["content"])
        self.assertIn("[SECTION_UPDATE_START]", answer)
        fallback.assert_not_awaited()

    async def test_search_results_returned_to_model(self):
        call = {"id": "search-1", "type": "function", "function": {
            "name": "web_search", "arguments": json.dumps({"query": "测试查询"})}}
        context, hc = self.mock_http([self.response(None, "tool_calls", [call]), self.response("根据搜索结果回答")])
        with patch.object(chat.httpx, "AsyncClient", return_value=context), \
             patch.object(chat, "fetch_from_web", new_callable=AsyncMock, return_value="来源链接及摘要") as search:
            answer = await chat._codex_reply("问题", "规则")
        search.assert_awaited_once_with("测试查询")
        messages = hc.post.call_args.kwargs["json"]["messages"]
        self.assertEqual(messages[-1], {"role": "tool", "tool_call_id": "search-1", "content": "来源链接及摘要"})
        self.assertEqual(answer, "根据搜索结果回答")

    async def test_failures_fall_back_with_same_rag_and_rules(self):
        bad_status = httpx.Response(503, request=httpx.Request("POST", "https://relay.example"))
        for response in [bad_status, self.response(""), self.response("不完整修改", "length"),
                         httpx.ReadTimeout("timeout")]:
            with self.subTest(response=type(response).__name__):
                context, _ = self.mock_http([response])
                with patch.object(chat.httpx, "AsyncClient", return_value=context), \
                     patch.object(chat.client.aio.models, "generate_content", new_callable=AsyncMock,
                                  return_value=SimpleNamespace(text="Gemini 答复")) as fallback:
                    self.assertEqual(await chat.generate_writing_chat_reply("RAG 资料与历史", "确认规则"), "Gemini 答复")
                kwargs = fallback.call_args.kwargs
                self.assertEqual(kwargs["contents"], "RAG 资料与历史")
                self.assertEqual(kwargs["model"], "gemini-3-flash-preview")
                self.assertEqual(kwargs["config"].system_instruction, "确认规则")
                self.assertTrue(kwargs["config"].tools)

    async def test_missing_config_falls_back(self):
        with patch.object(chat.settings, "codex_api_key", None), \
             patch.object(chat.client.aio.models, "generate_content", new_callable=AsyncMock,
                          return_value=SimpleNamespace(text="fallback")):
            self.assertEqual(await chat.generate_writing_chat_reply("p", "s"), "fallback")

    async def test_both_providers_fail(self):
        with patch.object(chat, "_codex_reply", new_callable=AsyncMock, side_effect=ValueError), \
             patch.object(chat.client.aio.models, "generate_content", new_callable=AsyncMock, side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                await chat.generate_writing_chat_reply("p", "s")


class RouteRagTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_three_chat_scopes_keep_user_filtered_rag_and_history(self):
        task = {"session_id": "writing-session", "reference_files": ["ref.pdf"], "title": "任务",
                "style_req": "风格", "content_req": "要求", "word_count": 2000, "outline": "大纲"}
        section = {"heading": "章节", "content": "原正文", "sub_outline": "细纲"}
        from contextlib import ExitStack
        for route in (writing.writing_task_chat, writing.section_chat, writing.section_image_chat):
            with self.subTest(route=route.__name__), ExitStack() as stack:
                def mock(name, result=None):
                    return stack.enter_context(patch.object(writing, name, new_callable=AsyncMock, return_value=result))
                owner = mock("_ensure_task_owner")
                mock("get_writing_task", task)
                mock("get_writing_content", {"content": "全文"})
                mock("get_writing_section", section)
                embedding = mock("get_embedding", [0.1, 0.2])
                rag = mock("query_rag_by_files", [{"content": "仅此用户可见的参考资料片段"}])
                save = mock("save_message")
                mock("get_context", [{"role": "assistant", "content": "上轮征询修改确认"}])
                generate = mock("generate_writing_chat_reply", "讨论答复")
                kwargs = {"task_id": "task", "message": "同意", "user": {"id": 42}}
                if route != writing.writing_task_chat:
                    kwargs["section_id"] = "section"
                if route == writing.section_image_chat:
                    kwargs.update(marker_text="配图位置", current_prompt="图片草稿")
                self.assertEqual(await route(**kwargs), {"answer": "讨论答复"})
                owner.assert_awaited_once_with("task", 42)
                embedding.assert_awaited_once()
                rag.assert_awaited_once_with([0.1, 0.2], 42, ["ref.pdf"])
                prompt, rules = generate.call_args.args
                self.assertIn("仅此用户可见的参考资料片段", prompt)
                self.assertIn("上轮征询修改确认", prompt)
                self.assertIn("获得同意后才可执行", rules)
                self.assertEqual(save.await_count, 2)


if __name__ == "__main__":
    unittest.main()
