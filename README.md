# Caret — the caret/v4 any-agent protocol

Caret is an iOS keyboard. Beyond on-device recognition, everything it
does it does by talking to a backend, and the app offers three modes:

| Mode | Backend | Documented at |
| --- | --- | --- |
| Offline | On-device speech recognition. Nothing to configure. | nothing to document |
| Caret API | Caret's own hosted dictation, set up inside the app. Not a developer API. | nothing to document |
| Your Agent | A backend you run on the open `caret/v4` protocol: a base URL and a credential, no account, no relay, no third party in the middle of your typing. | [`docs/agent/`](docs/agent/index.html), [`reference/`](reference/README.md) |

The open contract is **`caret/v4`**: one base URL serving three
WebSocket routes — `/dictate`, `/ask`, `/imagine` — over one shared
live-audio lifecycle, with health and capability discovery on
`GET /health`. The protocol has no privileged party: any client and any
backend may implement it, and two conforming implementations
interoperate without ever having met.

This repository is the public home of that protocol.

> **License notice:** this repository is **source-available, not open
> source**. You may use, copy, and modify it to configure, run, or extend
> a Caret integration — and for that purpose only. See
> [LICENSE](LICENSE).

## Start here

**Start with the reference:** [`reference/`](reference/README.md),
published at <https://docs.typewithcaret.com/reference/>. Clone the public
repository, choose Go or Python, run the loopback backend, and run its
conformance checker. Then wire the reference's provider adapters to the
speech, agent, cleanup, and image tools you already use. Go is the
recommended default. Python is the supported alternative. Both implement
the same V4 behavior and use only their language's standard library.

**Use the protocol as a fallback:** [`docs/protocol/`](docs/protocol/index.html),
published at <https://docs.typewithcaret.com/protocol/>. It is the complete,
normative contract for maintainers and for runtimes that cannot run either
reference implementation. Do not start by writing a backend from scratch
when the reference runs on your machine.

The short version of what a backend serves:

| Route | Purpose |
| --- | --- |
| `GET /health` | Say who you are and what you can do (anonymous, HTTPS) |
| `wss /dictate` | Speech becomes polished text. **Required.** |
| `wss /ask` | An instruction becomes one message. Optional. |
| `wss /imagine` | A prompt becomes an image. Optional. |

All three WebSocket routes speak the same lifecycle: authenticate on
upgrade, `start`, stream binary audio (or send typed text), receive
cumulative `partial` transcripts, send the route's `finalize` frame, and
receive exactly one typed terminal result. Reliability is client-side
replay: the client keeps its audio until it holds a result, finalize totals
prove the server heard everything, and `client_request_id` keeps a replay
from becoming a second bill.

## The rest of the active documentation

| Page | What it covers |
| --- | --- |
| [Overview](https://docs.typewithcaret.com/) | The three backend modes and the design principles. |
| [Agent instructions](https://docs.typewithcaret.com/agent/) | One URL to give a coding agent so it clones, configures and verifies the public reference backend for a self-hosted setup: the plain-Markdown source is [`docs/agent/instructions.md`](docs/agent/instructions.md), published at <https://docs.typewithcaret.com/agent/instructions.md>. |
| [Reference implementation](https://docs.typewithcaret.com/reference/) | Two runnable V4 backends and conformance checkers, in [`reference/`](reference/README.md): Go (recommended) and Python, standard library only. |
| [Cleanup (`caret-cleanup/1`)](https://docs.typewithcaret.com/cleanup/) | The published transcript-cleanup wording, versioned independently of the protocol, vendored in [`spec/cleanup/v1/`](spec/cleanup/v1/). |
| [Imagine references](https://docs.typewithcaret.com/imagine-references/) | Generating the same person, pet, or object repeatably — a backend behavior on top of `/imagine`. |

## Checking a change to this repository

One command, no dependencies beyond Python 3:

```sh
make test
```

That runs, in order: the public-repo guard (`scripts/public_guard.py` —
this repository must never reference private machines, private
repositories, or credentials), the docs structure check
(`scripts/docs_check.py` — every page shares one header and sidebar,
nothing outside the current page set is published, every internal link
resolves), the cleanup-spec digest check, the unit tests for those
scripts, the V4 reference implementations' suites (`make reference-test`
on its own; the Go half is skipped with a notice when no Go toolchain is
installed), and the hermetic test suite of the pre-V4 backend kept under
`legacy/` for history. `make site` assembles the deployable static site
into `_site/` and runs both guards against the output.

## License

**Source-available. NOT open source.** Use, copying, and modification are
permitted solely to configure, run, or extend a Caret-compatible
integration; standalone or competing use, redistribution, and commercial
exploitation outside Caret are not licensed. No warranty; see
[LICENSE](LICENSE) for the exact terms. This is a product license — if
you need rights beyond it, contact Caret via
<https://typewithcaret.com>.
