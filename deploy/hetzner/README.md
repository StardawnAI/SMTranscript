# The production worker: Hetzner `smtranscript-worker`

Since 2026-11-14 the worker the AWG hub calls runs on its own Hetzner Cloud
server (cx23, Nürnberg, 46.224.50.117, Ubuntu 24.04), in `/opt/smtranscript`
with this `docker-compose.yml`. stardawneg64 is the founder's home network and
is for tests only, never production.

- **No open port.** The worker listens on 127.0.0.1:47830; the hub reaches it
  at `https://smtranscript.stardawn-service.com` through the Cloudflare tunnel
  `smtranscript-worker-hetzner` (cloudflared in this compose, profile
  `tunnel`), with Cloudflare Access in front. The hostname has its own DNS
  record, ahead of the `*.stardawn-service.com` wildcard.
- **Platform requests leave from the server's own IP.** Transcription streams
  through the Cloudflare relay to Groq; nothing is downloaded for it.
- **Settings** in `/opt/smtranscript/.env` (mode 600): the API keys of the
  callers, the relay URL and token, the Groq key and the vision keys - copied
  from the old host, never into this repository. `TUNNEL_TOKEN` is the tunnel's.
- **Update:** set `SMTRANSCRIPT_REF` (or the default here) to the new commit,
  then `docker compose up -d --build smtranscript`.
- **Cost:** 5.49 EUR a month (cx23) + 0.50 EUR (IPv4), Hetzner's price list of
  2026-11-14.
