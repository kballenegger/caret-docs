# Caret reference backend

THE canonical Caret reference implementation: one complete `caret/v1`
backend in stdlib Python with pluggable adapters. The Ask adapter
connects the agent you already run — Hermes, Claude Code, Codex,
OpenClaw, or any hosted agent over HTTP — and the STT adapter provides
dictation and spoken input, defaulting to local
[OpenWhisper](https://github.com/openai/whisper). One backend, varied
Ask and STT providers, all selected by environment variables.

It exists to be read as much as run: every rule in the contract is
implemented here once, in an obvious place, so you can copy the behaviour
into a backend of your own in whatever language you prefer.

```
caret_backend/server.py     the contract — routing, auth, validation, async jobs
caret_backend/store.py      state — sessions, audio chunks, jobs, idempotency
caret_backend/adapters.py   the boundary — agent presets, STT presets, images
caret_backend/errors.py     the error envelope
conformance.py              a checker you can run against any caret/v1 backend
tests/                      hermetic tests, no network, no model calls
```

## Requirements

Python 3.10 or newer. That is the whole list. For local dictation,
OpenWhisper (`brew install openai-whisper` or see
[github.com/openai/whisper](https://github.com/openai/whisper)) — the
backend detects it and turns dictation on; without it, dictation reports
honestly off.

## Run it

```sh
export CARET_AGENT=claude-code   # or: hermes | codex | openclaw | custom-http | auto
export CARET_API_KEYS="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
echo "your API key: $CARET_API_KEYS"          # put it in the Caret app
python3 -m caret_backend --check              # validate configuration first
python3 -m caret_backend --port 8787
```

`CARET_AGENT=auto` (the default) picks the first of `hermes`,
`claude-code`, `codex` on your `PATH`, and the built-in echo agent
otherwise; `CARET_STT=auto` (the default) picks OpenWhisper when
`whisper` is on your `PATH`. Check what you got:

```sh
curl -s localhost:8787/v1/health | python3 -m json.tool
```

```json
{
  "status": "ok",
  "contract": "caret/v1",
  "capabilities": {
    "draft": true,
    "dictation": false,
    "imagine": false,
    "input_modes": {"draft": ["text"]}
  }
}
```

`capabilities` is the honest answer, not the hopeful one: dictation is
`false` until you configure a transcriber, and Imagine is `false` until you
configure an image command. The Caret app reads this and hides what you
have not enabled, so an unconfigured surface never fails in the user's
hands.

Then run the conformance checker against yourself:

```sh
python3 conformance.py --base-url http://localhost:8787 --api-key "$CARET_API_KEYS"
```

## Configuration

Everything is an environment variable. Nothing is a config file, and no
key is ever written into this repository.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CARET_API_KEYS` | *(none)* | Comma-separated bearer keys. **Empty means every request is 401** and health reports `degraded`. |
| `CARET_HOST` | `127.0.0.1` | Bind address. Keep it loopback and put a TLS proxy in front. |
| `CARET_PORT` | `8787` | Port. |
| `CARET_DATA_DIR` | `~/.caret-reference/data` | Sessions, audio chunks, job results. |
| `CARET_AGENT` | `auto` | Ask preset: `hermes`, `claude-code`, `codex`, `openclaw`, `custom-http`, `echo`, or `auto` (first installed of hermes/claude-code/codex, else echo). |
| `CARET_AGENT_COMMAND` | *(none)* | Any command line. Overrides the preset; **required** for `openclaw`. `{prompt}` substitutes into argv (stdin when absent); `{out}` names an answer file. |
| `CARET_AGENT_NAME` | `custom` | Label for logs and error messages. |
| `CARET_AGENT_HTTP_URL` | *(none)* | `custom-http` only: the POST endpoint of your hosted agent. |
| `CARET_AGENT_HTTP_BEARER` | *(none)* | `custom-http` only: sent as `Authorization: Bearer …`, never logged. |
| `CARET_AGENT_TIMEOUT_S` | `90` | Per-draft budget. Overrun is `504 draft_timeout`. |
| `CARET_STT` | `auto` | STT preset: `agent` (only if the agent adapter verifiably transcribes — no shipped preset does), `openwhisper`, `http`, `echo` (tests only), `off`, or `auto` (agent if it transcribes, else `http` when `CARET_STT_HTTP_URL` is set, else OpenWhisper when `whisper` is installed, else off). |
| `CARET_STT_COMMAND` | *(none)* | Any speech-to-text command. Overrides the preset. `{audio}` is a WAV path; transcript on stdout, or in `{out_dir}/audio.txt` when the template names `{out_dir}`. |
| `CARET_STT_MODEL` | `turbo` | OpenWhisper model name. |
| `CARET_STT_HTTP_URL` | *(none)* | Hosted STT: one POST of the WAV body → `{"text"}`. |
| `CARET_STT_HTTP_BEARER` | *(none)* | Hosted STT bearer, sent and never logged. |
| `CARET_IMAGE_COMMAND` | *(none)* | Image generation command. Enables Imagine. |
| `CARET_IMAGE` | *(none)* | Set to `fake` for a 1×1 PNG (tests only). |
| `CARET_POLISH` | `on` | `off` disables transcript clean-up, saving one model call per dictation. |
| `CARET_RATE_LIMIT_PER_MINUTE` | `120` | Per key. `0` disables the limit. |
| `CARET_LOG_LEVEL` | `INFO` | Standard Python levels. |

Configuration is validated at startup, and `python3 -m caret_backend
--check` prints the resolved adapters and capabilities without serving. A
command containing a known permission-bypass flag
(`--dangerously-skip-permissions`, `danger-full-access`, …) is refused at
startup: this backend never widens the permissions of the agent it
fronts.

## Capability routing

Every surface routes through the selected agent when — and only when —
its adapter verifiably provides that capability, and health's `routes`
extension block reports the resolved route per surface, truthfully:

| Surface | Route |
| --- | --- |
| Ask | Always the agent — that is what an agent adapter is. |
| Cleanup | Always the agent, as a constrained text-only cleanup request (`POLISH_FRAMING`: fix the transcript, do not answer it, take no action). The CLI presets run in their read-only modes, so "no action tools" is enforced where the runtime can enforce it. |
| Dictation / STT | Through the agent **only if its adapter implements `transcribe()`**. None of the shipped presets does — no documented, stable non-interactive audio-transcription interface could be verified for Hermes, OpenClaw, Claude Code, Codex, or the custom-http contract (text-JSON by definition) — so STT falls back to local OpenWhisper by default, or the `http`/command adapter you configure. |
| Imagine | Through the agent **only if its adapter implements `generate()`**. Same finding — none of the shipped presets has a verified image-output interface — so Imagine uses `CARET_IMAGE_COMMAND` if you configure one, and reports honestly off otherwise. |

If you integrate an agent that genuinely transcribes audio or renders
images, give its adapter a `transcribe(pcm, *, sample_rate)` /
`generate(prompt, *, aspect_ratio, quality)` method and the router
prefers it automatically. Check the resolved routes any time:

```sh
curl -s localhost:8787/v1/health | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["routes"], indent=2))'
```

## The adapter boundary

`adapters.py` is the only file you should need to touch, and usually you do
not touch it at all — you set an environment variable. The contract needs
exactly three capabilities:

| Capability | Shape | Used by |
| --- | --- | --- |
| Agent | text in → text out | Ask, and transcript polish |
| Transcriber | audio in → text out | Dictate, spoken Ask, spoken Imagine |
| Image generator | text in → PNG out | Imagine (optional) |

Each is a command line. `{prompt}` is substituted with the prompt; if the
template has no `{prompt}`, the prompt is written to the process's stdin.
Whatever the command prints on stdout is the answer. A non-zero exit, a
missing binary, a timeout or an empty answer all become a contract-shaped
error with the exit status or the stderr tail in the message — this backend
will not hand your keyboard a silently empty draft.

### Hermes

```sh
CARET_AGENT=hermes python3 -m caret_backend
```

This runs one command per request:

```sh
hermes chat -q "<prompt>" -Q
```

Stock, documented public CLI syntax and nothing else. `-q/--query` is
single-query non-interactive mode; `-Q/--quiet` is quiet mode for
programmatic use, which suppresses the banner, the spinner and tool
previews. The result is that stdout carries the final response text and
nothing else — Hermes prints its session id on stderr, which this backend
ignores. No Hermes source is patched and no Hermes config file is read or
written by this backend.

To pin a model or provider, extend the command line rather than the code
(`-m/--model` and `--provider` are documented `hermes chat` options):

```sh
export CARET_AGENT_COMMAND='hermes chat -q "{prompt}" -Q -m gpt-5.2 --provider openai'
```

**This backend does not sandbox your agent.** Be clear-eyed about what the
preset does and does not give you:

- The drafting call runs with whatever toolsets your own Hermes
  configuration enables. Ask can therefore reach a browser, a shell or your
  files if you have those on, even though the prompt says not to act.
- The read-only instruction in `DRAFT_FRAMING` ("this is a draft the user
  has not sent or inserted yet") is a prompt, not an enforcement boundary.
  A model can ignore it.
- The command runs with no terminal attached, so anything that would stop
  to ask you a question cannot be answered. The 90-second agent timeout is
  the backstop, and you get a `504 draft_timeout` rather than a hang.

If you want the drafting call narrowed, do it with the documented
`-t/--toolsets` flag, which restricts the run to the toolsets you name:

```sh
# a drafting call that can reach nothing but the one toolset you allow
export CARET_AGENT_COMMAND='hermes chat -q "{prompt}" -Q -t clarify'
```

Pick the toolset list yourself — `hermes tools list` shows what your
install has. This repository deliberately ships the preset *without*
`-t` so it does not silently override a configuration you chose.

### Claude Code

```sh
CARET_AGENT=claude-code python3 -m caret_backend
```

Runs `claude -p --permission-mode plan --no-session-persistence
"<prompt>"` per request — the CLI's documented non-interactive print mode
in its read-only plan mode (no edits, no mutating tools), with nothing
written to disk. **Tested**: live-verified end to end on a real install.

### Codex

```sh
CARET_AGENT=codex python3 -m caret_backend
```

Runs `codex exec --sandbox read-only --skip-git-repo-check --ephemeral
--output-last-message <file> "<prompt>"` per request — the documented
non-interactive mode in a read-only sandbox, reading the answer from the
`--output-last-message` file because codex's stdout carries the event
log. **Tested**: live-verified end to end on a real install.

### A hosted agent (`custom-http`)

```sh
export CARET_AGENT=custom-http
export CARET_AGENT_HTTP_URL='https://your-agent.example/draft'
export CARET_AGENT_HTTP_BEARER='…'        # optional, never logged
```

One `POST {"prompt": "…"}` per call; the response is
`{"text": "…"}` with HTTP 200. Anything else — non-200, unreachable,
non-JSON, missing `text` — is a contract-shaped 503. **Backend-supported**:
the backend's side of this contract is fully covered by the hermetic
suite against a stub upstream; your upstream is yours to verify.

### Any other agent

Any CLI that takes a prompt and prints a reply works:

```sh
# a local model
export CARET_AGENT_COMMAND='ollama run llama3.2'          # no {prompt} → stdin

# a shell wrapper you write yourself
export CARET_AGENT_COMMAND='/usr/local/bin/my-agent --quiet {prompt}'
```

**OpenClaw.** OpenClaw is not installed on the machine this repository was
developed and tested on, so there is no verified command line to publish
for it here and this project does not claim one. The extension point is the
same as for every other agent: a command that reads a prompt and prints a
reply on stdout. If OpenClaw's CLI can do that, set `CARET_AGENT_COMMAND`
to it and run `conformance.py` — that check is the definition of "works",
and it costs nothing to run. If its CLI cannot, write a five-line wrapper
script that calls it and prints the reply, and point `CARET_AGENT_COMMAND`
at the wrapper. Please open an issue with a verified command line and it
will be documented as tested.

### Speech to text

STT is capability-routed (see above): it goes through the agent only
when the agent adapter verifiably transcribes — which no shipped preset
does — and otherwise through its own adapter lane. Speech is transcribed
first, and only then does text reach the agent (for spoken Ask, via the
same polish-then-draft path as dictation). Dictation stays off until a
transcriber resolves, because a backend that accepts audio it cannot
read is worse than one that says so.

**OpenWhisper (the default).** With
[OpenWhisper](https://github.com/openai/whisper) installed (the
open-source Whisper speech-recognition CLI, `brew install
openai-whisper`), `CARET_STT=auto` finds it and dictation turns on. Per
transcription the backend runs the tool's stock documented syntax:

```sh
whisper <audio.wav> --model turbo --output_format txt --output_dir <tmp>
```

and reads the transcript file it writes. `CARET_STT_MODEL` picks the
model (`turbo` by default; `tiny`/`base`/`small`/… trade accuracy for
speed). To be precise about verification: the hermetic suite asserts this
command construction and executes the file-reading mechanics against a
stub, but does not run the real tool — validate your install with
`python3 -m caret_backend --check` and one real dictation from the
keyboard.

**A hosted STT service.** `CARET_STT_HTTP_URL` posts each WAV and expects
`{"text": "…"}` back — same narrow-contract shape as `custom-http` Ask.

**Any other STT tool.** `CARET_STT_COMMAND` takes a command line with
`{audio}` (a WAV file path — this backend converts the contract's raw
PCM16 into a container for you). The transcript is stdout, or
`{out_dir}/audio.txt` if the template names `{out_dir}`:

```sh
export CARET_STT_COMMAND='whisper-cli -m /path/to/model.bin -f {audio} --no-timestamps'
```

### Images

```sh
export CARET_IMAGE_COMMAND='my-image-tool --prompt {prompt} --out {out} --size {aspect_ratio}'
```

`{out}` is a PNG path the command must write. `{aspect_ratio}` is
`square`, `landscape` or `portrait`; `{quality}` is `fast`, `standard` or
`best`. If the file is missing or empty the request fails loudly.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Or, from the repository root, `make test` — the same suite, quieter, and
the entrypoint any CI should call.

83 tests, no network, no model calls, half a minute. They start a real
server on a real socket and speak real HTTP to it, so routing, parsing and
serialisation are all covered — only the agent itself is stubbed. The final
test runs `conformance.py` against that server, so the checker you ship to
users is the checker CI runs.

## Running it for real

**TLS.** This process speaks plain HTTP and binds to loopback. Put a
reverse proxy in front of it (Caddy, nginx, a Tailscale/Cloudflare tunnel)
and let that terminate TLS. Caret requires `https://`.

**Keys.** Generate them with `secrets.token_urlsafe(32)` or equivalent, one
per device, and revoke by removing one from `CARET_API_KEYS` and
restarting. Comparison is constant-time. Keys are never logged.

**Audio retention.** Chunks are deleted as soon as a session produces a
terminal result, and a janitor removes whole sessions one hour past
expiry. Nothing keeps recordings around.

**One process.** State is coordinated by an in-process lock, so run one
process against one `CARET_DATA_DIR`. That is enough for a personal
backend by a wide margin — the work is model latency, and threads handle it.
If you genuinely need several processes, replace the lock in `store.py`
with a file lock (`fcntl.flock`) or move the store to a database; nothing
above it changes.

**Restarts.** All state is on disk, so a restart mid-dictation is
survivable: the client re-POSTs and finds its session and its job.

**Logs.** One line per request with the method, path, status and
`request_id`, on stderr. `request_id` also appears in every response, so a
user report gives you a grep target.

## What is not here

No database, no queue, no auth server, no metrics exporter, no Docker
machinery. A backend for one person and one phone does not need them, and
leaving them out is what keeps this readable. If you need them, the seams
are obvious: `store.py` is the state boundary and `adapters.py` is the
model boundary.
