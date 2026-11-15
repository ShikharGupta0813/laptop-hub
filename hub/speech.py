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
                                  "run scripts\\setup-all.ps1 (or: pip install -r requirements-voice.txt)") from None
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        return self._model

    def preload(self) -> bool:
        """Load the model now (called at startup and when voice mode opens) so the first sentence is not slow."""
        try:
            with self._lock:
                self._load()
            return True
        except SpeechError:
            return False

    def transcribe(self, audio_path: str) -> str:
        with self._lock:  # one recording at a time; a CPU can't do two faster anyway
            model = self._load()
            # beam_size=1 (greedy) is several times faster on a CPU and plenty for short spoken commands
            segments, _ = model.transcribe(audio_path, vad_filter=True, beam_size=1,
                                           condition_on_previous_text=False)
            return " ".join(s.text.strip() for s in segments).strip()

    def transcribe_detailed(self, audio_path: str) -> dict:
        """Text plus word timings and confidences, for the English coach.

        Returns {"text", "duration" (seconds), "words": [{"word","start","end","prob"}]}.
        The prompt nudges Whisper to keep "um" and "uh" instead of tidying them away.
        """
        with self._lock:
            model = self._load()
            segments, info = model.transcribe(
                audio_path, vad_filter=True, word_timestamps=True,
                initial_prompt="Um, uh, so, like, you know, I mean, well, okay.")
            texts, words = [], []
            for segment in segments:
                texts.append(segment.text.strip())
                for w in segment.words or []:
                    words.append({"word": w.word.strip(), "start": round(w.start, 2),
                                  "end": round(w.end, 2), "prob": round(w.probability, 3)})
            return {"text": " ".join(texts).strip(), "duration": round(info.duration, 2), "words": words}

    def transcribe_pcm(self, pcm: bytes) -> str:
        """Text from 16 kHz mono int16 audio held in memory (the meeting assistant): no file is written."""
        import numpy as np          # installed together with faster-whisper
        samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
        with self._lock:
            model = self._load()
            segments, _ = model.transcribe(samples, vad_filter=True, beam_size=1, condition_on_previous_text=False)
            return " ".join(s.text.strip() for s in segments).strip()
