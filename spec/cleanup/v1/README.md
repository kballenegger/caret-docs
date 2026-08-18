# `caret-cleanup/1` — the transcript cleanup spec

The canonical wording of Caret's transcript cleanup pass, as data.

Dictation is the one mandatory Caret operation, and cleanup is the one
place a backend hands the user's own words to a language model. This
directory is the single public source for what that model is told, so
that every implementation — the reference backend in this repository, a
hosted service, a port in another language — can be checked against the
same bytes instead of against a paraphrase.

## Files

| File | Written by | What it is |
| --- | --- | --- |
| `prompt.md` | hand | The system prompt, verbatim. |
| `glossary.json` | hand | The default glossary: canonical spellings, and how speech-to-text tends to mangle them. |
| `composed.txt` | generated | `prompt.md` composed with the default glossary — the exact system prompt a default deployment sends. |
| `manifest.json` | generated | sha256 of the three files, plus `digest`: a 16-character name for "this exact spec". |

Regenerate the two derived files after editing either source:

```sh
python3 scripts/build_cleanup_spec.py          # rewrite composed.txt + manifest.json
python3 scripts/build_cleanup_spec.py --check  # fail if they are stale (runs in make test)
```

## What the prompt guarantees

* **The transcript is inert data.** It is delivered wrapped in
  `<transcript>` … `</transcript>`, and the prompt declares that region
  text to format rather than a message addressed to the model. The
  model never answers it, never acts on it, never browses or researches
  on its behalf, never reports an action, and never adds commentary.
  The envelope does not alter the transcript, not even to escape a
  literal closing tag: meaning preservation outranks tidiness.
* **Meaning is preserved completely.** Nothing is added, removed,
  summarized, expanded, reinterpreted, re-toned, translated, or
  rewritten into corporate prose. Wording the speech-to-text engine was
  unsure about is kept as transcribed rather than guessed at.
* **Only formatting changes.** Filler and false starts go; punctuation,
  capitalization and paragraphs arrive; dictated lists become real
  lists; numbers become digits where digits are the written norm;
  technical abbreviations stay abbreviated; spoken punctuation and
  layout words convert only when unambiguous.
* **Code stays plain.** Code, commands, paths, identifiers, URLs, JSON
  and YAML are written as plain typed text with exact characters. The
  prompt explicitly forbids adding Markdown backticks or fences that the
  speaker did not dictate — a transcript pasted into a chat box should
  not arrive wearing code formatting nobody asked for.
* **The output is only the text.** No preface, no explanation, no
  quotes, no fences, no transcript tags.

## The glossary

`glossary.json` holds public Caret vocabulary only: the contract, the
agents a backend can front, and the nouns a user says while setting one
up. It biases spelling **in context** and is never a find-and-replace —
an entry applies only when the surrounding words are clearly about that
term.

A deployment supplies its own vocabulary instead:

| Variable | Default | Effect |
| --- | --- | --- |
| `CARET_CLEANUP_GLOSSARY` | `on` | `off` sends the prompt with no glossary section at all. |
| `CARET_CLEANUP_GLOSSARY_PATH` | *(none)* | Path to a JSON file in the same shape. **Replaces** the defaults. |
| `CARET_CLEANUP_SPEC_DIR` | this directory | Point a deployment at its own vendored copy of the spec. |

Replacing rather than extending is deliberate: whatever the model sees
is always exactly one list a human can read in one sitting. A
deployment that wants both copies the defaults into its own file.

## Consuming the spec without importing this repository

`composed.txt` exists so two codebases can prove they agree without
either one depending on the other. A consumer:

1. vendors the four files, byte for byte;
2. composes its own system prompt from `prompt.md` and its glossary;
3. asserts that composing with `glossary.json` reproduces
   `composed.txt` exactly;
4. pins `digest` in its own source, so a silently re-vendored spec fails
   its test rather than shipping.

Report the digest, never the prompt, on any health or status surface:
`caret-cleanup/1 97dbce336ba0bebb` identifies the wording without
disclosing a line of it.

## Failure policy

Cleanup is best-effort, in every implementation. If the model errors,
times out, or returns nothing, the **raw transcript** is returned
unchanged. A dictation that arrives unpolished is a small
disappointment; a dictation that arrives as an error message, or as an
answer to itself, is a lost thought.
