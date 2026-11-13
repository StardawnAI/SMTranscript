# Streaming transcription

The resolver reads the social post metadata and existing captions. When captions
answer the request, no media or speech-to-text request is needed. Otherwise the
Cloudflare Worker fetches a progressive media URL directly from the platform CDN
and streams multipart bytes to Groq Whisper Large V3 Turbo. The resolver receives
only the timed transcript. Oracle does not carry the media in either direction.

No audio or video is written to a local file, R2, KV or a Durable Object. The
Durable Object stores budget reservations and completed transcripts for replay.
The existing resolver database continues to store post metadata and text tracks.
Groq's own data retention controls are separate and were not changed here.

## Connection settings

The platform operator configures the worker connection; customers submit a social
URL through the existing interface and do not need command-line setup.

| Worker setting               | Value                                                                  |
| ---------------------------- | ---------------------------------------------------------------------- |
| `SMT_ASR_MODEL`              | `groq:whisper-large-v3-turbo`                                          |
| `SMT_KEEP_MEDIA`             | `0`                                                                    |
| `SMT_TRANSCRIPT_RELAY_URL`   | The deployed HTTPS relay origin                                        |
| `SMT_TRANSCRIPT_RELAY_TOKEN` | Relay secret, kept outside the repository                              |
| `SMT_GROQ_API_KEY`           | Existing Groq key, transmitted over HTTPS but not stored in Cloudflare |

The Cloudflare deployment uses `wrangler.jsonc`, a secret named `RELAY_TOKEN` and
the SQLite Durable Object binding `BUDGET`. A shared global budget serializes
reservations across callers. The default ceiling is EUR 0.25 per UTC day, with a
half-budget warning returned to the calling job. Each reservation conservatively
uses the trusted resolver duration plus 30 seconds, USD 0.04 per hour and
2 EUR per USD. Reservations are not refunded after failures. This is an
application reservation ceiling, not a verified Groq account billing limit.

A job may name its `member` (`<tenant>:<user>`, for example one user of the AWG
hub). A member then also spends from a share of their own, `MEMBER_DAILY_EUR_LIMIT`
(EUR 0.10 per UTC day), beside the day's total, so one user cannot use up everybody's
day. A video already transcribed costs nobody anything again.

Stable media/model/language identifiers replay completed results without another
paid call. An interrupted provider call is not automatically repeated. The Groq
key and source cookies are excluded from errors and persisted relay state.

## Limits

- Progressive HTTPS media only; no HLS/DASH manifests or local FFmpeg fallback.
- Known duration of at most 900 seconds and Content-Length at most 25 MiB.
- Two-minute request deadline; CDN hostname and redirect validation.
- Source cookies remain scoped to the original CDN hostname.
- Video description and media retention are incompatible with this mode.
- A CDN may reject a URL resolved through another IP/session. A successful TikTok
  test does not establish that every Instagram session works across those IPs.
- No paid retry or local download fallback hides a Cloudflare/CDN failure.

## Acceptance on 2026-11-04

A real 34-second TikTok first returned existing German captions in 6.83 seconds.
Forcing ASR then returned 34.602 seconds of timed speech through Cloudflare/Groq
in 9.83 seconds: 705,387 media bytes, no local audio/video files, no temporary files.
Replaying the exact request returned `cached: true` and the same 820-character
transcript. Its conservative reservation was EUR 0.001423; the published audio
tariff implies about USD 0.0003845. The invoice amount was not independently read.

For 10,000 one-minute videos, Groq's published tariff is about USD 6.67. At the
measured file size, the media would enter and leave Cloudflare; no media crosses
Oracle. Cloudflare Workers does not charge separately for bandwidth/egress, but
request, CPU and Durable Object usage/plan limits still apply. No paid plan upgrade
was requested or performed by this implementation.

Sources: [Groq speech-to-text](https://console.groq.com/docs/speech-to-text),
[Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/),
[Durable Objects pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/).
