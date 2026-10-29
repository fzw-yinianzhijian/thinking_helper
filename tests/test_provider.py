"""Research gates distinguish a completed model call from useful progress."""
import copy
import json
import tempfile
import unittest
import httpx
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.core import Store, LeaseLost
from app.providers import Provider


class ResearchQualityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.db")
        self.source = {"id": "src-abc", "url": "https://docs.example.org/schedules", "title": "Schedules",
                       "excerpt": "Cloud schedules run server-side. Local schedules require a live process.",
                       "provenance": "fetched_page"}
        self.topic = self.store.create_topic("持久后台", sources=[self.source], document={
            "summary": "查证运行条件", "claims": [], "questions": [{"id": "q1", "text": "后台在哪里运行？", "status": "open"}], "next_steps": []})
        queued = self.store.enqueue_job(self.topic["id"])
        self.job = self.store.claim_job("test-owner")
        self.provider = Provider(Settings(api_key="test-key"), self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def result(self, claims):
        doc = copy.deepcopy(self.topic["document"])
        doc["claims"] = claims
        return json.dumps({"document": doc, "summary": "候选", "reflection": "仍需核查",
                           "progress_kind": "evidence", "addresses_question_ids": ["q1"], "input_event_ids": []})

    async def test_plan_only_output_is_no_increment_and_does_not_change_document(self):
        self.provider.completion = AsyncMock(return_value=self.result([]))
        with patch("app.providers.fetch_evidence", AsyncMock(return_value={"sources": [{**self.source, "verified_content": True}], "errors": []})):
            proposal = await self.provider.research(self.topic, self.job)
        self.assertEqual(proposal["document"], self.topic["document"])
        self.assertEqual(proposal["summary"], "本轮没有可整合的新依据")
        self.assertTrue(any(e["kind"] == "research.no_increment" for e in self.store.list_events(self.topic["id"])))

    async def test_read_excerpt_and_real_source_id_required_for_increment(self):
        claim = {"id": "c1", "text": "云调度在服务端运行，本地调度需要进程存活。", "status": "supported", "source_ids": ["src-abc"], "question_ids": ["q1"],
                 "evidence_quotes": [{"source_id": "src-abc", "quote": "Cloud schedules run server-side."}]}
        self.provider.completion = AsyncMock(return_value=self.result([claim]))
        with patch("app.providers.fetch_evidence", AsyncMock(return_value={"sources": [{**self.source, "verified_content": True}], "errors": []})):
            proposal = await self.provider.research(self.topic, self.job)
        self.assertEqual(proposal["document"]["claims"], [claim])
        self.assertIn("search_checkpoint", self.store.get_job(self.job["id"])["payload"])
        sent = self.provider.completion.call_args.args[0][-1]["content"]
        self.assertIn("Cloud schedules run server-side", sent)

    async def test_checkpoint_reuse_avoids_retrieval_and_cancel_fences_before_paid_call(self):
        checkpoint = {"notes": "已获取正文", "sources": [{**self.source, "verified_content": True}]}
        self.store.checkpoint_job(self.job["id"], {"search_checkpoint": checkpoint}, owner="test-owner")
        self.job = self.store.get_job(self.job["id"])
        self.store.cancel_job(self.job["id"])
        self.provider.completion = AsyncMock()
        with patch("app.providers.fetch_evidence", AsyncMock()) as fetch:
            with self.assertRaises(LeaseLost):
                await self.provider.research(self.topic, self.job)
            fetch.assert_not_awaited()
        self.provider.completion.assert_not_awaited()

    async def test_missing_readable_page_never_asks_model_to_invent_evidence(self):
        self.provider.completion = AsyncMock()
        with patch("app.providers.fetch_evidence", AsyncMock(return_value={"sources": [], "errors": [{"reason": "timeout"}]})):
            with self.assertRaisesRegex(RuntimeError, "没有取得"):
                await self.provider.research(self.topic, self.job)
        self.provider.completion.assert_not_awaited()

    async def test_citation_id_without_matching_original_quote_is_not_evidence(self):
        claim = {"id": "c1", "text": "本地调度无需常驻。", "status": "supported", "source_ids": ["src-abc"],
                 "question_ids": ["q1"], "evidence_quotes": [{"source_id": "src-abc", "quote": "A fabricated passage from the model."}]}
        self.provider.completion = AsyncMock(return_value=self.result([claim]))
        with patch("app.providers.fetch_evidence", AsyncMock(return_value={"sources": [{**self.source, "verified_content": True}], "errors": []})):
            proposal = await self.provider.research(self.topic, self.job)
        self.assertEqual(proposal["document"], self.topic["document"])
        self.assertIn("原文定位", proposal["reflection"])

    async def test_existing_read_sources_do_not_prevent_new_search_and_question_rotation(self):
        topic = copy.deepcopy(self.topic)
        topic["sources"][0]["verified_content"] = True
        topic["document"]["questions"].append({"id": "q2", "text": "语音何时让出话轮？", "status": "open"})
        self.store.add_event(topic["id"], "research.focus", {"job_id": "previous-job", "question_id": "q1"})
        self.provider._post = AsyncMock(return_value={"output": {"choices": [{"message": {"content": "新线索"}}],
                                  "search_info": {"search_results": [{"url": "https://docs.example.org/new"}]}}})
        self.provider.completion = AsyncMock(return_value=self.result([]))
        with patch("app.providers.fetch_evidence", AsyncMock(return_value={"sources": [{**self.source, "verified_content": True}], "errors": []})) as fetch:
            await self.provider.research(topic, self.job)
        self.assertEqual(fetch.call_args.args[0][0], "https://docs.example.org/new")
        search_text = self.provider._post.call_args.args[1]["input"]["messages"][0]["content"]
        self.assertIn("语音何时让出话轮", search_text)

    async def test_ordinary_user_input_becomes_reviewable_question_without_web_search(self):
        event = self.store.add_event(self.topic["id"], "user_message", {"text": "我还关心怎样拒绝更新而不丢掉原话", "report": False})
        event_id = event["id"] if isinstance(event, dict) else event
        doc = copy.deepcopy(self.topic["document"])
        doc["questions"].append({"id": "q-reject", "text": "拒绝更新后如何保留原话？", "status": "open"})
        self.provider.completion = AsyncMock(return_value=json.dumps({"summary": "接住新分支", "document": doc,
            "progress_kind": "reframe", "addresses_question_ids": ["q-reject"], "input_event_ids": [event_id],
            "unconfirmed": True, "new_questions": [{"id": "q-reject", "text": "拒绝更新后如何保留原话？", "input_event_ids": [event_id]}]}))
        organize_job = {**self.job, "kind": "organize"}
        with patch("app.providers.fetch_evidence", AsyncMock()) as fetch:
            proposal = await self.provider.research(self.topic, organize_job)
        fetch.assert_not_awaited()
        self.assertTrue(proposal["requires_review"])
        self.assertEqual(proposal["input_event_ids"], [event_id])
        self.store.update_topic(self.topic["id"], auto_apply=True)
        candidate = self.store.finish_job(self.job["id"], "test-owner", proposal)
        self.assertEqual(candidate["status"], "pending")
        self.assertEqual(self.store.get_topic(self.topic["id"])["version"], 1)
        self.assertIn("未确认", candidate["review_reason"])

    async def test_rejection_reason_reaches_next_research_instead_of_being_dropped(self):
        self.store.add_event(self.topic["id"], "candidate.rejected", {"candidate_id": "old", "reason": "来源无关，请查原始技术文档"})
        self.provider.completion = AsyncMock(return_value=self.result([]))
        with patch("app.providers.fetch_evidence", AsyncMock(return_value={"sources": [{**self.source, "verified_content": True}], "errors": []})):
            await self.provider.research(self.topic, self.job)
        sent = self.provider.completion.call_args.args[0][-1]["content"]
        self.assertIn("来源无关，请查原始技术文档", sent)

    async def test_method_proposal_cannot_self_declare_implementation_or_passed_evaluation(self):
        event = self.store.add_event(self.topic["id"], "interaction.backchannel", {"text": "嗯"})
        event_id = event["id"] if isinstance(event, dict) else event
        self.provider.completion = AsyncMock(return_value=json.dumps({"summary": "调整附和处理", "proposal": {
            "module": "interaction", "problem": "附和需要区分", "evidence_event_ids": [event_id]},
            "evaluation": {"status": "passed", "runtime_code_changed": True}}))
        proposal = await self.provider.research(self.topic, {**self.job, "kind": "selfupdate"})
        self.assertEqual(proposal["evaluation"]["status"], "not_run")
        self.assertFalse(proposal["evaluation"]["runtime_code_changed"])
        self.assertTrue(proposal["runtime_revision"])


    async def test_oral_report_excludes_old_assistant_claims_and_speculative_plan(self):
        topic = copy.deepcopy(self.topic)
        topic["document"]["summary"] = "Letta已经接入"
        topic["document"]["next_steps"] = ["修复尚未存在的MemFS同步"]
        captured = {}
        async def handler(request):
            captured.update(json.loads(request.content))
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"只按真实状态汇报。"}}]}\n\ndata: [DONE]\n\n', headers={"Content-Type":"text/event-stream"})
        original = httpx.AsyncClient
        self.provider._reserve = lambda: None
        with patch("app.providers.httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(handler))):
            result = [x async for x in self.provider.stream_chat(topic,"汇报",[{"role":"assistant","content":"旧错误：MemFS已部署"}],report=True)]
        self.assertEqual(result,["只按真实状态汇报。"])
        sent=json.dumps(captured,ensure_ascii=False)
        self.assertNotIn("旧错误：MemFS已部署",sent)
        self.assertNotIn("修复尚未存在的MemFS同步",sent)
        self.assertNotIn("Letta已经接入",sent)
        self.assertIn("外部调研对象",sent)


    async def test_growing_archive_does_not_resend_every_historical_page(self):
        topic=copy.deepcopy(self.topic)
        topic["sources"] += [{"id":f"s-{i}","url":f"https://docs.example.org/{i}","excerpt":"OLD_PAGE_ARCHIVE"*1000,"verified_content":True} for i in range(100)]
        checkpoint={"notes":"本轮正文","sources":[{**self.source,"verified_content":True}]}
        self.store.checkpoint_job(self.job["id"],{"search_checkpoint":checkpoint},owner="test-owner")
        job=self.store.get_job(self.job["id"])
        self.provider.completion=AsyncMock(return_value=self.result([]))
        await self.provider.research(topic,job)
        sent=self.provider.completion.call_args.args[0][-1]["content"]
        self.assertNotIn("OLD_PAGE_ARCHIVE",sent)
        self.assertLess(len(sent),40000)
        self.assertIn("Cloud schedules run server-side",sent)


if __name__ == "__main__":
    unittest.main()
