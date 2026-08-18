---
name: caret-connect
description: Connect the Caret iOS keyboard to this machine's Claude Code. Use when the user says "connect Caret", "set up Caret", "point Caret at my agent", "Caret keyboard backend", or wants their iOS keyboard to draft through the agent they already run.
---

# Connect Caret to Claude Code

Caret is an iOS keyboard that drafts messages by calling a `caret/v1`
HTTP backend the user controls. Your job: deploy the canonical Caret
reference backend on this machine, configured to use Claude Code as its
Ask adapter, then hand the user the URL and API key to paste into the
Caret app.

Follow the full prompt at
<https://docs.typewithcaret.com/agent-prompts/connect-caret.md> — it is
the authoritative version of these steps. In short:

1. **Get the reference backend** (canonical public source —
   `github.com/kballenegger/caret-docs`, source-available license):

   ```sh
   git clone https://github.com/kballenegger/caret-docs.git
   cd caret-docs/reference-backend
   ```

2. **Configure.** Claude Code as the Ask adapter; STT defaults to local
   OpenWhisper when `whisper` is installed (dictation reports honestly
   off otherwise):

   ```sh
   export CARET_AGENT=claude-code
   export CARET_API_KEYS="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
   ```

   The preset runs `claude -p --permission-mode plan
   --no-session-persistence` — read-only, non-interactive, nothing
   persisted. Never add permission-bypass flags; the backend refuses
   them at startup.

3. **Validate before deploying:**

   ```sh
   python3 -m caret_backend --check
   python3 -m caret_backend --port 8787 &
   curl -s http://127.0.0.1:8787/v1/health | python3 -m json.tool
   ```

   Health must report `"status": "ok"`, honest capabilities, and
   `"adapters": {"agent": "claude-code", …}`. Run one real draft with
   the key; a wrong key must get 401.

4. **Put TLS in front** (Caret requires `https://`): `tailscale serve
   --bg 8787` on a tailnet the phone joins (recommended), or a reverse
   proxy with a public certificate on a VPS. Keep the backend itself on
   loopback. Keep it alive with launchd/systemd — the runbook at
   <https://docs.typewithcaret.com/connect/claude-code/> has both paths.

5. **Hand over** the final `https://` base URL and the API key; the user
   pastes both into Caret's settings. The key lives in the environment,
   never in a committed file.

Report honestly anything that does not verify.
