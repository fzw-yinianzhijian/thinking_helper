from dataclasses import dataclass
from pathlib import Path
import json
import os
import re

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    api_key: str = ""
    voice_id: str = ""
    upstream_host: str = "dashscope.aliyuncs.com"
    model: str = "qwen-plus"
    chat_model: str = "qwen-flash"
    tts_model: str = "cosyvoice-v3.5-flash"
    asr_model: str = "fun-asr-realtime"
    asr_silence_ms: int = 1300
    daily_call_limit: int = 24
    chat_daily_call_limit: int = 120
    tts_daily_chars: int = 12000
    asr_daily_seconds: int = 1800
    database: str = str(ROOT / "data" / "workspace.sqlite3")
    tick_seconds: float = 2.0

    @classmethod
    def load(cls):
        path = Path(os.environ.get("TH_CREDENTIALS", str(Path.home() / ".config/thinking-helper/credentials.json")))
        credentials = json.loads(path.read_text()) if path.exists() else {}
        key = os.environ.get("DASHSCOPE_API_KEY", credentials.get("api_key", ""))
        local_key = ROOT.parent / "apikey.txt"
        if not key and local_key.exists():
            match = re.search(r"^\s*qwen_api\s*=\s*[\"']?([^\s\"']+)", local_key.read_text(), re.M)
            key = match.group(1) if match else ""
        voice = os.environ.get("TH_VOICE_ID", credentials.get("voice_id", ""))
        previous = ROOT.parent / "cosyvoice-demo/.local/voices.json"
        if not voice and previous.exists():
            voices = json.loads(previous.read_text())
            voice = voices[0]["id"] if voices else ""
        return cls(
            api_key=key,
            voice_id=voice,
            upstream_host=os.environ.get("TH_UPSTREAM_HOST", "dashscope.aliyuncs.com"),
            model=os.environ.get("TH_MODEL", "qwen-plus"),
            chat_model=os.environ.get("TH_CHAT_MODEL", "qwen-flash"),
            database=os.environ.get("TH_DATABASE", str(ROOT / "data/workspace.sqlite3")),
            daily_call_limit=int(os.environ.get("TH_DAILY_CALLS", "24")),
            chat_daily_call_limit=int(os.environ.get("TH_CHAT_CALLS", "120")),
            tts_daily_chars=int(os.environ.get("TH_TTS_CHARS", "12000")),
            asr_daily_seconds=int(os.environ.get("TH_ASR_SECONDS", "1800")),
        )

    def safe_error(self, error):
        text = str(error)
        if self.api_key:
            text = text.replace(self.api_key, "[redacted]")
        return re.sub(r"sk-[\w-]+", "[redacted]", text)[:500]
