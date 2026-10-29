"""Real HTTP contracts over an isolated Store; cloud calls are replaced explicitly."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.core import Store
from app.engine import Engine


class FakeProvider:
    async def research(self, topic, job):
        return {"document": topic["document"], "sources": topic["sources"],
                "source_provenance": "user", "summary": "没有新增事实", "reflection": "只用于合同测试"}

    async def stream_chat(self, topic, text, history, report=False):
        self.received_history = history
        self.received_topic = topic
        yield "当前重点是保留问题结构。"
        yield "这是一条测试回复。"


class APIContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = (main.store, main.provider, main.engine)
        main.store = Store(Path(self.temp.name) / "test.sqlite3")
        main.provider = FakeProvider()
        main.engine = Engine(main.store, main.provider.research, settings={"tick_seconds": 0.02})
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        main.store.close()
        main.store, main.provider, main.engine = self.saved
        self.temp.cleanup()

    def make_topic(self):
        response = self.client.post("/api/topics", json={"title": "长期问题", "goal": "让新发现回到原结构"})
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_correction_is_version_checked_and_export_has_context(self):
        t = self.make_topic()
        r = self.client.post(f"/api/topics/{t['id']}/correction", json={"text": "保留另一个分支", "version": 1})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["version"], 2)
        old = self.client.post(f"/api/topics/{t['id']}/correction", json={"text": "旧窗口覆盖", "version": 1})
        self.assertEqual(old.status_code, 409)
        exported = self.client.get(f"/api/topics/{t['id']}/export")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("保留另一个分支", exported.text)
        self.assertIn("attachment", exported.headers["content-disposition"])

    def test_chat_generated_is_not_claimed_to_have_been_heard(self):
        t = self.make_topic()
        r = self.client.post(f"/api/topics/{t['id']}/chat", json={"text": "请汇报", "report": True})
        self.assertIn('"type": "delta"', r.text)
        events = main.store.list_events(t["id"])
        answer = next(e for e in events if e["kind"] == "assistant_message")
        self.assertTrue(answer["data"]["complete"])
        self.assertFalse(any(e["kind"] == "playback" for e in events))
        self.client.post(f"/api/topics/{t['id']}/playback", json={"report_id": answer['data']['report_id'], "text": "已听完第一段", "complete": True})
        self.assertEqual(main.store.list_events(t["id"])[0]["kind"], "playback")

    def test_cross_origin_and_private_paths_rejected(self):
        r = self.client.post("/api/topics", headers={"Origin": "https://other.example"}, json={"title": "bad"})
        self.assertEqual(r.status_code, 403)
        for path in ("/.env", "/data/workspace.sqlite3", "/static/../config.py", "/api/key"):
            self.assertEqual(self.client.get(path).status_code, 404)
        status = self.client.get("/api/status").json()
        self.assertNotIn("api_key", status)

    def test_pause_persists_and_research_rejects_paused_topic(self):
        t = self.make_topic()
        self.client.patch(f"/api/topics/{t['id']}", json={"paused": True})
        r = self.client.post(f"/api/topics/{t['id']}/research")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(self.client.get(f"/api/topics/{t['id']}").json()["topic"]["paused"])


    def test_interrupted_voice_report_does_not_put_unheard_points_in_normal_history(self):
        t = self.make_topic()
        main.store.add_event(t["id"], "assistant_message", {"text": "第一点。第二点。第三点。", "report_id": "r1", "voice_mode": True})
        self.client.post(f"/api/topics/{t['id']}/playback", json={"report_id": "r1", "segment_id": "s1", "text": "第一点。", "complete": True, "played_ms": 900})
        self.client.post(f"/api/topics/{t['id']}/playback", json={"report_id": "r1", "segment_id": "s2", "text": "第二点。", "complete": False, "played_ms": 200})
        self.client.post(f"/api/topics/{t['id']}/chat", json={"text": "接着说", "voice_mode": True})
        history = json.dumps(main.provider.received_history, ensure_ascii=False)
        self.assertIn("第一点", history)
        self.assertIn("不能假设完整听到", history)
        self.assertNotIn("第三点", history)
        unheard = main.provider.received_topic["document"]["unheard_reports"]
        self.assertIn("第三点", unheard[0]["generated_text"])

    def test_adjacent_user_utterances_coalesce_into_one_delayed_organize_job(self):
        t = self.make_topic()
        for text in ("我还没想清楚", "我想保留两个分支"):
            self.client.post(f"/api/topics/{t['id']}/chat", json={"text": text})
        jobs = [j for j in main.store.list_jobs(t["id"]) if j["kind"] == "organize"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "queued")
        data = self.client.get(f"/api/topics/{t['id']}").json()
        self.assertEqual([x["text"] for x in data["pending_inputs"]], ["我还没想清楚", "我想保留两个分支"])


if __name__ == "__main__":
    unittest.main()
