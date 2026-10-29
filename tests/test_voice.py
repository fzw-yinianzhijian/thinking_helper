"""Protocol-only tests: no microphone, cloud connections, or paid inference."""

import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.voice import install_voice, weighted_characters


class FakeUpstream:
    def __init__(self, finish=True, failure=None):
        self.finish = finish
        self.failure = failure
        self.sent = []
        self.closed = False
        self.queue = None
        self.task = None

    async def __aenter__(self):
        self.queue = asyncio.Queue()
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send(self, raw):
        self.sent.append(raw)
        if isinstance(raw, bytes):
            for sentence in [
                {"sentence_id": 1, "text": "你好", "sentence_begin": True, "sentence_end": False},
                {"sentence_id": 1, "text": "你好，继续思考。", "sentence_end": True},
                {"sentence_id": 1, "text": "你好，继续思考。", "sentence_end": True},
                {"sentence_id": 0, "text": "", "heartbeat": True},
            ]:
                await self.queue.put(json.dumps({"header": {"event": "result-generated"},
                                                 "payload": {"output": {"sentence": sentence}}}))
            return
        data = json.loads(raw)
        action = data["header"]["action"]
        if action == "run-task":
            self.task = data["payload"]["task"]
            if self.failure:
                await self.queue.put(json.dumps({"header": {"event": "task-failed", "error_code": "Denied",
                                                             "error_message": self.failure}}))
            else:
                await self.queue.put(json.dumps({"header": {"event": "task-started"}}))
        elif action == "continue-task":
            await self.queue.put(b"\x00\x00\xff\x7f\x00\x80")
        elif action == "finish-task" and self.finish:
            await self.queue.put(json.dumps({"header": {"event": "task-finished"}}))

    async def recv(self):
        return await self.queue.get()


class VoiceProtocolTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(api_key="sk-secret-test", upstream_host="test.invalid", voice_id="test-voice",
                                        tts_model="cosyvoice-v3.5-flash", asr_model="fun-asr-realtime")
        self.usage = []
        self.app = FastAPI()
        install_voice(self.app, self.settings, self.reserve)
        self.client = TestClient(self.app)

    def reserve(self, *, kind, units):
        self.usage.append((kind, units))

    def test_weighted_characters(self):
        self.assertEqual(weighted_characters("你好AI"), 6)
        self.assertEqual(weighted_characters("\U00020001"), 2)

    def test_tts_streams_real_protocol_and_pcm(self):
        upstream = FakeUpstream()
        calls = []

        def fake_connect(url, **kwargs):
            calls.append((url, kwargs))
            return upstream

        with patch("app.voice.connect", fake_connect), self.client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"type": "text", "text": "你好AI"})
            self.assertEqual(ws.receive_json()["type"], "ready")
            self.assertEqual(ws.receive_json()["type"], "first_audio")
            self.assertEqual(ws.receive_bytes(), b"\x00\x00\xff\x7f\x00\x80")
            done = ws.receive_json()
            self.assertEqual(done["type"], "done")
            self.assertEqual(done["characters"], 6)
        self.assertTrue(upstream.closed)
        self.assertEqual(self.usage, [("tts", 6)])
        url, kwargs = calls[0]
        self.assertEqual(url, "wss://test.invalid/api-ws/v1/inference")
        self.assertEqual(kwargs["additional_headers"]["Authorization"], "Bearer sk-secret-test")
        commands = [json.loads(item) for item in upstream.sent if isinstance(item, str)]
        self.assertEqual([item["header"]["action"] for item in commands], ["run-task", "continue-task", "finish-task"])
        self.assertEqual(commands[0]["payload"]["model"], "cosyvoice-v3.5-flash")
        self.assertEqual(commands[0]["payload"]["parameters"]["sample_rate"], 24000)
        self.assertEqual(commands[1]["payload"]["input"]["text"], "你好AI")

    def test_tts_cancel_closes_upstream(self):
        upstream = FakeUpstream(finish=False)
        with patch("app.voice.connect", return_value=upstream), self.client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"type": "text", "text": "请朗读"})
            self.assertEqual(ws.receive_json()["type"], "ready")
            ws.send_json({"type": "cancel"})
            for _ in range(4):
                message = ws.receive()
                if message.get("text") and json.loads(message["text"])["type"] == "cancelled":
                    break
            else:
                self.fail("Cancellation was not acknowledged")
        self.assertTrue(upstream.closed)

    def test_asr_preserves_partials_and_deduplicates_final(self):
        upstream = FakeUpstream()
        with patch("app.voice.connect", return_value=upstream), self.client.websocket_connect("/ws/asr") as ws:
            ws.send_json({"type": "start", "silenceMs": 1100})
            self.assertEqual(ws.receive_json()["type"], "ready")
            ws.send_bytes(b"\x00\x00" * 320)
            self.assertEqual(ws.receive_json()["type"], "speech")
            self.assertEqual(ws.receive_json(), {"type": "partial", "text": "你好", "sentenceId": 1})
            final = ws.receive_json()
            self.assertEqual(final["type"], "final")
            self.assertEqual(final["text"], "你好，继续思考。")
            ws.send_json({"type": "stop"})
            self.assertEqual(ws.receive_json()["type"], "done")
        self.assertEqual(self.usage, [("asr", 30)])
        run = json.loads(upstream.sent[0])
        self.assertEqual(run["payload"]["function"], "recognition")
        self.assertEqual(run["payload"]["parameters"]["max_sentence_silence"], 1100)
        self.assertTrue(run["payload"]["parameters"]["heartbeat"])
        self.assertTrue(upstream.closed)

    def test_punctuation_only_segment_never_spends_or_opens_cloud(self):
        with patch("app.voice.connect") as cloud, self.client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"type": "text", "text": "」。；……"})
            error=ws.receive_json()
            self.assertEqual(error["type"],"error")
            self.assertIn("纯标点",error["message"])
        cloud.assert_not_called()
        self.assertEqual(self.usage,[])

    def test_missing_voice_fails_before_reserving_or_connecting(self):
        self.settings.voice_id = ""
        with patch("app.voice.connect") as cloud, self.client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"type": "text", "text": "你好"})
            error = ws.receive_json()
            self.assertEqual(error["type"], "error")
            self.assertIn("音色", error["message"])
        cloud.assert_not_called()
        self.assertEqual(self.usage, [])

    def test_provider_error_redacts_key(self):
        upstream = FakeUpstream(failure="bad token sk-secret-test")
        with patch("app.voice.connect", return_value=upstream), self.client.websocket_connect("/ws/tts") as ws:
            ws.send_json({"type": "text", "text": "你好"})
            error = ws.receive_json()
            self.assertEqual(error["type"], "error")
            self.assertNotIn("sk-secret-test", error["message"])
        self.assertTrue(upstream.closed)

    def test_budget_denial_never_opens_cloud(self):
        app = FastAPI()

        def exhausted(**kwargs):
            raise RuntimeError("今日语音额度已用完")

        install_voice(app, self.settings, exhausted)
        with TestClient(app) as client, patch("app.voice.connect") as cloud:
            with client.websocket_connect("/ws/tts") as ws:
                ws.send_json({"type": "text", "text": "你好"})
                self.assertIn("额度", ws.receive_json()["message"])
        cloud.assert_not_called()

    def test_cross_origin_socket_is_rejected(self):
        from starlette.websockets import WebSocketDisconnect

        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/ws/asr", headers={"origin": "https://unrelated.invalid"}):
                pass

    @unittest.skipUnless(shutil.which("node"), "Node is only an optional browser-state test runtime")
    def test_browser_playback_cancellation_and_stale_generations(self):
        if int(subprocess.check_output(["node", "--version"], text=True).strip().lstrip("v").split(".")[0]) < 18:
            self.skipTest("Browser-state harness needs Node >=18; the application has no Node dependency")
        script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
const sockets = [];
const nodes = [];
class Audio {
  state = 'running'; currentTime = 1; destination = {};
  createBuffer(channels, length, rate) {
    const samples = new Float32Array(length);
    return {duration: length / rate, getChannelData: () => samples};
  }
  createBufferSource() {
    const node = {connect() {}, disconnect() {}, start(at) {this.at = at;}, stop() {this.stopped = true;}};
    nodes.push(node); return node;
  }
  async close() {this.state = 'closed';}
}
class Socket {
  static OPEN = 1; readyState = 0; sent = []; binaryType = '';
  constructor(url) {
    this.url = url; sockets.push(this);
    queueMicrotask(() => {if (this.readyState === 3) return; this.readyState = 1; this.onopen?.();});
  }
  send(raw) {
    const event = JSON.parse(raw); this.sent.push(event);
    if (event.type === 'text') queueMicrotask(() => {
      this.onmessage?.({data: JSON.stringify({type: 'ready', sampleRate: 24000})});
      this.onmessage?.({data: new Int16Array(24000).buffer});
      this.onmessage?.({data: JSON.stringify({type: 'done'})});
    });
  }
  close() {this.readyState = 3;}
}
globalThis.window = {location: {href: 'http://localhost:8890'}, AudioContext: Audio};
globalThis.WebSocket = Socket;
const source = fs.readFileSync('app/static/voice.js', 'utf8');
const {VoiceIO} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const tick = () => new Promise(resolve => setImmediate(resolve));

const voice = new VoiceIO();
const playback = voice.speak('你好');
await tick();
assert.equal(voice.sources.size, 1);
assert.equal(voice.isSpeaking, true);
assert.ok(voice.metrics.firstPCM >= 0);
assert.ok(voice.metrics.firstPlayback >= voice.metrics.firstPCM);
nodes.at(-1).onended();
const completed = await playback;
assert.equal(completed.cancelled, false);
assert.equal(completed.playedMs, 1000);
assert.equal(completed.scheduledMs, 1000);
assert.equal(completed.playbackMeasurement, 'web_audio_timeline_estimate');

const interrupted = voice.speak('继续朗读');
await tick();
const socket = sockets.at(-1);
const node = nodes.at(-1);
voice.context.currentTime = node.at + 0.375;
voice.interrupt();
const partial = await interrupted;
assert.equal(partial.cancelled, true);
assert.equal(partial.playedMs, 375);
assert.equal(partial.scheduledMs, 1000);
assert.ok(partial.firstPCM >= 0);
assert.equal(socket.readyState, 3);
assert.equal(socket.sent.at(-1).type, 'cancel');
assert.equal(node.stopped, true);
assert.equal(voice.sources.size, 0);
socket.onmessage({data: new Int16Array([1, 2]).buffer});
assert.equal(voice.sources.size, 0, 'late PCM must never resume cancelled speech');

const failed = voice.speak('出错时必须断开上游');
await tick();
const failedSocket = sockets.at(-1);
failedSocket.onmessage({data: JSON.stringify({type: 'error', message: 'test error'})});
await assert.rejects(failed, /test error/);
assert.equal(failedSocket.readyState, 3);
assert.equal(failedSocket.sent.at(-1).type, 'cancel');
assert.equal(voice.sources.size, 0);

const delayed = new VoiceIO();
const resolvers = [];
delayed.ensureAudio = () => new Promise(resolve => resolvers.push(resolve));
const count = sockets.length;
const first = delayed.speak('obsolete');
const second = delayed.speak('current');
const audio = new Audio(); delayed.context = audio;
resolvers[0](audio); resolvers[1](audio);
await tick();
const obsolete = await first;
assert.equal(obsolete.cancelled, true);
assert.equal(obsolete.playedMs, 0);
assert.equal(obsolete.firstPCM, null);
assert.equal(sockets.length, count + 1, 'cancel during audio resume must not open a stale cloud session');
assert.equal(sockets.at(-1).sent[0].text, 'current');
delayed.interrupt(); await second;
await voice.close(); await delayed.close();
'''
        result = subprocess.run(["node", "--input-type=module", "-"], input=script, text=True,
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is only an optional browser-state test runtime")
    def test_browser_frozen_audio_clock_cancels_without_completed_playback(self):
        if int(subprocess.check_output(["node", "--version"], text=True).strip().lstrip("v").split(".")[0]) < 18:
            self.skipTest("Browser-state harness needs Node >=18")
        script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
const sockets = [], nodes = [], errors = [], intervals = new Map();
let wallTime = 0, timerId = 0;
Object.defineProperty(globalThis, 'performance', {value: {now: () => wallTime}});
globalThis.setInterval = callback => {intervals.set(++timerId, callback); return timerId;};
globalThis.clearInterval = id => intervals.delete(id);
class Audio {
  state = 'running'; currentTime = 0.005333; destination = {};
  createBuffer(channels, length, rate) {
    return {duration: length / rate, getChannelData: () => new Float32Array(length)};
  }
  createBufferSource() {
    const node = {connect() {}, disconnect() {}, start(at) {this.at = at;}, stop() {this.stopped = true;}};
    nodes.push(node); return node;
  }
  async close() {this.state = 'closed';}
}
class Socket {
  static OPEN = 1; readyState = 0; sent = [];
  constructor() {sockets.push(this); queueMicrotask(() => {
    if (this.readyState === 3) return; this.readyState = 1; this.onopen?.();
  });}
  send(raw) {this.sent.push(JSON.parse(raw));}
  pcm() {this.onmessage?.({data: new Int16Array(24000).buffer});}
  done() {this.onmessage?.({data: JSON.stringify({type: 'done'})});}
  close() {this.readyState = 3;}
}
globalThis.window = {location: {href: 'http://localhost:8890'}, AudioContext: Audio};
globalThis.WebSocket = Socket;
const {VoiceIO} = await import('data:text/javascript;base64,' + Buffer.from(fs.readFileSync('app/static/voice.js', 'utf8')).toString('base64'));
const tick = () => new Promise(resolve => setImmediate(resolve));
const advance = async (milliseconds, context = null) => {
  for (let elapsed = 0; elapsed < milliseconds; elapsed += 250) {
    wallTime += 250;
    if (context) context.currentTime += 0.25;
    for (const callback of [...intervals.values()]) callback();
    await tick();
  }
};

const voice = new VoiceIO({onError: error => errors.push(error)});
const first = voice.enqueue('当前句'), second = voice.enqueue('预取句'), third = voice.enqueue('尚未合成');
let settled = false;
first.then(() => {settled = true;}, () => {settled = true;});
await tick();
await advance(5000);
assert.equal(intervals.size, 0, 'waiting for the first PCM is not a frozen playback clock');
assert.equal(errors.length, 0);
const current = sockets[0], lookahead = sockets[1];
current.pcm(); current.done(); lookahead.pcm(); lookahead.done();
assert.equal(intervals.size, 1);
assert.equal(nodes.length, 1, 'prefetch never starts its own playback watchdog');
await advance(3750);
assert.equal(settled, false, 'the watchdog allows a full four-second observation window');
await advance(250);
await assert.rejects(first, /音频输出卡住/);
assert.equal(errors.length, 1);
assert.equal(voice.isSpeaking, false);
assert.equal(voice.sources.size, 0);
assert.equal(nodes[0].stopped, true);
assert.equal(intervals.size, 0);
assert.equal(current.readyState, 3); assert.equal(lookahead.readyState, 3);
assert.equal(current.sent.at(-1).type, 'cancel');
assert.equal(lookahead.sent.at(-1).type, 'cancel');
assert.equal(voice.metrics.playedMs, 0, 'a timeout is never converted into estimated played time');
assert.equal(voice.metrics.scheduledMs, 1000);
assert.equal((await second).cancelled, true);
assert.equal((await third).cancelled, true);
assert.equal(sockets.length, 2, 'unsynthesized queue entries are cancelled without cloud work');
await voice.close();

// Long synthesis gaps, a progressing clock, and explicit suspension are not
// the failure above. A later user interrupt must still clear this watchdog.
const healthy = new VoiceIO({onError: error => errors.push(error)});
const report = healthy.enqueue('慢合成与暂停');
await tick();
const active = sockets.at(-1);
await advance(5000);
active.pcm();
await advance(1500, healthy.context);
nodes.at(-1).onended();
await advance(5000);
assert.equal(errors.length, 1, 'a slow next chunk with no pending playback must not fail');
active.pcm();
await advance(3000);
healthy.context.state = 'suspended';
await advance(6000);
assert.equal(errors.length, 1, 'an explicitly suspended output is not a broken running clock');
healthy.context.state = 'running';
await advance(3750);
assert.equal(errors.length, 1, 'resuming gets a fresh observation window');
await advance(1000, healthy.context);
healthy.interrupt();
assert.equal((await report).cancelled, true);
assert.equal(intervals.size, 0, 'manual interruption releases the watchdog');
await advance(5000);
assert.equal(errors.length, 1);
await healthy.close();
'''
        result = subprocess.run(["node", "--input-type=module", "-"], input=script, text=True,
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is only an optional browser-state test runtime")
    def test_browser_turn_gate_keeps_questions_and_filters_only_final_backchannels(self):
        if int(subprocess.check_output(["node", "--version"], text=True).strip().lstrip("v").split(".")[0]) < 18:
            self.skipTest("Browser-state harness needs Node >=18")
        script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
globalThis.window = {location: {href: 'http://localhost:8890'}};
globalThis.WebSocket = {OPEN: 1};
const source = fs.readFileSync('app/static/voice.js', 'utf8');
const {VoiceIO} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
function probe(at = 2) {
  const events = [];
  const voice = new VoiceIO({onPartial: text => events.push(['partial', text]),
    onFinal: text => events.push(['final', text]), onSpeech: value => events.push(['speech', value]),
    onBackchannel: text => events.push(['backchannel', text])});
  voice.context = {state: 'running', currentTime: at};
  voice.sources.add({playbackStart: 1, playbackEnd: 3, stop() {}, disconnect() {}});
  voice.currentSpeechText = '我们需要先核实长期记忆的能力，再通过完整的语音测试判断这个方案是否适合持续使用。';
  return {voice, events};
}

const question = probe();
question.voice.handleRecognition({type: 'final', text: '长期记忆？'});
assert.ok(question.events.some(([type, text]) => type === 'final' && text === '长期记忆？'));
assert.ok(question.events.some(([type, value]) => type === 'speech' && value.interrupted));

for (const text of ['嗯', '好', '对', '明白', '明白了', '收到']) {
  const {voice, events} = probe();
  voice.handleRecognition({type: 'partial', text});
  assert.deepEqual(events, [['partial', text]], 'a short partial remains visible without committing a turn');
  voice.handleRecognition({type: 'final', text});
  assert.ok(events.some(([type, value]) => type === 'backchannel' && value === text));
  assert.ok(!events.some(([type]) => type === 'speech' || type === 'final'));
  assert.equal(voice.sources.size, 1, 'acknowledgment must not stop the report');
}

const expanded = probe();
expanded.voice.handleRecognition({type: 'partial', text: '好'});
expanded.voice.handleRecognition({type: 'final', text: '好，但是这个条件还没讲清楚。'});
assert.ok(expanded.events.some(([type, text]) => type === 'final' && text.startsWith('好，但是')));
assert.ok(!expanded.events.some(([type]) => type === 'backchannel'));

for (const at of [1.001, 2.999, 3.001]) {
  for (const text of ['停', '不对', '等一下', '换个角度']) {
    const {voice, events} = probe(at);
    voice.handleRecognition({type: 'partial', text});
    assert.ok(events.some(([type]) => type === 'speech'), `correction must pass at speech boundary: ${at}, ${text}`);
    voice.handleRecognition({type: 'final', text});
    assert.ok(events.some(([type, value]) => type === 'final' && value === text));
  }
}

const listening = probe(); listening.voice.sources.clear();
listening.voice.handleRecognition({type: 'final', text: '好'});
assert.ok(listening.events.some(([type, text]) => type === 'final' && text === '好'));
assert.ok(!listening.events.some(([type]) => type === 'backchannel'));

const alreadyActive = probe(); alreadyActive.voice.speechActive = true;
alreadyActive.voice.handleRecognition({type: 'partial', text: '不对'});
assert.ok(alreadyActive.events.some(([type, value]) => type === 'speech' && value.interrupted),
  'a previous microphone activity signal must not swallow an explicit correction');

const echo = probe();
echo.voice.handleRecognition({type: 'final', text: echo.voice.currentSpeechText});
assert.deepEqual(echo.events, [], 'only a long exact copy without local speech energy is suppressed');
const quote = probe();
quote.voice.energySince = performance.now() - 200; quote.voice.lastEnergyAt = performance.now();
quote.voice.handleRecognition({type: 'final', text: quote.voice.currentSpeechText});
assert.ok(quote.events.some(([type]) => type === 'final'), 'real microphone energy permits quoting a long report');
'''
        result = subprocess.run(["node", "--input-type=module", "-"], input=script, text=True,
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is only an optional browser-state test runtime")
    def test_browser_one_sentence_prefetch_and_queue_cancellation(self):
        if int(subprocess.check_output(["node", "--version"], text=True).strip().lstrip("v").split(".")[0]) < 18:
            self.skipTest("Browser-state harness needs Node >=18")
        script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
const sockets = [], nodes = [], measurements = [], unhandled = [];
process.on('unhandledRejection', error => unhandled.push(error));
class Audio {
  state = 'running'; currentTime = 1; destination = {};
  createBuffer(channels, length, rate) {
    const samples = new Float32Array(length);
    return {duration: length / rate, getChannelData: () => samples};
  }
  createBufferSource() {
    const node = {connect() {}, disconnect() {}, start(at) {this.at = at;}, stop() {this.stopped = true;}};
    nodes.push(node); return node;
  }
  async close() {this.state = 'closed';}
}
class Socket {
  static OPEN = 1; readyState = 0; sent = [];
  constructor(url) {
    sockets.push(this);
    queueMicrotask(() => {if (this.readyState === 3) return; this.readyState = 1; this.onopen?.();});
  }
  send(raw) {
    const value = JSON.parse(raw); this.sent.push(value);
    if (value.type === 'text') queueMicrotask(() => {
      this.onmessage?.({data: JSON.stringify({type: 'ready', sampleRate: 24000})});
      this.onmessage?.({data: new Int16Array(24000).buffer});
    });
  }
  complete() {this.onmessage?.({data: JSON.stringify({type: 'done'})});}
  close() {this.readyState = 3;}
}
globalThis.window = {location: {href: 'http://localhost:8890'}, AudioContext: Audio};
globalThis.WebSocket = Socket;
const source = fs.readFileSync('app/static/voice.js', 'utf8');
const {VoiceIO} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const tick = () => new Promise(resolve => setImmediate(resolve));
const voice = new VoiceIO({onMetrics: value => measurements.push(value)});
const first = voice.enqueue('第一句'), second = voice.enqueue('第二句'), third = voice.enqueue('第三句');
await tick();
assert.equal(sockets.length, 2, 'only the active sentence and one lookahead may incur synthesis');
assert.equal(nodes.length, 1, 'prefetched PCM must not schedule itself');
assert.equal(measurements.filter(value => value.firstPCM != null).length, 1, 'prefetch must not emit playback metrics');
assert.equal(sockets[1].sent[0].text, '第二句');
assert.ok(measurements[0].scheduledStartAt >= 0);
sockets[0].complete(); voice.context.currentTime = nodes[0].at + 1; nodes[0].onended();
await tick();
assert.equal((await first).cancelled, false);
assert.equal(sockets.length, 3, 'third sentence starts only when second becomes current');
assert.equal(nodes.length, 2, 'adopting lookahead schedules cached PCM without a new request');
assert.equal(sockets[2].sent[0].text, '第三句');
voice.context.currentTime = nodes[1].at + .25;
voice.interrupt();
const replacement = voice.enqueue('新话题');
const cancelledSecond = await second, cancelledThird = await third;
assert.equal(cancelledSecond.cancelled, true); assert.equal(cancelledSecond.playedMs, 250);
assert.equal(cancelledThird.cancelled, true); assert.equal(cancelledThird.firstPCM, null);
assert.equal(cancelledThird.playedMs, 0, 'cached but never played is not a completed playback');
assert.equal(sockets[1].sent.at(-1).type, 'cancel');
assert.equal(sockets[2].sent.at(-1).type, 'cancel');
await tick();
assert.equal(nodes.length, 3, 'a new queue may start immediately after interrupt');
const beforeLate = nodes.length;
sockets[2].onmessage?.({data: new Int16Array(24000).buffer});
assert.equal(nodes.length, beforeLate, 'late prefetched PCM cannot revive the old report');
voice.interrupt(); await replacement;

const a = voice.enqueue('当前句'), b = voice.enqueue('过大的预取');
await tick();
const current = sockets.at(-2), next = sockets.at(-1), startCount = sockets.length;
next.onmessage?.({data: new ArrayBuffer(2 * 1024 * 1024 + 2)});
await tick();
assert.equal(next.readyState, 3, 'oversized lookahead closes its upstream');
assert.equal(voice.sources.size, 1, 'prefetch failure does not stop the current sentence prematurely');
current.complete(); nodes.at(-1).onended();
await a; await tick();
assert.equal(unhandled.length, 0, 'a deferred caller must not cause unhandled rejection');
await assert.rejects(b, /2MB/);
assert.equal(sockets.length, startCount, 'a failed prefetch is not retried and charged silently');
await voice.close();
assert.equal(unhandled.length, 0);
'''
        result = subprocess.run(["node", "--input-type=module", "-"], input=script, text=True,
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is only an optional browser-state test runtime")
    def test_browser_mic_graceful_stop_discard_and_restart(self):
        if int(subprocess.check_output(["node", "--version"], text=True).strip().lstrip("v").split(".")[0]) < 18:
            self.skipTest("Browser-state harness needs Node >=18")
        script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
const sockets = [], streams = [], finals = [], errors = [];
const makeStream = () => {const track = {stopped: false, stop() {this.stopped = true;}};
  const stream = {getTracks: () => [track], track}; streams.push(stream); return stream;};
const node = () => ({connect() {}, disconnect() {}});
class Audio {
  state = 'running'; sampleRate = 48000; currentTime = 1; destination = {};
  createMediaStreamSource() {return node();}
  createScriptProcessor() {return node();}
  createGain() {return {...node(), gain: {value: 1}};}
  async close() {this.state = 'closed';}
}
class Socket {
  static OPEN = 1; readyState = 0; sent = [];
  constructor() {sockets.push(this);queueMicrotask(() => {if (this.readyState === 3) return;this.readyState = 1;this.onopen?.();});}
  send(raw) {const value = JSON.parse(raw);this.sent.push(value);
    if (value.type === 'start') queueMicrotask(() => this.event({type: 'ready'}));}
  event(value) {this.onmessage?.({data: JSON.stringify(value)});}
  close() {if (this.readyState === 3) return;this.readyState = 3;queueMicrotask(() => this.onclose?.());}
}
globalThis.window = {isSecureContext: true, location: {href: 'http://localhost:8890'}, AudioContext: Audio};
Object.defineProperty(globalThis, 'navigator', {configurable: true, value: {mediaDevices: {getUserMedia: async () => makeStream()}}});
globalThis.WebSocket = Socket;
const {VoiceIO} = await import('data:text/javascript;base64,' + Buffer.from(fs.readFileSync('app/static/voice.js','utf8')).toString('base64'));
const tick = () => new Promise(resolve => setImmediate(resolve));
const voice = new VoiceIO({micDrainMs: 20, onFinal: text => finals.push(text), onError: error => errors.push(error)});
await voice.startMic();
const gracefulSocket = sockets.at(-1), gracefulStream = streams.at(-1);
const drain = voice.stopMic();
assert.equal(gracefulStream.track.stopped, true, 'end immediately releases physical microphone');
assert.equal(gracefulSocket.sent.at(-1).type, 'stop');
gracefulSocket.event({type: 'final', text: '停止前的最后一句'});
assert.deepEqual(finals, ['停止前的最后一句']);
gracefulSocket.event({type: 'done'});await drain;
gracefulSocket.event({type: 'final', text: 'done后的迟到重复'});
assert.equal(finals.length, 1);

await voice.startMic();const discardSocket = sockets.at(-1);
await voice.stopMic({discard: true});
discardSocket.event({type: 'final', text: '切topic后旧会话最后一句'});
assert.equal(finals.length, 1, 'discard fences old final before changing topic');

await voice.startMic();const oldSocket = sockets.at(-1);
const oldDrain = voice.stopMic();
await voice.startMic();await oldDrain;
const newSocket = sockets.at(-1), newStream = streams.at(-1);
oldSocket.event({type: 'final', text: '重启前旧final'});oldSocket.onclose?.();
assert.equal(finals.length, 1);
assert.equal(voice.isListening, true);assert.equal(newStream.track.stopped, false);
newSocket.event({type: 'final', text: '新一代输入'});
assert.equal(finals.at(-1), '新一代输入');
await voice.stopMic();
assert.equal(voice.micSocket, null, 'drain timeout is bounded even without cloud done');
newSocket.event({type: 'final', text: '超时后的旧final'});
assert.equal(finals.at(-1), '新一代输入');

const requests = [];
navigator.mediaDevices.getUserMedia = () => new Promise((resolve, reject) => requests.push({resolve,reject}));
const abandoned = voice.startMic();await tick();
voice.stopMic({discard: true});
const replacement = voice.startMic();await tick();
requests[0].reject(new Error('old permission request denied'));await abandoned;
assert.equal(voice.micStarting, true, 'an old permission rejection cannot clear a new mic startup');
requests[1].resolve(makeStream());await replacement;
assert.equal(voice.isListening, true);assert.equal(errors.length, 0);
await voice.close();
assert.equal(voice.isListening, false);
'''
        result = subprocess.run(["node", "--input-type=module", "-"], input=script, text=True,
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
