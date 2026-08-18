# Connect Caret to the agent your user already runs

You are a coding agent. Your job is to connect Caret — an agentic iOS
keyboard — to the agent runtime your user already runs, by deploying the
**Caret reference backend**: the canonical public reference
implementation, stdlib Python you download, configure, validate, and put
behind TLS. When you are done, your user pastes a URL and an API key into
the Caret app and their keyboard drafts through their own agent — and,
with local OpenWhisper installed, takes dictation too.

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

- **Ask** — your user's agent, always.
- **Cleanup** — the same agent, as a constrained text-only cleanup
  request (no actions).
- **Dictation/STT** — NOT the agent: no supported runtime has a verified
  transcription interface. Speech is transcribed by the backend's own
  STT adapter — local [OpenWhisper](https://github.com/openai/whisper)
  by default when `whisper` is on PATH — and only then does text reach
  the agent. Without any STT, dictation reports honestly off and the
  keyboard hides the microphone.
- **Imagine** — only a provider the user configures
  (`CARET_IMAGE_COMMAND`); off otherwise.

Do not promise voice or images through an agent runtime. Health's
`routes` block is the ground truth.

## Step 1 — ask your user two questions

Ask both at once, then proceed.

1. **Which agent runtime do they run?** The `CARET_AGENT` switch:
   `claude-code` (tested), `codex` (tested), `hermes`
   (backend-supported), `custom-http` for a hosted agent behind one JSON
   POST (backend-supported), `openclaw` (not yet verified — requires an
   explicit command line). Repeat these labels honestly; do not promise
   more than the matrix does.
2. **Where should it run, and how will the phone reach it?** The backend
   binds loopback and Caret requires `https://`, so the real choice is
   the TLS layer: a tailnet (`tailscale serve`) on a local Mac or
   private box — the recommended default — or a reverse proxy with a
   public certificate on a VPS. Port-forwarding a home router or
   binding `0.0.0.0` bare is not an option you offer.

Also check for `whisper` on PATH and tell the user whether dictation
will be on (local OpenWhisper — audio never leaves the machine) or off;
installing it (`brew install openai-whisper`) is their call, not a
requirement.

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
export CARET_API_KEYS="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
```

Rules that are not optional:

- Generate the key with a CSPRNG as above; one key per device. The key
  lives in the environment — never in a committed file, never in code,
  never in a shell history line you leave in a script.
- Keep `CARET_HOST` on `127.0.0.1`; TLS terminates in front.
- `custom-http` needs `CARET_AGENT_HTTP_URL` (and optionally
  `CARET_AGENT_HTTP_BEARER`); `openclaw` refuses to start without an
  explicit `CARET_AGENT_COMMAND` — that refusal is honest, not a bug.
- Never pass a permission-bypass flag in `CARET_AGENT_COMMAND`; the
  backend refuses them at startup by design.
- For Hermes, offer the documented toolset narrowing
  (`CARET_AGENT_COMMAND='hermes chat -q "{prompt}" -Q -t <toolset>'`)
  and let the user choose.

## Step 4 — validate before deploying

```sh
python3 -m caret_backend --check
python3 -m caret_backend --port 8787 &
curl -s http://127.0.0.1:8787/v1/health | python3 -m json.tool
curl -s -X POST http://127.0.0.1:8787/v1/draft \
  -H "Authorization: Bearer $CARET_API_KEYS" \
  -H 'Content-Type: application/json' \
  -d '{"client_request_id":"smoke-1","input":{"type":"text","text":"tell Sam I am running ten minutes late"}}'
```

Health must show `"status": "ok"`, capabilities that match reality, and
a `routes` block naming the chosen agent for Ask/cleanup and the
resolved STT (or off) for dictation; the draft must come back from the
real runtime. A request with a wrong key must get `401`. If the runtime
CLI fails, run the runbook's smoke command by hand as the same user
before touching the backend. If dictation is on, verify one real
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

Then have the user paste the base URL and the key into Caret's settings.

## Definition of done

Verify every line by running a command, and report honestly anything
that is not true:

- [ ] `--check` exits 0; the backend starts and stays up under launchd/systemd.
- [ ] `GET /v1/health` over the final `https://` URL reports `ok`,
      truthful capabilities and routes, and `auth.valid: true` with the key.
- [ ] A live draft round-trips through the user's actual runtime over
      the final URL — and a live dictation, if STT resolved.
- [ ] A wrong or missing key gets `401`; no key appears in any tracked
      file, log line, or committed script.
- [ ] The user has the two strings — base URL and API key — and knows to
      paste them into the Caret app.
- [ ] The user knows which surfaces are on, and why (routes block), and
      where voice comes from if they want it later.

## The advanced path, if asked

A backend the user fully owns — their own language, process model, and
providers — is the full caret/v1 contract: start from
[implement-caret-backend.md](https://docs.typewithcaret.com/agent-prompts/implement-caret-backend.md),
which builds on the [implementation guide](https://docs.typewithcaret.com/your-agent/).
