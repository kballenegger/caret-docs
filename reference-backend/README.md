# Caret reference backend

A complete `caret/v1` backend in about a thousand lines of Python. No
dependencies — the standard library only. Point Caret at it and your own
agent writes your messages.

It exists to be read as much as run: every rule in the contract is
implemented here once, in an obvious place, so you can copy the behaviour
into a backend of your own in whatever language you prefer.

```
caret_backend/server.py     the contract — routing, auth, validation, async jobs
caret_backend/store.py      state — sessions, audio chunks, jobs, idempotency
caret_backend/adapters.py   the boundary — where your agent plugs in
caret_backend/errors.py     the error envelope
conformance.py              a checker you can run against any caret/v1 backend
tests/                      53 hermetic tests, no network, no model calls
```

## Requirements

Python 3.10 or newer. That is the whole list.

## Run it

```sh
export CARET_API_KEYS="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
echo "your API key: $CARET_API_KEYS"          # put it in the Caret app
python3 -m caret_backend --port 8787
```

By default the agent is [Hermes](#hermes) if `hermes` is on your `PATH`,
and the built-in echo agent otherwise. Check what you got:

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
| `CARET_AGENT` | `auto` | `auto` (Hermes if present, else echo), `hermes`, or `echo`. |
| `CARET_AGENT_COMMAND` | *(none)* | Any command line. Overrides `CARET_AGENT`. |
| `CARET_AGENT_NAME` | `custom` | Label for logs and error messages. |
| `CARET_STT_COMMAND` | *(none)* | Speech-to-text command. Enables dictation and spoken Ask/Imagine. |
| `CARET_STT` | *(none)* | Set to `echo` for a fake transcriber (tests only). |
| `CARET_IMAGE_COMMAND` | *(none)* | Image generation command. Enables Imagine. |
| `CARET_IMAGE` | *(none)* | Set to `fake` for a 1×1 PNG (tests only). |
| `CARET_POLISH` | `on` | `off` disables transcript clean-up, saving one model call per dictation. |
| `CARET_RATE_LIMIT_PER_MINUTE` | `120` | Per key. `0` disables the limit. |
| `CARET_LOG_LEVEL` | `INFO` | Standard Python levels. |

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

Fully implemented and tested end to end.

```sh
CARET_AGENT=hermes python3 -m caret_backend
```

This runs `hermes -z "<prompt>" --safe-mode` per request, which is
documented public CLI behaviour: one-shot mode prints only the final
response text on stdout, and `--safe-mode` keeps a drafting call from
picking up tools it has no business using while writing a message. Nothing
is patched and no Hermes config file is read or written by this backend.

To pin a model or provider, extend the command line rather than the code:

```sh
export CARET_AGENT_COMMAND='hermes -z "{prompt}" --safe-mode -m gpt-5.2 --provider openai'
```

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

Dictation stays off until you configure a transcriber, because a backend
that accepts audio it cannot read is worse than one that says so. The
command receives a WAV file path in `{audio}` — this backend converts the
contract's raw PCM16 into a container for you — and prints the transcript:

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

53 tests, no network, no model calls, a few seconds. They start a real
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
