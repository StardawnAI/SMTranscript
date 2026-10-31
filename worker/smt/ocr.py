"""Text written on pictures, read with Tesseract on this machine.

Picture posts and carousels on Instagram often are the content: a tip per
slide, a list, a quote. Tesseract reads German and English; the picture is
deleted by the caller once it is read.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def read_text(image: Path, languages: str = "deu+eng") -> str:
    out = subprocess.run(
        ["tesseract", str(image), "stdout", "-l", languages, "--psm", "3"],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(f"tesseract could not read {image.name}: {out.stderr.strip()[:300]}")
    # Lines as they stand on the slide, without the blank runs Tesseract leaves.
    lines = [re.sub(r"\s+", " ", line).strip() for line in out.stdout.splitlines()]
    return "\n".join(line for line in lines if line)


def version() -> str | None:
    try:
        out = subprocess.run(["tesseract", "--version"], capture_output=True, text=True, timeout=20)
        return out.stdout.splitlines()[0] if out.returncode == 0 and out.stdout else None
    except OSError:
        return None
