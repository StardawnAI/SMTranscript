# The production worker: Windows, without Docker

Since 2026-11-14 the worker the AWG hub calls runs on the Hetzner server
`mp-streamfab-test` (cx33, Nürnberg, 2.31.183.133, Windows Server 2022) in
`C:\smt-worker`. That VM has no hardware virtualization, so no Docker: the
worker runs as a plain Python program (scheduled task "SMTranscript worker",
SYSTEM, at boot, restarted on failure). The founder chose this over a separate
Linux server, to pay for no second machine. stardawneg64 is his home network:
tests only, never production.

- **Setup:** `setup.ps1 -Commit <sha>` (as Administrator) installs Python 3.12,
  ffmpeg 7.1.1 and Tesseract with German, the worker code of that commit and its
  pinned packages, and the task. The Tesseract installer's host refuses that
  machine's VPN address; copy the installer there and run it with `/S` first.
- **Settings:** `C:\smt-worker\worker.env` (Administrators and SYSTEM only),
  named as in the compose files; `run-worker.py` maps them to `SMT_*`.
- **No VPN for the platforms.** The machine's default route is NordVPN (for
  StreamFab). The worker's own line is off (`SMT_DIRECT_OFF`); its only route is
  the route proxy on the same machine (`tools/route-proxy.py`, scheduled task
  "SMTranscript route proxy", 127.0.0.1:1080), which leaves from 2.31.183.133.
- **Reached through** the Cloudflare tunnel `smtranscript-worker-hetzner`
  (cloudflared as a Windows service, ingress `http://localhost:8765`) at
  `https://smtranscript.stardawn-service.com`, Cloudflare Access in front.
- **Update:** run `setup.ps1 -Commit <new sha>`, then restart the task.
- **Cost:** nothing beyond the server it shares (cx33, 8.49 EUR a month).
