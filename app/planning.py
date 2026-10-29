"""Pure planning contracts; no I/O and no claims of semantic verification.

The caller verifies input event IDs against the supplied user-input inbox.
Explicit question/source links are necessary checks, not proof that prose is
correct or useful. Model proposals still pass Store validation and review.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter


def _normalized(text):
    if not isinstance(text, str):
        return ""
    # Keep operators and interior punctuation: a=b and a!=b are different.
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", "", text).strip("。.!！?？；;，,：:")


def _event_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return int(value)
    return None


def _string_ids(value):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        return None
    return list(dict.fromkeys(value))


def _event_ids(value):
    if not isinstance(value, list):
        return None
    values = [_event_id(item) for item in value]
    if any(item is None for item in values):
        return None
    return set(values)


def _questions(topic):
    questions = []
    seen = set()
    for item in topic.get("document", {}).get("questions", []):
        if isinstance(item, str):
            text, status, question_id = item, "open", None
        elif isinstance(item, dict):
            text, status, question_id = item.get("text"), item.get("status", "open"), item.get("id")
        else:
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        if not isinstance(question_id, str) or not question_id.strip():
            question_id = "legacy-" + hashlib.sha256(_normalized(text).encode("utf-8")).hexdigest()[:12]
        if question_id in seen:
            continue
        seen.add(question_id)
        questions.append({"id": question_id, "text": text, "status": str(status).lower()})
    return questions


def pending_user_inputs(events, candidates, limit=8):
    """Return the latest uncaptured original user utterances, oldest first.

    Pending/applied proposals have custody of their input IDs. Rejecting or
    invalidating that proposal releases the input for a later attempt.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    events = list(events)
    classified_reports = set()
    for event in events:
        if not isinstance(event, dict) or event.get("kind") != "input.classified":
            continue
        data = event.get("data", {})
        if not isinstance(data, dict) or data.get("classification") != "report_request":
            continue
        ids = data.get("input_event_ids")
        if isinstance(ids, list):
            classified_reports.update(value for value in ids if type(value) is int and value > 0)
    captured = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("status") not in {"pending", "applied"}:
            continue
        nested = candidate.get("proposal")
        nested = nested if isinstance(nested, dict) else {}
        captured.update(_event_ids(candidate.get("input_event_ids", nested.get("input_event_ids", []))) or set())
    available = {}
    for event in events:
        if not isinstance(event, dict) or event.get("kind") != "user_message":
            continue
        data = event.get("data", {})
        if not isinstance(data, dict) or data.get("report") is True:
            continue
        text, event_id = data.get("text"), _event_id(event.get("id"))
        if event_id is None or event_id in captured or event_id in classified_reports or not isinstance(text, str) or not text.strip():
            continue
        available[event_id] = {"event_id": event_id, "text": text}
    if limit == 0:
        return []
    return [available[event_id] for event_id in sorted(available)[-limit:]]


def choose_focus(topic, events):
    """Rotate open questions by attempt count, independently of source presence."""
    questions = [question for question in _questions(topic) if question["status"] != "resolved"]
    if not questions:
        return {"question_id": "topic", "question_text": topic.get("goal") or topic.get("title") or "Review the current objective", "operation": "update"}
    counts = Counter()
    attempted_jobs = set()
    for event in events:
        if not isinstance(event, dict) or event.get("kind") != "research.focus":
            continue
        data = event.get("data", {})
        if not isinstance(data, dict):
            continue
        question_id = data.get("question_id")
        if not isinstance(question_id, str):
            continue
        # Retrying the same persisted job is not a new research direction.
        job_id = data.get("job_id")
        key = (question_id, job_id) if job_id is not None else None
        if key is not None and key in attempted_jobs:
            continue
        if key is not None:
            attempted_jobs.add(key)
        counts[question_id] += 1
    question = min(questions, key=lambda item: counts[item["id"]])
    operation = ("verify", "counterexample", "update")[counts[question["id"]] % 3]
    return {"question_id": question["id"], "question_text": question["text"], "operation": operation}


def progress_contract(topic, proposal, read_source_ids):
    """Check declared progress against known questions and durable old content.

    New addressed questions use new_questions:[{id,text,input_event_ids:[...]}].
    Each grounded/novel claim uses question_ids:[...] to identify its target.
    Hypotheses/reframes need unconfirmed:true and always require human review.
    """
    proposal = proposal if isinstance(proposal, dict) else {}
    kind = proposal.get("progress_kind")
    kind = kind if isinstance(kind, str) else None
    addresses = _string_ids(proposal.get("addresses_question_ids"))

    def result(valid, reason):
        return {"valid": valid, "reason": reason, "progress_kind": kind,
                "addresses_question_ids": addresses or [],
                "requires_review": kind in {"hypothesis", "reframe"}}

    if kind not in {"evidence", "hypothesis", "reframe"}:
        return result(False, "更新须声明 evidence、hypothesis 或 reframe 类型。")
    if not addresses:
        return result(False, "更新须明确回答哪个问题。")
    old_questions = _questions(topic)
    known_questions = {item["id"] for item in old_questions}
    if not any(item["status"] != "resolved" for item in old_questions):
        known_questions.add("topic")
    document = proposal.get("document")
    if not isinstance(document, dict) or not isinstance(document.get("claims"), list) or not isinstance(document.get("questions"), list):
        return result(False, "更新缺少结构化观点或问题。")
    new_doc_questions = _questions({"document": document})
    new_by_id = {item["id"]: item for item in new_doc_questions}
    input_ids = _event_ids(proposal.get("input_event_ids", []))
    if input_ids is None:
        return result(False, "input_event_ids 须为真实事件ID列表。")
    new_questions = proposal.get("new_questions", [])
    if not isinstance(new_questions, list):
        return result(False, "new_questions 须为问题列表。")
    for question in new_questions:
        if not isinstance(question, dict):
            return result(False, "新增问题须声明原文来源事件。")
        question_id = question.get("id")
        refs = _event_ids(question.get("input_event_ids", []))
        if not isinstance(question_id, str) or question_id not in new_by_id or not refs or not refs <= input_ids:
            return result(False, "新增问题须出现在候选结构中，并关联本轮用户原话。")
        if _normalized(question.get("text")) != _normalized(new_by_id[question_id]["text"]):
            return result(False, "新增问题声明与候选结构不一致。")
        known_questions.add(question_id)
    if not set(addresses) <= known_questions:
        return result(False, "更新指向未知问题，且未提供新增问题的原话依据。")

    old_question_ids = {item["id"] for item in old_questions}
    old_question_records = {item["id"]: item for item in topic.get("document", {}).get("questions", [])
                            if isinstance(item, dict) and isinstance(item.get("id"), str)}
    linked_existing_input = False
    for question in document["questions"]:
        if not isinstance(question, dict) or "input_event_ids" not in question:
            continue
        raw_refs = question["input_event_ids"]
        if not isinstance(raw_refs, list) or any(type(value) is not int for value in raw_refs):
            return result(False, "问题原话引用须为合法事件整数ID列表。")
        refs = _event_ids(raw_refs)
        if refs is None:
            return result(False, "问题原话引用须为合法事件整数ID列表。")
        question_id = question.get("id")
        if not isinstance(question_id, str):
            return result(False, "关联原话的问题必须有稳定ID。")
        previous = old_question_records.get(question_id, {})
        previous_refs = _event_ids(previous.get("input_event_ids", [])) or set()
        added_refs = refs - previous_refs
        if not added_refs <= input_ids:
            return result(False, "问题新增引用不属于本轮已提供的用户原话。")
        if question_id in old_question_ids and question_id in addresses and added_refs:
            linked_existing_input = True

    old_claims = [item for item in topic.get("document", {}).get("claims", []) if isinstance(item, dict)]
    old_by_id = {item.get("id"): item for item in old_claims}
    old_texts = {_normalized(item.get("text")) for item in old_claims}
    read_ids = set(read_source_ids or [])

    def targeted(claim):
        ids = _string_ids(claim.get("question_ids", claim.get("addresses_question_ids")))
        return bool(ids and set(ids) & set(addresses))

    def text_is_new(claim):
        text = _normalized(claim.get("text"))
        return bool(text and text not in old_texts)

    claims = document["claims"]
    if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
           or not isinstance(item.get("text"), str) or not item["text"].strip()
           or not isinstance(item.get("status"), str) or item["status"] not in {"hypothesis", "supported", "contested"}
           or _string_ids(item.get("source_ids", [])) is None for item in claims):
        return result(False, "观点必须有稳定ID、文本和来源ID列表。")
    if kind == "evidence":
        for claim in claims:
            refs = _string_ids(claim.get("source_ids"))
            if not targeted(claim) or claim.get("status") not in {"supported", "contested"} or refs is None or not set(refs) & read_ids:
                continue
            previous = old_by_id.get(claim.get("id"))
            new_evidence = previous is not None and bool((set(refs) - set(previous.get("source_ids", []))) & read_ids)
            changed_status = previous is not None and previous.get("status") != claim.get("status")
            # Same prose under a fresh ID is duplication, even with another URL.
            if text_is_new(claim) or (previous is not None and (new_evidence or changed_status)):
                return result(True, "更新包含针对明确问题的新依据或有据修正。")
        return result(False, "没有针对声明问题、附实读依据的新观点或修正。")

    if proposal.get("unconfirmed") is not True:
        return result(False, "假设与问题重构须明确标记 unconfirmed=true。")
    def material_change(claim):
        previous = old_by_id.get(claim["id"])
        return previous is None or (_normalized(claim["text"]) != _normalized(previous.get("text"))
                                    or claim.get("status") != previous.get("status")
                                    or set(claim.get("source_ids", [])) != set(previous.get("source_ids", [])))

    changed_claims = [claim for claim in claims if material_change(claim)]
    if any(claim.get("status") != "hypothesis" for claim in changed_claims):
        return result(False, "未确认更新不能将变更观点标成已获证据。")
    if kind == "hypothesis":
        if any(claim.get("status") == "hypothesis" and targeted(claim) and text_is_new(claim) for claim in changed_claims):
            return result(True, "新增未确认假设，需人工审阅后采用。")
        return result(False, "没有新假设；换ID或重复旧文不算进展。")
    old_question_texts = {_normalized(item["text"]) for item in old_questions}
    if any(item["id"] in addresses and _normalized(item["text"]) not in old_question_texts for item in new_doc_questions):
        return result(True, "问题表述有明确变化，需人工审阅。")
    if linked_existing_input:
        return result(True, "仅将原话关联既有分支，不是新发现；需人工审阅。")
    return result(False, "没有新的问题表述；只改计划或换ID不算重构。")
