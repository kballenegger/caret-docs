# caret/v4 reference implementations

Two runnable backends and two conformance checkers for the
[`caret/v4` protocol](https://docs.typewithcaret.com/protocol/): one in
Go, one in Python. Both are complete, both are dependency-free, and both
speak the same wire.

**Go is the recommended default.** It is the one to read first, the one
to deploy, and the one the docs lead with. Python is the supported
alternative for operators who would rather run Python, and it is the
second opinion that keeps the Go implementation honest: each language's
checker is run against the other language's server, so agreement is
demonstrated rather than asserted.

```
reference/
  go/                        recommended; go build ./... on a fresh checkout
    protocol.go              error table, close codes, audio and vocabulary rules
    ws.go                    RFC 6455, standard library only
    server.go                /health, routing, credentials, the result cache
    session.go               the one lifecycle state machine, three finalizers
    lanes.go                 recognizer, agent, image and cleanup providers
    cleanup.go               caret-cleanup/1, consumed from spec/cleanup/v1/
    conformance.go           the checker
    cmd/caret-v4-backend/    the server binary
    cmd/caret-v4-conform/    the checker binary
  python/                    same protocol, same checks, standard library only
    caret_v4/                protocol.py ws.py server.py session.py lanes.py
                             cleanup.py conformance.py __main__.py
    tests/                   contract tests over a real socket
```

Every rule the protocol states is implemented once, in the file its name
suggests. Neither implementation imports the other, and neither imports
anything that is not shipped with its language.

## Go, in one command

```
cd reference/go
go run ./cmd/caret-v4-backend -addr 127.0.0.1:8080 -keys dev-key
```

That is a conforming backend. With no lane flags it serves `/dictate`
with the built-in loopback providers: no model, no network, no API keys
of anyone's. Point a client at it, run the checker against it, develop
against it on a plane.

In another terminal:

```
cd reference/go
go run ./cmd/caret-v4-conform -url http://127.0.0.1:8080 -key dev-key -insecure
```

Exit 0 means a V4 client will be happy. Build standalone binaries with
`go build -o caret-v4-backend ./cmd/caret-v4-backend` and the same for
`./cmd/caret-v4-conform`.

## Python, in one command

```
cd reference/python
python3 -m caret_v4 serve   --addr 127.0.0.1:8080 --keys dev-key
python3 -m caret_v4 conform --url http://127.0.0.1:8080 --key dev-key --insecure
```

Python 3.11 or newer. No virtualenv, no `pip install`, no
`requirements.txt`, because there is nothing to install.

## Wiring real providers

A lane is a string. The same grammar works for every provider, in both
languages, on the flag or in the environment variable:

| Lane | Flag (Go / Python) | Environment | Off by default |
| --- | --- | --- | --- |
| speech | `-stt` / `--stt` | `CARET_STT` | no, defaults to `loopback` |
| agent, for `/ask` | `-agent` / `--agent` | `CARET_AGENT` | yes |
| image, for `/imagine` | `-image` / `--image` | `CARET_IMAGE` | yes |
| cleanup | `-cleanup` / `--cleanup` | `CARET_CLEANUP` | yes |

| Spec | Meaning |
| --- | --- |
| `none`, `off`, empty | the lane is off, and `/health` says so |
| `loopback` | the built-in deterministic provider; not a model |
| `command:<argv>` | run a program; text in, text out |
| `https://host/path` | POST JSON, read JSON back |

A `command:` speech lane receives a WAV file. Use `{audio}` where the
path goes, or read `CARET_AUDIO_PATH`; the client's vocabulary arrives as
`CARET_VOCABULARY`, newline separated, and the language hint as
`CARET_LANGUAGE_HINT`. A command lane for `/ask`, `/imagine` or cleanup
reads its prompt on stdin and writes its answer on stdout, image bytes
included.

```
# a local Whisper-class recognizer and an agent CLI
go run ./cmd/caret-v4-backend \
  -keys dev-key \
  -stt 'command:whisper-cli --model base.en --file {audio} --output-txt -' \
  -agent 'command:my-agent --prompt-stdin' \
  -cleanup 'command:my-agent --prompt-stdin'
```

Capabilities follow the wiring. A route with no provider is advertised
`false` on `/health` and answers `not_supported`, because the protocol
forbids advertising what the backend cannot do. `/dictate` is mandatory,
so a backend with no speech lane reports `status: not_ready` with a
`no_stt` blocker rather than pretending.

Credentials are required. With no `-keys` the backend reports
`not_ready` with a `no_credentials` blocker and refuses every operation.
Keys are hashed at startup and compared in constant time, and the
credential never appears on `/health` or in a log line.

## TLS

Both take `-tls-cert`/`-tls-key` (`--tls-cert`/`--tls-key`) and serve
HTTPS directly, or leave them off and terminate TLS in front. A
conforming client requires `https`/`wss`, so plain HTTP is for loopback
development only, and both backends say so at startup. The checkers
refuse an `http://` base URL unless you pass `-insecure`, and
`-insecure-skip-verify` exists for a self-signed certificate on your own
machine.

## The conformance checker

The checker treats the backend under test as a black box at a base URL:
any language, any host, a production backend or a first attempt written
this afternoon. It exercises what is easy to get subtly wrong.

* `/health` shape, protocol name, honest capabilities, auth reporting
* a bogus credential refused, with close code 4401
* the dictation happy path: result type, text, `raw_transcript`,
  `stt_route`, `request_id`, close code 1000
* partials that are cumulative and never regress
* exactly one terminal event, and nothing after it
* finalize totals that disagree with what arrived, giving
  `audio_incomplete` with `retryable: true`
* replay under a repeated `client_request_id`
* unknown JSON fields ignored, so future additive changes survive
* the error table with its close codes: `protocol_error`,
  `bad_request`, `audio_too_short`, `no_speech_detected`,
  `not_supported`
* `/imagine` results whose `byte_length` and `sha256` match the bytes
  actually delivered

Exit codes: 0 all required checks passed, 1 something failed, 2 the
checker could not run. Warnings never fail a run: digital silence
producing a word rather than `no_speech_detected` is a recognizer's
business, not a protocol violation. Add `-json` (`--json`) for machine
output.

Point it at any V4 backend, not just these:

```
go run ./cmd/caret-v4-conform -url https://backend.example.com -key "$CARET_API_KEY"
```

## Tests

```
make reference-test          both suites
cd reference/go     && go test ./...
cd reference/python && python3 -m unittest discover -s tests
```

Nothing is mocked at the transport. Every test starts a backend on an
ephemeral loopback port and talks to it over a real socket, because the
parts of a protocol that break are the parts between two processes. Both
suites run the conformance checker against their own server, in three
configurations, and both assert that composing `caret-cleanup/1` from
`spec/cleanup/v1/` reproduces `composed.txt` byte for byte and digests to
the value in `manifest.json`. Both currently report
`caret-cleanup/1 20ab781577580c29`, from two codebases that share no
code.

## Limitations, honestly

* **The loopback providers are not models.** Loopback speech counts
  voiced 20 ms windows and spells the client's vocabulary followed by
  filler; loopback cleanup capitalizes a sentence and adds a full stop;
  loopback imagine draws a gradient seeded by the prompt. They make the
  lifecycle, vocabulary routing, silence handling and image hashing
  exercisable with no model and no network. They are not a transcription
  service, and they never pretend to be one on `/health`.
* **Text input reports no `stt_route`.** Nothing was recognized, so
  there is no route to report, and inventing `"stream"` would be a lie
  told to a client that may be logging it. `raw_transcript` carries the
  text as it arrived, before cleanup.
* **Partials come from the recognizer.** A buffered `command:` or HTTP
  speech lane has nothing to stream, so `partials` is advertised `false`
  for that configuration and the result reports
  `stt_route: "fallback"`. This is the protocol working, not a gap.
* **In-memory state only.** The result cache and the audio buffer live
  in the process. Two instances behind a load balancer will not
  deduplicate each other's `client_request_id`, so pin a client to an
  instance or put a shared cache behind the interface.
* **No rate limiting.** `rate_limited` is implemented in the error
  table, and nothing in these backends emits it. Enforce quota in front.
* **Single process, no supervision.** No clustering, no graceful
  draining beyond a five second shutdown, no metrics endpoint.

## License

Source-available, Caret only. See [`../LICENSE`](../LICENSE): you may
run, modify and extend this code to operate a backend that serves Caret.
It is not open source.
