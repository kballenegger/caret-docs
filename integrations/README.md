# Packaged integrations

The Caret integration is always the same thing underneath: the
[reference backend](../reference-backend/) with its Ask adapter pointed
at your runtime. Where a runtime has a real, documented plugin or
extension architecture, the integration is *also* packaged in that
architecture's native format so installing it is one command. Plugins
here are thin wrappers around the one unified backend — never forks of
it.

## Decision matrix

Verdicts are evidence-based: a plugin format is only used where it was
verified against the runtime's own CLI or published schema. Where no
suitable architecture exists (or none could be verified), the universal
adapter — `CARET_AGENT` configuration of the reference backend — is the
supported path, and that is stated rather than papered over.

| Runtime | Native extension architecture | Verdict | STT / Imagine through this runtime? | Artifact |
| --- | --- | --- | --- | --- |
| Claude Code | Plugins: `.claude-plugin/plugin.json` + skills, installed from marketplaces (`claude plugin marketplace add <repo>`, `claude plugin install <name>@<marketplace>`). Verified against the CLI's `plugin` subcommand and the schema of an installed official marketplace. | **Packaged as a plugin.** | No — no documented non-interactive audio-transcription or image-output interface. STT falls back to local OpenWhisper; Imagine needs `CARET_IMAGE_COMMAND`. | [`claude-code/caret-connect/`](claude-code/caret-connect/) + the repo-root [`.claude-plugin/marketplace.json`](../.claude-plugin/marketplace.json) |
| Hermes | Skills: `hermes skills install <identifier or direct HTTP(S) URL to a SKILL.md>`, documented in `hermes skills install --help`. (Hermes also has `hermes plugins install <git-url>`, but its plugin manifest format is not documented in the CLI help, so nothing is claimed for it.) | **Packaged as an installable skill.** | No verified interface. A Hermes install may have voice/media providers configured, but there is no documented stable CLI contract this backend could call — so nothing is claimed. Same fallbacks. | [`hermes/caret-connect/SKILL.md`](hermes/caret-connect/SKILL.md) |
| Codex | `codex plugin` exists but only installs from configured marketplace snapshots; no third-party authoring/manifest path is documented in the CLI help. | **Universal adapter** (`CARET_AGENT=codex`). No plugin is claimed without a documented authoring path. | No — text-only non-interactive interface. Same fallbacks. | — |
| OpenClaw | Not installed on the development machine; no interface of any kind could be verified. | **Universal adapter** (`CARET_AGENT=openclaw` + explicit `CARET_AGENT_COMMAND`), labelled not-yet-verified. | Unverified — nothing is claimed. Same fallbacks. | — |
| Custom HTTP / hosted (GrokBot path) | A hosted agent has no host runtime to install anything into; the narrow `POST {"prompt"} → {"text"}` contract *is* the integration. | **Universal adapter** (`CARET_AGENT=custom-http`). | No — the contract is text-JSON by definition. Hosted STT has its own lane (`CARET_STT_HTTP_URL`); images need `CARET_IMAGE_COMMAND`. | — |

Capability routing is mechanical, not aspirational: the backend routes
STT/Imagine through an agent adapter only when that adapter implements
`transcribe()`/`generate()` (see
[reference-backend/README.md](../reference-backend/README.md#capability-routing)),
and health's `routes` block reports what actually resolved.

## Installing

**Claude Code**

```sh
claude plugin marketplace add kballenegger/caret-docs
claude plugin install caret-connect@caret-docs
```

Then ask Claude Code to "connect Caret" and the skill walks it through
deploying the reference backend with `CARET_AGENT=claude-code`.

**Hermes**

```sh
hermes skills install https://raw.githubusercontent.com/kballenegger/caret-docs/main/integrations/hermes/caret-connect/SKILL.md
```

Then ask Hermes to "connect Caret" and the skill walks it through
deploying the reference backend with `CARET_AGENT=hermes`.

Both skills are instructions for the agent, not code that runs in the
host: the backend they deploy is always this repository's reference
backend, subject to the same source-available [LICENSE](../LICENSE) and
the same safety rules (read-only presets, no permission bypasses).

## Tests

`scripts/test_integrations.py` (run by `make test`) validates every
packaged artifact hermetically: manifests parse and carry the required
fields, marketplace sources point at directories that exist, and every
skill has well-formed frontmatter. The suite never executes the host
runtimes.
