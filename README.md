# Caret — the hosted Caret API and the caret/v4 any-agent protocol

Caret is an iOS keyboard. Beyond on-device recognition, everything it
does it does by talking to a backend, and the app offers three modes:

| Mode | Backend | Documented at |
| --- | --- | --- |
| Offline | On-device speech recognition. Nothing to configure. | nothing to document |
| Caret API | The hosted service at `api.typewithcaret.com`. Paste a key, dictate. Dictate only. | [`docs/hosted/`](docs/hosted/index.html), <https://docs.typewithcaret.com/hosted/> |
| Your Agent | A backend you run on the open `caret/v4` protocol: a base URL and a credential, no account, no relay, no third party in the middle of your typing. | [`docs/protocol/`](docs/protocol/index.html), <https://docs.typewithcaret.com/protocol/> |

The open contract is **`caret/v4`**: one base URL serving three
WebSocket routes — `/dictate`, `/ask`, `/imagine` — over one shared
live-audio lifecycle, with health and capability discovery on
`GET /health`. The protocol has no privileged party: any client and any
backend may implement it, and two conforming implementations
interoperate without ever having met.

The hosted service also calls itself `caret/v4` but is a different wire:
it answers health with `"contract": "caret/v4"` (the open protocol says
`"protocol"`), dictates on `wss /v4/dictate` with sequence-numbered Opus
or PCM16 frames, acks and resume, and keeps a per-account dictionary at
`/v2/dictionary`. Auth on both is `Authorization: Bearer <key>`; the old
hosted client-proof scheme is gone. The hosted page lists every
difference.

This repository is the public home of both.

> **License notice:** this repository is **source-available, not open
> source**. You may use, copy, and modify it to configure, run, or extend
> a Caret integration — and for that purpose only. See
> [LICENSE](LICENSE).

## Start here

**Read the protocol:** [`docs/protocol/`](docs/protocol/index.html),
published at <https://docs.typewithcaret.com/protocol/>. It is the
complete, normative contract — the shared lifecycle, the per-route
finalizers and typed terminal results, backend-owned auth, vocabulary
injection, the reliability rules, the error table, and the conformance
checklists for both sides. It is written to be sufficient on its own:
you can implement a backend or a client from that one page.

The short version of what a backend serves:

| Route | Purpose |
| --- | --- |
| `GET /health` | Say who you are and what you can do (anonymous, HTTPS) |
| `wss /dictate` | Speech becomes polished text — live partials, then a `"dictation"` result. **Required.** |
| `wss /ask` | An instruction becomes one message — a `"message"` result. Optional. |
| `wss /imagine` | A prompt becomes an image — an `"image"` result. Optional. |

All three WebSocket routes speak the same lifecycle: authenticate on
upgrade, `start`, stream binary audio (or send typed text), receive
cumulative `partial` transcripts, send the route's `finalize` frame,
receive exactly one typed terminal result. Reliability is client-side
replay: the client keeps its audio until it holds a result, finalize
totals prove the server heard everything, and `client_request_id` keeps
a replay from becoming a second bill.

## The rest of the active documentation

| Page | What it covers |
| --- | --- |
| [Overview](https://docs.typewithcaret.com/) | The three backend modes, how to tell the two wires apart, and the design principles. |
| [Hosted Caret API](https://docs.typewithcaret.com/hosted/) | The `api.typewithcaret.com` contract: Bearer auth, `/health` and `/v4/health`, the `/v4/dictate` lifecycle, errors and close codes, limits, `/v2/dictionary`. |
| [Reference implementation](https://docs.typewithcaret.com/reference/) | Two runnable V4 backends and conformance checkers, in [`reference/`](reference/README.md): Go (recommended) and Python, standard library only. |
| [Migration](https://docs.typewithcaret.com/migration/) | Coming from `caret/v2`/`v3`: what changed, side-by-side serving at one base URL, cutover steps. |
| [Cleanup (`caret-cleanup/1`)](https://docs.typewithcaret.com/cleanup/) | The published transcript-cleanup wording, versioned independently of the protocol, vendored in [`spec/cleanup/v1/`](spec/cleanup/v1/). |
| [Imagine references](https://docs.typewithcaret.com/imagine-references/) | Generating the same person, pet, or object repeatably — a backend behavior on top of `/imagine`. |

## Legacy: everything pre-V4

`caret/v2` (REST) and `caret/v3` (the optional live-dictation WebSocket)
are retired. Everything that documented them — the implementation guide,
the connect runbooks, `openapi.yaml`, the V2 reference backend and its
conformance checker, the packaged Claude Code plugin and Hermes skill,
the agent prompts — is preserved, clearly marked, under
[`legacy/`](legacy/README.md) in this repository and
<https://docs.typewithcaret.com/legacy/> on the docs site. Old URLs
redirect. Nothing in the archive applies to V4 except as history.

## Checking a change to this repository

One command, no dependencies beyond Python 3:

```sh
make test
```

That runs, in order: the public-repo guard (`scripts/public_guard.py` —
this repository must never reference private machines, private
repositories, or credentials), the docs structure check
(`scripts/docs_check.py` — active pages link only to active pages,
archived pages carry their banner, every internal link resolves), the
cleanup-spec digest check, the unit tests for those scripts, the V4
reference implementations' suites (`make reference-test` on its own; the
Go half is skipped with a notice when no Go toolchain is installed), and
the archived V2 reference backend's hermetic test suite (archived means
frozen, not broken). `make site` assembles the deployable static site
into `_site/` and runs both guards against the output.

## License

**Source-available. NOT open source.** Use, copying, and modification are
permitted solely to configure, run, or extend a Caret-compatible
integration; standalone or competing use, redistribution, and commercial
exploitation outside Caret are not licensed. No warranty; see
[LICENSE](LICENSE) for the exact terms. This is a product license — if
you need rights beyond it, contact Caret via
<https://typewithcaret.com>.
