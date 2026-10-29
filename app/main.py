import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import ROOT, Settings
from .core import Store, BudgetExceeded
from .engine import Engine
from .providers import Provider
from .voice import install_voice
from .version import RUNTIME_REVISION
from .planning import pending_user_inputs
from .evaluation import list_evaluations, get_evaluation

settings = Settings.load()
store = Store(settings.database, daily_call_limit=settings.daily_call_limit)
provider = Provider(settings, store)
engine = Engine(store, provider.research, settings=settings)


@asynccontextmanager
async def lifespan(app):
    await engine.start()
    yield
    await engine.stop()


app = FastAPI(title="Thinking Helper", lifespan=lifespan)


@app.middleware("http")
async def local_only(request, call_next):
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    hostname = urlparse("http://" + host).hostname
    if hostname not in ("127.0.0.1", "localhost", "testserver") or (origin and urlparse(origin).netloc != host):
        return JSONResponse({"error": "请通过本机地址或 SSH 隧道访问。"}, status_code=403)
    if int(request.headers.get("content-length", "0")) > 100000:
        return JSONResponse({"error": "请求过大。"}, status_code=413)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(ValueError)
async def bad_value(request, error):
    return JSONResponse({"error": settings.safe_error(error)}, status_code=409)


@app.exception_handler(KeyError)
async def missing_value(request, error):
    return JSONResponse({"error": "记录不存在。"}, status_code=404)


@app.exception_handler(BudgetExceeded)
async def budget_value(request, error):
    return JSONResponse({"error": settings.safe_error(error)}, status_code=429)


class NewTopic(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    goal: str = Field(default="", max_length=3000)


class Correction(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    version: int


class ChatInput(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    report: bool = False
    voice_mode: bool = False


class Rejection(BaseModel):
    reason: str = Field(default="", max_length=2000)


def ensure_input_job(topic_id):
    """Keep a new-version follow-up when integration invalidates an old job."""
    if not store.get_topic(topic_id)["paused"] and pending_user_inputs(
            store.list_events(topic_id, limit=None), store.list_candidates(topic_id)):
        store.enqueue_job(topic_id, kind="organize", run_at=time.time() + 20)
        engine.wake()


@app.get("/")
async def index():
    return FileResponse(ROOT / "app/static/index.html")


@app.get("/favicon.ico", status_code=204)
async def favicon():
    return None


@app.get("/static/{filename}")
async def static(filename: str):
    if filename not in ("app.js", "voice.js", "style.css", "mic-worklet.js"):
        raise HTTPException(404)
    return FileResponse(ROOT / "app/static" / filename)


@app.get("/api/status")
async def status():
    return {"service": "thinking-helper", "version": "0.1.0", "model": settings.model, "chat_model": settings.chat_model,
            "key_configured": bool(settings.api_key), "voice_configured": bool(settings.voice_id),
            "asr_model": settings.asr_model, "tts_model": settings.tts_model,
            "usage": store.get_usage(), "limits": {"llm": settings.daily_call_limit,
                        "tts": settings.tts_daily_chars, "asr": settings.asr_daily_seconds},
            "time": datetime.now(timezone.utc).isoformat(), "storage": "SQLite / durable jobs",
            "autonomous_code_editing": False, "scheduler_running": engine.running, "pid": os.getpid(),
            "runtime_revision": RUNTIME_REVISION}


@app.get("/api/topics")
async def topics():
    return store.list_topics()


@app.post("/api/topics", status_code=201)
async def create_topic(data: NewTopic):
    return store.create_topic(data.title.strip(), data.goal.strip())


@app.get("/api/topics/{topic_id}")
async def topic(topic_id: str):
    candidates = store.list_candidates(topic_id)
    all_events = store.list_events(topic_id, limit=None)
    events = all_events[:500]
    return {"topic": store.get_topic(topic_id), "jobs": store.list_jobs(topic_id),
            "candidates": candidates, "events": events,
            "evaluations": [{k: r.get(k) for k in ("id", "status", "reason", "runtime_revision", "baseline_revision", "candidate_id", "checks", "checks_ok", "product_value_verified")}
                            for r in list_evaluations(topic_id=topic_id)[:5]],
            "pending_inputs": pending_user_inputs(all_events, candidates)}


@app.get("/api/evaluations/{record_id}")
async def evaluation(record_id: str):
    return get_evaluation(record_id)


@app.patch("/api/topics/{topic_id}")
async def configure(topic_id: str, request: Request):
    changes = await request.json()
    allowed = {k: v for k, v in changes.items() if k in ("auto_apply", "interval_seconds")}
    if "interval_seconds" in allowed and not 300 <= allowed["interval_seconds"] <= 604800:
        raise ValueError("后台间隔应为 5 分钟至 7 天。")
    if "auto_apply" in allowed and type(allowed["auto_apply"]) is not bool:
        raise ValueError("auto_apply 必须为布尔值。")
    if "paused" in changes and type(changes["paused"]) is not bool:
        raise ValueError("paused 必须为布尔值。")
    result = store.update_topic(topic_id, **allowed) if allowed else store.get_topic(topic_id)
    if "paused" in changes:
        result = store.pause_topic(topic_id) if changes["paused"] else store.resume_topic(topic_id)
    engine.wake()
    return result


@app.post("/api/topics/{topic_id}/correction")
async def correct(topic_id: str, data: Correction):
    current = store.get_topic(topic_id)
    if current["version"] != data.version:
        raise ValueError("结构刚刚更新，请刷新后再补充，避免覆盖。")
    doc = json.loads(json.dumps(current["document"]))
    doc.setdefault("claims", []).append({"id": "user-" + uuid4().hex[:10], "text": data.text,
                                           "status": "hypothesis", "source_ids": []})
    result = store.correct_topic(topic_id, doc, note="用户补充/纠正：" + data.text, expected_version=data.version)
    ensure_input_job(topic_id)
    return result


@app.post("/api/topics/{topic_id}/research", status_code=202)
async def research(topic_id: str):
    job = store.enqueue_job(topic_id, kind="research")
    engine.wake()
    return job


@app.post("/api/topics/{topic_id}/reflect", status_code=202)
async def reflect(topic_id: str):
    job = store.enqueue_job(topic_id, kind="selfupdate")
    engine.wake()
    return job


@app.post("/api/jobs/{job_id}/cancel")
async def cancel(job_id: str):
    return store.cancel_job(job_id)


@app.post("/api/candidates/{candidate_id}/apply")
async def apply(candidate_id: str):
    result = store.apply_candidate(candidate_id)
    if result["status"] == "applied":
        ensure_input_job(result["topic_id"])
    return result


@app.post("/api/candidates/{candidate_id}/reject")
async def reject(candidate_id: str, data: Rejection = Rejection()):
    return store.reject_candidate(candidate_id, reason=data.reason)


@app.get("/api/topics/{topic_id}/export")
async def export(topic_id: str):
    data = store.export_topic(topic_id)
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="thinking-{topic_id}.json"'})


@app.get("/api/guide/{name}")
async def guide(name: str):
    names = {"brief": ROOT / "docs/BRIEF.md", "readme": ROOT / "README.md", "handoff": ROOT / "docs/HANDOFF.md",
             "status": ROOT / "docs/STATUS.md", "product": ROOT / "docs/PRODUCT.md",
             "architecture": ROOT / "docs/ARCHITECTURE.md", "decisions": ROOT / "docs/DECISIONS.md"}
    if name not in names or not names[name].exists():
        raise HTTPException(404)
    return {"text": names[name].read_text()}


@app.post("/api/topics/{topic_id}/playback")
async def playback(topic_id: str, request: Request):
    data = await request.json()
    store.add_event(topic_id, "playback", {"text": str(data.get("text", ""))[:2000],
                                          "complete": bool(data.get("complete")),
                                          "played_ms": max(0, min(180000, float(data.get("played_ms") or 0))),
                                          "segment_id": str(data.get("segment_id", ""))[:120],
                                          "measurement": "web_audio_timeline_estimate",
                                          "report_id": str(data.get("report_id", ""))[:100]})
    return {"ok": True}


@app.post("/api/topics/{topic_id}/observation")
async def observation(topic_id: str, request: Request):
    data = await request.json()
    kind = data.get("kind")
    if kind == "backchannel":
        item = {"text": str(data.get("text", ""))[:50]}
    elif kind == "latency":
        item = {k: v for k, v in data.items() if k in ("first_pcm_ms", "submit_to_scheduled_output_ms")
                and isinstance(v, (int, float)) and 0 <= v < 180000}
        item["report_id"] = str(data.get("report_id", ""))[:100]
        item["measurement"] = "submit_to_web_audio_scheduled_output_not_physical_loopback"
    else:
        raise ValueError("未知观察类型。")
    store.add_event(topic_id, "interaction." + kind, item)
    return {"ok": True}


@app.post("/api/topics/{topic_id}/chat")
async def chat(topic_id: str, data: ChatInput, request: Request):
    snapshot = store.get_topic(topic_id)
    events = store.list_events(topic_id)
    history = []
    invalid_reports = {e["data"].get("report_id"): e["data"].get("reason", "旧汇报已被复核为错误")
                       for e in events if e["kind"] == "interaction.report_review" and e["data"].get("valid") is False}
    played = {}
    partial = {}
    for event in reversed(events):
        if event["kind"] == "playback":
            p = event["data"]
            rid = p.get("report_id")
            if p.get("complete"):
                played.setdefault(rid, {})[p.get("segment_id") or str(event["id"])] = p.get("text", "")
            else:
                partial[rid] = p
    unheard = []
    for event in reversed(events):
        if event["kind"] in ("user_message", "assistant_message"):
            item = event.get("data", {})
            content = item.get("text", "")
            if event["kind"] == "assistant_message" and item.get("report_id") in invalid_reports:
                history.append({"role": "assistant", "content": "旧汇报已被复核为错误，不可沿用：" + invalid_reports[item["report_id"]]})
                continue
            if event["kind"] == "assistant_message" and item.get("voice_mode"):
                rid = item.get("report_id")
                confirmed = "".join(played.get(rid, {}).values())
                interrupted = partial.get(rid)
                content = "语音界面已完整播放的段落：" + (confirmed or "无完整段落。")
                if interrupted:
                    content += f"\n被打断段落：{interrupted.get('text', '')}；仅有播放进度估计，不能假设完整听到。"
                if item.get("text") != confirmed:
                    unheard.append({"report_id": rid, "generated_text": item.get("text", ""),
                                    "played_complete_text": confirmed, "note": "未确认播放的内容不能当作用户已知。"})
            history.append({"role": "user" if event["kind"] == "user_message" else "assistant",
                            "content": content})
    store.add_event(topic_id, "user_message", {"text": data.text, "version": snapshot["version"],
                                               "report": data.report, "voice_mode": data.voice_mode})
    if not data.report and not snapshot["paused"]:
        # One delayed job coalesces adjacent utterances; raw speech is never
        # silently promoted to a confirmed claim or used to change the goal.
        store.enqueue_job(topic_id, kind="organize", run_at=time.time() + 20)
        engine.wake()
    # Both foreground and background report the same durable state.
    candidates = store.list_candidates(topic_id)
    live = {"jobs": [{"kind": j["kind"], "status": j["status"]} for j in store.list_jobs(topic_id)[:6]],
            "candidates": [{"summary": c.get("summary"), "status": c["status"], "kind": c["kind"],
                            "note": "方法建议不是已执行的代码变更" if c["kind"] == "selfupdate" else "仅applied已进入当前结构"}
                           for c in candidates[:6]]}
    latest_changes = [{"summary": c.get("summary"), "base_version": c["base_version"],
                       "applied_version": c.get("applied_version"), "reflection": c.get("reflection")}
                      for c in candidates if c["status"] == "applied"][:3]
    snapshot["document"] = {**snapshot["document"], "current_background": live, "latest_integrated_changes": latest_changes,
                            "pending_user_inputs": pending_user_inputs(events, candidates), "unheard_reports": unheard[-2:]}
    report_id = uuid4().hex

    async def stream():
        content, completed = "", False
        def emit(event):
            return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        try:
            yield emit({"type": "start", "version": snapshot["version"], "report_id": report_id})
            async for delta in provider.stream_chat(snapshot, data.text, history, report=data.report):
                if await request.is_disconnected():
                    break
                content += delta
                yield emit({"type": "delta", "text": delta})
            else:
                completed = True
            yield emit({"type": "done", "report_id": report_id})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield emit({"type": "error", "error": settings.safe_error(exc)})
        finally:
            store.add_event(topic_id, "assistant_message", {"text": content, "complete": completed,
                            "version": snapshot["version"], "report_id": report_id,
                            "voice_mode": data.voice_mode, "runtime_revision": RUNTIME_REVISION,
                            "note": "生成记录不代表已完整播放；见playback事件。"})
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


def reserve_voice(kind, units):
    limit = settings.tts_daily_chars if kind == "tts" else settings.asr_daily_seconds
    return store.reserve_usage(kind=kind, units=units, limit=limit)


install_voice(app, settings, reserve_voice)
