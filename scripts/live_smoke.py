"""Explicitly paid, synthetic speech only. Uses the running server's budget."""
import argparse
import asyncio
import json
import struct
import time

from websockets.asyncio.client import connect


async def check(base):
    pcm = bytearray()
    started = time.monotonic()
    first = None
    text = "我们先把问题说清楚，再根据新的发现继续思考。"
    async with connect(base + "/ws/tts", open_timeout=15) as socket:
        await socket.send(json.dumps({"type": "text", "text": text}))
        async for raw in socket:
            if isinstance(raw, bytes):
                if first is None:
                    first = round((time.monotonic() - started) * 1000)
                pcm.extend(raw)
            else:
                event = json.loads(raw)
                if event["type"] == "error":
                    raise RuntimeError(event)
                if event["type"] == "done":
                    break
    assert len(pcm) > 24000, "No meaningful PCM audio"
    print(json.dumps({"tts_first_pcm_ms": first, "audio_seconds": round(len(pcm) / 48000, 2)}, ensure_ascii=False), flush=True)
    # Convert synthetic 24kHz speech to 16kHz without a system ffmpeg dependency.
    samples = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
    resampled = []
    for out in range(int(len(samples) * 2 / 3)):
        pos = out * 1.5
        i = int(pos)
        a, b = samples[i], samples[min(i + 1, len(samples) - 1)]
        resampled.append(round(a + (b - a) * (pos - i)))
    audio = struct.pack("<" + "h" * len(resampled), *resampled)
    final = []
    async with connect(base + "/ws/asr", open_timeout=15) as socket:
        await socket.send(json.dumps({"type": "start", "silenceMs": 1300}))
        while True:
            event = json.loads(await socket.recv())
            if event["type"] == "ready":
                break
            if event["type"] == "error":
                raise RuntimeError(event)

        async def feed():
            for i in range(0, len(audio), 1600):
                await socket.send(audio[i:i + 1600])
                await asyncio.sleep(.025)
            await socket.send(bytes(32000 * 2))
            await socket.send(json.dumps({"type": "stop"}))

        producer = asyncio.create_task(feed())
        async for raw in socket:
            event = json.loads(raw)
            if event["type"] == "final":
                final.append(event["text"])
            if event["type"] == "error":
                raise RuntimeError(event)
            if event["type"] == "done":
                break
        await producer
    transcript = "".join(final)
    assert "问题" in transcript and "思考" in transcript, f"Unexpected synthetic transcript: {transcript}"
    print(json.dumps({"synthetic_asr_transcript": transcript, "microphone_acoustics_tested": False}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--paid", action="store_true", help="permit real billable TTS and ASR calls")
    parser.add_argument("--base", default="ws://127.0.0.1:8890")
    args = parser.parse_args()
    if not args.paid:
        parser.error("This is a billable smoke check. Pass --paid explicitly.")
    asyncio.run(asyncio.wait_for(check(args.base), timeout=90))


if __name__ == "__main__":
    main()
