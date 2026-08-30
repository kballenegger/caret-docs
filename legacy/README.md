# Legacy archive — pre-V4 material

**Everything in this directory is archived.** It documents the retired
`caret/v2` REST contract and the `caret/v3` live-dictation WebSocket,
and it is preserved for operators who still run a pre-V4 backend and
for the historical record. Nothing here describes the current product.

The active contract is **`caret/v4`**, documented at
<https://docs.typewithcaret.com/protocol/> and in [`docs/`](../docs/) at
the root of this repository. New clients and new backends must implement
V4 and only V4. Migration notes:
<https://docs.typewithcaret.com/migration/>.

What is archived here:

| Path | What it was |
| --- | --- |
| [`openapi.yaml`](openapi.yaml) | The normative `caret/v2` REST contract (marked archived in its header). |
| [`reference-backend/`](reference-backend/) | The complete `caret/v2` reference backend, stdlib Python, with its hermetic test suite and `conformance.py` checker. Still runs, still tested by `make test`, no longer the supported path. |
| [`agent-prompts/`](agent-prompts/) | Prompts that walked a coding agent through connecting or implementing a `caret/v2` backend. |
| [`integrations/`](integrations/) | The packaged Claude Code plugin and Hermes skill that deployed the `caret/v2` reference backend, plus the plugin decision matrix. |
| [`claude-plugin/`](claude-plugin/) | The repo-root Claude Code marketplace manifest for the plugin above. Moved here so `claude plugin marketplace add` no longer offers a retired integration. |

The archived docs pages (`connect/`, `your-agent/`, `live-dictation/`)
live under [`../docs/legacy/`](../docs/legacy/) and stay published at
`https://docs.typewithcaret.com/legacy/…` with an archive banner; the
old URLs redirect there.

History is intact: every file was moved with `git mv`, so
`git log --follow` works from either path.
