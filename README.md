# Caret — bring your own backend

Caret is an iOS keyboard. Everything it does — writing a message, taking
dictation, generating an image — it does by calling an HTTP API. This
repository is everything you need to make that API yours.

Point Caret at a URL you control and your keyboard runs on your agent, your
model, your machine. No account, no relay, no third party in the middle of
your typing.

> **License notice:** this repository is **source-available, not open
> source**. You may use, copy, and modify it to configure, run, or extend
> a Caret integration — and for that purpose only. See
> [LICENSE](LICENSE).

```
reference-backend/  THE PRIMARY PATH — one complete caret/v1 backend in
                    stdlib Python with pluggable, capability-routed
                    adapters: your agent answers Ask, local OpenWhisper
                    takes dictation by default, every route swappable
integrations/       native packaging where a runtime supports it —
                    a Claude Code plugin, a Hermes skill — plus the
                    evidence-based plugin decision matrix
docs/               the docs site: connect runbooks + implementation guide
agent-prompts/      prompts your coding agent can follow end to end
openapi.yaml        the caret/v1 contract — the normative document
scripts/            the public-repo guard and repo checks
```

## Start here: connect the agent you already run

Already running Claude Code, Codex, Hermes, or a hosted agent of your own?
Deploy the reference backend, pick your agent, and your keyboard drafts
through it:

```sh
git clone https://github.com/kballenegger/caret-docs.git
cd caret-docs/reference-backend

export CARET_AGENT=claude-code              # hermes | codex | openclaw | custom-http
export CARET_API_KEYS="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
python3 -m caret_backend --check
python3 -m caret_backend --port 8787
```

Put TLS in front (a tailnet or a reverse proxy — Caret requires
`https://`), paste the URL and key into the app, done. The runbooks at
<https://docs.typewithcaret.com/connect/> cover each runtime on a local
Mac and on a VPS/hosted box, with honest compatibility labels.

**The fastest way: let your agent do it.** Point your coding agent at
[`agent-prompts/connect-caret.md`](agent-prompts/connect-caret.md)
(also at <https://docs.typewithcaret.com/agent-prompts/connect-caret.md>).
It clones this repository, configures the backend, validates it, and
deploys it where you want it. Claude Code users can install the packaged
plugin instead (`claude plugin marketplace add kballenegger/caret-docs`),
and Hermes users the packaged skill — see
[`integrations/`](integrations/).

**Capabilities are routed, not assumed.** Your agent answers Ask and
cleans up transcripts (as a constrained text-only request). Speech is a
separate lane: no agent runtime has a verified transcription interface,
so dictation uses local [OpenWhisper](https://github.com/openai/whisper)
by default when installed — audio never leaves the machine — or the
STT adapter you configure (`CARET_STT_HTTP_URL`, `CARET_STT_COMMAND`),
and reports honestly off otherwise. Imagine uses the image provider you
configure, or stays off. `GET /v1/health` reports the resolved route per
surface.

## The advanced path: implement caret/v1 yourself

Want a backend you fully own — a different language, your own process
model? Read [`docs/your-agent/`](docs/your-agent/) — one page, every
request and response shape, in the order you would implement them. Point
your agent at
[`agent-prompts/implement-caret-backend.md`](agent-prompts/implement-caret-backend.md)
to have it built for you. Then check any implementation, in any language,
against the contract:

```sh
python3 reference-backend/conformance.py --base-url https://your-host --api-key KEY
```

## What the contract asks of you

Six endpoints. Only the first two are required.

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/health` | Say who you are and what you can do |
| `POST /v1/draft` | Ask — write or rewrite text |
| `POST /v1/imagine` | Generate an image (optional) |
| `POST /v1/dictation/sessions` | Open an audio session (optional) |
| `PUT /v1/dictation/sessions/{id}/chunks/{seq}` | Upload audio as it is spoken |
| `POST /v1/dictation/sessions/{id}/transcript` | Finish and transcribe |

`capabilities` in your health response decides what the keyboard shows. A
text-only backend is a legitimate, complete backend; declare
`{"draft": true, "dictation": false, "imagine": false}` and Caret hides
the rest.

## Checking a change to this repository

One command, no dependencies:

```sh
make test
```

That runs the public-repo guard (`scripts/public_guard.py` — this
repository must never reference private machines, private repositories,
or credentials), the packaged-integration checks, and the reference
backend's hermetic test suite — all standard library, no network beyond
loopback, no model calls.

## License

**Source-available. NOT open source.** Use, copying, and modification are
permitted solely to configure, run, or extend a Caret-compatible
integration; standalone or competing use, redistribution, and commercial
exploitation outside Caret are not licensed. No warranty; see
[LICENSE](LICENSE) for the exact terms. This is a product license — if
you need rights beyond it, contact Caret via
<https://typewithcaret.com>.
