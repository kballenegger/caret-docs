# Connect Caret to GrokBot

You are the GrokBot agent. This prompt tells you how to put yourself behind
Caret, the agentic iOS keyboard.

Read [connect-caret.md](https://docs.typewithcaret.com/agent-prompts/connect-caret.md)
first for the general shape, then this. **Use the public Caret reference
backend. Do not build a new backend and do not fork this one.**

GrokBot is a first-party Caret agent backend with its own preset,
`CARET_AGENT=grokbot`. It is *not* a `custom-http` deployment: you host the
reference backend inside your own system and serve Ask, transcript cleanup,
and — when your image capability is enabled — Imagine from your own model.

Runbook with the full contract:
<https://docs.typewithcaret.com/connect/grokbot/>

## 0. Ground rules

- Everything you need is public: the backend, its tests, its contract. Clone
  it; do not reimplement it.
- Do not invent GrokBot endpoints, model names, or capabilities. Implement
  the endpoint contract in step 2 inside whatever GrokBot actually exposes.
- **Never claim a capability until you have tested it end to end.** If your
  image capability is not enabled, leave Imagine off. Honest "off" is a
  valid backend; a false "on" is a broken keyboard.
- After adding Ask or Imagine, make `/v1/health` advertise the matching
  capability so Caret shows the surface: `capabilities.draft: true` for Ask
  and `capabilities.imagine: true` for Imagine. Keep either `false` until
  its real endpoint passes a round-trip test.
- Secrets go in the host's secret store or environment, never into a repo,
  a log line, or a chat message.

## 1. Get the reference backend

```sh
git clone https://github.com/kballenegger/caret-docs.git
cd caret-docs/reference-backend
```

Read `README.md` and `caret_backend/adapters.py` (the GrokBot section) before
configuring anything. Keep the backend's public contract and its safety
checks intact.

## 2. Implement the GrokBot endpoint

One route, discriminated by `task`. `task` is explicit rather than inferred
from the prompt, because cleanup has to hit a deterministic no-tools path on
your side:

```
POST <CARET_GROKBOT_URL>
Content-Type: application/json
Authorization: Bearer <CARET_GROKBOT_BEARER>     // if you set one

{"task": "ask",     "prompt": "<framed drafting prompt>"}  -> 200 {"text": "..."}
{"task": "cleanup", "prompt": "<framed cleanup prompt>"}   -> 200 {"text": "..."}
{"task": "imagine", "prompt": "...",
 "aspect_ratio": "1:1", "quality": "standard"}             -> 200 {"image_base64": "<base64 PNG>"}
```

- **Ask** — your own LLM, answering the framed prompt.
- **Cleanup** — the same LLM, no tools, no actions, no memory writes. Fix
  the transcript; do not answer it.
- **Imagine** — your own image capability, returning a base64-encoded PNG.
  Implement this route only if that capability is genuinely enabled.

Anything else — non-200, missing field, non-PNG image — the backend turns
into a `503 internal_error` for the phone. Do not return placeholders.

Never log the prompt, the reply, the transcript, or either key. Log a
request id and a status, the way the backend does.

## 3. Configure the backend

```sh
export CARET_AGENT=grokbot
export CARET_GROKBOT_URL='https://grokbot.internal/caret'
export CARET_GROKBOT_BEARER='...'          # whatever your endpoint checks
export CARET_GROKBOT_IMAGE=on              # only if imagine is really served

python3 -c 'import secrets;print(secrets.token_urlsafe(32))'  # generate a key — copy the output
export CARET_API_KEYS=paste-the-key-here                      # this goes into the Caret app
```

`CARET_GROKBOT_IMAGE` takes only `on` or `off`; anything else is a startup
error rather than a guess.

## 4. Dictation is mandatory — and it is not yours

A valid `caret/v1` backend must take speech. No stable non-interactive
GrokBot transcription interface is claimed here, so speech does **not** route
through your endpoint. Configure the backend's own STT lane:

- local [OpenWhisper](https://github.com/openai/whisper) on the machine
  running the backend (the default, and the private one), or
- `CARET_STT_HTTP_URL` for a hosted STT service, or
- `CARET_STT_COMMAND` for any command that transcribes audio.

With none of those resolved, health reports `"status": "not_ready"` with the
`no_stt_adapter` blocker, `--check` exits non-zero, and the connection QR
refuses to generate. Do not work around that. A GrokBot deployment that
answers Ask beautifully and cannot take dictation is not a Caret backend.

Ask and Imagine, by contrast, are optional and reported honestly off.

## 5. Choose phone access

Ask the user which they want:

- **Public HTTPS — simpler for the phone.** Keep the backend bound to
  loopback behind a TLS reverse proxy on a dedicated subdomain; the API key
  and the proxy are the entire perimeter.
- **Tailscale only — private.** The phone must join and stay on the same
  tailnet for the keyboard to work.

Test phone-to-backend reachability before moving on.

## 6. Run it as a supervised service

Run the backend under the host's supervisor with restart-on-failure, and
verify it comes back after a forced restart and after a reboot.

## 7. Verify before handoff

```sh
python3 -m caret_backend --check
curl -s https://<your-url>/v1/health | python3 -m json.tool
```

Confirm, from the phone's network:

- `readiness.ready` is `true` and `blockers` is empty;
- `capabilities.dictation` is `true` — non-negotiable;
- `capabilities.draft` is `true` and `adapters.agent` is `grokbot`;
- `capabilities.imagine` matches reality — `true` only if you tested a real
  image round-trip, `false` otherwise;
- `routes` matches the live setup;
- a request without the API key gets `401`;
- one real draft, one real transcription, and (if enabled) one real image
  each came back end to end.

## 8. Hand over a connection QR

Only after step 7 passes:

```sh
python3 -m caret_backend --qr --url https://<your-url>
```

This prints a `caret-connect:v1` code the Caret app scans — base URL and key
in one step, no typing. Add `--save ~/caret-connect.png` to write an
owner-only PNG instead.

**Treat the QR as the credential it is.** It carries the API key, not a
pointer to one. Do not log it, do not paste it into a chat transcript, do not
echo the payload — the CLI prints only a masked fingerprint, and you should
too. Anyone who scans it can use this backend until `CARET_API_KEYS` is
rotated; rotating the key invalidates every QR ever generated from it.

Show it to the user's phone once, then delete any saved file. Also give them
the base URL and the key in a secure channel as the manual fallback.
