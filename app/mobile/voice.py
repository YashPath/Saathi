"""Voice notes from the app: transcribe with the chat model (Gemini hears audio)."""

import base64
import logging

from langchain_core.messages import HumanMessage

from app.agent.llm import get_llm

log = logging.getLogger(__name__)
MAX_BYTES = 10 * 1024 * 1024

TRANSCRIBE_PROMPT = """Transcribe this voice note exactly. It may be Hindi, English or a mix (Hinglish).
Write Hindi words in Roman script (for example "Sharma ji ka 500 likh do"), never in Devanagari.
Write numbers as digits. Return only the transcript, nothing else. If there is no clear speech, return nothing."""


class VoiceError(Exception):
    """The voice note could not be fetched or understood; the user should type instead."""


def transcribe(audio: bytes, mime: str, llm=None) -> str:
    """Speech to text with the chat model. Raises VoiceError when nothing usable comes back."""
    message = HumanMessage(content=[
        {"type": "text", "text": TRANSCRIBE_PROMPT},
        {"type": "media", "mime_type": mime, "data": base64.b64encode(audio).decode()},
    ])
    try:
        text = (llm or get_llm()).invoke([message]).text.strip()
    except Exception as exc:
        raise VoiceError(f"transcription failed: {exc}") from exc
    if not text:
        raise VoiceError("no speech found")
    return text
