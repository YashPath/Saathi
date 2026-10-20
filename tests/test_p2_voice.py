"""P2-6 checks: voice transcription (no network, no real model)."""

import base64

import pytest
from langchain_core.messages import AIMessage

from app.mobile import voice


class CaptureLLM:
    def __init__(self, reply="Sharma ji ka 500 likh do"):
        self.reply, self.seen = reply, None

    def invoke(self, messages):
        self.seen = messages
        if isinstance(self.reply, Exception):
            raise self.reply
        return AIMessage(self.reply)


def test_transcribe_sends_audio_as_media_block():
    llm = CaptureLLM()
    assert voice.transcribe(b"\x01\x02", "audio/ogg", llm) == "Sharma ji ka 500 likh do"
    text, media = llm.seen[0].content
    assert "Roman script" in text["text"]
    assert media == {"type": "media", "mime_type": "audio/ogg", "data": base64.b64encode(b"\x01\x02").decode()}


@pytest.mark.parametrize("reply", ["", "   ", RuntimeError("quota")])
def test_transcribe_failures(reply):
    with pytest.raises(voice.VoiceError):
        voice.transcribe(b"x", "audio/ogg", CaptureLLM(reply))
