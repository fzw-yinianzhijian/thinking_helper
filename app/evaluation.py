"""Trace a manual method change to fixed local checks, never execute a proposal.

checks_passed means regression checks passed on a changed source revision. It
does not establish usefulness, voice quality, causal improvement or rollback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .version import RUNTIME_REVISION


ROOT = Path(__file__).resolve().parent.parent
CHECKS = {
    "core": "test_core.py",
    "planning": "test_product_scenarios.py",
    "provider": "test_provider.py",
    "reporting": "test_reporting.py",
    "evidence": "test_evidence.py",
    "api": "test_api.py",
    "voice": "test_voice.py",
    "evaluation": "test_evaluation.py",
    "all": None,
}
_RECORD_ID = re.compile(r"eval-[0-9a-f]{32}\Z")
_REVISION = re.compile(r"[0-9a-f]{16}\Z")


def _revision(root):
    app = root / "app"
    digest = hashlib.sha256()
    for path in sorted(app.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".js", ".html", ".css"}:
            digest.update(str(path.relative_to(app)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _directory(output_dir=None, project_root=None):
    return Path(output_dir) if output_dir is not None else Path(project_root or ROOT) / "data" / "evaluations"


def _selected(checks):
    if isinstance(checks, str):
        checks = [checks]
    selected = list(dict.fromkeys(checks))
    if not selected or any(check not in CHECKS for check in selected):
        raise ValueError("checks must be selected from: " + ", ".join(CHECKS))
    if "all" in selected and len(selected) != 1:
        raise ValueError("all cannot be combined with individual checks")
    return selected


def plan_evaluation(*, checks=("core", "planning"), timeout_seconds=120, project_root=None):
    """Return the exact allowlisted commands without executing or recording."""
    root = Path(project_root or ROOT).resolve()
    if not (root / "app").is_dir() or not (root / "tests").is_dir():
        raise ValueError("project_root must contain app/ and tests/")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 1800:
        raise ValueError("timeout_seconds must be greater than zero and at most 1800")
    selected = _selected(checks)
    commands = []
    for name in selected:
        filename = CHECKS[name]
        if filename is not None and not (root / "tests" / filename).is_file():
            raise ValueError(f"fixed check file is missing: {filename}")
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
        if filename is not None:
            command += ["-p", filename]
        command += ["-v"]
        commands.append({"check": name, "command": command})
    return {"executed": False, "project_root": str(root), "runtime_revision": _revision(root),
            "checks": selected, "commands": commands, "timeout_seconds": timeout_seconds}


def get_evaluation(record_id, *, output_dir=None, project_root=None):
    if not isinstance(record_id, str) or not _RECORD_ID.fullmatch(record_id):
        raise ValueError("invalid evaluation record ID")
    path = _directory(output_dir, project_root) / (record_id + ".json")
    if not path.is_file():
        raise KeyError("evaluation record not found")
    with path.open(encoding="utf-8") as handle:
        record = json.load(handle)
    if not isinstance(record, dict) or record.get("id") != record_id:
        raise ValueError("invalid evaluation record contents")
    return record


def list_evaluations(*, output_dir=None, project_root=None, topic_id=None, candidate_id=None):
    result = []
    for path in _directory(output_dir, project_root).glob("eval-*.json"):
        try:
            record = get_evaluation(path.stem, output_dir=path.parent)
        except (KeyError, ValueError, OSError):
            continue
        if topic_id is not None and record.get("topic_id") != topic_id:
            continue
        if candidate_id is not None and record.get("candidate_id") != candidate_id:
            continue
        result.append(record)
    return sorted(result, key=lambda item: item.get("started_at", 0), reverse=True)


def _save(record, directory):
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / ("." + record["id"] + "-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / (record["id"] + ".json"))
    finally:
        if temporary.exists():
            temporary.unlink()


def _output(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = re.sub(r"sk-[\w-]+", "[redacted]", str(value or ""))
    return value[-16000:]


def _reference(value, name):
    if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 500):
        raise ValueError(f"{name} must be a non-empty reference of at most 500 characters")
    return value


def run_evaluation(store, *, topic_id=None, candidate_id=None, checks=("core", "planning"),
                   baseline_ref=None, rollback_ref=None, change_ref=None,
                   timeout_seconds=120, project_root=None, output_dir=None, _runner=None):
    """Execute allowlisted tests and save their actual outcome before auditing.

    baseline_ref resolves an earlier evaluation ID. Arbitrary git labels remain
    annotations, never proof of an observed baseline revision. change_ref binds
    a manually implemented change; rollback_ref is recorded, never executed.
    _runner is a test adapter and is explicitly marked in saved test records.
    """
    plan = plan_evaluation(checks=checks, timeout_seconds=timeout_seconds, project_root=project_root)
    root, directory = Path(plan["project_root"]), _directory(output_dir, project_root)
    baseline_ref = _reference(baseline_ref, "baseline_ref")
    rollback_ref = _reference(rollback_ref, "rollback_ref")
    change_ref = _reference(change_ref, "change_ref")
    candidate = None
    if candidate_id is not None:
        candidate = store.get_candidate(candidate_id)
        if candidate["kind"] not in {"selfupdate", "self_update", "system_update"}:
            raise ValueError("method evaluation must reference a system improvement proposal")
        if topic_id is not None and topic_id != candidate["topic_id"]:
            raise ValueError("candidate belongs to another topic")
        topic_id = candidate["topic_id"]
    if topic_id is not None:
        store.get_topic(topic_id)
    baseline = None
    if baseline_ref is not None and _RECORD_ID.fullmatch(baseline_ref):
        try:
            baseline = get_evaluation(baseline_ref, output_dir=directory)
        except KeyError:
            pass
    baseline_revision = None
    if baseline and baseline.get("execution_mode") == "subprocess" and _REVISION.fullmatch(str(baseline.get("runtime_revision", ""))):
        baseline_revision = baseline["runtime_revision"]
    started = time.time()
    record = {
        "id": "eval-" + uuid.uuid4().hex, "schema_version": 1,
        "topic_id": topic_id, "candidate_id": candidate_id,
        "runtime_revision": plan["runtime_revision"],
        "running_runtime_revision": RUNTIME_REVISION if root == ROOT else None,
        "candidate_runtime_revision": candidate.get("runtime_revision") if candidate else None,
        "baseline_ref": baseline_ref, "baseline_revision": baseline_revision,
        "rollback_ref": rollback_ref, "rollback_verified": False,
        "change_ref": change_ref, "started_at": started, "finished_at": None,
        "checks": plan["checks"], "results": [], "timeout_seconds": timeout_seconds,
        "execution_mode": "test_adapter" if _runner is not None else "subprocess",
        "strategy_changed": False, "product_value_verified": False,
        "status": "baseline_only", "reason": "", "checks_ok": False,
    }
    record["record_path"] = str(directory / (record["id"] + ".json"))
    deadline = time.monotonic() + timeout_seconds
    runner = _runner or subprocess.run
    # Tests never receive production credentials or the live workspace database.
    with tempfile.TemporaryDirectory(prefix="thinking-evaluation-") as isolated:
        env = os.environ.copy()
        env.update({"TH_DATABASE": str(Path(isolated) / "workspace.sqlite3"),
                    "TH_CREDENTIALS": str(Path(isolated) / "no-credentials.json"),
                    "DASHSCOPE_API_KEY": "evaluation-offline-no-credential",
                    "TH_VOICE_ID": "evaluation-offline-no-voice",
                    "PYTHONPATH": str(root)})
        for item in plan["commands"]:
            remaining = deadline - time.monotonic()
            outcome = {"check": item["check"], "command": item["command"], "returncode": None,
                       "timed_out": False, "tests_run": 0, "passed": False, "stdout": "", "stderr": ""}
            before = time.monotonic()
            try:
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(item["command"], timeout_seconds)
                completed = runner(item["command"], cwd=str(root), env=env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=remaining, check=False)
                outcome.update(returncode=completed.returncode, stdout=_output(completed.stdout), stderr=_output(completed.stderr))
                match = re.search(r"Ran (\d+) tests? in", outcome["stdout"] + "\n" + outcome["stderr"])
                outcome["tests_run"] = int(match.group(1)) if match else 0
                outcome["passed"] = completed.returncode == 0 and outcome["tests_run"] > 0
                if completed.returncode == 0 and not outcome["passed"]:
                    outcome["error"] = "No unittest cases were confirmed as executed."
            except subprocess.TimeoutExpired as error:
                outcome.update(timed_out=True, stdout=_output(error.stdout), stderr=_output(error.stderr), error="Evaluation timeout; checks did not finish.")
            except Exception as error:
                outcome["error"] = f"{type(error).__name__}: could not execute the fixed check."
            outcome["duration_seconds"] = round(time.monotonic() - before, 4)
            record["results"].append(outcome)
    record["finished_at"] = time.time()
    record["source_revision_after"] = _revision(root)
    record["source_changed_during_run"] = record["source_revision_after"] != record["runtime_revision"]
    record["checks_ok"] = all(item["passed"] for item in record["results"]) and bool(record["results"]) and not record["source_changed_during_run"]
    if not record["checks_ok"]:
        record["status"] = "checks_failed"
        record["reason"] = "执行失败、超时、未发现测试，或检查期间源码发生变化。"
    elif _runner is not None:
        record["status"] = "baseline_only"
        record["reason"] = "测试适配器结果，不作为真实子进程通过证据。"
    elif baseline_revision is None or not change_ref or baseline_revision == record["runtime_revision"]:
        record["status"] = "baseline_only"
        record["reason"] = "检查通过；尚未绑定可追溯变更及不同版本的实际基准，仅记录基线。"
    else:
        record["status"] = "checks_passed"
        record["reason"] = "变更版本通过所列回归；尚未证明产品价值、改善因果或回退成功。"
    _save(record, directory)
    event = store.add_event(topic_id, "system.evaluated", {
        key: record[key] for key in ("id", "candidate_id", "runtime_revision", "baseline_ref", "baseline_revision", "rollback_ref", "change_ref", "checks", "checks_ok", "status", "reason", "record_path", "product_value_verified", "strategy_changed")
    })
    record["event_id"] = event["id"]
    _save(record, directory)
    return record
