"""What a picture shows and what happens in a video - not only the words.

Pictures go to Claude, Gemini or OpenAI; videos to Gemini, which takes the
video itself, picture and sound. Plain HTTPS calls, no SDKs. The media is
sent inline and nothing is uploaded to be kept: the worker deletes its copy
right after, as it does after a transcript.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

from .errors import JobError

IMAGE_PROMPT = """Describe this picture from a social media post for a searchable knowledge base.
Write in the language of the text on the picture (German if there is no text or you are unsure).
Answer in three short parts, each starting on its own line:
Shown: what is in the picture - people, objects, setting, charts, numbers.
Happening: what is being done or explained, if anything.
Text: every piece of text on the picture, word for word, in reading order ("-" if none).
Facts only; do not guess intentions."""

VIDEO_PROMPT = """Describe this short social media video for a searchable knowledge base, in the
language spoken in it (German if unsure). Say what happens and what is shown, scene by scene with
rough times (e.g. "0:05"): actions, places, people, objects, documents, charts and numbers, and any
text shown on screen. The spoken words are stored separately as a transcript - do not repeat them;
say what the pictures add. Facts only; do not guess intentions."""

INLINE_LIMIT = 18 * 1024 * 1024  # under Gemini's 20 MB request limit, with room for the prompt


def provider(model: str) -> str:
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("gemini"):
        return "gemini"
    if model.startswith(("gpt", "o3", "o4", "o5")):
        return "openai"
    raise JobError("invalid_input", f"unknown model {model!r}")


def available(env=os.environ) -> dict[str, str]:
    """The providers this worker has a key for, with the key."""
    keys = {"anthropic": env.get("SMT_ANTHROPIC_API_KEY"), "gemini": env.get("SMT_GEMINI_API_KEY"),
            "openai": env.get("SMT_OPENAI_API_KEY")}
    return {p: k for p, k in keys.items() if k}


def _post(url: str, body: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if exc.code == 429 or exc.code >= 500:
            raise JobError("network", f"model API HTTP {exc.code}: {detail}") from exc
        if exc.code == 402:
            raise JobError("internal", "the model provider's credit is used up - top it up, then ask again") from exc
        if exc.code in (401, 403):
            raise JobError("internal", f"the model API refused the key (HTTP {exc.code}): {detail}") from exc
        raise JobError("internal", f"model API HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise JobError("network", f"model API unreachable: {exc}") from exc


def _key(prov: str, keys: dict) -> str:
    key = keys.get(prov)
    if not key:
        raise JobError("invalid_input", f"no API key for {prov} on this worker")
    return key


def describe_image(model: str, data: bytes, mime: str, keys: dict, timeout: int = 120) -> str:
    prov = provider(model)
    b64 = base64.b64encode(data).decode()
    if prov == "anthropic":
        out = _post("https://api.anthropic.com/v1/messages", {
            "model": model, "max_tokens": 1200,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": IMAGE_PROMPT}]}],
        }, {"x-api-key": _key(prov, keys), "anthropic-version": "2023-06-01"}, timeout)
        return "".join(p.get("text", "") for p in out.get("content", []) if p.get("type") == "text").strip()
    if prov == "openai":
        out = _post("https://api.openai.com/v1/chat/completions", {
            "model": model, "max_completion_tokens": 1200,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": IMAGE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}],
        }, {"Authorization": f"Bearer {_key(prov, keys)}"}, timeout)
        return (out["choices"][0]["message"].get("content") or "").strip()
    return _gemini(model, b64, mime, IMAGE_PROMPT, keys, timeout)


def describe_video(model: str, data: bytes, mime: str, keys: dict, timeout: int = 600) -> str:
    if provider(model) != "gemini":
        raise JobError("invalid_input", f"{model} cannot watch videos; a Gemini model can")
    if len(data) > INLINE_LIMIT:
        raise JobError("too_large", f"the video copy is {len(data) // 2**20} MB, over the "
                                    f"{INLINE_LIMIT // 2**20} MB a model takes inline")
    return _gemini(model, base64.b64encode(data).decode(), mime, VIDEO_PROMPT, keys, timeout)


def _gemini(model: str, b64: str, mime: str, prompt: str, keys: dict, timeout: int) -> str:
    out = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", {
        "contents": [{"parts": [{"inline_data": {"mime_type": mime, "data": b64}}, {"text": prompt}]}],
    }, {"x-goog-api-key": _key("gemini", keys)}, timeout)
    candidates = out.get("candidates") or []
    if not candidates:
        reason = (out.get("promptFeedback") or {}).get("blockReason") or "no answer"
        raise JobError("internal", f"Gemini gave no description ({reason})")
    return "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", [])).strip()
