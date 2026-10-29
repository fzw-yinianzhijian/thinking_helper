"""Product scenarios over pure contracts; no provider, network or paid calls."""
import copy
import unittest

from app.planning import choose_focus, pending_user_inputs, progress_contract


def workspace():
    return {
        "title": "持续思考的搭档", "goal": "帮助我清晰思考，允许慢慢说",
        "auto_apply": True,
        "sources": [{"id": "s-old", "url": "https://example.org/old", "title": "Existing material"}],
        "document": {"summary": "两个不同的未决问题", "claims": [],
                     "questions": [{"id": "q-runtime", "text": "离开页面后，工作怎样继续？", "status": "open"},
                                   {"id": "q-voice", "text": "怎样在长停顿时避免抢话？", "status": "open"}],
                     "next_steps": []},
    }


def claim(claim_id="c-new", text="端点检测设置会影响长停顿的处理。", *, question="q-voice", status="supported", sources=None):
    return {"id": claim_id, "text": text, "status": status,
            "source_ids": ["s-read"] if sources is None else sources, "question_ids": [question]}


def update(topic, claims=None, *, kind="evidence", addresses=None):
    doc = copy.deepcopy(topic["document"])
    if claims is not None:
        doc["claims"] = claims
    return {"document": doc, "progress_kind": kind,
            "addresses_question_ids": addresses or ["q-voice"], "input_event_ids": []}


class ThinkingContinuityScenarios(unittest.TestCase):
    def test_spoken_input_is_released_when_its_candidate_is_rejected_or_stale(self):
        original = "我说话会停很久，这个分支请保留。"
        events = [
            {"id": 13, "kind": "user_message", "data": {"text": "请汇报整体", "report": True}},
            {"id": 12, "kind": "assistant_message", "data": {"text": "应当立即降低判停时间"}},
            {"id": 11, "kind": "user_message", "data": {"text": original}},
        ]
        self.assertEqual(pending_user_inputs(events, []), [{"event_id": 11, "text": original}])
        candidate = {"status": "pending", "proposal": {"input_event_ids": [11]}}
        self.assertEqual(pending_user_inputs(events, [candidate]), [])
        for state in ("rejected", "stale"):
            candidate["status"] = state
            self.assertEqual(pending_user_inputs(events, [candidate]), [{"event_id": 11, "text": original}])
        candidate["status"] = "applied"
        self.assertEqual(pending_user_inputs(events, [candidate]), [])

    def test_recent_input_limit_preserves_original_order_and_does_not_import_assistant_claims(self):
        events = [{"id": i, "kind": "user_message", "data": {"text": f"原话 {i}"}} for i in range(1, 12)]
        events += [{"id": 90, "kind": "assistant_message", "data": {"text": "用户确定想要自动化一切"}}]
        selected = pending_user_inputs(list(reversed(events)), [], limit=3)
        self.assertEqual([item["event_id"] for item in selected], [9, 10, 11])
        self.assertEqual([item["text"] for item in selected], ["原话 9", "原话 10", "原话 11"])

    def test_explicit_audit_classifies_legacy_report_without_guessing_or_deleting_originals(self):
        events = [
            {"id": 1, "kind": "user_message", "data": {"text": "请汇报当前整体"}},
            {"id": 2, "kind": "user_message", "data": {"text": "我想继续研究汇报形式这个问题"}},
            {"id": 3, "kind": "user_message", "data": {"text": "请汇报，但没有人工标注不能猜测"}},
            {"id": 4, "kind": "input.classified", "data": {"input_event_ids": [1, "3", True, -2], "classification": "report_request"}},
            {"id": 5, "kind": "input.classified", "data": {"input_event_ids": [2], "classification": "unknown_future_class"}},
        ]
        original = copy.deepcopy(events)
        selected = pending_user_inputs(list(reversed(events)), [])
        self.assertEqual([item["event_id"] for item in selected], [2, 3])
        self.assertEqual(events, original)

    def test_existing_sources_do_not_lock_out_an_unattempted_question(self):
        topic = workspace()
        events = [{"kind": "research.focus", "data": {"job_id": "first", "question_id": "q-runtime"}}]
        # A duplicate event from a retry must not count as another attempt.
        events.append(copy.deepcopy(events[0]))
        self.assertEqual(choose_focus(topic, events), {"question_id": "q-voice", "question_text": "怎样在长停顿时避免抢话？", "operation": "verify"})
        events.append({"kind": "research.focus", "data": {"job_id": "second", "question_id": "q-voice"}})
        self.assertEqual(choose_focus(topic, events)["question_id"], "q-runtime")
        self.assertEqual(choose_focus(topic, events)["operation"], "counterexample")
        events.append({"kind": "research.focus", "data": {"job_id": "third", "question_id": "q-runtime"}})
        self.assertEqual(choose_focus(topic, events)["question_id"], "q-voice")
        topic["document"]["questions"][0]["status"] = "resolved"
        events.append({"kind": "research.focus", "data": {"job_id": "fourth", "question_id": "q-voice"}})
        self.assertEqual(choose_focus(topic, events)["operation"], "update")
        self.assertEqual(choose_focus(topic, events)["question_id"], "q-voice")

    def test_legacy_question_identity_survives_source_changes_and_list_reordering(self):
        topic = workspace()
        topic["document"]["questions"] = ["我停顿多久会被判为说完？", "还有别的问题吗？"]
        original = choose_focus(topic, [])
        topic["sources"] = []
        topic["document"]["questions"].reverse()
        selected = choose_focus(topic, [{"kind": "research.focus", "data": {"question_id": choose_focus(topic, [])["question_id"]}}])
        self.assertEqual(selected["question_id"], original["question_id"])
        self.assertEqual(selected["question_text"], original["question_text"])


class ProgressMeaningScenarios(unittest.TestCase):
    def test_a_new_cited_fact_must_address_the_declared_question(self):
        topic = workspace()
        proposal = update(topic, [claim(question="q-runtime")])
        self.assertFalse(progress_contract(topic, proposal, {"s-read"})["valid"])
        proposal["document"]["claims"][0]["question_ids"] = ["q-voice"]
        self.assertFalse(progress_contract(topic, proposal, {"s-other"})["valid"])
        self.assertTrue(progress_contract(topic, proposal, {"s-read"})["valid"])

    def test_renaming_an_old_fact_is_not_progress_but_verifying_the_existing_hypothesis_is(self):
        topic = workspace()
        old = claim("c-old", status="hypothesis", sources=[])
        topic["document"]["claims"] = [old]
        renamed = claim("c-renamed")
        self.assertFalse(progress_contract(topic, update(topic, [renamed]), {"s-read"})["valid"])
        verified = claim("c-old")
        self.assertTrue(progress_contract(topic, update(topic, [verified]), {"s-read"})["valid"])

    def test_marked_hypothesis_can_be_useful_without_claiming_verified_evidence(self):
        topic = workspace()
        original = copy.deepcopy(topic)
        hypothesis = claim("h1", "我可能需要在停顿时听到可选择的轻提示，而非缩短等待。", status="hypothesis", sources=[])
        proposal = update(topic, [hypothesis], kind="hypothesis")
        self.assertFalse(progress_contract(topic, proposal, set())["valid"])
        proposal["unconfirmed"] = True
        checked = progress_contract(topic, proposal, set())
        self.assertTrue(checked["valid"])
        self.assertTrue(checked["requires_review"])
        self.assertEqual(topic, original)
        # Turning the same untested idea into 'supported' is never speculation.
        proposal["document"]["claims"][0]["status"] = "supported"
        self.assertFalse(progress_contract(topic, proposal, set())["valid"])

    def test_a_new_user_question_needs_original_input_provenance_and_review(self):
        topic = workspace()
        proposal = update(topic, kind="reframe", addresses=["q-new"])
        proposal["unconfirmed"] = True
        proposal["document"]["questions"].append({"id": "q-new", "text": "怎样判断助手是在帮助我思考，还是替我思考？", "status": "open"})
        self.assertFalse(progress_contract(topic, proposal, set())["valid"])
        proposal["input_event_ids"] = [21]
        proposal["new_questions"] = [{"id": "q-new", "text": proposal["document"]["questions"][-1]["text"], "input_event_ids": [22]}]
        self.assertFalse(progress_contract(topic, proposal, set())["valid"])
        proposal["new_questions"][0]["input_event_ids"] = [21]
        checked = progress_contract(topic, proposal, set())
        self.assertTrue(checked["valid"])
        self.assertTrue(checked["requires_review"])

    def test_rephrased_question_can_progress_but_a_plan_or_renamed_question_cannot(self):
        topic = workspace()
        proposal = update(topic, kind="reframe")
        proposal["unconfirmed"] = True
        proposal["document"]["next_steps"] = ["下次多找几篇资料"]
        self.assertFalse(progress_contract(topic, proposal, set())["valid"])
        proposal["document"]["questions"][1]["text"] = "用户在构思时的停顿和已经说完，应如何区别？"
        self.assertTrue(progress_contract(topic, proposal, set())["valid"])
        proposal["document"]["questions"][1] = {"id": "q-copy", "text": topic["document"]["questions"][1]["text"], "status": "open"}
        proposal["addresses_question_ids"] = ["q-copy"]
        proposal["input_event_ids"] = [23]
        proposal["new_questions"] = [{**proposal["document"]["questions"][1], "input_event_ids": [23]}]
        self.assertFalse(progress_contract(topic, proposal, set())["valid"])

    def test_new_original_input_can_be_linked_to_an_unchanged_existing_question(self):
        topic = workspace()
        proposal = update(topic, kind="reframe")
        proposal.update(unconfirmed=True, input_event_ids=[41])
        proposal["document"]["questions"][1]["input_event_ids"] = [41]
        checked = progress_contract(topic, proposal, set())
        self.assertTrue(checked["valid"])
        self.assertTrue(checked["requires_review"])
        self.assertIn("仅将原话关联既有分支，不是新发现", checked["reason"])
        self.assertEqual(proposal["document"]["questions"][1]["text"], topic["document"]["questions"][1]["text"])
        # A subsequent pass over exactly the same linkage is not new progress.
        topic["document"] = copy.deepcopy(proposal["document"])
        self.assertFalse(progress_contract(topic, proposal, set())["valid"])
        # Preserving the old origin does not require claiming it as a new input.
        proposal["input_event_ids"] = [42]
        proposal["document"]["questions"][1]["input_event_ids"] = [41, 42]
        self.assertTrue(progress_contract(topic, proposal, set())["valid"])

    def test_unknown_original_input_is_rejected_even_when_question_text_changes(self):
        topic = workspace()
        proposal = update(topic, kind="reframe")
        proposal.update(unconfirmed=True, input_event_ids=[41])
        question = proposal["document"]["questions"][1]
        question["input_event_ids"] = [999]
        self.assertFalse(progress_contract(topic, proposal, set())["valid"])
        question["text"] = "该如何区别思考中的停顿与真正结束发言？"
        self.assertFalse(progress_contract(topic, proposal, set())["valid"])
        question["input_event_ids"] = [True]
        self.assertFalse(progress_contract(topic, proposal, set())["valid"])

    def test_bad_model_shapes_fail_closed_instead_of_crashing_the_scheduler(self):
        topic = workspace()
        malformed = [None, {"progress_kind": []}, {"progress_kind": "evidence", "addresses_question_ids": "q-voice"}]
        for proposal in malformed:
            with self.subTest(proposal=proposal):
                self.assertFalse(progress_contract(topic, proposal, {"s-read"})["valid"])
        proposal = update(topic, [claim()])
        proposal["document"]["claims"][0]["status"] = {}
        self.assertFalse(progress_contract(topic, proposal, {"s-read"})["valid"])


if __name__ == "__main__":
    unittest.main()
