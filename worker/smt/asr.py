"""Speech to text, with faster-whisper on this machine or OpenAI's API.

One model per process, loaded on first use and kept: loading large-v3-turbo
takes longer than transcribing a whole reel, so a model per job would spend
most of its time loading. On an NVIDIA card it runs in float16, otherwise on
the CPU in int8.

A model named "openai:<model>" (OpenAiAsr) sends the audio to OpenAI
instead, with the local model as its fallback. On stardawneg64's CPU, held
down by its thermal guard, large-v3-turbo finished 4-27 reels an hour over
the twelve hours to 2026-11-02 13:00 UTC (from the StarBase database).

Silence and music are valid input. The VAD filter keeps Whisper from being
fed stretches without speech, which is where it invents text; what still
looks invented (the same line over and over, text over almost no speech) is
flagged rather than passed off as a clean transcript.
"""

from __future__ import annotations

import ctypes
import json
import logging
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

LOG = logging.getLogger("smt.asr")

OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
# Under the API's 25 MB upload limit: 16 kHz mono WAV is 32 KB a second, so
# about 13 minutes. Longer audio goes to the local model.
OPENAI_UPLOAD_LIMIT = 24 * 1024 * 1024


def has_audio(path: Path) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.strip()[:300]}")
    return "audio" in out.stdout


def extract_audio(video: Path, wav: Path) -> None:
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-f", "wav", str(wav)],
        capture_output=True, text=True, timeout=1800,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffmpeg could not extract the audio: {out.stderr.strip()[:300]}")


def read_wav(path: Path):
    """The 16 kHz mono samples extract_audio wrote, as Whisper takes them.

    Handed over as an array rather than a path: given a path, faster-whisper
    1.2.1 decodes with PyAV and passes it an argument PyAV 19 no longer has
    (measured 2026-10-31). ffmpeg has already decoded the file anyway.
    """
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise ValueError(f"{path.name} is not 16 kHz mono 16-bit audio")
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def _cuda_usable() -> bool:
    """Whether CUDA inference can run, without trying it.

    ctranslate2 aborts the whole process — no exception — when it reaches for
    a cuDNN library that is not there. So the libraries are looked for first.
    """
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() < 1:
            return False
        for lib in ("libcublas.so.12", "libcudnn_ops.so.9"):
            ctypes.CDLL(lib)
        return True
    except Exception:  # noqa: BLE001 — any failure means: not here
        return False


def quality(segments: list[dict], language_probability: float, speech_seconds: float) -> list[str]:
    flags = []
    norm = [re.sub(r"\W+", " ", s["text"].lower()).strip() for s in segments]
    run = best = 1
    for a, b in zip(norm, norm[1:]):
        run = run + 1 if a and a == b else 1
        best = max(best, run)
    if best >= 3:
        flags.append("repeated_segments")
    if any(s.get("compression_ratio", 0) > 2.4 for s in segments):
        flags.append("suspect_repetition")
    if segments and language_probability < 0.5:
        flags.append("uncertain_language")
    if segments and speech_seconds < 1.0:
        flags.append("text_with_little_speech")
    return flags


def judge(segments: list[dict], language_probability: float, speech_seconds: float) -> tuple[list[dict], list[str], str | None]:
    """Segments to keep, flags, and the text thrown away as not speech.

    A few words over less than a second of detected speech are Whisper's
    stock answers to music — "You", "Thank you.", "Untertitel im Auftrag des
    ZDF" — not something said (measured 2026-10-31: a 30-second music reel came
    back as "You", 0.86 s of speech, language probability 0.23).
    """
    flags = quality(segments, language_probability, speech_seconds)
    words = sum(len(s["text"].split()) for s in segments)
    if segments and speech_seconds < 1.0 and words <= 4:
        return [], flags + ["discarded_as_no_speech"], " ".join(s["text"] for s in segments)
    return segments, flags, None


class Asr:
    def __init__(self, model: str, device: str, models_dir: Path):
        self.name = model
        self.wanted_device = device
        self.models_dir = models_dir
        self._model = None
        self.device = None
        self.compute_type = None
        self.revision = None
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel
            from faster_whisper.utils import download_model

            device = self.wanted_device
            if device == "auto":
                device = "cuda" if _cuda_usable() else "cpu"
            compute = "float16" if device == "cuda" else "int8"
            started = time.time()
            path = Path(download_model(self.name, cache_dir=str(self.models_dir)))
            self.revision = path.name if path.parent.name == "snapshots" else None
            self._model = WhisperModel(str(path), device=device, compute_type=compute)
            self.device, self.compute_type = device, compute
            LOG.info("loaded %s (%s) on %s/%s in %.1f s", self.name, self.revision, device, compute,
                     time.time() - started)
            return self._model

    def transcribe(self, wav: Path, language: str | None = None) -> dict:
        model = self.load()
        started = time.time()
        params = {"beam_size": 5, "vad_filter": True, "condition_on_previous_text": False}
        raw, info = model.transcribe(read_wav(wav), language=language, **params)
        segments = []
        for s in raw:
            # Whisper's own rule for a segment that is most likely not speech.
            if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
                continue
            text = s.text.strip()
            if text:
                segments.append({
                    "start_ms": int(s.start * 1000), "end_ms": int(s.end * 1000), "text": text,
                    "compression_ratio": round(s.compression_ratio, 2),
                })
        speech = float(getattr(info, "duration_after_vad", info.duration) or 0)
        segments, flags, discarded = judge(segments, info.language_probability, speech)
        for s in segments:
            s.pop("compression_ratio", None)
        return {
            "status": "available" if segments else "no_speech",
            # Whisper names a language even for music; without speech that is a guess.
            "language": info.language if segments else None,
            "segments": segments,
            "text": " ".join(s["text"] for s in segments),
            "quality": {
                "flags": flags,
                **({"discarded_text": discarded} if discarded else {}),
                "language_probability": round(info.language_probability, 3),
                "audio_seconds": round(info.duration, 2),
                "speech_seconds": round(speech, 2),
                "seconds": round(time.time() - started, 2),
            },
            "model": self.name,
            "model_revision": self.revision,
            "params": {**params, "device": self.device, "compute_type": self.compute_type, "language": language},
        }


def speech_seconds(wav: Path) -> tuple[float, float]:
    """(audio seconds, speech seconds) by Silero VAD, the filter the local
    model runs before it transcribes."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    samples = read_wav(wav)
    stamps = get_speech_timestamps(samples, VadOptions())
    return len(samples) / 16000, sum(s["end"] - s["start"] for s in stamps) / 16000


def openai_transcribe(key: str, model: str, wav: Path, language: str | None, timeout: int = 300) -> dict:
    """One file to OpenAI's transcription endpoint, as multipart form data."""
    boundary = secrets.token_hex(16)
    fields = {"model": model, "response_format": "json", "chunking_strategy": "auto"}
    if language:
        fields["language"] = language
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()
    ) + (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
         f"Content-Type: audio/wav\r\n\r\n").encode() + wav.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(OPENAI_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OpenAI HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc


class OpenAiAsr:
    """Speech to text with OpenAI, the local model when OpenAI cannot.

    Whether anyone speaks is still decided here, by the same VAD the local
    model uses: music and silence never leave the machine and cost nothing.
    The API returns the text without timestamps, so a transcript is one
    segment over the whole audio - for a reel of under a minute that is
    what a reader needs. The tokens it reports are kept with the track, so
    what a run cost is computed from them, not guessed.
    """

    def __init__(self, name: str, key: str | None, fallback, post=openai_transcribe, vad=speech_seconds):
        self.name = name
        self.api_model = name.split(":", 1)[1]
        self.key = key
        self.fallback = fallback
        self.device, self.compute_type, self.revision = "openai", None, None
        self._post = post
        self._vad = vad

    def load(self):
        return self

    def _local(self, wav: Path, language: str | None, why: str) -> dict:
        LOG.warning("%s: transcribing locally instead: %s", self.name, why)
        result = self.fallback().transcribe(wav, language)
        result["quality"]["flags"] = [why[:200], *result["quality"].get("flags", [])]
        return result

    def transcribe(self, wav: Path, language: str | None = None) -> dict:
        started = time.time()
        audio, speech = self._vad(wav)
        params = {"backend": "openai", "chunking_strategy": "auto", "language": language}
        quality = {"flags": [], "audio_seconds": round(audio, 2), "speech_seconds": round(speech, 2)}
        if speech < 1.0:
            return {"status": "no_speech", "language": None, "segments": [], "text": "",
                    "quality": {**quality, "seconds": round(time.time() - started, 2)},
                    "model": self.name, "model_revision": None, "params": params}
        if not self.key:
            return self._local(wav, language, "openai_failed: no OpenAI key on this worker")
        if wav.stat().st_size > OPENAI_UPLOAD_LIMIT:
            return self._local(wav, language, "too_long_for_openai")
        try:
            out = self._post(self.key, self.api_model, wav, language)
        except Exception as exc:  # noqa: BLE001 — any failure: the local model answers
            return self._local(wav, language, f"openai_failed: {exc}")
        text = (out.get("text") or "").strip()
        segments = [{"start_ms": 0, "end_ms": int(audio * 1000), "text": text}] if text else []
        return {
            "status": "available" if segments else "no_speech",
            "language": language if segments else None,
            "segments": segments,
            "text": text,
            "quality": {**quality, "seconds": round(time.time() - started, 2),
                        **({"usage": out["usage"]} if out.get("usage") else {})},
            "model": self.name,
            "model_revision": None,
            "params": params,
        }
