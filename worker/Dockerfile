# SMTranscript worker: yt-dlp, Instaloader, ffmpeg and faster-whisper behind a small HTTP job API.
#
#   docker build -t smtranscript-worker worker                    # CPU
#   docker build -t smtranscript-worker --build-arg CUDA=1 worker # + NVIDIA (about 1 GB more)
#
# Smoke test before pinning a new image (YouTube must offer a downloadable mp4):
#   docker run --rm --entrypoint yt-dlp smtranscript-worker --simulate \
#     -f 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b' https://www.youtube.com/watch?v=jNQXAC9IVRw
#
# The Whisper model is not in the image: it is downloaded on first start into
# the data volume (/data/models) and kept there.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ARG CUDA=0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tesseract-ocr tesseract-ocr-deu \
    && rm -rf /var/lib/apt/lists/*

# deno: the JavaScript runtime yt-dlp needs for YouTube's challenges (with yt-dlp-ejs
# from requirements.lock); without it YouTube offers no usable formats. A pinned
# release binary, checked against its published SHA-256.
ARG DENO_VERSION=2.9.8
ARG TARGETARCH
RUN set -e; \
    case "${TARGETARCH:-amd64}" in \
      amd64) arch=x86_64; sum=e890975bc2549807bbcc0b584ffafda72cc7c93349dd99c80483dd784f6c4609 ;; \
      arm64) arch=aarch64; sum=d49596c20b9788cf8f5d8ddf644103d4bde56ed239753f0bdb92ba9361d51905 ;; \
      *) echo "no deno build for ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/tmp/deno.zip')" \
      "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${arch}-unknown-linux-gnu.zip"; \
    echo "${sum}  /tmp/deno.zip" | sha256sum -c -; \
    python -m zipfile -e /tmp/deno.zip /usr/local/bin; \
    chmod 755 /usr/local/bin/deno; rm /tmp/deno.zip; \
    deno --version

COPY requirements.txt requirements.lock requirements-cuda.txt /app/
RUN pip install --no-cache-dir --no-deps -r /app/requirements.lock \
    && pip check \
    && python -c "import yt_dlp_ejs" \
    && if [ "$CUDA" = "1" ]; then pip install --no-cache-dir -r /app/requirements-cuda.txt; fi

# Where ctranslate2 finds cuBLAS and cuDNN when they were installed above.
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib \
    SMT_DATA=/data \
    HF_HOME=/data/models/hf \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Not root: the parsers and ffmpeg read files from the internet.
RUN useradd --uid 10001 --create-home smt && mkdir /data && chown smt /data
COPY smt /app/smt
WORKDIR /app
USER smt

EXPOSE 8765
VOLUME /data
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=5)"
CMD ["python", "-m", "smt"]
