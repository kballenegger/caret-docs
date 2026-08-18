---
name: caret-connect
description: Connect the Caret iOS keyboard to this machine's Hermes agent. Use when the user says "connect Caret", "set up Caret", "point Caret at Hermes", "Caret keyboard backend", or wants their iOS keyboard to draft through the agent they already run.
---

# Connect Caret to Hermes

Caret is an iOS keyboard that drafts messages by calling a `caret/v1`
HTTP backend the user controls. Your job: deploy the canonical Caret
reference backend on this machine, configured to use the stock Hermes
CLI as its Ask adapter, then hand the user the URL and API key to paste
into the Caret app.

Follow the full prompt at
<https://docs.typewithcaret.com/agent-prompts/connect-caret.md> — it is
the authoritative version of these steps. In short:

1. **Get the reference backend** (canonical public source —
   `github.com/kballenegger/caret-docs`, source-available license):

   ```sh
   git clone https://github.com/kballenegger/caret-docs.git
   cd caret-docs/reference-backend
   ```

2. **Configure.** Hermes as the optional Ask adapter. Dictation is
   **mandatory**: STT defaults to local OpenWhisper when `whisper` is on
   PATH, otherwise set `CARET_STT_HTTP_URL` or `CARET_STT_COMMAND`.
   With none resolved, `--check` fails and health reports
   `"status": "not_ready"` — that is not a backend you may hand over:

   ```sh
   export CARET_AGENT=hermes
   python3 -c 'import secrets;print(secrets.token_urlsafe(32))'  # generate a key — copy the output
   export CARET_API_KEYS=paste-the-key-here                      # the same key goes into the Caret app
   ```

   The preset runs stock documented syntax only: `hermes chat -q
   "<prompt>" -Q`. It does not sandbox Hermes — drafting inherits the
   toolsets the user's own Hermes configuration enables. Offer the
   documented narrowing (`-t/--toolsets` via `CARET_AGENT_COMMAND`) and
   let the user choose; `hermes tools list` shows what this install has.
   Never add permission-bypass flags; the backend refuses them at
   startup.

3. **Validate before deploying:**

   ```sh
   python3 -m caret_backend --check
   python3 -m caret_backend --port 8787 &
   curl -s http://127.0.0.1:8787/v1/health | python3 -m json.tool
   ```

   Health must report `"status": "ok"`,
   `"readiness": {"ready": true, "blockers": []}`, honest capabilities
   with `"dictation": true`, and
   `"adapters": {"agent": "hermes", …}`. Run one real draft and one real
   transcription with the key; a wrong key must get 401.

4. **Put TLS in front** (Caret requires `https://`): `tailscale serve
   --bg 8787` on a tailnet the phone joins (recommended), or a reverse
   proxy with a public certificate on a VPS. Keep the backend itself on
   loopback. Keep it alive with launchd/systemd — the runbook at
   <https://docs.typewithcaret.com/connect/hermes/> has both paths.

5. **Hand over a connection QR** — only once step 3 passes over the
   final `https://` URL:

   ```sh
   python3 -m caret_backend --qr --url https://<the final base URL>
   ```

   Caret scans it and configures itself: base URL and key in one step.
   **The code is the credential** — it carries the API key, so never log
   it, never paste the payload anywhere, and tell the user that anyone who
   scans it can use the backend until they rotate `CARET_API_KEYS`. Show
   it to one phone, then delete any saved file. The manual fallback is the
   two strings, given in a secure channel. The key lives in the
   environment, never in a committed file.

Report honestly anything that does not verify.
