import asyncio
import copy
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.core import BudgetExceeded, LeaseLost, Store
from app.engine import Engine


def proposal(summary="A useful change"):
    return {"summary": summary, "reflection": "Needs further checking", "document": {
        "summary": summary, "claims": [], "questions": ["What would disprove this?"],
        "next_steps": ["Check one primary source"],
    }, "sources": []}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "workspace.sqlite3"
        self.store = Store(self.path, daily_call_limit=3)
        self.topic = self.store.create_topic("A long-term question", "Learn with evidence")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def claimed(self, *, kind="research", owner="worker"):
        job = self.store.enqueue_job(self.topic["id"], kind)
        claimed = self.store.claim_job(owner)
        self.assertEqual(claimed["id"], job["id"])
        return claimed

    def terminal_observation(self, kind, status, when):
        with patch("app.core.time.time", return_value=when):
            job = self.store.enqueue_job(self.topic["id"], kind)
            claimed = self.store.claim_job("observation-worker")
            self.assertEqual(claimed["id"], job["id"])
            if status == "failed":
                self.store.fail_job(job["id"], "observation-worker", "Observed bounded failure", max_attempts=1)
            else:
                value = proposal(f"Observation {kind} at {when}") if status == "completed" else {"document": self.store.get_topic(self.topic["id"])["document"], "sources": []}
                self.store.finish_job(job["id"], "observation-worker", value)
        self.assertEqual(self.store.get_job(job["id"])["status"], status)
        return job["id"]

    def test_reflection_requires_three_terminal_observations_not_cancellations_or_stale_work(self):
        base = time.time() + 5
        observed = [self.terminal_observation("research", "completed", base + 1),
                    self.terminal_observation("organize", "noop", base + 2)]
        with patch("app.core.time.time", return_value=base + 3):
            cancelled = self.store.enqueue_job(self.topic["id"])
            self.store.cancel_job(cancelled["id"])
            obsolete = self.store.enqueue_job(self.topic["id"], "organize")
            self.store.claim_job("old-work")
            self.store.correct_topic(self.topic["id"], {"summary": "User correction"})
            self.store.finish_job(obsolete["id"], "old-work", proposal("Late result"))
        self.assertEqual(self.store.schedule_reflections(now=base + 4), [])
        observed.append(self.terminal_observation("organize", "failed", base + 5))
        scheduled = self.store.schedule_reflections(now=base + 6)
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0]["kind"], "selfupdate")
        self.assertEqual(set(scheduled[0]["payload"]["observation_job_ids"]), set(observed))

    def test_reflection_scheduling_is_exclusive_across_connections(self):
        base = time.time() + 5
        for offset in range(3):
            self.terminal_observation("research", "noop", base + offset)
        other = Store(self.path)
        barrier = threading.Barrier(2)
        def schedule(store):
            barrier.wait()
            return store.schedule_reflections(now=base + 5)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(schedule, store) for store in (self.store, other)]
                self.assertEqual(sum(len(future.result()) for future in futures), 1)
            self.assertEqual(len([job for job in self.store.list_jobs() if job["kind"] == "selfupdate"]), 1)
        finally:
            other.close()

    def test_reflection_cooldown_and_pending_proposal_block_but_empty_proposal_does_not(self):
        base = time.time() + 5
        for offset in range(3):
            self.terminal_observation("research", "noop", base + offset)
        first = self.store.schedule_reflections(now=base + 10)[0]
        self.store.claim_job("method-review", now=base + 10)
        candidate = self.store.finish_job(first["id"], "method-review", {"summary": "Review repeated noops", "proposal": {"module": "background", "change": "Compare the next selected question"}}, now=base + 11)
        for offset in range(3):
            self.terminal_observation("organize", "noop", base + 20 + offset)
        # Even after the three-hour cooldown, an actual pending proposal blocks.
        self.assertEqual(self.store.schedule_reflections(now=base + 10811), [])
        self.store.reject_candidate(candidate["id"], "Keep observing")
        # Removing the pending proposal does not bypass the cooldown.
        self.assertEqual(self.store.schedule_reflections(now=base + 30), [])
        second = self.store.schedule_reflections(now=base + 10811)[0]
        self.store.claim_job("empty-review", now=base + 10811)
        self.store.finish_job(second["id"], "empty-review", {"summary": "Not enough evidence", "proposal": None}, now=base + 10812)
        for offset in range(3):
            self.terminal_observation("research", "noop", base + 10820 + offset)
        self.assertEqual(self.store.schedule_reflections(now=base + 10830), [])
        self.assertEqual(len(self.store.schedule_reflections(now=base + 21612)), 1)

    def test_default_review_and_idempotent_apply(self):
        job = self.claimed()
        candidate = self.store.finish_job(job["id"], "worker", proposal())
        self.assertEqual(candidate["status"], "pending")
        self.assertEqual(self.store.get_topic(self.topic["id"])["version"], 1)
        applied = self.store.apply_candidate(candidate["id"])
        self.assertEqual(applied["applied_version"], 2)
        self.store.apply_candidate(candidate["id"])
        self.assertEqual(self.store.get_topic(self.topic["id"])["version"], 2)
        self.assertEqual(len(self.store.list_candidates()), 1)

    def test_explicit_auto_apply(self):
        self.store.update_topic(self.topic["id"], auto_apply=True)
        job = self.claimed()
        result = self.store.finish_job(job["id"], "worker", proposal())
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.store.get_topic(self.topic["id"])["document"], proposal()["document"])

    def test_concurrent_claim_is_exclusive_across_connections(self):
        job = self.store.enqueue_job(self.topic["id"])
        second = Store(self.path)
        barrier = threading.Barrier(2)
        def claim(store, owner):
            barrier.wait()
            return store.claim_job(owner)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(claim, self.store, "one"), pool.submit(claim, second, "two")]
                results = [future.result() for future in futures]
            successful = [item for item in results if item]
            self.assertEqual(len(successful), 1)
            self.assertEqual(successful[0]["id"], job["id"])
            self.assertEqual(successful[0]["attempts"], 1)
        finally:
            second.close()

    def test_repeated_and_concurrent_enqueue_returns_one_active_job(self):
        second = Store(self.path)
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit((self.store if i % 2 else second).enqueue_job, self.topic["id"]) for i in range(12)]
                jobs = [future.result() for future in futures]
            self.assertEqual(len({job["id"] for job in jobs}), 1)
            self.assertEqual(len(self.store.list_jobs()), 1)
            self.store.claim_job("worker")
            self.assertEqual(self.store.enqueue_job(self.topic["id"])["id"], jobs[0]["id"])
        finally:
            second.close()

    def test_running_organize_keeps_one_queued_successor_for_new_utterances(self):
        first = self.store.enqueue_job(self.topic["id"], "organize")
        running = self.store.claim_job("organizer")
        self.assertEqual(first["id"], running["id"])
        self.store.add_event(self.topic["id"], "user_message", {"text": "A new branch arrived during organization"})
        followup = self.store.enqueue_job(self.topic["id"], "organize")
        self.assertNotEqual(followup["id"], running["id"])
        self.assertEqual(followup["status"], "queued")
        self.store.add_event(self.topic["id"], "user_message", {"text": "Another part of that same new branch"})
        coalesced = self.store.enqueue_job(self.topic["id"], "organize")
        self.assertEqual(coalesced["id"], followup["id"])
        self.assertTrue(coalesced["deduplicated"])
        self.assertEqual(len(self.store.list_jobs(self.topic["id"])), 2)
        # A second owner cannot run the successor alongside the current worker.
        self.assertIsNone(self.store.claim_job("other-organizer"))
        self.store.finish_job(running["id"], "organizer", proposal())
        self.assertEqual(self.store.claim_job("next-organizer")["id"], followup["id"])

    def test_running_research_and_selfupdate_still_deduplicate(self):
        for kind in ("research", "selfupdate"):
            with self.subTest(kind=kind):
                topic = self.store.create_topic("Separate topic for " + kind)
                original = self.store.enqueue_job(topic["id"], kind)
                running = self.store.claim_job("one-worker")
                self.assertEqual(running["id"], original["id"])
                repeated = self.store.enqueue_job(topic["id"], kind)
                self.assertEqual(repeated["id"], running["id"])
                self.assertTrue(repeated["deduplicated"])
                self.store.cancel_job(running["id"])

    def test_checkpoint_survives_retry_and_is_fenced_by_owner(self):
        job = self.claimed()
        checkpoint = {"search_checkpoint": {"notes": "A costly search result", "sources": []}}
        saved = self.store.checkpoint_job(job["id"], checkpoint, owner="worker")
        self.assertEqual(saved["payload"]["search_checkpoint"]["notes"], "A costly search result")
        self.store.defer_job(job["id"], "worker", run_at=time.time(), restore_attempt=True)
        self.store.close()
        self.store = Store(self.path)
        retry = self.store.claim_job("replacement")
        self.assertEqual(retry["payload"]["search_checkpoint"], checkpoint["search_checkpoint"])
        with self.assertRaises(LeaseLost):
            self.store.checkpoint_job(job["id"], {"search_checkpoint": {}}, owner="worker")

    def test_checkpoint_after_correction_marks_job_stale(self):
        job = self.claimed()
        self.store.correct_topic(self.topic["id"], {"summary": "New direction"})
        with self.assertRaises(LeaseLost):
            self.store.checkpoint_job(job["id"], {"search_checkpoint": {"notes": "old"}}, owner="worker")
        self.assertEqual(self.store.get_job(job["id"])["status"], "stale")
        self.assertNotIn("search_checkpoint", self.store.get_job(job["id"])["payload"])

    def test_auto_apply_cannot_delete_prior_branches(self):
        initial = {"summary": "An existing idea", "claims": [{"id": "c-old", "text": "A retained possibility", "status": "hypothesis", "source_ids": []}], "questions": [{"id": "q-old", "text": "An unresolved question"}], "next_steps": []}
        self.store.correct_topic(self.topic["id"], initial)
        self.store.update_topic(self.topic["id"], auto_apply=True)
        job = self.claimed()
        result = self.store.finish_job(job["id"], "worker", proposal())
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["review_issues"]["removed_claim_ids"], ["c-old"])
        self.assertEqual(result["review_issues"]["removed_question_ids"], ["q-old"])
        self.assertEqual(self.store.get_topic(self.topic["id"])["document"], initial)
        # An explicit review may still choose this replacement.
        self.assertEqual(self.store.apply_candidate(result["id"])["status"], "applied")

    def test_auto_apply_cannot_rewrite_user_claim_text(self):
        initial = {"summary": "An existing idea", "claims": [{"id": "user-original", "text": "I am uncertain", "status": "hypothesis", "source_ids": []}], "questions": [], "next_steps": []}
        self.store.correct_topic(self.topic["id"], initial)
        self.store.update_topic(self.topic["id"], auto_apply=True)
        job = self.claimed()
        changed = copy.deepcopy(initial)
        changed["claims"][0]["text"] = "I am completely certain"
        result = self.store.finish_job(job["id"], "worker", {"document": changed})
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["review_issues"]["changed_user_claim_ids"], ["user-original"])
        self.assertEqual(self.store.get_topic(self.topic["id"])["document"]["claims"][0]["text"], "I am uncertain")

    def test_auto_apply_cannot_replace_question_text_under_the_same_id(self):
        initial = {"summary": "Keep the user's original branch", "claims": [],
                   "questions": [{"id": "q-original", "text": "How can I pause without being interrupted?", "status": "open"}],
                   "next_steps": []}
        self.store.correct_topic(self.topic["id"], initial)
        self.store.update_topic(self.topic["id"], auto_apply=True)
        evidence = copy.deepcopy(initial)
        evidence["questions"][0]["text"] = "Which unrelated framework should I deploy?"
        evidence["claims"] = [{"id": "c-evidence", "text": "A documented endpoint setting exists.", "status": "supported", "source_ids": ["s-real"]}]
        update = {"document": evidence, "sources": [{"id": "s-real", "url": "https://example.org/endpoint", "title": "Endpoint guide"}],
                  "source_provenance": "verified_search", "progress_kind": "evidence", "requires_review": False}
        job = self.claimed()
        candidate = self.store.finish_job(job["id"], "worker", update)
        self.assertEqual(candidate["status"], "pending")
        self.assertEqual(candidate["review_issues"]["replaced_question_ids"], ["q-original"])
        self.assertEqual(self.store.get_topic(self.topic["id"])["document"], initial)
        self.store.reject_candidate(candidate["id"], "Keep my original question")
        # Resolving a question without rewriting its text keeps the old rule.
        update["document"]["questions"][0] = {**initial["questions"][0], "status": "resolved"}
        next_job = self.claimed()
        accepted = self.store.finish_job(next_job["id"], "worker", update)
        self.assertEqual(accepted["status"], "applied")

    def test_duplicate_or_invalid_question_ids_do_not_advance_the_workspace(self):
        for questions in ([{"id": "same", "text": "First"}, {"id": "same", "text": "Second"}],
                          [{"id": "", "text": "Empty ID"}], [{"id": 4, "text": "Non-string ID"}],
                          [{"text": "Missing ID"}]):
            with self.subTest(questions=questions), self.assertRaises(ValueError):
                self.store.correct_topic(self.topic["id"], {"questions": questions})
        self.assertEqual(self.store.get_topic(self.topic["id"])["version"], 1)
        legacy = self.store.correct_topic(self.topic["id"], {"questions": ["A legacy question without an ID"]})
        self.assertEqual(legacy["document"]["questions"], ["A legacy question without an ID"])

    def test_lease_recovery_survives_restart_and_rejects_old_owner(self):
        self.store.enqueue_job(self.topic["id"], run_at=1000)
        first = self.store.claim_job("old", lease_seconds=2, now=1000)
        self.store.close()
        self.store = Store(self.path)
        self.assertIsNone(self.store.claim_job("new", now=1001))
        recovered = self.store.claim_job("new", now=1003)
        self.assertEqual(recovered["id"], first["id"])
        self.assertEqual(recovered["attempts"], 2)
        with self.assertRaises(LeaseLost):
            self.store.finish_job(first["id"], "old", proposal(), now=1004)
        result = self.store.finish_job(first["id"], "new", proposal(), now=1004)
        self.assertEqual(result["status"], "pending")

    def test_correction_makes_late_result_stale(self):
        self.store.update_topic(self.topic["id"], auto_apply=True)
        job = self.claimed()
        corrected = copy.deepcopy(self.topic["document"])
        corrected["summary"] = "The user's correction is authoritative"
        self.store.correct_topic(self.topic["id"], corrected, "I changed my mind")
        result = self.store.finish_job(job["id"], "worker", proposal("Old background conclusion"))
        self.assertEqual(result["status"], "stale")
        current = self.store.get_topic(self.topic["id"])
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["document"], corrected)
        self.assertEqual(self.store.get_job(job["id"])["status"], "stale")
        with self.assertRaises(ValueError):
            self.store.apply_candidate(result["id"])

    def test_correction_invalidates_pending_and_queued_work(self):
        first = self.claimed()
        candidate = self.store.finish_job(first["id"], "worker", proposal())
        queued = self.store.enqueue_job(self.topic["id"])
        self.store.correct_topic(self.topic["id"], {"summary": "New scope"})
        self.assertEqual(self.store.get_candidate(candidate["id"])["status"], "stale")
        self.assertEqual(self.store.get_job(queued["id"])["status"], "stale")

    def test_pause_and_cancel_survive_restart_and_block_results(self):
        job = self.claimed()
        self.store.pause_topic(self.topic["id"])
        with self.assertRaises(LeaseLost):
            self.store.finish_job(job["id"], "worker", proposal())
        self.store.close()
        self.store = Store(self.path)
        self.assertTrue(self.store.get_topic(self.topic["id"])["paused"])
        self.assertIsNone(self.store.claim_job("another"))
        self.assertEqual(self.store.get_job(job["id"])["status"], "cancelled")
        self.store.resume_topic(self.topic["id"])
        cancelled = self.claimed()
        self.store.cancel_job(cancelled["id"])
        with self.assertRaises(LeaseLost):
            self.store.finish_job(cancelled["id"], "worker", proposal())
        self.assertEqual(self.store.list_candidates(), [])

    def test_noop_does_not_create_candidate_or_version(self):
        job = self.claimed()
        no_change = {"document": self.topic["document"], "sources": []}
        result = self.store.finish_job(job["id"], "worker", no_change)
        self.assertEqual(result["status"], "noop")
        self.assertEqual(self.store.list_candidates(), [])
        self.assertEqual(self.store.get_topic(self.topic["id"])["version"], 1)

    def test_new_search_metadata_without_document_progress_is_noop(self):
        job = self.claimed()
        result = self.store.finish_job(job["id"], "worker", {
            "document": self.topic["document"], "source_provenance": "verified_search",
            "sources": [{"id": "s1", "url": "https://example.org/source", "title": "An old result", "retrieved_at": "new time"}],
        })
        self.assertEqual(result["status"], "noop")

    def test_duplicate_pending_proposal_is_deduplicated(self):
        one = self.claimed()
        candidate = self.store.finish_job(one["id"], "worker", proposal())
        two = self.claimed()
        repeated = proposal()
        repeated["reflection"] = "Different wording, same actual progress"
        result = self.store.finish_job(two["id"], "worker", repeated)
        self.assertEqual(result["status"], "noop")
        self.assertEqual(result["candidate_id"], candidate["id"])
        self.assertEqual(len(self.store.list_candidates()), 1)

    def test_claims_require_real_ids_and_citations(self):
        job = self.claimed()
        bad = proposal()
        bad["document"]["claims"] = [{"id": "c1", "text": "Unsupported fact", "status": "supported", "source_ids": []}]
        with self.assertRaisesRegex(ValueError, "require source_ids"):
            self.store.finish_job(job["id"], "worker", bad)
        bad["document"]["claims"][0]["source_ids"] = ["invented"]
        with self.assertRaisesRegex(ValueError, "unknown source"):
            self.store.finish_job(job["id"], "worker", bad)
        bad["sources"] = [{"id": "real", "url": "https://example.org/paper", "title": "Primary source"}]
        bad["source_provenance"] = "verified_search"
        bad["document"]["claims"][0]["source_ids"] = ["real"]
        good = self.store.finish_job(job["id"], "worker", bad)
        self.assertEqual(good["document"]["claims"][0]["source_ids"], ["real"])

    def test_missing_document_is_not_silently_applied(self):
        job = self.claimed()
        with self.assertRaisesRegex(ValueError, "require a complete document"):
            self.store.finish_job(job["id"], "worker", {"summary": "Oops"})
        self.assertEqual(self.store.list_candidates(), [])

    def test_partial_research_document_cannot_erase_other_fields(self):
        job = self.claimed()
        with self.assertRaisesRegex(ValueError, "include summary"):
            self.store.finish_job(job["id"], "worker", {"document": {"summary": "Partial answer"}})

    def test_failure_after_correction_does_not_retry_obsolete_work(self):
        job = self.claimed()
        self.store.correct_topic(self.topic["id"], {"summary": "New user direction"})
        self.store.fail_job(job["id"], "worker", "Provider failed", retry_delay_seconds=0)
        self.assertEqual(self.store.get_job(job["id"])["status"], "stale")
        self.assertIsNone(self.store.claim_job("next"))

    def test_system_proposal_never_auto_changes_runtime_or_document(self):
        self.store.update_topic(self.topic["id"], auto_apply=True)
        job = self.claimed(kind="selfupdate")
        candidate = self.store.finish_job(job["id"], "worker", {"summary": "Try shorter reports", "proposal": {"change": "Shorter reports", "validation": "Compare examples"}})
        self.assertEqual(candidate["status"], "pending")
        accepted = self.store.apply_candidate(candidate["id"])
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(self.store.get_topic(self.topic["id"])["version"], 1)

    def test_usage_budget_is_atomic_and_persistent(self):
        second = Store(self.path, daily_call_limit=3)
        def reserve(index):
            try:
                (self.store if index % 2 else second).reserve_usage()
                return True
            except BudgetExceeded:
                return False
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                successes = list(pool.map(reserve, range(20)))
            self.assertEqual(sum(successes), 3)
        finally:
            second.close()
        self.store.close()
        self.store = Store(self.path, daily_call_limit=3)
        self.assertEqual(self.store.get_usage()["llm"]["used"], 3)
        with self.assertRaises(BudgetExceeded):
            self.store.reserve_usage()
        voice = self.store.reserve_usage("voice", units=120, limit=120)
        self.assertEqual(voice["remaining"], 0)

    def test_custom_unit_budget_is_reported_and_cannot_expand_during_day(self):
        self.store.reserve_usage("tts_chars", units=1200, limit=1200)
        self.assertEqual(self.store.get_usage()["tts_chars"]["limit"], 1200)
        with self.assertRaises(BudgetExceeded):
            self.store.reserve_usage("tts_chars", units=1, limit=2400)
        with self.assertRaises(BudgetExceeded):
            self.store.reserve_usage("llm", units=4, limit=100)

    def test_export_includes_original_correction_pending_work_and_history(self):
        self.store.add_event(self.topic["id"], "chat.user", {"text": "My original thought"})
        self.store.correct_topic(self.topic["id"], {"summary": "User corrected this"}, "Explicit correction")
        job = self.claimed()
        self.store.finish_job(job["id"], "worker", proposal())
        exported = self.store.export_topic(self.topic["id"])
        self.assertEqual(exported["topic"]["version"], 2)
        self.assertEqual(len(exported["candidates"]), 1)
        self.assertEqual(exported["jobs"][0]["topic_snapshot"]["version"], 2)
        self.assertTrue(any(event["data"].get("text") == "My original thought" for event in exported["events"]))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._conn.execute("DELETE FROM events")


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "workspace.sqlite3"
        self.store = Store(self.path, daily_call_limit=1)
        self.topic = self.store.create_topic("An ongoing question", interval_seconds=3600)
        self.engines = []

    async def asyncTearDown(self):
        for engine in self.engines:
            await engine.stop()
        self.store.close()
        self.tmp.cleanup()

    def engine(self, worker, **settings):
        engine = Engine(self.store, worker, settings)
        self.engines.append(engine)
        return engine

    async def test_persisted_due_time_runs_after_restart_without_browser(self):
        seen = []
        async def worker(topic, job):
            seen.append((topic["version"], job["kind"]))
            return proposal()
        self.store.close()
        self.store = Store(self.path)
        engine = self.engine(worker)
        self.assertEqual(await engine.run_once(), 1)
        self.assertEqual(seen, [(1, "research")])
        self.assertEqual(len(self.store.list_candidates()), 1)
        self.assertGreater(self.store.get_topic(self.topic["id"])["next_due_at"], time.time())
        self.assertEqual(await engine.run_once(), 0)

    async def test_heartbeat_prevents_duplicate_execution(self):
        started = asyncio.Event()
        async def worker(topic, job):
            started.set()
            await asyncio.sleep(0.20)
            return proposal()
        engine = self.engine(worker, lease_seconds=0.06)
        task = asyncio.create_task(engine.run_once())
        await started.wait()
        await asyncio.sleep(0.10)
        second = Store(self.path)
        try:
            self.assertIsNone(second.claim_job("competing-worker", lease_seconds=0.06))
        finally:
            second.close()
        await task
        self.assertEqual(self.store.list_jobs()[0]["status"], "completed")

    async def test_pause_while_running_blocks_result(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def worker(topic, job):
            started.set()
            await finish.wait()
            return proposal()
        engine = self.engine(worker)
        task = asyncio.create_task(engine.run_once())
        await started.wait()
        self.store.pause_topic(self.topic["id"])
        finish.set()
        await task
        self.assertEqual(self.store.list_jobs()[0]["status"], "cancelled")
        self.assertEqual(self.store.list_candidates(), [])

    async def test_budget_defers_to_next_day_without_busy_retry_or_double_charge(self):
        calls = []
        self.store.reserve_usage()
        async def worker(topic, job):
            calls.append(job["id"])
            self.store.reserve_usage()
            return proposal()
        engine = self.engine(worker)
        await engine.run_once()
        job = self.store.list_jobs()[0]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["attempts"], 0)
        self.assertGreater(job["run_at"], time.time())
        self.assertEqual(await engine.run_once(), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.get_usage()["llm"]["used"], 1)

    async def test_timeout_is_bounded_and_failure_does_not_change_document(self):
        async def worker(topic, job):
            await asyncio.sleep(10)
        engine = self.engine(worker, worker_timeout_seconds=0.02, max_attempts=1)
        await engine.run_once()
        job = self.store.list_jobs()[0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("TimeoutError", job["last_error"])
        self.assertEqual(self.store.get_topic(self.topic["id"])["version"], 1)

    async def test_stop_releases_claim_for_restart(self):
        started = asyncio.Event()
        async def slow_worker(topic, job):
            started.set()
            await asyncio.sleep(10)
            return proposal()
        engine = self.engine(slow_worker)
        await engine.start()
        await started.wait()
        await engine.stop()
        job = self.store.list_jobs()[0]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["attempts"], 0)
        async def replacement(topic, job):
            return proposal()
        restarted = self.engine(replacement)
        await restarted.run_once()
        self.assertEqual(self.store.get_job(job["id"])["status"], "completed")

    async def test_old_snapshot_cannot_overwrite_correction(self):
        started, finish = asyncio.Event(), asyncio.Event()
        snapshots = []
        async def worker(topic, job):
            snapshots.append(topic["version"])
            started.set()
            await finish.wait()
            return proposal()
        self.store.update_topic(self.topic["id"], auto_apply=True)
        engine = self.engine(worker)
        task = asyncio.create_task(engine.run_once())
        await started.wait()
        self.store.correct_topic(self.topic["id"], {"summary": "User knows better"})
        finish.set()
        await task
        self.assertEqual(snapshots, [1])
        self.assertEqual(self.store.list_candidates()[0]["status"], "stale")
        self.assertEqual(self.store.get_topic(self.topic["id"])["document"]["summary"], "User knows better")


if __name__ == "__main__":
    unittest.main()
