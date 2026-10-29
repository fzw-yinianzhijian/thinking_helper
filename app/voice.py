"""Small, cancellable DashScope audio bridges. No provider credentials reach clients.

Browser protocol:
  ASR: {type: start, silenceMs?: 1100}, PCM16/16kHz binary, {type: stop}.
       Server emits ready, speech, partial, final, done (or error).
  TTS: {type: text, text: ...}, then optional {type: cancel}.
       Server emits ready, first_audio, PCM16/24kHz binary, done (or error).

reserve_usage(kind=..., units=...) reserves weighted characters for TTS or
audio seconds for ASR. It may be synchronous or asynchronous and must raise
when the configured budget has been exhausted.
"""

import asyncio
import inspect
import json
import math
import re
import time
import uuid
from contextlib import suppress
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect


DEFAULT_INSTRUCTION = "用平静理性的普通话自然讲解，语速适中，语调平稳，重点轻微强调。"
ASR_BLOCK_SECONDS = 30
MAX_ASR_SECONDS = 1800
MAX_TTS_CHARACTERS = 8000


def weighted_characters(text: str) -> int:
    return sum(2 if "\u3400" <= char <= "\u9fff" or "\U00020000" <= char <= "\U000323af" else 1 for char in text)


def _error(exc: Exception, key: str) -> str:
    message = str(exc) or exc.__class__.__name__
    if key:
        message = message.replace(key, "[已隐藏]")
    return re.sub(r"sk-[\w-]+", "[已隐藏]", message)[:450]


def _command(action: str, task_id: str, input_data: Any = None) -> str:
    return json.dumps({"header": {"action": action, "task_id": task_id, "streaming": "duplex"},
                       "payload": {"input": input_data or {}}}, ensure_ascii=False)


def _run_task(task_id: str, task: str, model: str, parameters: dict) -> str:
    return json.dumps({"header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                       "payload": {"task_group": "audio", "task": task,
                                   "function": "recognition" if task == "asr" else "SpeechSynthesizer",
                                   "model": model, "parameters": parameters, "input": {}}}, ensure_ascii=False)


def _same_origin(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    if not origin:  # Non-browser clients still pass through the application's authentication.
        return True
    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == ws.headers.get("host", "").lower()


async def _reserve(callback: Callable, kind: str, units: int) -> None:
    result = callback(kind=kind, units=units)
    if inspect.isawaitable(result):
        result = await result
    if result is False:
        raise RuntimeError("语音预算已用完，请调整预算后重试。")


async def _race(*coroutines) -> None:
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def install_voice(app: FastAPI, settings: Any, reserve_usage: Callable) -> None:
    """Install ASR/TTS routes, using only settings for cloud credentials."""

    async def prepare(ws: WebSocket) -> bool:
        if not _same_origin(ws):
            await ws.close(code=1008, reason="来源不匹配")
            return False
        await ws.accept()
        if not getattr(settings, "api_key", ""):
            await ws.send_json({"type": "error", "message": "服务端尚未配置语音 API Key。"})
            await ws.close(code=1000)
            return False
        return True

    async def report_error(ws: WebSocket, exc: Exception) -> None:
        with suppress(Exception):
            await ws.send_json({"type": "error", "message": _error(exc, settings.api_key)})

    def upstream_url() -> str:
        host = settings.upstream_host.strip().rstrip("/")
        if "://" in host:
            host = urlsplit(host).netloc
        if not host or "/" in host or "@" in host:
            raise RuntimeError("语音服务地址配置无效。")
        return "wss://" + host + "/api-ws/v1/inference"

    @app.websocket("/ws/asr")
    async def asr(ws: WebSocket) -> None:
        if not await prepare(ws):
            return
        task_id = str(uuid.uuid4())
        started = time.monotonic()
        audio_bytes = 0
        reserved_seconds = 0
        ready = asyncio.Event()
        finishing = False
        seen_finals = set()
        try:
            data = await asyncio.wait_for(ws.receive_json(), timeout=20)
            if data.get("type") != "start":
                raise ValueError("请先启动麦克风识别。")
            silence_ms = data.get("silenceMs", getattr(settings, "asr_silence_ms", 1100))
            if not isinstance(silence_ms, int) or not 200 <= silence_ms <= 6000:
                raise ValueError("停顿阈值需为 200–6000 毫秒。")
            await _reserve(reserve_usage, "asr", ASR_BLOCK_SECONDS)
            reserved_seconds = ASR_BLOCK_SECONDS
            async with connect(upstream_url(), additional_headers={"Authorization": "Bearer " + settings.api_key},
                               open_timeout=15, close_timeout=2, max_size=2**20, max_queue=16) as upstream:
                await upstream.send(_run_task(task_id, "asr", getattr(settings, "asr_model", "fun-asr-realtime"),
                                              {"format": "pcm", "sample_rate": 16000,
                                               "max_sentence_silence": silence_ms,
                                               "semantic_punctuation_enabled": False, "heartbeat": True}))

                async def from_browser() -> None:
                    nonlocal audio_bytes, reserved_seconds, finishing
                    await asyncio.wait_for(ready.wait(), timeout=20)
                    while True:
                        frame = await asyncio.wait_for(ws.receive(), timeout=45)
                        if frame["type"] == "websocket.disconnect":
                            return
                        binary = frame.get("bytes")
                        if binary is not None:
                            if finishing:
                                continue
                            if len(binary) > 65536 or len(binary) % 2:
                                raise ValueError("麦克风音频帧格式无效。")
                            audio_bytes += len(binary)
                            seconds = audio_bytes / 32000
                            if seconds > MAX_ASR_SECONDS:
                                raise RuntimeError("本次麦克风已连续开启 30 分钟，请结束后按剩余额度重新开启。")
                            if seconds > reserved_seconds:
                                extra = math.ceil((seconds - reserved_seconds) / ASR_BLOCK_SECONDS) * ASR_BLOCK_SECONDS
                                await _reserve(reserve_usage, "asr", extra)
                                reserved_seconds += extra
                            await upstream.send(binary)
                        elif frame.get("text") is not None:
                            control = json.loads(frame["text"])
                            if control.get("type") == "cancel":
                                await ws.send_json({"type": "cancelled"})
                                return
                            if control.get("type") == "stop" and not finishing:
                                finishing = True
                                await upstream.send(_command("finish-task", task_id))
                            else:
                                raise ValueError("未知麦克风操作。")

                async def from_cloud() -> None:
                    while True:
                        raw = await asyncio.wait_for(upstream.recv(), timeout=45)
                        if not isinstance(raw, str):
                            continue
                        event = json.loads(raw)
                        header = event.get("header", {})
                        name = header.get("event")
                        if name == "task-started":
                            ready.set()
                            await ws.send_json({"type": "ready", "sampleRate": 16000,
                                                "connectionMs": round((time.monotonic() - started) * 1000)})
                        elif name == "task-failed":
                            raise RuntimeError(str(header.get("error_code", "识别失败")) + ": " +
                                               str(header.get("error_message", "语音识别任务失败。")))
                        elif name == "result-generated":
                            sentence = event.get("payload", {}).get("output", {}).get("sentence", {})
                            if sentence.get("heartbeat"):
                                continue
                            text = sentence.get("text", "")
                            sentence_id = sentence.get("sentence_id", sentence.get("begin_time"))
                            if sentence.get("sentence_begin"):
                                await ws.send_json({"type": "speech", "sentenceId": sentence_id})
                            if sentence.get("sentence_end"):
                                identity = (sentence_id, text)
                                if text.strip() and identity not in seen_finals:
                                    seen_finals.add(identity)
                                    await ws.send_json({"type": "final", "text": text, "sentenceId": sentence_id,
                                                        "beginMs": sentence.get("begin_time"), "endMs": sentence.get("end_time")})
                            elif text:
                                await ws.send_json({"type": "partial", "text": text, "sentenceId": sentence_id})
                        elif name == "task-finished":
                            await ws.send_json({"type": "done", "audioSeconds": round(audio_bytes / 32000, 2)})
                            return

                await _race(from_browser(), from_cloud())
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            await report_error(ws, exc)
        finally:
            with suppress(Exception):
                await ws.close(code=1000)

    @app.websocket("/ws/tts")
    async def tts(ws: WebSocket) -> None:
        if not await prepare(ws):
            return
        started = time.monotonic()
        task_id = str(uuid.uuid4())
        text_at = None
        first_at = None
        audio_bytes = 0
        try:
            data = await asyncio.wait_for(ws.receive_json(), timeout=20)
            text = data.get("text")
            if data.get("type") not in {"text", "start"} or not isinstance(text, str) or not text.strip():
                raise ValueError("请输入需要朗读的文本。")
            text = text.strip()
            if not any(char.isalnum() for char in text):
                raise ValueError("没有可朗读的文字，已跳过纯标点片段。")
            characters = weighted_characters(text)
            if characters > MAX_TTS_CHARACTERS:
                raise ValueError("一次朗读最多 8000 计费字符。")
            voice = getattr(settings, "voice_id", "")
            if not voice:
                raise ValueError("服务端尚未配置 CosyVoice 音色。")
            await _reserve(reserve_usage, "tts", characters)
            async with connect(upstream_url(), additional_headers={"Authorization": "Bearer " + settings.api_key},
                               open_timeout=15, close_timeout=2, max_size=2**20, max_queue=16) as upstream:
                parameters = {"text_type": "PlainText", "voice": voice, "format": "pcm", "sample_rate": 24000,
                              "enable_ssml": False, "rate": 1, "pitch": 1, "volume": 50,
                              "instruction": DEFAULT_INSTRUCTION}
                await upstream.send(_run_task(task_id, "tts", getattr(settings, "tts_model", "cosyvoice-v3.5-flash"), parameters))

                async def from_browser() -> None:
                    while True:
                        frame = await ws.receive()
                        if frame["type"] == "websocket.disconnect":
                            return
                        if frame.get("text") is not None:
                            command = json.loads(frame["text"])
                            if command.get("type") in {"cancel", "stop"}:
                                await ws.send_json({"type": "cancelled"})
                                return
                            raise ValueError("本连接仅朗读一段文本。")

                async def from_cloud() -> None:
                    nonlocal text_at, first_at, audio_bytes
                    while True:
                        raw = await asyncio.wait_for(upstream.recv(), timeout=45)
                        if isinstance(raw, bytes):
                            if not first_at:
                                first_at = time.monotonic()
                                await ws.send_json({"type": "first_audio", "afterTextMs": round((first_at - (text_at or started)) * 1000),
                                                    "totalMs": round((first_at - started) * 1000)})
                            audio_bytes += len(raw)
                            if audio_bytes > 24000 * 2 * 10 * 60:
                                raise RuntimeError("朗读音频超过本次上限。")
                            await ws.send_bytes(raw)
                            continue
                        event = json.loads(raw)
                        header = event.get("header", {})
                        name = header.get("event")
                        if name == "task-started":
                            await ws.send_json({"type": "ready", "sampleRate": 24000,
                                                "connectionMs": round((time.monotonic() - started) * 1000)})
                            text_at = time.monotonic()
                            await upstream.send(_command("continue-task", task_id, {"text": text}))
                            await upstream.send(_command("finish-task", task_id))
                        elif name == "task-failed":
                            raise RuntimeError(str(header.get("error_code", "合成失败")) + ": " +
                                               str(header.get("error_message", "语音合成任务失败。")))
                        elif name == "task-finished":
                            if not audio_bytes:
                                raise RuntimeError("语音服务结束但未返回音频，请检查模型与音色。")
                            await ws.send_json({"type": "done", "bytes": audio_bytes, "characters": characters,
                                                "audioSeconds": round(audio_bytes / 48000, 3),
                                                "totalMs": round((time.monotonic() - started) * 1000)})
                            return

                await _race(from_browser(), from_cloud())
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            await report_error(ws, exc)
        finally:
            with suppress(Exception):
                await ws.close(code=1000)
