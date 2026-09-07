package caretv4

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Transcript cleanup, consumed as data.
//
// The wording is not written here and must never be forked into here.
// It lives in this repository's canonical spec directory,
// `spec/cleanup/v1/`, and this file reads it, checks it against its own
// manifest, and composes the system prompt the same way every other
// implementation does. `composed.txt` is the anti-drift artifact: two
// codebases that both reproduce those bytes from `prompt.md` plus the
// default glossary agree on the spec without importing each other.

// Spec identifiers.
const (
	CleanupSpecID      = "caret-cleanup"
	CleanupSpecVersion = 1
	CleanupSpecName    = "caret-cleanup/1"
)

// TranscriptOpenTag and TranscriptCloseTag delimit the inert-data
// envelope. The transcript inside is passed through unmodified: never
// escaped, never trimmed, never truncated. Meaning preservation
// outranks tidiness.
const (
	TranscriptOpenTag  = "<transcript>"
	TranscriptCloseTag = "</transcript>"
)

// glossarySectionHeader is part of the composed prompt's bytes. Changing
// a character here breaks the composed.txt check, which is the point.
const glossarySectionHeader = "Glossary. Canonical spellings for terms this speaker uses. Apply an " +
	"entry only when the surrounding words clearly refer to that term. " +
	"Never replace an ordinary word that merely sounds like one, and never " +
	"insert a term the speaker did not say."

// hashedSpecFiles are the files manifest.json covers, in digest order.
var hashedSpecFiles = []string{"prompt.md", "glossary.json", "composed.txt"}

// GlossaryEntry is one canonical spelling and the ways speech-to-text
// tends to mangle it. It biases spelling in context; it is never a
// find-and-replace.
type GlossaryEntry struct {
	Canonical             string   `json:"canonical"`
	CommonMisrecognitions []string `json:"common_misrecognitions"`
	Context               string   `json:"context"`
}

// CleanupSpec is the loaded, verified spec.
type CleanupSpec struct {
	Dir      string
	Prompt   string
	Glossary []GlossaryEntry
	Composed string
	Digest   string
}

// FindSpecDir locates `spec/cleanup/v1`. CARET_CLEANUP_SPEC_DIR points a
// deployment at its own vendored copy; otherwise the directory is found
// by walking up from the working directory and from the executable, so
// `go run`, `go test`, and an installed binary beside the repository all
// find the same bytes.
func FindSpecDir() (string, error) {
	if override := strings.TrimSpace(os.Getenv("CARET_CLEANUP_SPEC_DIR")); override != "" {
		return override, nil
	}
	var roots []string
	if wd, err := os.Getwd(); err == nil {
		roots = append(roots, wd)
	}
	if exe, err := os.Executable(); err == nil {
		roots = append(roots, filepath.Dir(exe))
	}
	for _, root := range roots {
		dir := root
		for i := 0; i < 8; i++ {
			candidate := filepath.Join(dir, "spec", "cleanup", "v1")
			if info, err := os.Stat(filepath.Join(candidate, "manifest.json")); err == nil && !info.IsDir() {
				return candidate, nil
			}
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			dir = parent
		}
	}
	return "", fmt.Errorf("cleanup spec not found: set CARET_CLEANUP_SPEC_DIR to a spec/cleanup/v1 directory")
}

// LoadCleanupSpec reads the spec and verifies it against its manifest.
// Every failure happens here, at configuration time, rather than in the
// middle of somebody's dictation.
func LoadCleanupSpec(dir string) (*CleanupSpec, error) {
	if dir == "" {
		found, err := FindSpecDir()
		if err != nil {
			return nil, err
		}
		dir = found
	}
	read := func(name string) (string, error) {
		b, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			return "", fmt.Errorf("cannot read %s: %w", name, err)
		}
		return string(b), nil
	}
	promptRaw, err := read("prompt.md")
	if err != nil {
		return nil, err
	}
	glossaryRaw, err := read("glossary.json")
	if err != nil {
		return nil, err
	}
	composedRaw, err := read("composed.txt")
	if err != nil {
		return nil, err
	}
	manifestRaw, err := read("manifest.json")
	if err != nil {
		return nil, err
	}

	var glossaryDoc struct {
		Entries []GlossaryEntry `json:"entries"`
	}
	if err := json.Unmarshal([]byte(glossaryRaw), &glossaryDoc); err != nil {
		return nil, fmt.Errorf("glossary.json is not valid JSON: %w", err)
	}
	for i, entry := range glossaryDoc.Entries {
		if strings.TrimSpace(entry.Canonical) == "" {
			return nil, fmt.Errorf("glossary.json: entry %d has no canonical term", i)
		}
	}

	var manifest struct {
		Files  map[string]string `json:"files"`
		Digest string            `json:"digest"`
	}
	if err := json.Unmarshal([]byte(manifestRaw), &manifest); err != nil {
		return nil, fmt.Errorf("manifest.json is not valid JSON: %w", err)
	}

	actual := map[string]string{
		"prompt.md":     sha256Hex([]byte(promptRaw)),
		"glossary.json": sha256Hex([]byte(glossaryRaw)),
		"composed.txt":  sha256Hex([]byte(composedRaw)),
	}
	for _, name := range hashedSpecFiles {
		if manifest.Files[name] != actual[name] {
			return nil, fmt.Errorf("%s does not match manifest.json", name)
		}
	}
	if digest := computeSpecDigest(actual); digest != manifest.Digest {
		return nil, fmt.Errorf("manifest digest is %q, files digest to %q", manifest.Digest, digest)
	}

	spec := &CleanupSpec{
		Dir:      dir,
		Prompt:   strings.TrimSpace(promptRaw),
		Glossary: glossaryDoc.Entries,
		Composed: strings.TrimRight(composedRaw, "\n"),
		Digest:   manifest.Digest,
	}
	if composed := spec.SystemPrompt(nil); composed != spec.Composed {
		return nil, fmt.Errorf("composed.txt is stale: prompt.md + glossary.json no longer compose to it")
	}
	return spec, nil
}

func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// computeSpecDigest is the short name for "these exact spec files":
// sha256 over "<name>:<sha256>\n" per file in fixed order, truncated to
// 16 hex characters.
func computeSpecDigest(hashes map[string]string) string {
	var joined strings.Builder
	for _, name := range hashedSpecFiles {
		fmt.Fprintf(&joined, "%s:%s\n", name, hashes[name])
	}
	return sha256Hex([]byte(joined.String()))[:16]
}

// SpecID is `caret-cleanup/1 <digest>` — safe to report over health. It
// identifies the prompt without disclosing a word of it.
func (s *CleanupSpec) SpecID() string {
	if s == nil || s.Digest == "" {
		return CleanupSpecName
	}
	return CleanupSpecName + " " + s.Digest
}

func renderGlossaryEntry(entry GlossaryEntry) string {
	line := "- " + entry.Canonical
	if ctx := strings.TrimSpace(entry.Context); ctx != "" {
		line += " — " + ctx
		if !strings.HasSuffix(line, ".") {
			line += "."
		}
	}
	var misheard []string
	for _, m := range entry.CommonMisrecognitions {
		if m = strings.TrimSpace(m); m != "" {
			misheard = append(misheard, m)
		}
	}
	if len(misheard) > 0 {
		line += " Sometimes mis-heard as: " + strings.Join(misheard, ", ") + "."
	}
	return line
}

// renderGlossarySection returns the glossary appendix, or the empty
// string when there is nothing to say, so an empty glossary is invisible
// rather than a dangling header.
func renderGlossarySection(entries []GlossaryEntry) string {
	if len(entries) == 0 {
		return ""
	}
	lines := make([]string, 0, len(entries)+2)
	lines = append(lines, glossarySectionHeader, "")
	for _, entry := range entries {
		lines = append(lines, renderGlossaryEntry(entry))
	}
	return strings.Join(lines, "\n")
}

// SystemPrompt is prompt.md plus the glossary section, with no trailing
// newline. Passing nil composes with the spec's own default glossary,
// which by construction equals composed.txt.
func (s *CleanupSpec) SystemPrompt(extra []string) string {
	entries := s.Glossary
	if len(extra) > 0 {
		entries = append(append([]GlossaryEntry{}, entries...), vocabularyEntries(entries, extra)...)
	}
	section := renderGlossarySection(entries)
	if section == "" {
		return s.Prompt
	}
	return s.Prompt + "\n\n" + section
}

// vocabularyEntries turns the operation's vocabulary list into glossary
// entries. §7 requires the list to reach the cleanup glossary, and a
// term the default glossary already carries is not repeated.
func vocabularyEntries(existing []GlossaryEntry, vocabulary []string) []GlossaryEntry {
	have := make(map[string]bool, len(existing))
	for _, entry := range existing {
		have[strings.ToLower(entry.Canonical)] = true
	}
	var out []GlossaryEntry
	for _, term := range vocabulary {
		key := strings.ToLower(term)
		if have[key] {
			continue
		}
		have[key] = true
		out = append(out, GlossaryEntry{
			Canonical: term,
			Context:   "Supplied by the client for this dictation",
		})
	}
	return out
}

// WrapTranscript is the inert-data envelope.
func WrapTranscript(text string) string {
	return TranscriptOpenTag + "\n" + text + "\n" + TranscriptCloseTag
}

// PolishPrompt is the whole cleanup request as one string, for a
// provider with no system-message slot. A provider that has one sends
// the framing as the system message and WrapTranscript as the user
// message: same two halves, same order.
func PolishPrompt(framing, transcript string) string {
	return framing + "\n\n" + WrapTranscript(transcript)
}
