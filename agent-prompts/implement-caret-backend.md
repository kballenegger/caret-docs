# Implement a Caret backend

You are a coding agent. Your job is to stand up a personal `caret/v1`
backend for your user — the HTTP API that Caret, an agentic iOS keyboard,
calls to draft messages, take dictation and generate images. When it is
running, your user pastes a URL and an API key into the Caret app and the
keyboard runs on their agent, their models, their machine.

You are implementing to a published contract, not inventing one. Three
documents are authoritative, in this order:

| Resource | What it is |
| --- | --- |
| [`/openapi.yaml`](https://docs.typewithcaret.com/openapi.yaml) | The normative contract. Where anything disagrees with it, it wins. |
| [The implementation guide](https://docs.typewithcaret.com/your-agent/) | Every request and response shape, in the order you would build them. |
| `reference-backend/` | A complete backend in stdlib Python, ~1000 lines, plus `conformance.py` — the checker that decides whether you are done. |

Read the implementation guide before you write anything. It is one page.

## Step 1 — ask your user four questions

Do this **first**, before any code. The contract is fixed; the models
behind it are entirely your user's choice, and you cannot pick well for
them by guessing. Ask all four at once:

1. **Speech to text.** Local and on-device (a `whisper.cpp`-style CLI), or
   a hosted transcription API? Local costs nothing per minute and keeps
   audio on the machine; hosted is usually more accurate and needs a key.
2. **The cleanup model.** Which LLM should polish raw transcripts into
   written text and draft messages? This is the model the user's keyboard
   will sound like.
3. **The agent harness.** Which agent do they already run — Hermes, or any
   other CLI agent that takes a prompt and prints a reply on stdout?
4. **Images.** Which image model for Imagine, if any? Imagine is optional
   and staying off is a legitimate answer.

Then **proceed**. If they have no preference, say which default you are
taking and get on with it — a backend running with defaults today is worth
more than a decision meeting. The defaults:

| Question | Default when the user has no preference |
| --- | --- |
| Harness | Hermes, if `hermes` is on `PATH` — the reference backend auto-detects it. Otherwise any CLI agent they already have installed. Otherwise the built-in `echo` agent, which is a dry run that proves the plumbing without spending a token. |
| Speech to text | A local `whisper.cpp`-style CLI. No key, no per-minute cost, audio never leaves the machine. |
| Cleanup model | The same harness you chose for drafting. One integration, one thing to configure, one thing to debug. |
| Images | None. Imagine stays off and `capabilities.imagine` reports `false`. |

Note the last one carefully: capabilities must be the honest answer, never
the hopeful one. A surface the backend advertises and cannot serve fails
in the user's hands, on their phone, mid-sentence.

## Step 2 — start from the reference backend

Do not start from an empty file. `reference-backend/` already implements
the parts of this contract that are easy to get subtly wrong — chunk
idempotency, checksum rejection, the one-terminal-result-per-session rule,
a stable `request_id` across polls, the error envelope, fail-closed auth.
That is contract plumbing. It is done. Leave it alone.

What you adapt is the three provider seams in
`caret_backend/adapters.py`, and usually you adapt them by setting an
environment variable rather than editing code:

| Seam | Variable | Shape |
| --- | --- | --- |
| Agent | `CARET_AGENT_COMMAND` | text in → text out. Used by Ask and by transcript polish. |
| Transcriber | `CARET_STT_COMMAND` | audio in → text out. Enables dictation and spoken Ask/Imagine. |
| Image generator | `CARET_IMAGE_COMMAND` | text in → PNG out. Enables Imagine. |

Each is an ordinary command line. `{prompt}` is substituted with the
prompt — and if the template contains no `{prompt}`, the prompt goes to
the process's stdin instead. `{audio}` is a WAV path the backend writes
for you; `{out}` is a PNG path the image command must write.

```sh
export CARET_AGENT_COMMAND='hermes chat -q "{prompt}" -Q'
export CARET_STT_COMMAND='whisper-cli -m /path/to/model.bin -f {audio} --no-timestamps'
export CARET_IMAGE_COMMAND='my-image-tool --prompt {prompt} --out {out} --size {aspect_ratio}'
```

If a command line genuinely cannot express the integration — an HTTP API
that needs signed requests, a streaming protocol, a model you must hold in
memory between calls — then replace the adapter class in `adapters.py`.
The three shapes above are the whole interface; anything satisfying them
drops in.

Two escape hatches are open to you, and you should use them when they fit:

- **Write a wrapper script.** Five lines that call the provider and print
  the reply on stdout turns any API into a valid `CARET_AGENT_COMMAND`.
  This is usually the shortest correct path for a hosted model.
- **Rewrite the backend entirely.** This is a personal backend and it is
  meant to be customised — a different language, a different process
  model, a database instead of files, all fine. If you go that way,
  `openapi.yaml` is the specification you are implementing and
  `conformance.py` is the judge. Nothing else about the reference backend
  is binding.

## Step 3 — requirements on the finished work

These are not optional, whichever path you took.

**Secrets.** API keys and model provider keys come from environment
variables or the user's secret store. Never from a file in the
repository, never from a literal in code, never from a shell history line
you leave behind in a committed script. Add `.env` and any local env files
to `.gitignore` before you create them, not after. Generate the Caret API
key with a CSPRNG — `python3 -c 'import secrets;print(secrets.token_urlsafe(32))'`
is fine — and issue one key per device so revoking one is a matter of
removing a string.

**TLS.** Caret refuses plain HTTP. The backend process itself can and
should stay bound to loopback; put a reverse proxy or a private tunnel in
front of it (Caddy, nginx, a Tailscale or Cloudflare tunnel) and let that
terminate TLS. Confirm with the user which they want before you configure
one.

**Auth fails closed.** No keys configured means every authenticated
request is `401` and health reports `degraded` — never "open to
everyone". Compare keys in constant time. Never log a key; log the
`request_id`, which appears in every response and is what a user report
will give you.

**Tests that spend nothing.** The test suite must not call a live model
API — tests that cost money and need network are tests nobody runs. Keep
the hermetic suite, extend it to cover whatever you changed, and run it
from the repository root:

```sh
make test
```

**Conformance.** Run the checker against the finished backend, over the
real URL the user will paste into Caret:

```sh
python3 reference-backend/conformance.py --base-url https://your-host --api-key "$CARET_API_KEY"
```

It knows nothing about any particular implementation, skips what your
capabilities say you do not implement, and exits `0` when the keyboard
will be happy. Exit `0` is the definition of working. Anything else is
not an opinion to argue with.

**A README for your user's deployment.** Short, theirs, not a copy of
this repository's. It must cover: how to start and stop the backend, what
each environment variable does in *their* configuration, and how to
rotate the API key — generate a new one, add it, update the phone, remove
the old one.

## Definition of done

Check yourself against this list before you tell the user you are
finished. Every line is something you can verify by running a command.

- [ ] `GET /v1/health` reports `capabilities` that are true right now —
      every surface reported `true` actually works, and every one that
      does not is reported `false`.
- [ ] `conformance.py` exits `0` against the deployed URL over `https://`.
- [ ] `make test` passes, and no test in it calls a live model API.
- [ ] `git status` is clean of secrets, and `.gitignore` covers env files.
      No key appears in any tracked file or commit.
- [ ] The backend is reachable over TLS and answers `401` to a request
      with no key or a wrong key.
- [ ] The user has the two strings they need — the base URL and their API
      key — and knows to paste them into the Caret app.
- [ ] A deployment README exists covering run, configuration and key
      rotation.

If something on that list is not true, say which one and why, rather than
reporting success. A backend that half works is discovered by its owner
mid-sentence, on a phone, with a keyboard that will not answer.
