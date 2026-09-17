# Build a self-hosted Caret backend

Instructions for a coding agent. A person has pointed you here because
they want their own backend for the Caret iOS keyboard, running on a
machine they control, speaking the open `caret/v4` protocol. When you are
done, the Caret app on their phone will be set to Your Agent mode with
the base URL and credential of the backend you stood up, and a dictated
sentence will have come back as clean text through it.

Everything you need is public and current:

- The protocol, complete and normative: https://docs.typewithcaret.com/protocol/
- Two reference backends and conformance checkers, Go and Python, standard library only: https://github.com/kballenegger/caret-docs/tree/main/reference
- Reference setup and lane wiring: https://github.com/kballenegger/caret-docs/blob/main/reference/README.md
- The published transcript-cleanup wording: https://docs.typewithcaret.com/cleanup/
- The Markdown you are reading: https://docs.typewithcaret.com/agent/instructions.md

Do not invent protocol. If the protocol page and your memory disagree,
the page wins. If you are unsure how a rule behaves, read how
`reference/go` or `reference/python` implements it; each rule lives in
the file its name suggests (`server.go`, `session.go`, `lanes.go`,
`protocol.go`, and the same names in Python).

## 1. What a backend is

One base URL that serves:

| Route | Purpose | Required |
| --- | --- | --- |
| `GET /health` | Anonymous JSON: `"protocol": "caret/v4"`, status, capabilities, limits, an auth report | yes |
| `wss /dictate` | Speech in, live partial transcripts out, one `"dictation"` result | yes |
| `wss /ask` | An instruction (spoken or typed) becomes one `"message"` result | optional |
| `wss /imagine` | A prompt becomes one `"image"` result, bytes verified by sha256 and length | optional |

All three WebSocket routes share one lifecycle: the client authenticates
on the upgrade with `Authorization: Bearer <credential>`, sends a `start`
frame, streams binary PCM16 audio (16 kHz, mono) or sends typed text,
receives cumulative `partial` frames, sends the route's `finalize` frame,
and receives exactly one typed terminal `result` or `error`. Reliability
is client-side replay keyed by `client_request_id`, so a backend must
cache results briefly and answer a repeated request with the cached
result instead of doing the work twice.

Dictate is mandatory. A backend that cannot take speech reports
`"dictate": false`, `"status": "not_ready"` and a `no_stt` blocker on
`/health`. Ask and Imagine are optional and must be advertised honestly:
the app hides what a backend does not serve.

The app accepts plain `http://` only for hosts it can tell are local or
private (localhost, RFC 1918 ranges, Tailscale addresses and `*.ts.net`
names). Anything else must be `https://`, either served by the backend
or terminated by a reverse proxy or tunnel in front of it.

## 2. Ask before you build

Do not guess at these. Ask the person, then write their answers down at
the top of your plan.

1. **Language and runtime.** Go (recommended, single static binary) or
   Python 3.11+ (no dependencies). Both reference backends are complete;
   pick the one that fits the machine and the person's comfort. Only
   write a backend from scratch in another language if they ask for it,
   and then port from the reference rather than from memory.
2. **Where it runs and how the phone reaches it.** Same LAN, a Tailscale
   or WireGuard network, a VPS with a domain, or a tunnel. This decides
   whether you need TLS in the backend, a reverse proxy, or nothing.
3. **Speech recognition.** The loopback lane is a test fixture, not a
   recognizer (see section 5). They need a real one: a local
   Whisper-class CLI, a self-hosted speech server, or a provider they
   already pay for. Ask which, and whether audio may leave the machine.
4. **Cleanup and agent.** Which language model, if any, should polish
   dictation and answer `/ask`? A local model CLI, a self-hosted server,
   or an API they hold a key for. Whether `/imagine` is wanted at all.
5. **Credentials.** How they want the bearer credential generated and
   where it is stored. A long random string per device is the norm.
6. **Supervision.** Whether the backend should survive reboots (systemd,
   launchd, a container) and where logs go.

## 3. Stand up the reference backend

Clone the public repository and run the reference with loopback lanes
first, so the lifecycle is proven before any real provider is wired.

Go:

```sh
git clone https://github.com/kballenegger/caret-docs
cd caret-docs/reference/go
go run ./cmd/caret-v4-backend -addr 127.0.0.1:8080 -keys dev-key
```

Python:

```sh
git clone https://github.com/kballenegger/caret-docs
cd caret-docs/reference/python
python3 -m caret_v4 serve --addr 127.0.0.1:8080 --keys dev-key
```

In another terminal, run the conformance checker from the same
directory against it:

```sh
# Go
go run ./cmd/caret-v4-conform -url http://127.0.0.1:8080 -key dev-key -insecure

# Python
python3 -m caret_v4 conform --url http://127.0.0.1:8080 --key dev-key --insecure
```

Exit 0 means a V4 client will be happy. `-insecure` exists only because
this is loopback HTTP; never use it against the address the phone will
use. Read the checker's output once in full so you know what it covers:
audio-total verification, exactly one terminal event, cumulative
partials, replay with a repeated `client_request_id`, honest
capabilities, image hashing, cancellation, the error table and close
codes.

Build a standalone binary for deployment with
`go build -o caret-v4-backend ./cmd/caret-v4-backend` (and the same for
`./cmd/caret-v4-conform`). The Python package runs from the checkout.

## 4. Configure it

Every setting is a flag or an environment variable; the README lists
both. The ones that matter:

| Setting | Go flag | Python flag | Environment |
| --- | --- | --- | --- |
| Listen address | `-addr` | `--addr` | `CARET_ADDR` |
| Bearer credentials, comma separated | `-keys` | `--keys` | `CARET_API_KEYS` |
| Speech lane | `-stt` | `--stt` | `CARET_STT` |
| Agent lane for `/ask` | `-agent` | `--agent` | `CARET_AGENT` |
| Image lane for `/imagine` | `-image` | `--image` | `CARET_IMAGE` |
| Cleanup lane | `-cleanup` | `--cleanup` | `CARET_CLEANUP` |
| Service name on `/health` | `-service` | `--service` | `CARET_SERVICE` |
| TLS | `-tls-cert` `-tls-key` | `--tls-cert` `--tls-key` | `CARET_TLS_CERT` `CARET_TLS_KEY` |
| Cleanup spec directory | `-cleanup-spec-dir` | `--cleanup-spec-dir` | `CARET_CLEANUP_SPEC_DIR` |

A lane is a string with one grammar in both languages:

| Lane value | Meaning |
| --- | --- |
| `none`, `off`, empty | The lane is off and `/health` says so |
| `loopback` | The built-in deterministic fixture. Not a model. |
| `command:<argv>` | Run a program; text in, text out |
| `https://host/path` | POST JSON, read JSON back |

A `command:` speech lane receives a WAV file; put `{audio}` where the
path goes or read `CARET_AUDIO_PATH`. The client's vocabulary arrives in
`CARET_VOCABULARY` (newline separated) and the language hint in
`CARET_LANGUAGE_HINT`. Command lanes for `/ask`, `/imagine` and cleanup
read the prompt on stdin and write the answer on stdout, image bytes
included. Example with a local recognizer and an agent CLI:

```sh
go run ./cmd/caret-v4-backend \
  -keys "$CARET_API_KEYS" \
  -stt 'command:whisper-cli --model base.en --file {audio} --output-txt -' \
  -agent 'command:my-agent --prompt-stdin' \
  -cleanup 'command:my-agent --prompt-stdin'
```

Capabilities follow the wiring. A route with no provider is advertised
`false` and answers `not_supported`. A buffered `command:` or HTTP speech
lane cannot stream, so `partials.dictate` is advertised `false` and
results report `stt_route: "fallback"`; that is the protocol working,
not a bug to hide.

Cleanup uses the published `caret-cleanup/1` wording from
`spec/cleanup/v1/`, found automatically when run from the checkout; set
the cleanup spec directory if you move the binary. `/health` reports the spec
digest, never the prompt.

## 5. Loopback is not transcription

The loopback speech lane counts voiced 20 ms windows and spells the
client's vocabulary followed by filler. Loopback cleanup capitalizes a
sentence and adds a full stop. Loopback imagine draws a gradient seeded
by the prompt. They exist so the lifecycle can be exercised with no
model and no network, and `/health` never claims otherwise.

A backend left on loopback will pass the conformance checker and still
be useless to a person dictating. Do not declare the job done on
loopback. Wire the recognizer the person chose in section 2 and prove it
with real audio (section 7).

## 6. Protect secrets

- Generate the bearer credential with a real random source, for example
  `openssl rand -base64 32`. One credential per device.
- Pass it through an environment variable or a file the service reads,
  not on a command line that shows up in `ps` or shell history, and not
  in a file you commit.
- Never print the credential, an API key for a provider, or a transcript
  in logs or in your own replies. The reference hashes credentials at
  startup, compares in constant time, and logs only the operation id;
  keep that property in anything you add.
- Anything the person hands you (provider keys, hostnames on their
  private network) stays on their machine. Do not paste it into an
  issue, a commit, or a chat.
- The backend keeps audio in memory only and caches results for
  minutes. Do not add transcript logging or audio persistence unless the
  person asks for it, and say so on `/health` if you do.

## 7. Verify before you say it works

All of these, in order, on the address the phone will actually use.

1. **Health.** `curl -s https://<host>/health` returns
   `"protocol": "caret/v4"` and `"status": "ok"` with `"dictate": true`.
   With the credential, `curl -s -H "Authorization: Bearer $KEY"
   https://<host>/health` reports `"auth": {"presented": true, "valid": true}`.
   With a wrong credential, `valid` is `false`.
2. **Conformance.** Run the checker against that same base URL with the
   real credential and without the insecure flag. It must exit 0. For a
   self-signed certificate on a private network use
   `-insecure-skip-verify` (Go) or `--insecure-skip-verify` (Python) and
   say so in your report.
3. **Real speech.** With the real recognizer wired, send a real
   recording through `/dictate` and read the text that comes back.
   The simplest way is the app itself: on the phone, choose Your Agent,
   enter the base URL and the credential, save (the app calls
   `/health` on save), and dictate one sentence into any text field.
   The sentence must arrive as clean text, not as vocabulary words and
   filler. If it does not, the recognizer is not wired; go back to
   section 4.
4. **Restart.** If the backend is supervised, restart the machine or the
   service and repeat step 1.

Your final report to the person should state the base URL, which lanes
are real and which are off, the checker's exit code, and the sentence
they dictated and the text they got back. It must never contain the
credential. If any step above failed, say which one and stop there.

## 8. If you are writing a backend from scratch

Only on request. Then:

- Implement against https://docs.typewithcaret.com/protocol/ section by
  section; section 11 is the conformance checklist for both sides.
- Use the reference in `reference/go` or `reference/python` as the
  executable specification: when a rule is ambiguous to you, read how
  the reference handles it and match it.
- Run the reference checker against your backend from the first day.
  It treats the backend as a black box at a base URL, so it works for
  any language.
- Vendor `spec/cleanup/v1/` byte for byte and pin its digest, so cleanup
  wording cannot drift silently.
- Keep the same secret-handling and retention properties as section 6.

## 9. Out of scope

This document covers the open `caret/v4` protocol only. Caret's own
hosted dictation is set up inside the app and is not a developer API;
nothing here applies to it. Older Caret wires are gone; implement only
`caret/v4`.
