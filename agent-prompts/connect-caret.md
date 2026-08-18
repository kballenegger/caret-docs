# Connect Caret to the agent your user already runs

You are a coding agent. Your job is to connect Caret — an agentic iOS
keyboard — to the agent runtime your user already runs, by deploying the
**Caret reference backend**: the canonical public reference
implementation, stdlib Python you download, configure, validate, and put
behind TLS. When you are done, your user scans one QR (or pastes a URL
and an API key) into the Caret app, and their keyboard takes dictation
and drafts through their own agent.

**Dictation is mandatory.** A valid `caret/v2` backend must take speech
and advertise `"dictate": true`. Ask and Imagine are optional and are
reported honestly off when nothing provides them — but a backend with no
working STT adapter is not a Caret backend, reports
`"status": "not_ready"`, and must never be handed to a user as done.

Do not write a backend from scratch. That is a different, advanced task
with its own prompt
([implement-caret-backend.md](https://docs.typewithcaret.com/agent-prompts/implement-caret-backend.md));
only take it if your user explicitly wants a backend they build and own.
For connecting an existing agent, the reference backend is the whole job.

Authoritative resources, in order:

| Resource | What it is |
| --- | --- |
| [`reference-backend/`](https://github.com/kballenegger/caret-docs/tree/main/reference-backend) | The backend itself — canonical source in the public `kballenegger/caret-docs` repository, with its tests and [README](https://github.com/kballenegger/caret-docs/blob/main/reference-backend/README.md) documenting every variable. |
| [The Connect guide](https://docs.typewithcaret.com/connect/) | Compatibility matrix, capability routing, security model, TLS options, per-runtime runbooks (local Mac and VPS/hosted each). |

Note the license before you start: the repository is source-available,
not open source — using, copying and adapting it to connect Caret is
exactly what it permits.

## Provider runbooks

Before configuring a provider, read its short provider runbook as well as
this prompt. It gives provider-specific defaults and integration steps.

- **GrokBot:** [connect-grok.md](https://raw.githubusercontent.com/kballenegger/caret-docs/main/agent-prompts/connect-grok.md)

## How the backend routes capabilities — set expectations first

One unified backend, one adapter per capability, each routed to what
actually provides it:

- **Dictation/STT — REQUIRED, and NOT the agent.** No supported runtime
  has a verified transcription interface. Speech is transcribed by the
  backend's own STT adapter — local
  [OpenWhisper](https://github.com/openai/whisper) by default when
  `whisper` is on PATH, `CARET_STT_HTTP_URL` for a hosted service,
  `CARET_STT_COMMAND` for anything else — and only then does text reach
  the agent. With none resolved the backend reports
  `"status": "not_ready"` with a `no_stt_adapter` blocker, `--check`
  exits non-zero, and the connection QR refuses to generate. Getting STT
  working is part of the job, not an optional extra.
- **Ask — optional.** Your user's agent, when one is configured.
  `CARET_AGENT=off` (or `auto` with nothing installed) is a valid
  dictate-only backend: health says `"ask": false` and `/v2/ask`
  answers `404`. That is honest, not broken.
- **Cleanup — optional.** The same agent, as a constrained text-only
  cleanup request (no actions). What the model is told is the published
  `caret-cleanup/1` spec that ships with the backend — see
  <https://docs.typewithcaret.com/cleanup/>. The transcript arrives as
  inert data inside a `<transcript>` envelope, meaning is preserved
  completely, only formatting changes, and dictated code, paths and URLs
  come back as plain text with no Markdown fences added. **Do not write
  your own cleanup wording**; every preset sends the same spec, and
  `/v2/health` names it by digest at `routes.dictate.cleanup.spec`. With
  no agent — or on any cleanup failure — dictation returns the raw
  transcript.
- **Imagine — optional.** `CARET_AGENT=grokbot` with
  `CARET_GROKBOT_IMAGE=on` serves it from GrokBot's own image
  capability; every other preset needs a provider the user configures
  (`CARET_IMAGE_COMMAND`), and it is off otherwise.

Do not promise voice or images through an agent runtime. Health's
`routes` and `readiness` blocks are the ground truth.

## Step 1 — ask your user two questions

Ask both at once, then proceed.

1. **Which agent runtime do they run?** The `CARET_AGENT` switch:
   `claude-code` (tested), `codex` (tested), `hermes`
   (backend-supported), `grokbot` (backend-supported — a first-party
   backend that hosts this reference backend itself and serves Ask,
   cleanup and opt-in Imagine from its own model; see
   [connect-grok.md](https://raw.githubusercontent.com/kballenegger/caret-docs/main/agent-prompts/connect-grok.md)),
   `custom-http` for any other hosted agent behind one JSON POST
   (backend-supported), `openclaw` (not yet verified — requires an
   explicit command line), or `off` for a dictation-only backend.
   Repeat these labels honestly; do not promise more than the matrix
   does.
2. **Where should it run, and how will the phone reach it?** The backend
   binds loopback and Caret requires `https://`, so the real choice is
   the TLS layer: a tailnet (`tailscale serve`) on a local Mac or
   private box — the recommended default — or a reverse proxy with a
   public certificate on a VPS. Port-forwarding a home router or
   binding `0.0.0.0` bare is not an option you offer.

Then check for `whisper` on PATH. If it is missing, say plainly that
dictation is required and offer the three ways to provide it: install
local OpenWhisper (`brew install openai-whisper` — audio never leaves
the machine, and this is the recommended default), point
`CARET_STT_HTTP_URL` at a hosted STT service, or set
`CARET_STT_COMMAND`. Which one is the user's call; having one is not.

## Step 2 — get the backend

From the canonical public repository:

```sh
git clone https://github.com/kballenegger/caret-docs.git
cd caret-docs/reference-backend
```

Do not fork its behaviour on a first deployment; configuration is
environment variables, and everything you are likely to need is already
a variable.

Native packaging, where the runtime supports it: Claude Code users can
instead `claude plugin marketplace add kballenegger/caret-docs` and
`claude plugin install caret-connect@caret-docs`; Hermes users can
`hermes skills install` the raw SKILL.md URL from
`integrations/hermes/caret-connect/`. Both wrap these same steps.

## Step 3 — configure

```sh
export CARET_AGENT=claude-code   # the runtime chosen in step 1
python3 -c 'import secrets;print(secrets.token_urlsafe(32))'  # generate a key — copy the output
export CARET_API_KEYS=paste-the-key-here                      # the same key goes into the Caret app
```

Rules that are not optional:

- Generate the key with a CSPRNG as above; one key per device. The key
  lives in the environment — never in a committed file, never in code,
  never in a shell history line you leave in a script.
- Keep `CARET_HOST` on `127.0.0.1`; TLS terminates in front.
- `custom-http` needs `CARET_AGENT_HTTP_URL` (and optionally
  `CARET_AGENT_HTTP_BEARER`); `grokbot` needs `CARET_GROKBOT_URL` (plus
  optional `CARET_GROKBOT_BEARER`, and `CARET_GROKBOT_IMAGE=on` only if
  that deployment genuinely serves the `imagine` task); `openclaw`
  refuses to start without an explicit `CARET_AGENT_COMMAND` — that
  refusal is honest, not a bug.
- Never pass a permission-bypass flag in `CARET_AGENT_COMMAND`; the
  backend refuses them at startup by design.
- For Hermes, offer the documented toolset narrowing
  (`CARET_AGENT_COMMAND='hermes chat -q "{prompt}" -Q -t <toolset>'`)
  and let the user choose.
- Cleanup vocabulary is optional and defaults to public Caret terms.
  `CARET_CLEANUP_GLOSSARY=off` sends no glossary at all;
  `CARET_CLEANUP_GLOSSARY_PATH=/path/to/glossary.json` **replaces** the
  defaults with the user's own terms (copy the shipped
  `spec/cleanup/v1/glossary.json` first if they want both). One file is
  shared by everyone this backend serves, so keep anything private or
  per-person out of it.

## Step 4 — validate before deploying

```sh
python3 -m caret_backend --check
python3 -m caret_backend --port 8787 &
curl -s http://127.0.0.1:8787/v2/health | python3 -m json.tool
curl -s -X POST http://127.0.0.1:8787/v2/ask \
  -H "Authorization: Bearer $CARET_API_KEYS" \
  -H 'Content-Type: application/json' \
  -d '{"client_request_id":"smoke-1","input":{"type":"text","text":"tell Sam I am running ten minutes late"}}'
```

Health must show `"status": "ok"` and
`"readiness": {"ready": true, "blockers": []}`, capabilities that match
reality, and a `routes` block naming the chosen agent for Ask/cleanup
and the resolved STT for dictation; the draft must come back from the
real runtime. A request with a wrong key must get `401`.

`routes.dictate.cleanup.spec` is the cleanup wording's digest — it
should read `caret-cleanup/1 <16 hex characters>`. That string, not the
prompt, is what you quote when reporting which wording is live. Health
is anonymous: nothing secret goes in it.

`"status": "not_ready"` means STT is missing — fix that before anything
else; it is the one blocker you may not hand over. `"degraded"` means no
API keys. If the runtime CLI fails, run the runbook's smoke command by
hand as the same user before touching the backend. Verify one real
transcription end to end rather than assuming OpenWhisper works.

## Step 5 — deploy where the user chose

Follow the matching runbook at
<https://docs.typewithcaret.com/connect/> — each covers a local Mac and
a VPS/hosted box separately, including keeping the backend alive
(launchd / systemd) and the TLS layer:

- **Tailnet (default):** `tailscale serve --bg 8787`, then the backend's
  URL is `https://<machine>.<tailnet>.ts.net` and the phone joins the
  tailnet.
- **VPS, public HTTPS:** a reverse proxy (Caddy in the runbooks)
  terminates TLS on a dedicated subdomain and forwards to
  `127.0.0.1:8787`; the bearer key and proxy are the entire perimeter.

## Step 6 — hand over a connection QR

Only after step 4 passes over the final `https://` URL. Caret configures
itself from a `caret-connect:v1` code — base URL and key in one scan,
instead of typing a 43-character key into a phone:

```sh
python3 -m caret_backend --qr --url https://<the final base URL>
```

It renders the symbol in the terminal and prints a masked fingerprint —
never the payload. `--save ~/caret-connect.png` writes an owner-only PNG
instead; `--no-display` skips the terminal render. The command refuses
while any readiness blocker stands, which is the point: never hand a
phone a code for a backend that cannot take dictation.

**The QR is the credential, not a pointer to one.** It carries the API
key. So:

- Generate it only when the user asks for it; never as a side effect.
- Never log it, never paste the payload into a transcript or a chat
  message, never print it in full. The masked fingerprint is what you
  quote.
- Tell the user plainly: anyone who scans that code — or photographs it,
  or finds it in a screen recording — can use their backend until they
  rotate `CARET_API_KEYS`. Rotating the key kills every QR made from it.
- Show it to one phone, then delete any saved file.

The wire format, if you need to build it yourself: minified JSON with
sorted keys `{"k":<api key>,"t":"agent","u":<base URL>}`, UTF-8,
base64url-encoded without padding, prefixed `caret-connect:v1:`. Full
spec, worked example, and a safe shell one-liner:
<https://docs.typewithcaret.com/connect/#qr>.

Also give the user the base URL and the key as strings, in a secure
channel, as the manual fallback.

## Definition of done

Verify every line by running a command, and report honestly anything
that is not true:

- [ ] `--check` exits 0; the backend starts and stays up under launchd/systemd.
- [ ] `GET /v2/health` over the final `https://` URL reports `ok`,
      `readiness.ready: true` with no blockers, truthful capabilities and
      routes, and `auth.valid: true` with the key.
- [ ] `capabilities.dictate` is `true` and a **live dictation**
      round-trips over the final URL. This one is not negotiable.
- [ ] If an agent was configured, a live draft round-trips through the
      user's actual runtime over the final URL. If none was, health says
      `"ask": false` and the user knows why.
- [ ] A wrong or missing key gets `401`; no key appears in any tracked
      file, log line, or committed script.
- [ ] The user can connect: either they scanned the QR from step 6, or
      they have the two strings — base URL and API key — to paste in.
- [ ] The user knows the QR carries their key, that scanning it grants
      access until the key is rotated, and how to rotate it.
- [ ] The user knows which surfaces are on and why (routes block).

## The advanced path, if asked

A backend the user fully owns — their own language, process model, and
providers — is the full caret/v2 contract: start from
[implement-caret-backend.md](https://docs.typewithcaret.com/agent-prompts/implement-caret-backend.md),
which builds on the [implementation guide](https://docs.typewithcaret.com/your-agent/).
