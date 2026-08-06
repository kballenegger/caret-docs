# Caret — bring your own backend

Caret is an iOS keyboard. Everything it does — writing a message, taking
dictation, generating an image — it does by calling an HTTP API. This
repository is everything you need to make that API yours.

Point Caret at a URL you control and your keyboard runs on your agent, your
model, your machine. No account, no relay, no third party in the middle of
your typing.

```
openapi.yaml        the caret/v1 contract — the normative document
docs/               the implementation guide, as a static site
reference-backend/  a complete backend in stdlib Python, ~1000 lines
```

## Start here

**Just want it working?** Run the reference backend, paste the URL and key
into Caret, done:

```sh
cd reference-backend
export CARET_API_KEYS="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
python3 -m caret_backend --port 8787
```

See [`reference-backend/README.md`](reference-backend/README.md) for TLS,
agent configuration, and speech-to-text.

**Writing your own?** Read [`docs/your-agent/`](docs/your-agent/) — one
page, every request and response shape, in the order you would implement
them. Then check yourself against the contract:

```sh
python3 reference-backend/conformance.py --base-url https://your-host --api-key KEY
```

The checker is standard-library Python and knows nothing about the
reference backend, so it works against an implementation in any language.

**Checking a change to this repository?** One command, no dependencies:

```sh
make test
```

That runs the reference backend's 55 hermetic tests, the last of which
runs `conformance.py` against a live instance of the server.

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

Ask and Imagine take **typed text or spoken audio through the same
field**. Audio is not a second protocol bolted on beside the first: a
spoken Ask opens an ordinary dictation session, uploads ordinary chunks,
and then hands the session id to `/v1/draft` instead of to the transcript
endpoint. If you implement dictation, you have already implemented most of
spoken Ask.

`capabilities` in your health response decides what the keyboard shows. A
text-only backend is a legitimate, complete backend; declare
`{"draft": true, "dictation": false, "imagine": false}` and Caret hides the
rest.

## Licence

MIT. See [LICENSE](LICENSE).
