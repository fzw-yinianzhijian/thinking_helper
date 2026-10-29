"""Persistent workspace and leased work queue. No provider or web dependencies."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


class BudgetExceeded(RuntimeError):
    """The request was not reserved and must not be sent to a provider."""


class LeaseLost(RuntimeError):
    """A cancelled, expired or reassigned job cannot commit a result."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _new_id() -> str:
    return uuid.uuid4().hex


def _timestamp(value=None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


def _text(value, field, *, empty=True):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{field} must be a {'non-empty ' if not empty else ''}string")
    return value


def _positive_int(value, field, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise ValueError(f"{field} must be a {'non-negative' if zero else 'positive'} integer")
    return value


def _sources(items) -> list[dict]:
    if not isinstance(items, list):
        raise ValueError("sources must be a list")
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each source must be an object")
        item = copy.deepcopy(item)
        source_id = _text(item.get("id"), "source.id", empty=False)
        _text(item.get("title"), "source.title", empty=False)
        url = _text(item.get("url"), "source.url", empty=False)
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("source.url must be an absolute http(s) URL")
        if source_id in seen:
            raise ValueError(f"duplicate source id: {source_id}")
        seen.add(source_id)
        result.append(item)
    _json(result)
    return result


def _merge_sources(existing, incoming):
    by_id = {item["id"]: item for item in _sources(existing)}
    for item in _sources(incoming):
        previous = by_id.get(item["id"])
        if previous and previous["url"] != item["url"]:
            raise ValueError(f"source id {item['id']} already identifies a different URL")
        by_id[item["id"]] = item
    return sorted(by_id.values(), key=lambda item: item["id"])


def _document(value, sources) -> dict:
    if not isinstance(value, dict):
        raise ValueError("document must be an object")
    document = {
        "summary": _text(value.get("summary", ""), "document.summary"),
        "claims": copy.deepcopy(value.get("claims", [])),
        "questions": copy.deepcopy(value.get("questions", [])),
        "next_steps": copy.deepcopy(value.get("next_steps", [])),
    }
    known_sources = {item["id"] for item in sources}
    claims = document["claims"]
    if not isinstance(claims, list):
        raise ValueError("document.claims must be a list")
    seen = set()
    for claim in claims:
        if not isinstance(claim, dict):
            raise ValueError("each claim must be an object")
        claim_id = _text(claim.get("id"), "claim.id", empty=False)
        _text(claim.get("text"), "claim.text", empty=False)
        if claim_id in seen:
            raise ValueError(f"duplicate claim id: {claim_id}")
        seen.add(claim_id)
        if claim.get("status") not in {"hypothesis", "supported", "contested"}:
            raise ValueError("claim.status must be hypothesis, supported or contested")
        refs = claim.get("source_ids", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise ValueError("claim.source_ids must be a list of source IDs")
        unknown = set(refs) - known_sources
        if unknown:
            raise ValueError("unknown source IDs: " + ", ".join(sorted(unknown)))
        if claim["status"] == "supported" and not refs:
            raise ValueError("supported claims require source_ids")
        claim["source_ids"] = list(dict.fromkeys(refs))
    for field in ("questions", "next_steps"):
        if not isinstance(document[field], list):
            raise ValueError(f"document.{field} must be a list")
        question_ids = set()
        for item in document[field]:
            if isinstance(item, str):
                _text(item, field, empty=False)
            elif isinstance(item, dict):
                _text(item.get("text"), f"{field}.text", empty=False)
                if field == "questions":
                    question_id = _text(item.get("id"), "question.id", empty=False)
                    if question_id in question_ids:
                        raise ValueError(f"duplicate question id: {question_id}")
                    question_ids.add(question_id)
            else:
                raise ValueError(f"{field} entries must be strings or objects with text")
    _json(document)
    return document


def _fingerprint(document, sources):
    # Search timestamps/snippets change on every call. They are not progress.
    # A new source matters when the reusable document actually changes to use it.
    refs = {ref for claim in document["claims"] for ref in claim.get("source_ids", [])}
    cited = [{"id": item["id"], "url": item["url"]} for item in sources if item["id"] in refs]
    return hashlib.sha256(_json({"document": document, "cited_sources": sorted(cited, key=lambda x: x["id"])}).encode()).hexdigest()


class Store:
    """SQLite transactions guard versions, queue ownership and request budgets."""

    def __init__(self, path, daily_call_limit=24, *, budget_timezone="Asia/Shanghai"):
        self.path = str(path)
        self.daily_call_limit = _positive_int(daily_call_limit, "daily_call_limit", zero=True)
        self.budget_timezone = ZoneInfo(budget_timezone)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser())
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=15, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS topics (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL,
            version INTEGER NOT NULL, document_json TEXT NOT NULL, sources_json TEXT NOT NULL,
            paused INTEGER NOT NULL DEFAULT 0, auto_apply INTEGER NOT NULL DEFAULT 0,
            interval_seconds INTEGER NOT NULL, next_due_at REAL NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id TEXT,
            kind TEXT NOT NULL, data_json TEXT NOT NULL, created_at REAL NOT NULL,
            FOREIGN KEY(topic_id) REFERENCES topics(id)
        );
        CREATE INDEX IF NOT EXISTS events_topic ON events(topic_id,id);
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
          BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
          BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, kind TEXT NOT NULL,
            base_version INTEGER NOT NULL, topic_snapshot_json TEXT NOT NULL,
            payload_json TEXT NOT NULL, status TEXT NOT NULL,
            run_at REAL NOT NULL, owner TEXT, lease_expires_at REAL,
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, result_id TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            FOREIGN KEY(topic_id) REFERENCES topics(id)
        );
        CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status,run_at,lease_expires_at);
        CREATE INDEX IF NOT EXISTS jobs_topic ON jobs(topic_id,status);
        CREATE TABLE IF NOT EXISTS candidates (
            id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, job_id TEXT,
            kind TEXT NOT NULL, base_version INTEGER NOT NULL, status TEXT NOT NULL,
            document_json TEXT, sources_json TEXT NOT NULL, proposal_json TEXT NOT NULL,
            content_hash TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            applied_version INTEGER,
            FOREIGN KEY(topic_id) REFERENCES topics(id), FOREIGN KEY(job_id) REFERENCES jobs(id),
            UNIQUE(topic_id,base_version,kind,content_hash)
        );
        CREATE TABLE IF NOT EXISTS usage (
            day TEXT NOT NULL, kind TEXT NOT NULL, units INTEGER NOT NULL,
            limit_units INTEGER,
            PRIMARY KEY(day,kind)
        );
        """)
        # Keep workspaces created by the first prototype readable.
        if "limit_units" not in {row["name"] for row in self._conn.execute("PRAGMA table_info(usage)")}:
            self._conn.execute("ALTER TABLE usage ADD COLUMN limit_units INTEGER")

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def close(self):
        with self._lock:
            self._conn.close()

    @staticmethod
    def _topic(row):
        if row is None:
            raise KeyError("topic not found")
        item = dict(row)
        item["document"] = json.loads(item.pop("document_json"))
        item["sources"] = json.loads(item.pop("sources_json"))
        item["paused"], item["auto_apply"] = bool(item["paused"]), bool(item["auto_apply"])
        return item

    @staticmethod
    def _job(row):
        if row is None:
            raise KeyError("job not found")
        item = dict(row)
        item["topic_snapshot"] = json.loads(item.pop("topic_snapshot_json"))
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    @staticmethod
    def _candidate(row):
        if row is None:
            raise KeyError("candidate not found")
        item = dict(row)
        proposal = json.loads(item.pop("proposal_json"))
        item["proposal"] = proposal
        for key, value in proposal.items():
            if key not in item:
                item[key] = value
        document = item.pop("document_json")
        item["document"] = json.loads(document) if document is not None else None
        item["sources"] = json.loads(item.pop("sources_json"))
        return item

    def _event(self, conn, topic_id, kind, data, now):
        cursor = conn.execute("INSERT INTO events(topic_id,kind,data_json,created_at) VALUES(?,?,?,?)", (topic_id, kind, _json(data), now))
        return {"id": cursor.lastrowid, "topic_id": topic_id, "kind": kind, "type": kind, "data": data, "created_at": now}

    def add_event(self, topic_id, kind, data):
        _text(kind, "kind", empty=False)
        with self._transaction() as conn:
            if topic_id is not None:
                self._topic(conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone())
            return self._event(conn, topic_id, kind, data, time.time())

    def list_events(self, topic_id=None, limit=100):
        if limit is not None:
            _positive_int(limit, "limit")
        sql, params = "SELECT * FROM events", []
        if topic_id is not None:
            sql += " WHERE topic_id=?"
            params.append(topic_id)
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["data"] = json.loads(item.pop("data_json"))
            item["type"] = item["kind"]
            result.append(item)
        return result

    def create_topic(self, title, goal="", document=None, *, sources=None, auto_apply=False, interval_seconds=3600, paused=False, now=None):
        _text(title, "title", empty=False)
        _text(goal, "goal")
        _positive_int(interval_seconds, "interval_seconds")
        sources = _merge_sources([], [] if sources is None else sources)
        document = _document({} if document is None else document, sources)
        now = _timestamp(now)
        topic_id = _new_id()
        with self._transaction() as conn:
            conn.execute("INSERT INTO topics VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (topic_id, title, goal, 1, _json(document), _json(sources), int(bool(paused)), int(bool(auto_apply)), interval_seconds, now, now, now))
            self._event(conn, topic_id, "topic.created", {"version": 1, "title": title, "goal": goal}, now)
            return self._topic(conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone())

    def get_topic(self, topic_id):
        with self._lock:
            return self._topic(self._conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone())

    def list_topics(self):
        with self._lock:
            return [self._topic(row) for row in self._conn.execute("SELECT * FROM topics ORDER BY created_at,id").fetchall()]

    def _invalidate(self, conn, topic_id, version, now, *, exclude_candidate=None):
        conn.execute("UPDATE candidates SET status='stale',updated_at=? WHERE topic_id=? AND base_version<? AND status='pending' AND id!=?", (now, topic_id, version, exclude_candidate or ""))
        conn.execute("UPDATE jobs SET status='stale',last_error='Topic changed before execution',updated_at=? WHERE topic_id=? AND base_version<? AND status='queued'", (now, topic_id, version))

    def update_topic(self, topic_id, **changes):
        allowed = {"title", "goal", "auto_apply", "interval_seconds", "paused", "next_due_at"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError("unknown topic settings: " + ", ".join(sorted(unknown)))
        with self._transaction() as conn:
            topic = self._topic(conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone())
            now = time.time()
            updated = dict(topic)
            for field, value in changes.items():
                if field in {"title", "goal"}:
                    _text(value, field, empty=field != "title")
                elif field == "interval_seconds":
                    _positive_int(value, field)
                elif field in {"auto_apply", "paused"}:
                    if not isinstance(value, bool):
                        raise ValueError(f"{field} must be a boolean")
                elif field == "next_due_at":
                    value = _timestamp(value)
                updated[field] = value
            content_changed = any(updated[field] != topic[field] for field in ("title", "goal"))
            updated["version"] += int(content_changed)
            if topic["paused"] and not updated["paused"] and "next_due_at" not in changes:
                updated["next_due_at"] = now
            conn.execute("UPDATE topics SET title=?,goal=?,version=?,paused=?,auto_apply=?,interval_seconds=?,next_due_at=?,updated_at=? WHERE id=?", (updated["title"], updated["goal"], updated["version"], int(updated["paused"]), int(updated["auto_apply"]), updated["interval_seconds"], updated["next_due_at"], now, topic_id))
            if content_changed:
                self._invalidate(conn, topic_id, updated["version"], now)
            if updated["paused"]:
                conn.execute("UPDATE jobs SET status='cancelled',owner=NULL,lease_expires_at=NULL,last_error='Topic paused',updated_at=? WHERE topic_id=? AND status IN ('queued','running')", (now, topic_id))
            self._event(conn, topic_id, "topic.settings", {"changes": changes, "version": updated["version"]}, now)
            return self._topic(conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone())

    def pause_topic(self, topic_id):
        return self.update_topic(topic_id, paused=True)

    def resume_topic(self, topic_id):
        return self.update_topic(topic_id, paused=False)

    def correct_topic(self, topic_id, document, note="", *, sources=None, expected_version=None):
        _text(note, "note")
        with self._transaction() as conn:
            topic = self._topic(conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone())
            if expected_version is not None and topic["version"] != expected_version:
                raise ValueError("topic version changed; reload before correcting")
            merged = _merge_sources(topic["sources"], sources or [])
            normalized = _document(document, merged)
            now, version = time.time(), topic["version"] + 1
            conn.execute("UPDATE topics SET document_json=?,sources_json=?,version=?,updated_at=? WHERE id=?", (_json(normalized), _json(merged), version, now, topic_id))
            self._invalidate(conn, topic_id, version, now)
            self._event(conn, topic_id, "topic.corrected", {"note": note, "base_version": topic["version"], "version": version, "document": normalized, "sources": merged}, now)
            return self._topic(conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone())

    def _enqueue(self, conn, topic, kind, run_at, payload, now):
        job_id = _new_id()
        conn.execute("INSERT INTO jobs(id,topic_id,kind,base_version,topic_snapshot_json,payload_json,status,run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,'queued',?,?,?)", (job_id, topic["id"], kind, topic["version"], _json(topic), _json(payload), run_at, now, now))
        self._event(conn, topic["id"], "job.queued", {"job_id": job_id, "kind": kind, "base_version": topic["version"], "run_at": run_at}, now)
        return self._job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def enqueue_job(self, topic_id, kind="research", *, run_at=None, payload=None):
        _text(kind, "kind", empty=False)
        now = time.time()
        with self._transaction() as conn:
            topic = self._topic(conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone())
            if topic["paused"]:
                raise ValueError("topic is paused")
            # An organize worker already took its input snapshot. New speech
            # needs one queued successor, not an alias to that running job.
            existing = conn.execute("SELECT * FROM jobs WHERE topic_id=? AND kind=? AND base_version=? AND (status='queued' OR (status='running' AND ?)) ORDER BY created_at LIMIT 1", (topic_id, kind, topic["version"], int(kind != "organize"))).fetchone()
            if existing is not None:
                result = self._job(existing)
                result["deduplicated"] = True
                return result
            return self._enqueue(conn, topic, kind, _timestamp(run_at) if run_at is not None else now, payload or {}, now)

    def checkpoint_job(self, job_id, checkpoint, *, owner=None):
        """Durably merge a stage result; pass owner to fence a previous attempt."""
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint must be an object")
        _json(checkpoint)
        now = time.time()
        stale = False
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("job not found")
            job = self._owned(conn, job_id, owner if owner is not None else row["owner"], now)
            topic = conn.execute("SELECT version,paused FROM topics WHERE id=?", (job["topic_id"],)).fetchone()
            if topic["paused"]:
                raise LeaseLost("topic is paused")
            if topic["version"] != job["base_version"]:
                stale = True
                conn.execute("UPDATE jobs SET status='stale',owner=NULL,lease_expires_at=NULL,last_error='Topic changed before checkpoint',updated_at=? WHERE id=?", (now, job_id))
                self._event(conn, job["topic_id"], "job.stale", {"job_id": job_id, "reason": "topic changed before checkpoint"}, now)
            else:
                payload = json.loads(job["payload_json"])
                if not isinstance(payload, dict):
                    raise ValueError("job payload must be an object")
                payload.update(copy.deepcopy(checkpoint))
                conn.execute("UPDATE jobs SET payload_json=?,updated_at=? WHERE id=?", (_json(payload), now, job_id))
                self._event(conn, job["topic_id"], "job.checkpoint", {"job_id": job_id, "keys": sorted(checkpoint), "base_version": job["base_version"]}, now)
            result = self._job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        if stale:
            raise LeaseLost("topic changed; checkpoint belongs to an obsolete version")
        return result

    def schedule_due_topics(self, *, now=None, limit=100):
        now = _timestamp(now)
        queued = []
        with self._transaction() as conn:
            rows = conn.execute("SELECT * FROM topics WHERE paused=0 AND next_due_at<=? ORDER BY next_due_at,id LIMIT ?", (now, limit)).fetchall()
            for row in rows:
                topic = self._topic(row)
                active = conn.execute("SELECT 1 FROM jobs WHERE topic_id=? AND kind='research' AND status IN ('queued','running') LIMIT 1", (topic["id"],)).fetchone()
                if active is None:
                    queued.append(self._enqueue(conn, topic, "research", now, {"scheduled": True}, now))
                conn.execute("UPDATE topics SET next_due_at=? WHERE id=?", (now + topic["interval_seconds"], topic["id"]))
        return queued

    def schedule_reflections(self, *, now=None):
        """Queue a bounded method review after three new terminal observations.

        A trigger is not proof that reflection is useful. The normal worker,
        provider budget and pending-candidate review rules still apply.
        """
        now = _timestamp(now)
        queued = []
        method_kinds = ("selfupdate", "self_update", "system_update")
        with self._transaction() as conn:
            rows = conn.execute("SELECT * FROM topics WHERE paused=0 ORDER BY created_at,id").fetchall()
            for row in rows:
                topic = self._topic(row)
                active = conn.execute("SELECT 1 FROM jobs WHERE topic_id=? AND kind IN (?,?,?) AND status IN ('queued','running') LIMIT 1", (topic["id"], *method_kinds)).fetchone()
                if active is not None:
                    continue
                pending = conn.execute("SELECT proposal_json FROM candidates WHERE topic_id=? AND kind IN (?,?,?) AND status='pending'", (topic["id"], *method_kinds)).fetchall()
                if any(isinstance((change := json.loads(item["proposal_json"]).get("proposal")), dict) and bool(change) for item in pending):
                    continue
                last = conn.execute("SELECT MAX(created_at) AS created_at FROM jobs WHERE topic_id=? AND kind IN (?,?,?)", (topic["id"], *method_kinds)).fetchone()["created_at"]
                if last is not None and now - last < 3 * 3600:
                    continue
                window_started_at = last if last is not None else 0
                observations = conn.execute("""SELECT id,kind,status,updated_at FROM jobs
                    WHERE topic_id=? AND kind IN ('research','organize')
                      AND status IN ('completed','noop','failed') AND updated_at>? AND updated_at<=?
                    ORDER BY updated_at,id""", (topic["id"], window_started_at, now)).fetchall()
                if len(observations) < 3:
                    continue
                queued.append(self._enqueue(conn, topic, "selfupdate", now, {
                    "trigger": "three_new_terminal_observations", "scheduled": True,
                    "window_started_at": last,
                    "observation_job_ids": [item["id"] for item in observations],
                }, now))
        return queued

    def get_job(self, job_id):
        with self._lock:
            return self._job(self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list_jobs(self, topic_id=None):
        with self._lock:
            if topic_id is None:
                rows = self._conn.execute("SELECT * FROM jobs ORDER BY created_at DESC,id").fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM jobs WHERE topic_id=? ORDER BY created_at DESC,id", (topic_id,)).fetchall()
            return [self._job(row) for row in rows]

    def claim_job(self, owner, *, lease_seconds=120, max_attempts=3, now=None):
        _text(owner, "owner", empty=False)
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = _timestamp(now)
        with self._transaction() as conn:
            exhausted = conn.execute("SELECT * FROM jobs WHERE status='running' AND lease_expires_at<=? AND attempts>=?", (now, max_attempts)).fetchall()
            for job in exhausted:
                conn.execute("UPDATE jobs SET status='failed',owner=NULL,lease_expires_at=NULL,last_error='Lease expired after maximum attempts',updated_at=? WHERE id=?", (now, job["id"]))
                self._event(conn, job["topic_id"], "job.failed", {"job_id": job["id"], "reason": "lease attempts exhausted"}, now)
            row = conn.execute("""SELECT j.* FROM jobs j JOIN topics t ON t.id=j.topic_id
                WHERE t.paused=0 AND j.attempts<? AND
                  ((j.status='queued' AND j.run_at<=?) OR (j.status='running' AND j.lease_expires_at<=?))
                  AND NOT EXISTS (SELECT 1 FROM jobs other WHERE other.topic_id=j.topic_id
                    AND other.id!=j.id AND other.status='running' AND other.lease_expires_at>?)
                ORDER BY j.run_at,j.created_at,j.id LIMIT 1""", (max_attempts, now, now, now)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE jobs SET status='running',owner=?,lease_expires_at=?,attempts=attempts+1,updated_at=? WHERE id=?", (owner, now + lease_seconds, now, row["id"]))
            self._event(conn, row["topic_id"], "job.claimed", {"job_id": row["id"], "owner": owner, "attempt": row["attempts"] + 1}, now)
            return self._job(conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    @staticmethod
    def _owned(conn, job_id, owner, now):
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("job not found")
        if row["status"] != "running" or row["owner"] != owner or (row["lease_expires_at"] or 0) <= now:
            raise LeaseLost("job no longer has a valid lease")
        return row

    def renew_lease(self, job_id, owner, *, lease_seconds=120, now=None):
        now = _timestamp(now)
        with self._transaction() as conn:
            self._owned(conn, job_id, owner, now)
            conn.execute("UPDATE jobs SET lease_expires_at=?,updated_at=? WHERE id=?", (now + lease_seconds, now, job_id))

    def cancel_job(self, job_id):
        now = time.time()
        with self._transaction() as conn:
            job = self._job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if job["status"] in {"queued", "running"}:
                conn.execute("UPDATE jobs SET status='cancelled',owner=NULL,lease_expires_at=NULL,last_error='Cancelled by user',updated_at=? WHERE id=?", (now, job_id))
                self._event(conn, job["topic_id"], "job.cancelled", {"job_id": job_id}, now)
            return self._job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def defer_job(self, job_id, owner, *, run_at, reason="", restore_attempt=False):
        now = time.time()
        with self._transaction() as conn:
            job = self._owned(conn, job_id, owner, now)
            attempts = max(0, job["attempts"] - int(restore_attempt))
            version = conn.execute("SELECT version FROM topics WHERE id=?", (job["topic_id"],)).fetchone()["version"]
            status = "stale" if version != job["base_version"] else "queued"
            conn.execute("UPDATE jobs SET status=?,owner=NULL,lease_expires_at=NULL,attempts=?,run_at=?,last_error=?,updated_at=? WHERE id=?", (status, attempts, _timestamp(run_at), reason[:2000], now, job_id))
            self._event(conn, job["topic_id"], "job.stale" if status == "stale" else "job.deferred", {"job_id": job_id, "reason": reason[:2000], "run_at": _timestamp(run_at)}, now)

    def fail_job(self, job_id, owner, error, *, max_attempts=3, retry_delay_seconds=30):
        now = time.time()
        with self._transaction() as conn:
            job = self._owned(conn, job_id, owner, now)
            version = conn.execute("SELECT version FROM topics WHERE id=?", (job["topic_id"],)).fetchone()["version"]
            status = "stale" if version != job["base_version"] else ("queued" if job["attempts"] < max_attempts else "failed")
            conn.execute("UPDATE jobs SET status=?,owner=NULL,lease_expires_at=NULL,run_at=?,last_error=?,updated_at=? WHERE id=?", (status, now + retry_delay_seconds, str(error)[:2000], now, job_id))
            event = "job.retry" if status == "queued" else ("job.stale" if status == "stale" else "job.failed")
            self._event(conn, job["topic_id"], event, {"job_id": job_id, "error": str(error)[:2000], "attempt": job["attempts"]}, now)

    def finish_job(self, job_id, owner, proposal, *, now=None):
        if not isinstance(proposal, dict):
            raise ValueError("worker proposal must be an object")
        _json(proposal)
        now = _timestamp(now)
        with self._transaction() as conn:
            job = self._owned(conn, job_id, owner, now)
            topic = self._topic(conn.execute("SELECT * FROM topics WHERE id=?", (job["topic_id"],)).fetchone())
            if topic["paused"]:
                raise LeaseLost("topic is paused")
            snapshot = json.loads(job["topic_snapshot_json"])
            system_proposal = job["kind"] in {"selfupdate", "self_update", "system_update"}
            sources = _merge_sources(snapshot["sources"], proposal.get("sources", []))
            if not system_proposal and "document" not in proposal:
                raise ValueError("research proposals require a complete document")
            if not system_proposal and (not isinstance(proposal["document"], dict) or not {"summary", "claims", "questions", "next_steps"}.issubset(proposal["document"])):
                raise ValueError("research document must include summary, claims, questions and next_steps")
            document = None if system_proposal else _document(proposal["document"], sources)
            provenance = proposal.get("source_provenance")
            if provenance is not None and provenance not in {"verified_search", "user"}:
                raise ValueError("source_provenance must be verified_search or user")
            if proposal.get("sources") and provenance is None:
                raise ValueError("new sources require source_provenance")
            content_hash = hashlib.sha256(_json(proposal).encode()).hexdigest() if system_proposal else _fingerprint(document, sources)
            stale = topic["version"] != job["base_version"]
            duplicate = conn.execute("SELECT * FROM candidates WHERE topic_id=? AND base_version=? AND kind=? AND content_hash=?", (topic["id"], job["base_version"], job["kind"], content_hash)).fetchone()
            no_change = not system_proposal and _fingerprint(topic["document"], topic["sources"]) == content_hash
            if not stale and (duplicate is not None or no_change):
                conn.execute("UPDATE jobs SET status='noop',owner=NULL,lease_expires_at=NULL,result_id=?,updated_at=? WHERE id=?", (duplicate["id"] if duplicate else None, now, job_id))
                self._event(conn, topic["id"], "job.noop", {"job_id": job_id, "reason": "duplicate candidate" if duplicate else "unchanged document"}, now)
                return {"status": "noop", "job_id": job_id, "candidate_id": duplicate["id"] if duplicate else None}
            if duplicate is not None:
                candidate_id = duplicate["id"]
            else:
                candidate_id = _new_id()
                conn.execute("INSERT INTO candidates(id,topic_id,job_id,kind,base_version,status,document_json,sources_json,proposal_json,content_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (candidate_id, topic["id"], job_id, job["kind"], job["base_version"], "stale" if stale else "pending", _json(document) if document is not None else None, _json(sources), _json(proposal), content_hash, now, now))
                self._event(conn, topic["id"], "candidate.stale" if stale else "candidate.created", {"candidate_id": candidate_id, "job_id": job_id, "base_version": job["base_version"], "current_version": topic["version"]}, now)
            if not stale and topic["auto_apply"] and not system_proposal:
                self._apply(conn, candidate_id, now, automatic=True)
            conn.execute("UPDATE jobs SET status=?,owner=NULL,lease_expires_at=NULL,result_id=?,updated_at=? WHERE id=?", ("stale" if stale else "completed", candidate_id, now, job_id))
            return self._candidate(conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone())

    def get_candidate(self, candidate_id):
        with self._lock:
            return self._candidate(self._conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone())

    def list_candidates(self, topic_id=None, status=None):
        sql, clauses, params = "SELECT * FROM candidates", [], []
        if topic_id is not None:
            clauses.append("topic_id=?")
            params.append(topic_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC,id"
        with self._lock:
            return [self._candidate(row) for row in self._conn.execute(sql, params).fetchall()]

    def _apply(self, conn, candidate_id, now, *, automatic=False):
        candidate = self._candidate(conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone())
        if candidate["status"] in {"applied", "accepted"}:
            return candidate
        if candidate["status"] != "pending":
            raise ValueError("only pending candidates can be applied")
        topic = self._topic(conn.execute("SELECT * FROM topics WHERE id=?", (candidate["topic_id"],)).fetchone())
        if candidate["base_version"] != topic["version"]:
            raise ValueError("candidate is stale; topic version changed")
        if candidate["kind"] in {"selfupdate", "self_update", "system_update"}:
            conn.execute("UPDATE candidates SET status='accepted',updated_at=? WHERE id=?", (now, candidate_id))
            self._event(conn, topic["id"], "system.proposal.accepted", {"candidate_id": candidate_id, "runtime_code_changed": False}, now)
            return self._candidate(conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone())
        document = _document(candidate["document"], candidate["sources"])
        if automatic:
            old_claims = {claim["id"]: claim for claim in topic["document"]["claims"]}
            new_claims = {claim["id"]: claim for claim in document["claims"]}
            old_questions = {item["id"] for item in topic["document"]["questions"] if isinstance(item, dict) and "id" in item}
            new_questions = {item["id"] for item in document["questions"] if isinstance(item, dict) and "id" in item}
            old_question_text = {item["id"]: item["text"] for item in topic["document"]["questions"] if isinstance(item, dict)}
            new_question_text = {item["id"]: item["text"] for item in document["questions"] if isinstance(item, dict)}
            issues = {
                "unconfirmed_proposal": candidate["proposal"].get("requires_review") is True,
                "removed_claim_ids": sorted(set(old_claims) - set(new_claims)),
                "removed_question_ids": sorted(old_questions - new_questions),
                "replaced_question_ids": sorted(question_id for question_id, text in old_question_text.items() if question_id in new_question_text and new_question_text[question_id] != text),
                "changed_user_claim_ids": sorted(claim_id for claim_id, claim in old_claims.items() if claim_id.startswith("user-") and claim_id in new_claims and claim["text"] != new_claims[claim_id]["text"]),
                "replaced_claim_ids": sorted(claim_id for claim_id, claim in old_claims.items() if claim_id in new_claims and claim["text"] != new_claims[claim_id]["text"]),
                "removed_legacy_questions": [q for q in topic["document"]["questions"] if isinstance(q, str) and q not in document["questions"]],
            }
            if any(issues.values()):
                proposal = candidate["proposal"]
                proposal["review_reason"] = "候选含未确认假设、重构，或删改已有分支与用户观点，需要明确审阅。"
                proposal["review_issues"] = issues
                conn.execute("UPDATE candidates SET proposal_json=?,updated_at=? WHERE id=?", (_json(proposal), now, candidate_id))
                self._event(conn, topic["id"], "candidate.review_required", {"candidate_id": candidate_id, **issues}, now)
                return self._candidate(conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone())
        version = topic["version"] + 1
        conn.execute("UPDATE topics SET document_json=?,sources_json=?,version=?,updated_at=? WHERE id=?", (_json(document), _json(candidate["sources"]), version, now, topic["id"]))
        conn.execute("UPDATE candidates SET status='applied',applied_version=?,updated_at=? WHERE id=?", (version, now, candidate_id))
        self._invalidate(conn, topic["id"], version, now, exclude_candidate=candidate_id)
        self._event(conn, topic["id"], "candidate.applied", {"candidate_id": candidate_id, "base_version": topic["version"], "version": version, "automatic": automatic}, now)
        return self._candidate(conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone())

    def apply_candidate(self, candidate_id):
        with self._transaction() as conn:
            return self._apply(conn, candidate_id, time.time())

    def reject_candidate(self, candidate_id, reason=""):
        with self._transaction() as conn:
            candidate = self._candidate(conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone())
            if candidate["status"] != "pending":
                raise ValueError("only pending candidates can be rejected")
            now = time.time()
            conn.execute("UPDATE candidates SET status='rejected',updated_at=? WHERE id=?", (now, candidate_id))
            self._event(conn, candidate["topic_id"], "candidate.rejected", {"candidate_id": candidate_id, "reason": reason}, now)
            return self._candidate(conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone())

    def _day(self, now=None):
        return datetime.fromtimestamp(_timestamp(now), self.budget_timezone).date().isoformat()

    def next_budget_reset(self, *, now=None):
        local = datetime.fromtimestamp(_timestamp(now), self.budget_timezone)
        return (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()

    def reserve_usage(self, kind="llm", units=1, limit=None):
        _text(kind, "kind", empty=False)
        _positive_int(units, "units")
        if limit is None:
            limit = 120 if kind == "voice" else self.daily_call_limit
        _positive_int(limit, "limit", zero=True)
        if kind == "llm":
            limit = min(limit, self.daily_call_limit)
        day = self._day()
        with self._transaction() as conn:
            row = conn.execute("SELECT units,limit_units FROM usage WHERE day=? AND kind=?", (day, kind)).fetchone()
            used = row["units"] if row else 0
            if row and row["limit_units"] is not None:
                limit = min(limit, row["limit_units"])
            if used + units > limit:
                raise BudgetExceeded(f"{kind} daily call budget exhausted ({used}/{limit})")
            conn.execute("INSERT INTO usage(day,kind,units,limit_units) VALUES(?,?,?,?) ON CONFLICT(day,kind) DO UPDATE SET units=excluded.units,limit_units=excluded.limit_units", (day, kind, used + units, limit))
        return {"day": day, "kind": kind, "units": units, "used": used + units, "limit": limit, "remaining": limit - used - units}

    def get_usage(self):
        day = self._day()
        with self._lock:
            counts = {row["kind"]: (row["units"], row["limit_units"]) for row in self._conn.execute("SELECT kind,units,limit_units FROM usage WHERE day=?", (day,)).fetchall()}
        result = {"day": day, "timezone": str(self.budget_timezone), "resets_at": self.next_budget_reset()}
        for kind in set(counts) | {"llm", "voice"}:
            default_limit = 120 if kind == "voice" else self.daily_call_limit
            used, stored_limit = counts.get(kind, (0, default_limit))
            limit = default_limit if stored_limit is None else stored_limit
            if kind == "llm":
                limit = min(limit, self.daily_call_limit)
            result[kind] = {"used": used, "calls": used, "limit": limit, "remaining": max(0, limit - used)}
        return result

    def export_topic(self, topic_id):
        # Hold one read transaction so every exported part describes the same instant.
        with self._transaction():
            topic = self.get_topic(topic_id)
            return {
                "schema_version": 1, "exported_at": time.time(), "topic": topic,
                "candidates": self.list_candidates(topic_id), "jobs": self.list_jobs(topic_id),
                "events": list(reversed(self.list_events(topic_id, limit=None))),
                "handoff": {
                    "objective": topic["goal"], "current_version": topic["version"],
                    "rules": ["Current topic document is the shared workspace.", "Claims marked supported must cite known source IDs.", "Research results are proposals against base_version; stale proposals cannot overwrite corrections.", "Review pending candidates before integration unless auto_apply is explicitly enabled.", "System improvement proposals never edit runtime code."],
                },
            }
