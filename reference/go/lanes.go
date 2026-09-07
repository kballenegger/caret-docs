package caretv4

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// The lanes: speech, agent, image, and cleanup providers behind four
// narrow interfaces. A lane that does not resolve is reported off in
// /health rather than papered over, which is what makes a dictate-only
// backend a complete backend instead of a broken one.
//
// Every lane is configured with one string:
//
//	""  or  "none"      the lane is off
//	"loopback"          the built-in development provider (see below)
//	"command:<argv>"    run a program
//	"http(s)://<url>"   POST to an endpoint
//
// The loopback providers are deterministic stand-ins, not models. They
// exist so the lifecycle, the reliability rules, and the conformance
// checker can be exercised end to end on a laptop with nothing
// installed. Loopback speech recognition does not recognize speech: it
// measures how much non-silent audio arrived and emits that many words.
// Say so out loud wherever you run it.

// ErrNoStreaming means the provider has no live recognizer. The
// operation still runs: audio is buffered and transcribed at finalize,
// and the result reports "stt_route": "fallback".
var ErrNoStreaming = errors.New("provider has no streaming recognizer")

// STTOptions carries everything a recognizer may bias on.
type STTOptions struct {
	Vocabulary   []string
	LanguageHint string
	SampleRateHz int
}

// STTStream is one live recognition session. Partial transcripts arrive
// through the callback given to Open; they are always cumulative.
type STTStream interface {
	Write(pcm []byte) error
	Finish() (string, error)
	Abort()
}

// STT is the speech lane.
type STT interface {
	Name() string
	Streaming() bool
	UsesVocabulary() bool
	Open(ctx context.Context, opts STTOptions, onPartial func(string)) (STTStream, error)
	Transcribe(ctx context.Context, pcm []byte, opts STTOptions) (string, error)
}

// Agent is the /ask lane: anything that takes a prompt and returns text.
type Agent interface {
	Name() string
	UsesVocabulary() bool
	Respond(ctx context.Context, prompt, visibleText string, vocabulary []string) (string, error)
}

// Image is the /imagine lane: anything that takes a prompt and returns
// image bytes.
type Image interface {
	Name() string
	Generate(ctx context.Context, prompt, aspectRatio, quality string) (mimeType string, data []byte, err error)
}

// Cleanup is the caret-cleanup/1 lane. It is best-effort by contract:
// every failure path returns the raw transcript, so a dictation that
// arrives unpolished is a small disappointment rather than a lost
// thought.
type Cleanup interface {
	Name() string
	Polish(ctx context.Context, framing, transcript string) (string, error)
}

// ---------------------------------------------------------- loopback STT

const (
	loopbackWindowBytes = 640 // 20 ms of PCM16 mono at 16 kHz
	loopbackMsPerWord   = 400
)

var loopbackFiller = []string{
	"let's", "push", "the", "review", "to", "thursday", "afternoon",
	"and", "tell", "the", "team", "before", "standup",
}

// loopbackWords is the whole of loopback recognition: count 20 ms
// windows that contain a non-zero sample, spend 400 ms of them per word,
// and spell the client's vocabulary first so a caller can prove the list
// reached the recognizer. Digital silence produces no words, which is
// how no_speech_detected stays testable.
func loopbackWords(pcm []byte, vocabulary []string) string {
	voiced := 0
	for offset := 0; offset+1 < len(pcm); offset += loopbackWindowBytes {
		end := offset + loopbackWindowBytes
		if end > len(pcm) {
			end = len(pcm)
		}
		for i := offset; i+1 < end; i += 2 {
			if int16(binary.LittleEndian.Uint16(pcm[i:i+2])) != 0 {
				voiced++
				break
			}
		}
	}
	count := voiced * 20 / loopbackMsPerWord
	if count <= 0 {
		return ""
	}
	words := make([]string, 0, count)
	for i := 0; i < count; i++ {
		if i < len(vocabulary) {
			words = append(words, vocabulary[i])
			continue
		}
		words = append(words, loopbackFiller[(i-len(vocabulary))%len(loopbackFiller)])
	}
	return strings.Join(words, " ")
}

type loopbackSTT struct{}

func (loopbackSTT) Name() string         { return "loopback" }
func (loopbackSTT) Streaming() bool      { return true }
func (loopbackSTT) UsesVocabulary() bool { return true }

func (s loopbackSTT) Open(_ context.Context, opts STTOptions, onPartial func(string)) (STTStream, error) {
	return &loopbackStream{opts: opts, onPartial: onPartial}, nil
}

func (s loopbackSTT) Transcribe(_ context.Context, pcm []byte, opts STTOptions) (string, error) {
	return loopbackWords(pcm, opts.Vocabulary), nil
}

type loopbackStream struct {
	mu        sync.Mutex
	buf       []byte
	last      string
	opts      STTOptions
	onPartial func(string)
	done      bool
}

func (s *loopbackStream) Write(pcm []byte) error {
	s.mu.Lock()
	if s.done {
		s.mu.Unlock()
		return errors.New("stream finished")
	}
	s.buf = append(s.buf, pcm...)
	text := loopbackWords(s.buf, s.opts.Vocabulary)
	emit := text != s.last && text != ""
	s.last = text
	callback := s.onPartial
	s.mu.Unlock()
	if emit && callback != nil {
		callback(text)
	}
	return nil
}

func (s *loopbackStream) Finish() (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.done = true
	return loopbackWords(s.buf, s.opts.Vocabulary), nil
}

func (s *loopbackStream) Abort() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.done = true
	s.buf = nil
}

// ----------------------------------------------------------- command STT

type commandSTT struct {
	argv    []string
	timeout time.Duration
}

func (c *commandSTT) Name() string         { return "command:" + c.argv[0] }
func (c *commandSTT) Streaming() bool      { return false }
func (c *commandSTT) UsesVocabulary() bool { return true }

func (c *commandSTT) Open(context.Context, STTOptions, func(string)) (STTStream, error) {
	return nil, ErrNoStreaming
}

// Transcribe writes the buffered audio to a temporary WAV file, runs the
// command, and deletes the file the moment the command returns —
// §10 allows a temporary file and requires exactly that.
func (c *commandSTT) Transcribe(ctx context.Context, pcm []byte, opts STTOptions) (string, error) {
	dir, err := os.MkdirTemp("", "caret-v4-stt-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(dir)
	path := filepath.Join(dir, "audio.wav")
	if err := os.WriteFile(path, EncodeWAV(pcm, opts.SampleRateHz), 0o600); err != nil {
		return "", err
	}
	argv := substituteArgs(c.argv, "{audio}", path)
	out, err := runCommand(ctx, argv, nil, c.timeout, map[string]string{
		"CARET_AUDIO_PATH":    path,
		"CARET_VOCABULARY":    strings.Join(opts.Vocabulary, "\n"),
		"CARET_LANGUAGE_HINT": opts.LanguageHint,
	})
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(out)), nil
}

// -------------------------------------------------------------- HTTP STT

type httpSTT struct {
	url     string
	timeout time.Duration
}

func (h *httpSTT) Name() string         { return "http:" + h.url }
func (h *httpSTT) Streaming() bool      { return false }
func (h *httpSTT) UsesVocabulary() bool { return true }

func (h *httpSTT) Open(context.Context, STTOptions, func(string)) (STTStream, error) {
	return nil, ErrNoStreaming
}

func (h *httpSTT) Transcribe(ctx context.Context, pcm []byte, opts STTOptions) (string, error) {
	body := map[string]any{
		"codec":          "pcm16",
		"sample_rate_hz": opts.SampleRateHz,
		"channels":       1,
		"audio_base64":   base64.StdEncoding.EncodeToString(pcm),
		"vocabulary":     opts.Vocabulary,
		"language_hint":  opts.LanguageHint,
	}
	var reply struct {
		Text string `json:"text"`
	}
	if err := postJSON(ctx, h.url, h.timeout, body, &reply); err != nil {
		return "", err
	}
	return strings.TrimSpace(reply.Text), nil
}

// ------------------------------------------------------------------ agent

type loopbackAgent struct{}

func (loopbackAgent) Name() string         { return "loopback" }
func (loopbackAgent) UsesVocabulary() bool { return false }

// Respond echoes the instruction back as the message. It is not an
// agent; it is the smallest thing that proves the /ask lifecycle without
// a model, and it is read-only by construction.
func (loopbackAgent) Respond(_ context.Context, prompt, _ string, _ []string) (string, error) {
	return strings.TrimSpace(prompt), nil
}

type commandAgent struct {
	argv    []string
	timeout time.Duration
}

func (c *commandAgent) Name() string         { return "command:" + c.argv[0] }
func (c *commandAgent) UsesVocabulary() bool { return true }

func (c *commandAgent) Respond(ctx context.Context, prompt, visibleText string, vocabulary []string) (string, error) {
	out, err := runCommand(ctx, c.argv, []byte(prompt), c.timeout, map[string]string{
		"CARET_VISIBLE_TEXT": visibleText,
		"CARET_VOCABULARY":   strings.Join(vocabulary, "\n"),
	})
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(out)), nil
}

type httpAgent struct {
	url     string
	timeout time.Duration
}

func (h *httpAgent) Name() string         { return "http:" + h.url }
func (h *httpAgent) UsesVocabulary() bool { return true }

func (h *httpAgent) Respond(ctx context.Context, prompt, visibleText string, vocabulary []string) (string, error) {
	var reply struct {
		Text string `json:"text"`
	}
	body := map[string]any{
		"prompt":       prompt,
		"visible_text": visibleText,
		"vocabulary":   vocabulary,
	}
	if err := postJSON(ctx, h.url, h.timeout, body, &reply); err != nil {
		return "", err
	}
	return strings.TrimSpace(reply.Text), nil
}

// ------------------------------------------------------------------ image

type loopbackImage struct{}

func (loopbackImage) Name() string { return "loopback" }

// Generate renders a deterministic PNG: the same prompt, aspect ratio,
// and quality always produce the same bytes, so a client's sha256 check
// has something real to verify. It draws a gradient, not your prompt.
func (loopbackImage) Generate(_ context.Context, prompt, aspectRatio, quality string) (string, []byte, error) {
	w, h := 512, 512
	switch aspectRatio {
	case "3:2":
		w, h = 600, 400
	case "2:3":
		w, h = 400, 600
	}
	switch quality {
	case "low":
		w, h = w/4, h/4
	case "medium":
		w, h = w/2, h/2
	}
	seed := 0
	for _, r := range prompt {
		seed = (seed*31 + int(r)) & 0xFFFF
	}
	img := image.NewRGBA(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			img.Set(x, y, color.RGBA{
				R: uint8((x*255/w + seed) % 256),
				G: uint8((y * 255 / h) % 256),
				B: uint8((seed / 256) % 256),
				A: 255,
			})
		}
	}
	var buf bytes.Buffer
	enc := png.Encoder{CompressionLevel: png.BestCompression}
	if err := enc.Encode(&buf, img); err != nil {
		return "", nil, err
	}
	return "image/png", buf.Bytes(), nil
}

type commandImage struct {
	argv    []string
	timeout time.Duration
}

func (c *commandImage) Name() string { return "command:" + c.argv[0] }

func (c *commandImage) Generate(ctx context.Context, prompt, aspectRatio, quality string) (string, []byte, error) {
	out, err := runCommand(ctx, c.argv, []byte(prompt), c.timeout, map[string]string{
		"CARET_ASPECT_RATIO": aspectRatio,
		"CARET_QUALITY":      quality,
	})
	if err != nil {
		return "", nil, err
	}
	mime := sniffImageMIME(out)
	if mime == "" {
		return "", nil, fmt.Errorf("command produced %d bytes that are not a PNG, JPEG, or WebP", len(out))
	}
	return mime, out, nil
}

type httpImage struct {
	url     string
	timeout time.Duration
}

func (h *httpImage) Name() string { return "http:" + h.url }

func (h *httpImage) Generate(ctx context.Context, prompt, aspectRatio, quality string) (string, []byte, error) {
	var reply struct {
		MimeType   string `json:"mime_type"`
		DataBase64 string `json:"data_base64"`
	}
	body := map[string]any{"prompt": prompt, "aspect_ratio": aspectRatio, "quality": quality}
	if err := postJSON(ctx, h.url, h.timeout, body, &reply); err != nil {
		return "", nil, err
	}
	data, err := base64.StdEncoding.DecodeString(reply.DataBase64)
	if err != nil {
		return "", nil, fmt.Errorf("data_base64 is not base64: %w", err)
	}
	mime := reply.MimeType
	if mime == "" {
		mime = sniffImageMIME(data)
	}
	if mime == "" {
		return "", nil, errors.New("provider returned no mime_type and unrecognized bytes")
	}
	return mime, data, nil
}

func sniffImageMIME(data []byte) string {
	switch {
	case bytes.HasPrefix(data, []byte("\x89PNG\r\n\x1a\n")):
		return "image/png"
	case bytes.HasPrefix(data, []byte{0xFF, 0xD8, 0xFF}):
		return "image/jpeg"
	case len(data) > 12 && bytes.HasPrefix(data, []byte("RIFF")) && bytes.Equal(data[8:12], []byte("WEBP")):
		return "image/webp"
	default:
		return ""
	}
}

// ---------------------------------------------------------------- cleanup

type loopbackCleanup struct{}

func (loopbackCleanup) Name() string { return "loopback" }

// Polish applies punctuation and capitalization and nothing else. It is
// deterministic, it is offline, and it is emphatically not the
// caret-cleanup/1 pass — that one needs a language model. It exists so
// the polish path has something to exercise in tests.
func (loopbackCleanup) Polish(_ context.Context, _ string, transcript string) (string, error) {
	text := strings.TrimSpace(transcript)
	if text == "" {
		return "", nil
	}
	runes := []rune(text)
	runes[0] = []rune(strings.ToUpper(string(runes[0])))[0]
	text = string(runes)
	if !strings.ContainsAny(text[len(text)-1:], ".!?") {
		text += "."
	}
	return text, nil
}

type commandCleanup struct {
	argv    []string
	timeout time.Duration
}

func (c *commandCleanup) Name() string { return "command:" + c.argv[0] }

func (c *commandCleanup) Polish(ctx context.Context, framing, transcript string) (string, error) {
	out, err := runCommand(ctx, c.argv, []byte(PolishPrompt(framing, transcript)), c.timeout, nil)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(out)), nil
}

type httpCleanup struct {
	url     string
	timeout time.Duration
}

func (h *httpCleanup) Name() string { return "http:" + h.url }

func (h *httpCleanup) Polish(ctx context.Context, framing, transcript string) (string, error) {
	var reply struct {
		Text string `json:"text"`
	}
	body := map[string]any{
		"system":     framing,
		"transcript": transcript,
		"prompt":     PolishPrompt(framing, transcript),
	}
	if err := postJSON(ctx, h.url, h.timeout, body, &reply); err != nil {
		return "", err
	}
	return strings.TrimSpace(reply.Text), nil
}

// ------------------------------------------------------------- resolution

// ResolveSTT turns a lane string into a speech provider. An empty or
// "none" spec returns nil, which /health reports as "dictate": false and
// "status": "not_ready" with a no_stt blocker.
func ResolveSTT(spec string, timeout time.Duration) (STT, error) {
	kind, value, err := parseLaneSpec(spec)
	if err != nil {
		return nil, err
	}
	switch kind {
	case laneOff:
		return nil, nil
	case laneLoopback:
		return loopbackSTT{}, nil
	case laneCommand:
		argv, err := splitArgs(value)
		if err != nil {
			return nil, err
		}
		return &commandSTT{argv: argv, timeout: timeout}, nil
	default:
		return &httpSTT{url: value, timeout: timeout}, nil
	}
}

// ResolveAgent turns a lane string into an /ask provider, or nil.
func ResolveAgent(spec string, timeout time.Duration) (Agent, error) {
	kind, value, err := parseLaneSpec(spec)
	if err != nil {
		return nil, err
	}
	switch kind {
	case laneOff:
		return nil, nil
	case laneLoopback:
		return loopbackAgent{}, nil
	case laneCommand:
		argv, err := splitArgs(value)
		if err != nil {
			return nil, err
		}
		return &commandAgent{argv: argv, timeout: timeout}, nil
	default:
		return &httpAgent{url: value, timeout: timeout}, nil
	}
}

// ResolveImage turns a lane string into an /imagine provider, or nil.
func ResolveImage(spec string, timeout time.Duration) (Image, error) {
	kind, value, err := parseLaneSpec(spec)
	if err != nil {
		return nil, err
	}
	switch kind {
	case laneOff:
		return nil, nil
	case laneLoopback:
		return loopbackImage{}, nil
	case laneCommand:
		argv, err := splitArgs(value)
		if err != nil {
			return nil, err
		}
		return &commandImage{argv: argv, timeout: timeout}, nil
	default:
		return &httpImage{url: value, timeout: timeout}, nil
	}
}

// ResolveCleanup turns a lane string into a polish provider, or nil.
// With no cleanup lane, /dictate returns the raw transcript and reports
// "polish_applied": false — conforming, and honest about it.
func ResolveCleanup(spec string, timeout time.Duration) (Cleanup, error) {
	kind, value, err := parseLaneSpec(spec)
	if err != nil {
		return nil, err
	}
	switch kind {
	case laneOff:
		return nil, nil
	case laneLoopback:
		return loopbackCleanup{}, nil
	case laneCommand:
		argv, err := splitArgs(value)
		if err != nil {
			return nil, err
		}
		return &commandCleanup{argv: argv, timeout: timeout}, nil
	default:
		return &httpCleanup{url: value, timeout: timeout}, nil
	}
}

type laneKind int

const (
	laneOff laneKind = iota
	laneLoopback
	laneCommand
	laneHTTP
)

func parseLaneSpec(spec string) (laneKind, string, error) {
	spec = strings.TrimSpace(spec)
	switch {
	case spec == "" || spec == "none" || spec == "off":
		return laneOff, "", nil
	case spec == "loopback":
		return laneLoopback, "", nil
	case strings.HasPrefix(spec, "command:"):
		value := strings.TrimSpace(strings.TrimPrefix(spec, "command:"))
		if value == "" {
			return laneOff, "", errors.New("command: lane needs a program to run")
		}
		return laneCommand, value, nil
	case strings.HasPrefix(spec, "http://"), strings.HasPrefix(spec, "https://"):
		return laneHTTP, spec, nil
	default:
		return laneOff, "", fmt.Errorf("unrecognized lane %q: use none, loopback, command:<argv>, or an http(s) URL", spec)
	}
}

// ------------------------------------------------------------- utilities

// EncodeWAV wraps raw PCM16 mono in the 44-byte canonical WAV header, so
// a command-line recognizer gets a file it recognizes.
func EncodeWAV(pcm []byte, sampleRateHz int) []byte {
	if sampleRateHz <= 0 {
		sampleRateHz = 16000
	}
	var buf bytes.Buffer
	put32 := func(v uint32) { _ = binary.Write(&buf, binary.LittleEndian, v) }
	put16 := func(v uint16) { _ = binary.Write(&buf, binary.LittleEndian, v) }
	buf.WriteString("RIFF")
	put32(uint32(36 + len(pcm)))
	buf.WriteString("WAVEfmt ")
	put32(16)
	put16(1) // PCM
	put16(1) // mono
	put32(uint32(sampleRateHz))
	put32(uint32(sampleRateHz * 2))
	put16(2)
	put16(16)
	buf.WriteString("data")
	put32(uint32(len(pcm)))
	buf.Write(pcm)
	return buf.Bytes()
}

func substituteArgs(argv []string, placeholder, value string) []string {
	out := make([]string, 0, len(argv)+1)
	found := false
	for _, arg := range argv {
		if strings.Contains(arg, placeholder) {
			found = true
			arg = strings.ReplaceAll(arg, placeholder, value)
		}
		out = append(out, arg)
	}
	if !found {
		out = append(out, value)
	}
	return out
}

// splitArgs is a minimal POSIX-ish splitter: whitespace separates,
// single and double quotes group. Enough for a lane string in a config
// file, deliberately not a shell.
func splitArgs(s string) ([]string, error) {
	var args []string
	var current strings.Builder
	var quote rune
	started := false
	for _, r := range s {
		switch {
		case quote != 0:
			if r == quote {
				quote = 0
			} else {
				current.WriteRune(r)
			}
		case r == '\'' || r == '"':
			quote = r
			started = true
		case r == ' ' || r == '\t':
			if started {
				args = append(args, current.String())
				current.Reset()
				started = false
			}
		default:
			current.WriteRune(r)
			started = true
		}
	}
	if quote != 0 {
		return nil, errors.New("unbalanced quote in command lane")
	}
	if started {
		args = append(args, current.String())
	}
	if len(args) == 0 {
		return nil, errors.New("command lane is empty")
	}
	return args, nil
}

func runCommand(ctx context.Context, argv []string, stdin []byte, timeout time.Duration, env map[string]string) ([]byte, error) {
	if timeout <= 0 {
		timeout = 120 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	if stdin != nil {
		cmd.Stdin = bytes.NewReader(stdin)
	}
	cmd.Env = os.Environ()
	for k, v := range env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		// The provider's stderr is operator diagnostics. It never
		// reaches the user-facing error message.
		return nil, fmt.Errorf("%s failed: %v: %s", argv[0], err, strings.TrimSpace(truncate(stderr.String(), 400)))
	}
	return stdout.Bytes(), nil
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

func postJSON(ctx context.Context, url string, timeout time.Duration, body any, out any) error {
	if timeout <= 0 {
		timeout = 120 * time.Second
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("%s answered HTTP %d", url, resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}
