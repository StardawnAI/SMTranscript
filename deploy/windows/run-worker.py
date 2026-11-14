"""Starts the SMTranscript worker on Windows without Docker.

First on the Hetzner server mp-streamfab-test (Windows Server 2022, no
virtualization, so no Docker), since 2026-11-14 the production worker of the
AWG hub. Settings come from worker.env next to this file (KEY=VALUE lines,
readable by Administrators and SYSTEM only), named as in the compose files
(INGEST_SMTRANSCRIPT_*, GROQ_API_KEY, ...), and are mapped to the worker's
SMT_* names here.

That machine's default route is a VPN (NordVPN, for another program). The
worker must never use it: every platform request goes out through the route
proxy on the same machine (tools/route-proxy.py), which leaves from the
machine's own address. So the worker's own line is off (SMT_DIRECT_OFF) and
the proxy is its only route.
"""
import os
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def read_env(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def main() -> None:
    env = read_env(HERE / "worker.env")
    data = HERE / "data"
    data.mkdir(exist_ok=True)
    settings = {
        "SMT_API_KEYS": "starbase:" + env["INGEST_SMTRANSCRIPT_KEY"] + env.get("INGEST_SMTRANSCRIPT_MORE_KEYS", ""),
        "SMT_ASR_MODEL": env.get("INGEST_SMTRANSCRIPT_MODEL", "groq:whisper-large-v3-turbo"),
        "SMT_TRANSCRIPT_RELAY_URL": env.get("INGEST_SMTRANSCRIPT_RELAY_URL", ""),
        "SMT_TRANSCRIPT_RELAY_TOKEN": env.get("INGEST_SMTRANSCRIPT_RELAY_TOKEN", ""),
        "SMT_GROQ_API_KEY": env.get("GROQ_API_KEY", ""),
        "SMT_ANTHROPIC_API_KEY": env.get("ANTHROPIC_API_KEY", ""),
        "SMT_GEMINI_API_KEY": env.get("GEMINI_API_KEY", ""),
        "SMT_OPENAI_API_KEY": env.get("OPENAI_API_KEY", ""),
        "SMT_EXTRA_ROUTES": env["SMT_EXTRA_ROUTES"],
        "SMT_SHARE_DIRECT": env.get("SMT_SHARE_DIRECT", "hetzner"),
        "SMT_DIRECT_OFF": "1",
        "SMT_FETCH_WORKERS": env.get("SMT_FETCH_WORKERS", "2"),
        "SMT_KEEP_MEDIA": "0",
        "SMT_DATA": str(data),
        "HF_HOME": str(data / "models" / "hf"),
        "SMT_BIND": "127.0.0.1",
        "SMT_PORT": "8765",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    os.environ.update({k: v for k, v in settings.items() if v != ""})
    tools = [env.get("FFMPEG_DIR"), env.get("TESSERACT_DIR")]
    os.environ["PATH"] = os.pathsep.join([t for t in tools if t] + [os.environ.get("PATH", "")])
    sys.path.insert(0, str(HERE / "app"))
    os.chdir(HERE / "app")
    runpy.run_module("smt", run_name="__main__")


if __name__ == "__main__":
    main()
