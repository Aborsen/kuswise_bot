"""Whisper transcription for Telegram voice messages (UA-biased)."""
from openai import OpenAI

from lib.config import OPENAI_API_KEY


_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


# Short UA-dish hint biases Whisper toward common Ukrainian food names.
# Don't pad with too many — the prompt itself counts against the hint budget.
_UA_DISH_PROMPT = (
    "борщ вареники голубці деруни сирники капусняк котлета гречка окрошка "
    "плов холодець пельмені млинці запіканка салат олів'є"
)


def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    """Transcribe Telegram OGG/Opus voice bytes to Ukrainian text."""
    resp = _get_client().audio.transcriptions.create(
        model="whisper-1",
        file=(filename, audio_bytes, "audio/ogg"),
        language="uk",
        prompt=_UA_DISH_PROMPT,
    )
    return (getattr(resp, "text", "") or "").strip()
