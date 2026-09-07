package caretv4

import (
	"crypto/sha256"
	"crypto/tls"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"strings"
	"sync/atomic"
	"time"
)

// The conformance checker.
//
// It treats the backend under test as a black box at a base URL — any
// language, any host, Caret's own production backend or a first attempt
// written this afternoon. It exercises what is easy to get subtly wrong:
// the finalize totals check, exactly-one-terminal-event ordering after
// queued partials, cumulative partial text, replay under a repeated
// client_request_id, honest capabilities against served routes, and the
// error table with its close codes. Exit 0 means a V4 client will be
// happy.

// CheckStatus is the outcome of one check.
type CheckStatus string

const (
	CheckPass CheckStatus = "pass"
	CheckFail CheckStatus = "fail"
	CheckSkip CheckStatus = "skip"
	CheckWarn CheckStatus = "warn"
)

// CheckResult is one line of the report.
type CheckResult struct {
	Name   string      `json:"name"`
	Status CheckStatus `json:"status"`
	Detail string      `json:"detail,omitempty"`
}

// Checker drives a live backend through the contract.
type Checker struct {
	// BaseURL is what a user would paste into the client: the routes are
	// appended verbatim.
	BaseURL string
	APIKey  string

	// AllowInsecure permits an http:// base URL. Clients refuse one, so
	// this is a deliberate flag for checking a backend on loopback
	// before a reverse proxy is in front of it.
	AllowInsecure bool
	// InsecureTLS skips certificate verification, for a self-signed
	// development certificate.
	InsecureTLS bool

	Timeout time.Duration

	health map[string]any
}

// Run executes every check and returns the report. The error is non-nil
// only when the checker could not start at all.
func (c *Checker) Run() ([]CheckResult, error) {
	if c.Timeout <= 0 {
		c.Timeout = 30 * time.Second
	}
	base, err := url.Parse(strings.TrimRight(c.BaseURL, "/"))
	if err != nil {
		return nil, fmt.Errorf("bad base URL: %w", err)
	}
	switch base.Scheme {
	case "https":
	case "http":
		if !c.AllowInsecure {
			return nil, errors.New("base URL is http://; a conforming client refuses it. Pass the insecure flag to check a loopback backend anyway")
		}
	default:
		return nil, fmt.Errorf("base URL scheme %q is not http or https", base.Scheme)
	}

	var results []CheckResult
	add := func(name string, status CheckStatus, format string, args ...any) {
		results = append(results, CheckResult{Name: name, Status: status, Detail: fmt.Sprintf(format, args...)})
	}

	// ---- health
	health, err := c.fetchHealth(c.APIKey)
	if err != nil {
		add("health.reachable", CheckFail, "%v", err)
		return results, nil
	}
	c.health = health
	add("health.reachable", CheckPass, "")

	if health["protocol"] != ProtocolName {
		add("health.protocol", CheckFail, "protocol is %v, want %q", health["protocol"], ProtocolName)
	} else {
		add("health.protocol", CheckPass, "")
	}

	status, _ := health["status"].(string)
	switch status {
	case "ok", "degraded", "not_ready":
		add("health.status", CheckPass, "%s", status)
	default:
		add("health.status", CheckFail, "status is %v, want ok, degraded, or not_ready", health["status"])
	}

	caps, _ := health["capabilities"].(map[string]any)
	if caps == nil {
		add("health.capabilities", CheckFail, "no capabilities object")
		return results, nil
	}
	dictate := boolCap(caps, RouteDictate)
	ask := boolCap(caps, RouteAsk)
	imagine := boolCap(caps, RouteImagine)
	add("health.capabilities", CheckPass, "dictate=%v ask=%v imagine=%v", dictate, ask, imagine)

	// §2: dictate is mandatory. A backend that cannot take speech must
	// say not_ready and give a machine-readable reason.
	if !dictate {
		blockers, _ := health["blockers"].([]any)
		if status != "not_ready" || len(blockers) == 0 {
			add("health.dictate_mandatory", CheckFail,
				"dictate is false but status is %q with %d blockers; §2 requires not_ready and a blocker", status, len(blockers))
		} else {
			add("health.dictate_mandatory", CheckPass, "honestly not ready")
		}
	} else {
		add("health.dictate_mandatory", CheckPass, "")
	}

	if _, ok := health["auth"].(map[string]any); ok {
		add("health.auth_report", CheckPass, "")
	} else {
		add("health.auth_report", CheckFail, "no auth object")
	}

	// ---- auth
	results = append(results, c.checkBadCredential()...)

	if !dictate {
		add("dictate.*", CheckSkip, "backend reports dictate off")
		return results, nil
	}

	results = append(results, c.checkDictateHappyPath(caps)...)
	results = append(results, c.checkAudioIncomplete())
	results = append(results, c.checkAudioTooShort())
	results = append(results, c.checkProtocolError())
	results = append(results, c.checkBadVocabulary())
	results = append(results, c.checkUnknownFields())
	results = append(results, c.checkReplay())
	results = append(results, c.checkSilence())
	results = append(results, c.checkTextInput(caps))
	results = append(results, c.checkUnservedRoutes(caps)...)

	if ask {
		results = append(results, c.checkAsk())
	} else {
		add("ask.happy_path", CheckSkip, "backend does not serve /ask")
	}
	if imagine {
		results = append(results, c.checkImagine())
	} else {
		add("imagine.happy_path", CheckSkip, "backend does not serve /imagine")
	}
	return results, nil
}

func boolCap(caps map[string]any, name string) bool {
	v, _ := caps[name].(bool)
	return v
}

func nestedCap(caps map[string]any, group, route string) (bool, bool) {
	inner, ok := caps[group].(map[string]any)
	if !ok {
		return false, false
	}
	v, ok := inner[route].(bool)
	return v, ok
}

// ------------------------------------------------------------------ HTTP

func (c *Checker) httpClient() *http.Client {
	transport := &http.Transport{}
	if c.InsecureTLS {
		transport.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} // #nosec G402 — opt-in, for a development certificate
	}
	return &http.Client{Timeout: c.Timeout, Transport: transport}
}

func (c *Checker) fetchHealth(key string) (map[string]any, error) {
	req, err := http.NewRequest(http.MethodGet, strings.TrimRight(c.BaseURL, "/")+"/health", nil)
	if err != nil {
		return nil, err
	}
	if key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	resp, err := c.httpClient().Do(req)
	if err != nil {
		return nil, fmt.Errorf("GET /health: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GET /health answered HTTP %d", resp.StatusCode)
	}
	var body map[string]any
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err := json.Unmarshal(raw, &body); err != nil {
		return nil, fmt.Errorf("GET /health is not JSON: %w", err)
	}
	return body, nil
}

// ------------------------------------------------------------- operations

// probe is one operation seen from the client side.
type probe struct {
	events   []map[string]any
	partials []string
	terminal map[string]any
	close    uint16
	httpCode int
	err      error
}

func (p *probe) terminalKind() string {
	if p.terminal == nil {
		return ""
	}
	kind, _ := p.terminal["event"].(string)
	return kind
}

func (p *probe) errorCode() string {
	if p.terminalKind() != "error" {
		return ""
	}
	code, _ := p.terminal["code"].(string)
	return code
}

func (c *Checker) wsURL(route string) string {
	base := strings.TrimRight(c.BaseURL, "/")
	base = strings.Replace(base, "https://", "wss://", 1)
	base = strings.Replace(base, "http://", "ws://", 1)
	return base + "/" + route
}

// operation is the whole client half of the lifecycle, parameterized
// enough to drive both the happy path and every error case.
type operation struct {
	route     string
	key       string
	start     map[string]any
	audio     [][]byte
	finalize  map[string]any
	skipStart bool
	rawFirst  string // send this text frame instead of start
}

func (c *Checker) run(op operation) *probe {
	p := &probe{}
	header := http.Header{}
	key := op.key
	if key == "" {
		key = c.APIKey
	}
	if key != "" {
		header.Set("Authorization", "Bearer "+key)
	}
	var tlsCfg *tls.Config
	if c.InsecureTLS {
		tlsCfg = &tls.Config{InsecureSkipVerify: true} // #nosec G402 — opt-in
	}
	conn, resp, err := Dial(c.wsURL(op.route), DialOptions{Header: header, TLSConfig: tlsCfg, Timeout: c.Timeout})
	if err != nil {
		if resp != nil {
			p.httpCode = resp.StatusCode
		}
		p.err = err
		return p
	}
	defer conn.Close(1000, "")

	deadline := time.Now().Add(c.Timeout)
	_ = conn.SetReadDeadline(deadline)

	send := func(v any) bool {
		b, _ := json.Marshal(v)
		return conn.WriteText(string(b)) == nil
	}

	if op.rawFirst != "" {
		if conn.WriteText(op.rawFirst) != nil {
			p.err = errors.New("write failed")
			return p
		}
	} else if !op.skipStart {
		if !send(op.start) {
			p.err = errors.New("write failed")
			return p
		}
	}

	sentAudio := false
	var stopSending atomic.Bool
	sendErr := make(chan error, 1)
	defer stopSending.Store(true)

	for {
		_ = conn.SetReadDeadline(time.Now().Add(c.Timeout))
		msg, err := conn.ReadMessage()
		if err != nil {
			var ce *CloseError
			if errors.As(err, &ce) {
				p.close = ce.Code
			} else {
				p.err = err
			}
			// A write that failed only matters when nothing came back
			// to explain it.
			if p.terminal == nil {
				select {
				case werr := <-sendErr:
					p.err = fmt.Errorf("send failed: %w", werr)
				default:
				}
			}
			return p
		}
		if msg.Binary {
			p.err = errors.New("server sent a binary frame")
			return p
		}
		var event map[string]any
		if err := json.Unmarshal(msg.Data, &event); err != nil {
			p.err = fmt.Errorf("server frame is not JSON: %w", err)
			return p
		}
		p.events = append(p.events, event)
		switch event["event"] {
		case "ready":
			if sentAudio {
				continue
			}
			sentAudio = true
			// Sending happens alongside reading, because §8 lets the
			// server deliver a cached result the moment it says ready
			// and requires the client to accept it and stop sending. A
			// client that uploads before it listens deadlocks against a
			// conforming backend.
			go func() {
				for _, chunk := range op.audio {
					if stopSending.Load() {
						return
					}
					if err := conn.WriteBinary(chunk); err != nil {
						select {
						case sendErr <- err:
						default:
						}
						return
					}
				}
				if op.finalize != nil && !stopSending.Load() && !send(op.finalize) {
					select {
					case sendErr <- errors.New("finalize write failed"):
					default:
					}
				}
			}()
		case "partial":
			text, _ := event["text"].(string)
			p.partials = append(p.partials, text)
		case "progress":
		case "result", "error":
			if p.terminal != nil {
				p.err = errors.New("more than one terminal event")
				return p
			}
			p.terminal = event
			stopSending.Store(true)
		default:
			// Unknown event types are ignored, per §11.
		}
	}
}

// ------------------------------------------------------------ audio input

// tone synthesizes PCM16 mono at 16 kHz: a quiet 220 Hz sine, which is
// non-silent in every window and therefore something a recognizer can
// legitimately find nothing in without lying.
func tone(ms int) []byte {
	samples := 16000 * ms / 1000
	out := make([]byte, 0, samples*2)
	for i := 0; i < samples; i++ {
		v := int16(6000 * math.Sin(2*math.Pi*220*float64(i)/16000))
		if v == 0 {
			v = 1 // keep every window non-silent
		}
		out = append(out, byte(uint16(v)), byte(uint16(v)>>8))
	}
	return out
}

func silence(ms int) []byte {
	return make([]byte, 16000*ms/1000*2)
}

func chunkAudio(pcm []byte, chunkMS int) [][]byte {
	size := 16000 * chunkMS / 1000 * 2
	var chunks [][]byte
	for offset := 0; offset < len(pcm); offset += size {
		end := offset + size
		if end > len(pcm) {
			end = len(pcm)
		}
		chunks = append(chunks, pcm[offset:end])
	}
	return chunks
}

func totals(chunks [][]byte) map[string]any {
	bytesTotal := 0
	for _, chunk := range chunks {
		bytesTotal += len(chunk)
	}
	return map[string]any{
		"frames":      len(chunks),
		"bytes":       bytesTotal,
		"duration_ms": bytesTotal * 1000 / bytesPerSecond,
	}
}

func startFrame(clientRequestID string, extra map[string]any) map[string]any {
	frame := map[string]any{
		"type":              "start",
		"protocol":          ProtocolVersion,
		"client_request_id": clientRequestID,
		"input": map[string]any{
			"type": "audio", "codec": "pcm16", "sample_rate_hz": 16000, "channels": 1,
		},
	}
	for k, v := range extra {
		frame[k] = v
	}
	return frame
}

func requestID(label string) string {
	return fmt.Sprintf("conform-%s-%d", label, time.Now().UnixNano())
}

// ---------------------------------------------------------------- checks

func (c *Checker) checkBadCredential() []CheckResult {
	chunks := chunkAudio(tone(1000), 200)
	p := c.run(operation{
		route:    RouteDictate,
		key:      "definitely-not-a-valid-credential",
		start:    startFrame(requestID("auth"), nil),
		audio:    chunks,
		finalize: map[string]any{"type": "finalize", "audio": totals(chunks)},
	})
	switch {
	case p.httpCode == http.StatusUnauthorized:
		return []CheckResult{{Name: "auth.rejects_bad_credential", Status: CheckPass, Detail: "refused the upgrade with HTTP 401"}}
	case p.errorCode() == ErrUnauthorized && p.close == CloseCodeFor(ErrUnauthorized):
		return []CheckResult{{Name: "auth.rejects_bad_credential", Status: CheckPass, Detail: "unauthorized / 4401"}}
	case p.errorCode() == ErrUnauthorized:
		return []CheckResult{{Name: "auth.rejects_bad_credential", Status: CheckFail,
			Detail: fmt.Sprintf("unauthorized event but close code %d, want 4401", p.close)}}
	default:
		return []CheckResult{{Name: "auth.rejects_bad_credential", Status: CheckFail,
			Detail: fmt.Sprintf("a bogus credential was not refused (terminal %q, close %d)", p.terminalKind(), p.close)}}
	}
}

func (c *Checker) checkDictateHappyPath(caps map[string]any) []CheckResult {
	chunks := chunkAudio(tone(3000), 200)
	p := c.run(operation{
		route:    RouteDictate,
		start:    startFrame(requestID("dictate"), map[string]any{"vocabulary": []string{"Sagrada Familia"}}),
		audio:    chunks,
		finalize: map[string]any{"type": "finalize", "audio": totals(chunks), "polish": true},
	})
	var out []CheckResult
	fail := func(name, format string, args ...any) {
		out = append(out, CheckResult{Name: name, Status: CheckFail, Detail: fmt.Sprintf(format, args...)})
	}
	pass := func(name, format string, args ...any) {
		out = append(out, CheckResult{Name: name, Status: CheckPass, Detail: fmt.Sprintf(format, args...)})
	}

	if p.err != nil {
		fail("dictate.happy_path", "%v", p.err)
		return out
	}
	if p.terminalKind() != "result" {
		fail("dictate.happy_path", "terminal event was %q %q", p.terminalKind(), p.errorCode())
		return out
	}
	result, _ := p.terminal["result"].(map[string]any)
	if result["type"] != "dictation" {
		fail("dictate.result_type", "result.type is %v, want \"dictation\"", result["type"])
	} else {
		pass("dictate.result_type", "")
	}
	if text, _ := result["text"].(string); strings.TrimSpace(text) == "" {
		fail("dictate.result_text", "result.text is empty")
	} else {
		pass("dictate.result_text", "%d characters", len(text))
	}
	if _, ok := result["raw_transcript"].(string); !ok {
		fail("dictate.raw_transcript", "result.raw_transcript is missing")
	} else {
		pass("dictate.raw_transcript", "")
	}
	if route, _ := result["stt_route"].(string); route != "stream" && route != "fallback" {
		fail("dictate.stt_route", "stt_route is %v, want stream or fallback", result["stt_route"])
	} else {
		pass("dictate.stt_route", "%s", route)
	}
	if p.close != 1000 {
		fail("dictate.close_code", "close code %d, want 1000", p.close)
	} else {
		pass("dictate.close_code", "")
	}
	if id, _ := p.terminal["request_id"].(string); id == "" {
		fail("dictate.request_id", "result has no request_id")
	} else {
		pass("dictate.request_id", "")
	}

	// Exactly one terminal event, and it comes after every partial.
	terminalIndex := -1
	for i, event := range p.events {
		if kind, _ := event["event"].(string); kind == "result" || kind == "error" {
			terminalIndex = i
			break
		}
	}
	if terminalIndex != len(p.events)-1 {
		fail("dictate.terminal_is_last", "%d events after the terminal event", len(p.events)-1-terminalIndex)
	} else {
		pass("dictate.terminal_is_last", "")
	}

	// Partials are cumulative and never go backwards.
	advertised, known := nestedCap(caps, "partials", RouteDictate)
	switch {
	case known && advertised && len(p.partials) == 0:
		fail("dictate.partials", "capabilities advertise partials on /dictate but none arrived")
	case len(p.partials) == 0:
		out = append(out, CheckResult{Name: "dictate.partials", Status: CheckSkip, Detail: "no partials advertised"})
	default:
		regressed := ""
		for i := 1; i < len(p.partials); i++ {
			if !strings.HasPrefix(p.partials[i], p.partials[i-1]) && len(p.partials[i]) < len(p.partials[i-1]) {
				regressed = fmt.Sprintf("partial %d is shorter than partial %d", i, i-1)
				break
			}
		}
		if regressed != "" {
			fail("dictate.partials_cumulative", "%s", regressed)
		} else {
			pass("dictate.partials_cumulative", "%d partials", len(p.partials))
		}
	}
	return out
}

func (c *Checker) checkAudioIncomplete() CheckResult {
	chunks := chunkAudio(tone(2000), 200)
	lie := totals(chunks)
	lie["frames"] = lie["frames"].(int) + 3
	p := c.run(operation{
		route:    RouteDictate,
		start:    startFrame(requestID("incomplete"), nil),
		audio:    chunks,
		finalize: map[string]any{"type": "finalize", "audio": lie},
	})
	if p.errorCode() != ErrAudioIncomplete {
		return CheckResult{Name: "reliability.audio_incomplete", Status: CheckFail,
			Detail: fmt.Sprintf("finalize totals that disagree produced %q, want audio_incomplete", p.errorCode())}
	}
	if retry, _ := p.terminal["retryable"].(bool); !retry {
		return CheckResult{Name: "reliability.audio_incomplete", Status: CheckFail, Detail: "audio_incomplete must be retryable"}
	}
	if p.close != CloseCodeFor(ErrAudioIncomplete) {
		return CheckResult{Name: "reliability.audio_incomplete", Status: CheckFail,
			Detail: fmt.Sprintf("close code %d, want 4409", p.close)}
	}
	return CheckResult{Name: "reliability.audio_incomplete", Status: CheckPass}
}

func (c *Checker) checkAudioTooShort() CheckResult {
	chunks := chunkAudio(tone(100), 50)
	p := c.run(operation{
		route:    RouteDictate,
		start:    startFrame(requestID("short"), nil),
		audio:    chunks,
		finalize: map[string]any{"type": "finalize", "audio": totals(chunks)},
	})
	if p.errorCode() != ErrAudioTooShort {
		return CheckResult{Name: "bounds.audio_too_short", Status: CheckFail,
			Detail: fmt.Sprintf("100 ms of audio produced %q, want audio_too_short", p.errorCode())}
	}
	if p.close != CloseCodeFor(ErrAudioTooShort) {
		return CheckResult{Name: "bounds.audio_too_short", Status: CheckFail,
			Detail: fmt.Sprintf("close code %d, want 4422", p.close)}
	}
	return CheckResult{Name: "bounds.audio_too_short", Status: CheckPass}
}

func (c *Checker) checkProtocolError() CheckResult {
	p := c.run(operation{
		route:    RouteDictate,
		rawFirst: `{"type":"finalize","audio":{"frames":0,"bytes":0,"duration_ms":0}}`,
	})
	if p.errorCode() != ErrProtocolError {
		return CheckResult{Name: "errors.protocol_error", Status: CheckFail,
			Detail: fmt.Sprintf("finalize as the first frame produced %q, want protocol_error", p.errorCode())}
	}
	if p.close != CloseCodeFor(ErrProtocolError) {
		return CheckResult{Name: "errors.protocol_error", Status: CheckFail,
			Detail: fmt.Sprintf("close code %d, want 4400", p.close)}
	}
	return CheckResult{Name: "errors.protocol_error", Status: CheckPass}
}

func (c *Checker) checkBadVocabulary() CheckResult {
	chunks := chunkAudio(tone(1000), 200)
	p := c.run(operation{
		route: RouteDictate,
		start: startFrame(requestID("vocab"), map[string]any{
			"vocabulary": []string{strings.Repeat("x", 200)},
		}),
		audio:    chunks,
		finalize: map[string]any{"type": "finalize", "audio": totals(chunks)},
	})
	if p.errorCode() != ErrBadRequest {
		return CheckResult{Name: "errors.bad_request", Status: CheckFail,
			Detail: fmt.Sprintf("a 200-character vocabulary entry produced %q, want bad_request", p.errorCode())}
	}
	return CheckResult{Name: "errors.bad_request", Status: CheckPass}
}

// checkUnknownFields is §11's "ignores unknown JSON fields everywhere".
// A backend that rejects them breaks every future additive change.
func (c *Checker) checkUnknownFields() CheckResult {
	chunks := chunkAudio(tone(1500), 200)
	start := startFrame(requestID("unknown"), map[string]any{
		"a_field_from_protocol_5": "ignore me",
	})
	final := map[string]any{"type": "finalize", "audio": totals(chunks), "some_future_finalizer": 12}
	p := c.run(operation{route: RouteDictate, start: start, audio: chunks, finalize: final})
	if p.terminalKind() != "result" {
		return CheckResult{Name: "forward_compat.unknown_fields", Status: CheckFail,
			Detail: fmt.Sprintf("unknown JSON fields produced %q %q", p.terminalKind(), p.errorCode())}
	}
	return CheckResult{Name: "forward_compat.unknown_fields", Status: CheckPass}
}

// checkReplay is §8's idempotency rule from the client's side: the same
// client_request_id, twice, must not produce two different answers.
func (c *Checker) checkReplay() CheckResult {
	id := requestID("replay")
	chunks := chunkAudio(tone(2000), 200)
	op := operation{
		route:    RouteDictate,
		start:    startFrame(id, nil),
		audio:    chunks,
		finalize: map[string]any{"type": "finalize", "audio": totals(chunks)},
	}
	first := c.run(op)
	if first.terminalKind() != "result" {
		return CheckResult{Name: "reliability.replay", Status: CheckFail,
			Detail: fmt.Sprintf("the first attempt failed with %q", first.errorCode())}
	}
	second := c.run(op)
	if second.terminalKind() != "result" {
		return CheckResult{Name: "reliability.replay", Status: CheckFail,
			Detail: fmt.Sprintf("the replay failed with %q", second.errorCode())}
	}
	firstResult, _ := first.terminal["result"].(map[string]any)
	secondResult, _ := second.terminal["result"].(map[string]any)
	if firstResult["text"] != secondResult["text"] {
		return CheckResult{Name: "reliability.replay", Status: CheckFail,
			Detail: "the same client_request_id and the same audio produced different text"}
	}
	return CheckResult{Name: "reliability.replay", Status: CheckPass}
}

// checkSilence is advisory: §8 says an empty transcript from a healthy
// recognizer is no_speech_detected, but a real recognizer handed digital
// silence may still hallucinate a word, and that is its business.
func (c *Checker) checkSilence() CheckResult {
	chunks := chunkAudio(silence(1500), 200)
	p := c.run(operation{
		route:    RouteDictate,
		start:    startFrame(requestID("silence"), nil),
		audio:    chunks,
		finalize: map[string]any{"type": "finalize", "audio": totals(chunks)},
	})
	if p.errorCode() == ErrNoSpeechDetected {
		return CheckResult{Name: "errors.no_speech_detected", Status: CheckPass}
	}
	return CheckResult{Name: "errors.no_speech_detected", Status: CheckWarn,
		Detail: fmt.Sprintf("1.5 s of digital silence produced %q %q rather than no_speech_detected", p.terminalKind(), p.errorCode())}
}

func (c *Checker) checkTextInput(caps map[string]any) CheckResult {
	supported, known := nestedCap(caps, "text_input", RouteDictate)
	start := map[string]any{
		"type":              "start",
		"protocol":          ProtocolVersion,
		"client_request_id": requestID("text"),
		"input":             map[string]any{"type": "text", "text": "lets push the review to thursday"},
	}
	p := c.run(operation{route: RouteDictate, start: start, finalize: map[string]any{"type": "finalize"}})
	switch {
	case known && !supported:
		if p.errorCode() != ErrNotSupported {
			return CheckResult{Name: "dictate.text_input", Status: CheckFail,
				Detail: fmt.Sprintf("text_input is advertised false but text input produced %q", p.errorCode())}
		}
		return CheckResult{Name: "dictate.text_input", Status: CheckPass, Detail: "honestly refused"}
	case p.terminalKind() != "result":
		return CheckResult{Name: "dictate.text_input", Status: CheckFail,
			Detail: fmt.Sprintf("text input produced %q %q", p.terminalKind(), p.errorCode())}
	default:
		if _, present := p.terminal["audio"]; present {
			return CheckResult{Name: "dictate.text_input", Status: CheckFail,
				Detail: "the result carries an audio object for text input"}
		}
		return CheckResult{Name: "dictate.text_input", Status: CheckPass}
	}
}

// checkUnservedRoutes holds the backend to its own health document: a
// route it says it does not serve must answer not_supported rather than
// half-working.
func (c *Checker) checkUnservedRoutes(caps map[string]any) []CheckResult {
	var out []CheckResult
	for _, route := range []string{RouteAsk, RouteImagine} {
		if boolCap(caps, route) {
			continue
		}
		p := c.run(operation{route: route, start: startFrame(requestID("unserved"), nil)})
		name := "capabilities.unserved_" + route
		switch {
		case p.httpCode == http.StatusNotFound:
			out = append(out, CheckResult{Name: name, Status: CheckPass, Detail: "HTTP 404 on upgrade"})
		case p.errorCode() == ErrNotSupported:
			out = append(out, CheckResult{Name: name, Status: CheckPass, Detail: "not_supported"})
		default:
			out = append(out, CheckResult{Name: name, Status: CheckFail,
				Detail: fmt.Sprintf("/%s is advertised off but answered %q %q", route, p.terminalKind(), p.errorCode())})
		}
	}
	return out
}

func (c *Checker) checkAsk() CheckResult {
	start := map[string]any{
		"type":              "start",
		"protocol":          ProtocolVersion,
		"client_request_id": requestID("ask"),
		"input":             map[string]any{"type": "text", "text": "tell sam i'm running ten minutes late"},
	}
	p := c.run(operation{
		route:    RouteAsk,
		start:    start,
		finalize: map[string]any{"type": "finalize", "visible_text": ""},
	})
	if p.terminalKind() != "result" {
		return CheckResult{Name: "ask.happy_path", Status: CheckFail,
			Detail: fmt.Sprintf("terminal event was %q %q", p.terminalKind(), p.errorCode())}
	}
	result, _ := p.terminal["result"].(map[string]any)
	if result["type"] != "message" {
		return CheckResult{Name: "ask.happy_path", Status: CheckFail,
			Detail: fmt.Sprintf("result.type is %v, want \"message\"", result["type"])}
	}
	if text, _ := result["text"].(string); strings.TrimSpace(text) == "" {
		return CheckResult{Name: "ask.happy_path", Status: CheckFail, Detail: "result.text is empty"}
	}
	return CheckResult{Name: "ask.happy_path", Status: CheckPass}
}

func (c *Checker) checkImagine() CheckResult {
	start := map[string]any{
		"type":              "start",
		"protocol":          ProtocolVersion,
		"client_request_id": requestID("imagine"),
		"input":             map[string]any{"type": "text", "text": "a lighthouse at dusk in watercolour"},
	}
	p := c.run(operation{
		route:    RouteImagine,
		start:    start,
		finalize: map[string]any{"type": "finalize", "aspect_ratio": "3:2", "quality": "low"},
	})
	if p.terminalKind() != "result" {
		return CheckResult{Name: "imagine.happy_path", Status: CheckFail,
			Detail: fmt.Sprintf("terminal event was %q %q", p.terminalKind(), p.errorCode())}
	}
	result, _ := p.terminal["result"].(map[string]any)
	if result["type"] != "image" {
		return CheckResult{Name: "imagine.happy_path", Status: CheckFail,
			Detail: fmt.Sprintf("result.type is %v, want \"image\"", result["type"])}
	}
	encoded, _ := result["data_base64"].(string)
	data, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return CheckResult{Name: "imagine.happy_path", Status: CheckFail, Detail: "data_base64 is not base64"}
	}
	// §6: byte_length and sha256 MUST describe the delivered bytes, and
	// clients verify them. So does this checker.
	wantLen, _ := result["byte_length"].(float64)
	if int(wantLen) != len(data) {
		return CheckResult{Name: "imagine.happy_path", Status: CheckFail,
			Detail: fmt.Sprintf("byte_length is %d, delivered %d bytes", int(wantLen), len(data))}
	}
	sum := sha256.Sum256(data)
	if got, _ := result["sha256"].(string); !strings.EqualFold(got, hex.EncodeToString(sum[:])) {
		return CheckResult{Name: "imagine.happy_path", Status: CheckFail, Detail: "sha256 does not match the delivered bytes"}
	}
	return CheckResult{Name: "imagine.happy_path", Status: CheckPass}
}

// ---------------------------------------------------------------- report

// Report writes a human-readable summary and reports whether every
// required check passed.
func Report(w io.Writer, results []CheckResult) bool {
	ok := true
	counts := map[CheckStatus]int{}
	for _, r := range results {
		counts[r.Status]++
		if r.Status == CheckFail {
			ok = false
		}
		line := fmt.Sprintf("%-6s %s", strings.ToUpper(string(r.Status)), r.Name)
		if r.Detail != "" {
			line += "  — " + r.Detail
		}
		fmt.Fprintln(w, line)
	}
	fmt.Fprintf(w, "\n%d passed, %d failed, %d warnings, %d skipped\n",
		counts[CheckPass], counts[CheckFail], counts[CheckWarn], counts[CheckSkip])
	return ok
}
