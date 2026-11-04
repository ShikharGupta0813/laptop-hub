"""Speech to text on the laptop with faster-whisper. No audio leaves the machine.

The model is downloaded once from the internet the first time you record. After that it
works offline. `base` is quick on a CPU; `small` is more accurate and slower.
"""
from __future__ import annotations

import threading


class SpeechError(Exception):
    """Transcription isn't possible. The message says how to fix it."""


class Transcriber:
    def __init__(self, model_name: str = "base"):
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                raise SpeechError("Voice notes need faster-whisper. Run: "
                                  "pip install -r requirements-voice.txt") from None
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        return self._model

    def transcribe(self, audio_path: str) -> str:
        with self._lock:  # one recording at a time; a CPU can't do two faster anyway
            model = self._load()
            segments, _ = model.transcribe(audio_path, vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()
