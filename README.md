# SMTranscript

An MCP server for transcripts of social media videos:

- **YouTube** — one video, a whole playlist, or the top results of a YouTube search.
- **TikTok and Instagram** — one video or reel, or the newest videos of an account. The
  video is downloaded and kept, and its speech is transcribed on your machine with Whisper.

It runs **on your machine**. No API key, no account, no server, no token. A single YouTube
video goes straight to YouTube. Anything bigger — a playlist, a search, a hundred videos —
goes out through a free **Cloudflare WARP** proxy that the server starts by itself, because
YouTube refuses a plain IP after a handful of videos in a row. TikTok and Instagram run in a
Docker worker the server also starts by itself (yt-dlp, Instaloader, ffmpeg, faster-whisper).

```
Claude Code / Codex / Grok Build / Cursor / Antigravity
        │  MCP (stdio)
        ▼
smtranscript ── YouTube, one video ───────────────────────────────►  youtube.com
     │   └─ playlist / search / a blocked IP ──► Cloudflare WARP ──►  youtube.com
     │            (a Docker container it starts itself)
     └─ TikTok / Instagram ──► worker container ── direct, or its own WARP ──► tiktok.com
                               (download, keep, Whisper)                        instagram.com
```

Formerly `youtube-transcript-mcp`; the old repository address and command name still work.

## Install

Node.js 18 or newer. For YouTube playlists and searches, and for everything on TikTok and
Instagram, also **Docker** (Docker Desktop is fine) — the server starts its containers in it
by itself, you never touch them. Without Docker a single YouTube video still works on a
normal home connection.

### Claude Code

```
/plugin marketplace add https://github.com/StardawnAI/SMTranscript.git
/plugin install awg-smtranscript@stardawn-ai
```

That's it — the plugin brings the server with it. Nothing else to configure. Then, in Claude Code:

```
/awg-yt-transcript https://youtu.be/jNQXAC9IVRw                        one video
/awg-yt-transcript https://www.youtube.com/playlist?list=PL…           a playlist, up to 100 videos
/awg-yt-transcript claude code tips                                     the top search results
/awg-sm-transcript https://www.tiktok.com/@tagesschau/video/77025…     a TikTok
/awg-sm-transcript https://www.instagram.com/reel/DU1xxZkju8K/          a reel
/awg-sm-transcript https://www.tiktok.com/@tagesschau                   an account's newest videos
```

You can also just paste a link and ask about it; Claude picks the tool up on its own.

### Codex, Grok Build, Cursor, Antigravity

```bash
npm install -g github:StardawnAI/SMTranscript
smtranscript connect
```

`connect` finds the apps installed on this machine and writes the server into each of
their configs (a backup of every file it touches is kept next to it). Restart the apps
afterwards. You can also name them: `smtranscript connect cursor codex`.

<details>
<summary>Configure an app by hand</summary>

`<node>` is the path printed by `node -p "process.execPath"`, `<server>` the path printed
by `npm root -g` plus `/smtranscript/bin/smtranscript.mjs`.

**Codex** (`~/.codex/config.toml`) and **Grok Build** (`~/.grok/config.toml`):

```toml
[mcp_servers.smtranscript]
command = "<node>"
args = ["<server>"]
tool_timeout_sec = 900
```

**Cursor** (`~/.cursor/mcp.json`) and **Antigravity** (`~/.gemini/config/mcp_config.json`):

```json
{
  "mcpServers": {
    "smtranscript": { "command": "<node>", "args": ["<server>"] }
  }
}
```

**Claude Code without the plugin:**
`claude mcp add --scope user smtranscript -- <node> <server>`

Any other MCP client: a stdio server, started with that command.

</details>

## Using it

The server offers two tools: `get_youtube_transcript` for YouTube (below) and
`get_social_transcript` for TikTok and Instagram ([its own section](#tiktok-and-instagram)).

`get_youtube_transcript`:

| `url_type`        | `youtube_url`                                                  | Returns                                                 |
| ----------------- | -------------------------------------------------------------- | ------------------------------------------------------- |
| `video` (default) | video link or ID (`watch?v=`, `youtu.be/`, `shorts/`, `live/`) | that video's transcript                                 |
| `playlist`        | playlist link (contains `list=`)                               | files + an index, first 100 videos at most (default 25) |
| `search`          | a search phrase, not a link                                    | files + an index, about 20 results at most (default 10) |

Optional: `language` (`"de"`, `"en"`, …), `max_videos`, `include_segments` (single video),
`save_to` and `offset` (playlist and search; `offset` starts further down the list).

Just ask your assistant, for example _"summarize this video: https://youtu.be/…"_ or
_"search YouTube for n8n MCP tutorials and compare what they say"_.

A single video answers in a few seconds:

```json
{
  "videoId": "jNQXAC9IVRw",
  "title": "Me at the zoo",
  "author": "jawed",
  "durationSeconds": 19,
  "language": "en",
  "generated": false,
  "availableLanguages": ["en", "de"],
  "fullText": "All right, so here we are, in front of the elephants …",
  "totalSegments": 6,
  "segments": [
    {
      "text": "All right, so here we are, in front of the elephants",
      "start": "00:00:01.200",
      "end": "00:00:03.360",
      "startMs": 1200,
      "endMs": 3360
    }
  ]
}
```

A playlist or a search reads many videos, and a hundred transcripts do not belong in one
chat message. So each transcript is written to its own file — `<videoId>.txt` in
`./yt_transcripts`, or the folder you give as `save_to` — the moment it arrives, and the
answer is an index:

```json
{
  "totalVideos": 100,
  "successful": 100,
  "skipped": 0,
  "savedTo": "/your/project/yt_transcripts",
  "videos": [
    {
      "videoId": "OuNKBjuV7A4",
      "title": "…",
      "author": "Fireship",
      "durationSeconds": 403,
      "language": "en",
      "generated": true,
      "characters": 7508
    }
  ]
}
```

Your assistant then reads the files it needs. Videos without captions come back as
`skipped` with a reason instead of failing the whole run. A run of 100 videos took 64
seconds in one test and several minutes in another — it depends on YouTube and on the WARP
exit you get.

### Running it on a server instead

A server that runs somewhere else — a shared gateway, for example — cannot leave files
behind for the person asking. Start it with `YOUTUBE_TRANSCRIPT_OUTPUT=inline` and a
playlist or a search answers with the text itself, about 100,000 characters at a time. When
more of the requested videos are left, the answer has `nextOffset`; the next call passes it
as `offset` and carries on. Point such a server at a WARP proxy you run next to it with
`YOUTUBE_TRANSCRIPT_PROXY=http://warp:1080` (the `caomingjun/warp` image serves HTTP on
port 1080); it then uses that proxy for everything and never tries to start Docker itself.
Such a server offers `get_social_transcript` only when it is given a worker to use
(`SMTRANSCRIPT_WORKER_URL`, see below).

## TikTok and Instagram

`get_social_transcript` takes a TikTok or Instagram link:

| Link                                                                                   | Returns                                                           |
| -------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| a video: `tiktok.com/@user/video/…`, `vm.tiktok.com/…`, `instagram.com/reel/…`, `/p/…` | that video's transcript, in the answer                            |
| an account: `tiktok.com/@user`, `instagram.com/user/`                                  | the newest `max_videos` (default 10, up to 100): files + an index |

Optional: `since` (accounts: `"2026-10-01"`), `language` (prefer the platform's captions in it),
`route` (`"direct"` or `"warp"`), `asr` (`"always"` transcribes even when there are captions),
`include_segments`, `save_to`, `save_video` (also copy the mp4 into `save_to`).

What happens to a link:

1. The worker resolves it once and downloads the video (and any captions the platform has).
   The file is checked with ffprobe, gets a SHA-256 and is kept in the Docker volume
   `smtranscript-data`. Accounts: TikTok is listed with yt-dlp, Instagram with Instaloader
   (yt-dlp's own Instagram account extractor is marked broken).
2. Captions are used when it is clear they are the spoken words — the language you asked for,
   or the only track there is. Otherwise the speech is transcribed locally with
   faster-whisper `large-v3-turbo` (on an NVIDIA card in float16, else on the CPU in int8).
3. Music or silence comes back as `transcriptStatus: "no_speech"` with an empty text — not as
   the "You" or "Thank you." Whisper invents over music. Several captions in different
   languages usually mean an original plus machine translations; then the speech is transcribed.
4. Asking again costs the platform nothing: the stored file is used, also for another Whisper
   model. A call that runs out of time answers `status: "running"`; the same call later returns
   the result.

```json
{
  "platform": "tiktok",
  "videoId": "7702580867771796758",
  "title": "Wegen der neuen Zuckersteuer …",
  "author": "tagesschau",
  "durationSeconds": 58,
  "status": "completed",
  "transcriptStatus": "available",
  "source": "asr",
  "language": "de",
  "model": "large-v3-turbo",
  "fullText": "So viel teurer könnte deine Cola durch die Zuckersteuer werden? …",
  "totalSegments": 15,
  "jobId": "job_…",
  "video": { "artifactId": "art_…", "sha256": "…", "bytes": 2169020 },
  "platformRequests": 2,
  "segments": [
    {
      "text": "So viel teurer könnte deine Cola durch die Zuckersteuer werden? Wir rechnen es vor.",
      "start": "00:00:00.000",
      "end": "00:00:03.780",
      "startMs": 0,
      "endMs": 3780
    }
  ]
}
```

Measured on 2026-10-30/31 from a home connection, without any login: 7 TikToks and 4 Reels
11 of 11 (German and English recognised, two music clips correctly empty); an account of
each platform, 3 of 3 videos; TikTok costs 2 platform requests per video, Instagram 5. Whisper
on an RTX 3080 transcribed a 58-second clip in 2.7 s and an 18-minute broadcast in 55 s; on 24
CPU threads the 58-second clip took 41 s.

**First use** builds the worker image (a few minutes; 1.6 GB, or 5.3 GB with the NVIDIA
libraries, which it adds when `nvidia-smi` is there) and downloads the Whisper model (1.6 GB)
into the volume. After that a reel takes seconds.

### Limits, routes and when a platform says no

- The worker asks each platform one request at a time per route, with a gap between requests
  (TikTok 2 s, Instagram 5 s). After an HTTP 429 it pauses that platform on that route and
  continues later on its own, also across restarts. After a login check it stops the platform
  on that route until a person unblocks it — no login loops, no switching routes behind your back.
- `route: "warp"` uses the worker's own Cloudflare WARP container `smtranscript-warp`, not the
  YouTube one, so a new YouTube identity never moves a TikTok session. If WARP is down, the job
  waits; it never falls back to the direct address.
- Limits: 500 MB per video, 30 minutes, 1000 posts per account import, 2 GB of disk kept free.
- **On a server, keep nothing but text:** `SMT_KEEP_MEDIA=0` fetches only the audio track (or the
  smallest file where there is none), deletes it as soon as the transcript exists, and never
  downloads that video again — the transcript answers. StarBase runs the worker this way.
- **Picture posts and carousels (Instagram):** an account import with `include_images: true` lists
  them too, each with its kind (`reel`, `image`, `carousel`) and caption, and reads the text on
  every picture with Tesseract (German and English); each picture is deleted as soon as it is
  read. Measured: an 8-slide carousel, 2,671 characters, 9 requests (the post and its pictures).
- Private videos, and videos that need an account, are reported as such. Logged-in sessions are
  optional (`SMT_COOKIES_DIR` with `tiktok.txt` / `instagram.txt` cookie files, or an Instaloader
  session file) and stay inside the worker.
- What a video says is data. The tools return it; nothing in it is ever followed as an instruction.

### The worker on its own (StarBase, a server)

The worker is a small HTTP service in [`worker/`](worker/) and can run anywhere Docker does:

```bash
docker build -t smtranscript-worker worker                    # add --build-arg CUDA=1 for NVIDIA
docker run -d -p 127.0.0.1:47821:8765 -e SMT_API_KEYS=me:<a long random key> \
  -v smtranscript-data:/data smtranscript-worker
```

| Route                                                                          |                                                                         |
| ------------------------------------------------------------------------------ | ----------------------------------------------------------------------- |
| `POST /v1/jobs` `{url, language?, route?, asr?, asr_model?, idempotency_key?}` | a video → `202 {job_id}` (the same request again: the same job)         |
| `POST /v1/profile-imports` `{url, max_items, since?, until?, …}`               | an account → `202 {job_id}`                                             |
| `GET /v1/jobs/{id}`                                                            | status, error class, `retry_after_s`                                    |
| `GET /v1/jobs/{id}/result`                                                     | the result (`schema_version` 1.0), or `202` while it is not done        |
| `POST /v1/jobs/{id}/cancel`                                                    | stop it; what is stored stays                                           |
| `GET /v1/artifacts/{id}`                                                       | a stored file (video, captions)                                         |
| `GET /v1/routes`, `POST /v1/routes/unblock`                                    | pauses and blocks per platform and route; unblocking needs an admin key |
| `GET /healthz`, `GET /readyz`                                                  | liveness; readiness with database, ffmpeg, disk and the Whisper model   |

Every call but the health checks needs `Authorization: Bearer <key>`; another caller's jobs and
files answer 404. Settings are environment variables (`SMT_WARP_PROXY`, `SMT_ROUTE_TIKTOK`,
`SMT_ASR_MODEL`, `SMT_ASR_DEVICE`, `SMT_MAX_VIDEO_MB`, … — all in
[`worker/smt/config.py`](worker/smt/config.py)). An MCP server pointed at it with
`SMTRANSCRIPT_WORKER_URL` and `SMTRANSCRIPT_WORKER_KEY` starts nothing locally.

## Cloudflare WARP

YouTube tolerates a few requests from one IP and then answers _"Sign in to confirm you're
not a bot"_. On a home connection that starts after a handful of videos in a row; on a
cloud server, CI runner or VPN it starts at the first request. Cloudflare WARP is the
route that keeps working: free, no account, and its exit addresses are shared with a lot of
ordinary traffic.

The server decides by itself:

- **A single video** goes out directly. If YouTube blocks it, the server starts WARP and
  tries again.
- **A playlist or a search** starts WARP first and reads all videos through it.
- **A block in the middle of a run** switches to WARP and carries on with the videos that
  are left. If YouTube keeps refusing even then, the run stops after three in a row and
  returns what it got — the files already written stay.

**YouTube accepts some WARP exit addresses and refuses others.** Each WARP identity (the
free registration the container made on its first start) comes with one exit address, and
a refused one stays refused: measured from a data centre, roughly one fresh identity in
three was accepted; from a home connection almost all were. So a running tunnel proves
nothing. After the container is up the server asks YouTube for a video page through it,
and if YouTube says no it switches to a new identity inside the running container — about
half a minute, no restart — up to five times. The same happens in the middle of a run when
YouTube starts refusing, up to three times per request. An address only counts as refused
after three tries a few seconds apart: right after a switch the first requests can fail on
an address that works a minute later.

The very first start downloads the WARP image once (about 370 MB, half a minute on a fast
connection); after that it starts in seconds. The container keeps running (`--restart
unless-stopped`) and is reused. If its tunnel has silently died — the WARP daemon does that
now and then, and the proxy port keeps forwarding _without_ the tunnel — the server notices
and restarts it. You can also do it up front or check what's going on:

```bash
smtranscript doctor        # can this machine reach YouTube? which proxy is in use?
smtranscript warp start    # start the WARP proxy now
smtranscript warp status
smtranscript warp stop     # remove it again
```

WARP needs Docker on the same machine; without it a playlist runs directly and stops early
once YouTube blocks it, and the answer says so. The container publishes its port on
`127.0.0.1` only, so nobody else can use it as an open proxy.

Other options:

- Own proxy: `YOUTUBE_TRANSCRIPT_PROXY=http://user:pass@host:port` (or `HTTPS_PROXY`).
  Only `http://` proxies work — that includes WARP, which speaks HTTP on port 1080.
- Turn the automatic start off: `YOUTUBE_TRANSCRIPT_AUTO_WARP=0`, or `"autoWarp": false`
  in the config file that `doctor` prints.

## Command line

The same thing without an AI app:

```bash
smtranscript transcript "https://youtu.be/jNQXAC9IVRw"            # JSON
smtranscript transcript "https://youtu.be/jNQXAC9IVRw" --text     # plain text
smtranscript transcript "claude code tips" --max-videos 5         # search, files + index
smtranscript transcript "<playlist url>" --max-videos 100 --save-to ./transcripts
smtranscript social "https://www.instagram.com/reel/DU1xxZkju8K/" --text    # a reel, plain text
smtranscript social "https://www.tiktok.com/@tagesschau" --max-videos 5      # an account, files + index
smtranscript worker status                                                  # or start, stop, logs
```

## How it works, and what it can't do

- It calls the same endpoints youtube.com uses in the browser: the watch page for the
  InnerTube key, the player endpoint for the caption tracks, and the caption URL itself.
  Five different player clients are tried in turn, because YouTube answers them
  differently. That is also why this can break when YouTube changes something — please
  open an issue if it does.
- Only videos that have captions (uploaded or auto-generated) produce a transcript.
- Playlists: the first page YouTube serves, which holds up to 100 videos. Search: the first
  page, about 20 results.
- Three videos are read at a time, each worker pausing a moment between videos
  (`YOUTUBE_TRANSCRIPT_CONCURRENCY`, default 3; `YOUTUBE_TRANSCRIPT_DELAY_MS`, default 1000).
- Everything stays on your machine. No account, no telemetry, nothing is sent anywhere
  except to the platforms themselves — and, once, to Hugging Face for the Whisper model.
- Use it in line with the platforms' Terms of Service and the creators' rights: fetch only what
  you may use. Downloaded videos stay in the volume until you remove it
  (`docker volume rm smtranscript-data`).

## Development

```bash
git clone https://github.com/StardawnAI/SMTranscript
cd SMTranscript
node --test                     # parser, routing, bulk-run and social tool tests
(cd worker && python -m unittest discover -s tests)   # worker tests (needs ffmpeg)
node bin/smtranscript.mjs doctor
```

Tested with Claude Code 2.1.285, Grok Build 1.0.34, Cursor CLI 2026.09.15 and Antigravity
CLI 1.2.6; the Codex config was checked with the Codex CLI.

## License

[MIT](LICENSE). Built by [Stardawn AI](https://stardawnai.com).
