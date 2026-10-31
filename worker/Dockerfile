# SMTranscript worker: yt-dlp, Instaloader, ffmpeg and faster-whisper behind a small HTTP job API.
#
#   docker build -t smtranscript-worker worker                    # CPU
#   docker build -t smtranscript-worker --build-arg CUDA=1 worker # + NVIDIA (about 1 GB more)
#
# The Whisper model is not in the image: it is downloaded on first start into
# the data volume (/data/models) and kept there.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ARG CUDA=0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tesseract-ocr tesseract-ocr-deu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock requirements-cuda.txt /app/
RUN pip install --no-cache-dir --no-deps -r /app/requirements.lock \
    && pip check \
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
