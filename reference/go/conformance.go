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
	"sync"
	"sync/atomic"
	"time"
)

// The conformance checker.
//
// It treats the backend under test as a black box at a base URL: any
// language, any host, Caret's own production backend or a first attempt
// written this afternoon. It exercises what is easy to get subtly wrong:
// the finalize totals check, exactly-one-terminal-event ordering after
// queued partials, cumulative partial text, replay under a repeated
// client_request_id, cancel before and after finalize, honest
// capabilities against served routes, and the error table with its
// close codes and retryable flags. Exit 0 means a V4 client will be
// happy. Rules it cannot reach from outside are listed as SKIP so the
// report says what was not tested rather than implying it was.

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

	// Every operation the checker runs validates the ready event and
	// the retryable flag of any error it sees; the problems are reported
	// once at the end.
	readyCount        int
	readyProblems     []string
	retryableProblems []string
}

// replayHold is how long the replay check waits for a cached result
// before uploading the audio again.
const replayHold = 3 * time.Second

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

	// ---- auth, on a route the backend serves so that a refusal is
	// about the credential and not the route.
	authRoute := RouteDictate
	for _, route := range []string{RouteDictate, RouteAsk, RouteImagine} {
		if boolCap(caps, route) {
			authRoute = route
			break
		}
	}
	results = append(results, c.checkBadCredential(authRoute))
	results = append(results, c.checkMissingCredential(authRoute))

	// ---- the lifecycle, on /dictate
	if dictate {
		results = append(results, c.checkDictateHappyPath(caps)...)
		results = append(results, c.checkPolishFalse())
		results = append(results, c.checkAudioIncomplete())
		results = append(results, c.checkAudioTooShort())
		results = append(results, c.checkOversizedFrame())
		results = append(results, c.checkProtocolError())
		results = append(results, c.checkBinaryBeforeReady())
		results = append(results, c.checkBadVocabulary())
		results = append(results, c.checkUnknownFields())
		results = append(results, c.checkReplay()...)
		results = append(results, c.checkCancel()...)
		results = append(results, c.checkSilence())
		results = append(results, c.checkTextInput(caps))
	} else {
		add("dictate.*", CheckSkip, "backend reports dictate off; the lifecycle checks need /dictate")
	}
	results = append(results, c.checkUnservedRoutes(caps)...)

	if ask {
		results = append(results, c.checkAsk(caps))
	} else {
		add("ask.happy_path", CheckSkip, "backend does not serve /ask")
	}
	if imagine {
		results = append(results, c.checkImagine(caps))
	} else {
		add("imagine.happy_path", CheckSkip, "backend does not serve /imagine")
	}

	// ---- what every operation above was watching for
	switch {
	case len(c.readyProblems) > 0:
		add("lifecycle.ready_shape", CheckFail, "%s", c.readyProblems[0])
	case c.readyCount == 0:
		add("lifecycle.ready_shape", CheckSkip, "no ready event was observed")
	default:
		add("lifecycle.ready_shape", CheckPass, "%d ready events checked", c.readyCount)
	}
	if len(c.retryableProblems) > 0 {
		add("errors.retryable_table", CheckFail, "%s", strings.Join(c.retryableProblems, "; "))
	} else {
		add("errors.retryable_table", CheckPass, "every observed error carried the §9 retryable flag")
	}

	// ---- rules a black box cannot show
	for _, skip := range untestable {
		add(skip.name, CheckSkip, "%s", skip.why)
	}
	return results, nil
}

// untestable lists the rules the checker cannot reach from outside, so
// the report names them instead of silently leaving them out.
var untestable = []struct{ name, why string }{
	{"untested.start_timeout", "the 10 s start timeout is not exercised; it would cost 10 s per run"},
	{"untested.frame_gap", "the 60 s frame gap timeout is not exercised; it would cost 60 s per run"},
	{"untested.audio_too_long", "audio_too_long needs max_audio_seconds of PCM; not uploaded"},
	{"untested.keepalive", "the 20 s progress keep-alive is not exercised; loopback lanes finish in milliseconds"},
	{"untested.constant_time_compare", "constant-time credential comparison is not observable over the wire"},
	{"untested.audio_buffering", "buffering audio independently of the streaming recognizer is not observable from outside"},
	{"untested.vocabulary_routing", "vocabulary routing is not black-box testable; a real recognizer given synthetic audio cannot be held to it"},
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

// limit reads an advertised limit from health, or the default.
func (c *Checker) limit(name string, fallback int) int {
	limits, _ := c.health["limits"].(map[string]any)
	if v, ok := limits[name].(float64); ok && v > 0 {
		return int(v)
	}
	return fallback
}

// ------------------------------------------------------------------ HTTP

func (c *Checker) httpClient() *http.Client {
	transport := &http.Transport{}
	if c.InsecureTLS {
		transport.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} // #nosec G402, opt-in for a development certificate
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
	ready    map[string]any
	terminal map[string]any
	// terminals counts terminal events; more than one is a failure.
	terminals int
	// lateTerminal is a terminal event that arrived after the close.
	lateTerminal bool
	// fromCache is a terminal event that arrived while the checker was
	// still holding its audio back (see operation.hold).
	fromCache bool
	close     uint16
	httpCode  int
	err       error
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

// When to send cancel, if at all.
const (
	cancelAfterAudio    = "after_audio"    // instead of finalize
	cancelAfterFinalize = "after_finalize" // right behind finalize
)

// operation is the whole client half of the lifecycle, parameterized
// enough to drive both the happy path and every error case.
type operation struct {
	route    string
	key      string
	noAuth   bool // send no Authorization header at all
	start    map[string]any
	audio    [][]byte
	finalize map[string]any
	// skipStart sends nothing at all and just listens.
	skipStart bool
	// rawFirst is sent as the first text frame instead of start.
	rawFirst string
	// binaryFirst is sent as the very first frame, before start and
	// without waiting for ready; nothing else follows.
	binaryFirst []byte
	// cancel is one of the cancelAfter* moments.
	cancel string
	// hold keeps every frame after start back for this long, unless a
	// terminal event arrives first: how the replay check gives §8's
	// cached result a chance to show up.
	hold time.Duration
}

func (c *Checker) run(op operation) *probe {
	p := &probe{}
	header := http.Header{}
	key := op.key
	if key == "" {
		key = c.APIKey
	}
	if key != "" && !op.noAuth {
		header.Set("Authorization", "Bearer "+key)
	}
	var tlsCfg *tls.Config
	if c.InsecureTLS {
		tlsCfg = &tls.Config{InsecureSkipVerify: true} // #nosec G402, opt-in
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

	send := func(v any) bool {
		b, _ := json.Marshal(v)
		return conn.WriteText(string(b)) == nil
	}

	switch {
	case op.binaryFirst != nil:
		if conn.WriteBinary(op.binaryFirst) != nil {
			p.err = errors.New("write failed")
			return p
		}
	case op.rawFirst != "":
		if conn.WriteText(op.rawFirst) != nil {
			p.err = errors.New("write failed")
			return p
		}
	case !op.skipStart:
		if !send(op.start) {
			p.err = errors.New("write failed")
			return p
		}
	}

	// Sending happens alongside reading, because §8 lets the server
	// deliver a cached result the moment it says ready and requires the
	// client to accept it and stop sending. A client that uploads before
	// it listens deadlocks against a conforming backend.
	stop := make(chan struct{})
	var stopOnce sync.Once
	stopSending := func() { stopOnce.Do(func() { close(stop) }) }
	stopped := func() bool {
		select {
		case <-stop:
			return true
		default:
			return false
		}
	}
	defer stopSending()
	var uploading atomic.Bool
	sendErr := make(chan error, 1)
	report := func(err error) {
		select {
		case sendErr <- err:
		default:
		}
	}
	upload := func() {
		if op.hold > 0 {
			select {
			case <-time.After(op.hold):
			case <-stop:
				return
			}
		}
		uploading.Store(true)
		for _, chunk := range op.audio {
			if stopped() {
				return
			}
			if err := conn.WriteBinary(chunk); err != nil {
				report(err)
				return
			}
		}
		if op.cancel == cancelAfterAudio {
			send(map[string]any{"type": "cancel"})
			return
		}
		if op.finalize != nil && !stopped() && !send(op.finalize) {
			report(errors.New("finalize write failed"))
			return
		}
		if op.cancel == cancelAfterFinalize {
			send(map[string]any{"type": "cancel"})
		}
	}

	for {
		_ = conn.SetReadDeadline(time.Now().Add(c.Timeout))
		msg, err := conn.ReadMessage()
		if err != nil {
			var ce *CloseError
			if errors.As(err, &ce) {
				p.close = ce.Code
				if op.cancel != "" {
					p.lateTerminal = c.readLateTerminal(conn)
				}
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
			if p.ready != nil {
				continue
			}
			p.ready = event
			c.validateReady(event)
			go upload()
		case "partial":
			text, _ := event["text"].(string)
			p.partials = append(p.partials, text)
		case "progress":
		case "result", "error":
			p.terminals++
			if p.terminal != nil {
				p.err = errors.New("more than one terminal event")
				continue
			}
			p.terminal = event
			p.fromCache = !uploading.Load()
			if event["event"] == "error" {
				c.validateRetryable(event)
			}
			stopSending()
		default:
			// Unknown event types are ignored, per §11.
		}
	}
}

// readLateTerminal looks briefly past the close frame for a terminal
// event that should not be there. A conforming backend sends the close
// last, so this normally reads nothing.
func (c *Checker) readLateTerminal(conn *Conn) bool {
	_ = conn.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
	for {
		msg, err := conn.ReadMessage()
		if err != nil {
			return false
		}
		if msg.Binary {
			continue
		}
		var event map[string]any
		if json.Unmarshal(msg.Data, &event) == nil && (event["event"] == "result" || event["event"] == "error") {
			return true
		}
	}
}

// validateReady is §4's ready shape: protocol 4, an op_id, and an stt
// value a client knows how to act on.
func (c *Checker) validateReady(event map[string]any) {
	c.readyCount++
	problem := func(format string, args ...any) {
		c.readyProblems = append(c.readyProblems, fmt.Sprintf(format, args...))
	}
	if v, _ := event["protocol"].(float64); int(v) != ProtocolVersion {
		problem("ready.protocol is %v, want %d", event["protocol"], ProtocolVersion)
	}
	if id, _ := event["op_id"].(string); id == "" {
		problem("ready.op_id is missing or empty")
	}
	switch stt := event["stt"]; stt {
	case "streaming", "buffered", nil:
	default:
		problem("ready.stt is %v, want streaming, buffered, or null", stt)
	}
}

// validateRetryable holds every error event to the §9 table. Codes the
// checker does not know are left alone: the list is append-only.
func (c *Checker) validateRetryable(event map[string]any) {
	code, _ := event["code"].(string)
	want, known := retryableCodes[code]
	if !known {
		return
	}
	if got, ok := event["retryable"].(bool); !ok || got != want {
		c.retryableProblems = append(c.retryableProblems,
			fmt.Sprintf("%s carried retryable=%v, §9 says %v", code, event["retryable"], want))
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

func textStartFrame(clientRequestID, text string) map[string]any {
	return map[string]any{
		"type":              "start",
		"protocol":          ProtocolVersion,
		"client_request_id": clientRequestID,
		"input":             map[string]any{"type": "text", "text": text},
	}
}

func requestID(label string) string {
	return fmt.Sprintf("conform-%s-%d", label, time.Now().UnixNano())
}

// audioEchoMatches compares the result's audio object with what the
// checker sent. duration_ms is left alone: the spec's own example is
// inconsistent there, so a strict compare would reject real clients.
func audioEchoMatches(terminal map[string]any, chunks [][]byte) string {
	echo, ok := terminal["audio"].(map[string]any)
	if !ok {
		return "the result carries no audio object"
	}
	want := totals(chunks)
	frames, _ := echo["frames"].(float64)
	bytesTotal, _ := echo["bytes"].(float64)
	if int(frames) != want["frames"].(int) || int(bytesTotal) != want["bytes"].(int) {
		return fmt.Sprintf("result.audio is %d frames / %d bytes, sent %d / %d",
			int(frames), int(bytesTotal), want["frames"], want["bytes"])
	}
	return ""
}

// ---------------------------------------------------------------- checks

func (c *Checker) checkBadCredential(route string) CheckResult {
	chunks := chunkAudio(tone(1000), 200)
	p := c.run(operation{
		route:    route,
		key:      "definitely-not-a-valid-credential",
		start:    startFrame(requestID("auth"), nil),
		audio:    chunks,
		finalize: map[string]any{"type": "finalize", "audio": totals(chunks)},
	})
	return credentialVerdict("auth.rejects_bad_credential", "a bogus credential", p)
}

// checkMissingCredential is §3's other half: no header at all is
// unauthorized too, not anonymous service.
func (c *Checker) checkMissingCredential(route string) CheckResult {
	p := c.run(operation{
		route:  route,
		noAuth: true,
		start:  startFrame(requestID("noauth"), nil),
	})
	return credentialVerdict("auth.rejects_missing_credential", "a missing credential", p)
}

func credentialVerdict(name, what string, p *probe) CheckResult {
	switch {
	case p.httpCode == http.StatusUnauthorized:
		return CheckResult{Name: name, Status: CheckPass, Detail: "refused the upgrade with HTTP 401"}
	case p.errorCode() == ErrUnauthorized && p.close == CloseCodeFor(ErrUnauthorized):
		return CheckResult{Name: name, Status: CheckPass, Detail: "unauthorized / 4401"}
	case p.errorCode() == ErrUnauthorized:
		return CheckResult{Name: name, Status: CheckFail,
			Detail: fmt.Sprintf("unauthorized event but close code %d, want 4401", p.close)}
	default:
		return CheckResult{Name: name, Status: CheckFail,
			Detail: fmt.Sprintf("%s was not refused (terminal %q, close %d)", what, p.terminalKind(), p.close)}
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
	if applied, ok := result["polish_applied"].(bool); !ok {
		fail("dictate.polish_true", "polish_applied is %v, want a boolean", result["polish_applied"])
	} else {
		pass("dictate.polish_true", "polish_applied=%v", applied)
	}
	if problem := audioEchoMatches(p.terminal, chunks); problem != "" {
		fail("dictate.audio_echo", "%s", problem)
	} else {
		pass("dictate.audio_echo", "")
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

// checkPolishFalse is §5: polish false returns the raw transcript and
// says so.
func (c *Checker) checkPolishFalse() CheckResult {
	chunks := chunkAudio(tone(1500), 200)
	p := c.run(operation{
		route:    RouteDictate,
		start:    startFrame(requestID("unpolished"), nil),
		audio:    chunks,
		finalize: map[string]any{"type": "finalize", "audio": totals(chunks), "polish": false},
	})
	if p.terminalKind() != "result" {
		return CheckResult{Name: "dictate.polish_false", Status: CheckFail,
			Detail: fmt.Sprintf("terminal event was %q %q", p.terminalKind(), p.errorCode())}
	}
	result, _ := p.terminal["result"].(map[string]any)
	if result["polish_applied"] != false {
		return CheckResult{Name: "dictate.polish_false", Status: CheckFail,
			Detail: fmt.Sprintf("polish_applied is %v with polish false", result["polish_applied"])}
	}
	if result["text"] != result["raw_transcript"] {
		return CheckResult{Name: "dictate.polish_false", Status: CheckFail,
			Detail: "text differs from raw_transcript with polish false"}
	}
	return CheckResult{Name: "dictate.polish_false", Status: CheckPass}
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

// checkOversizedFrame sends one frame past max_frame_bytes, advertised
// or default. §9 files that under bad_request.
func (c *Checker) checkOversizedFrame() CheckResult {
	size := c.limit("max_frame_bytes", DefaultMaxFrameBytes) + 1
	p := c.run(operation{
		route: RouteDictate,
		start: startFrame(requestID("oversize"), nil),
		audio: [][]byte{make([]byte, size)},
	})
	if p.errorCode() != ErrBadRequest {
		return CheckResult{Name: "bounds.oversized_frame", Status: CheckFail,
			Detail: fmt.Sprintf("a %d-byte frame produced %q %q, want bad_request", size, p.terminalKind(), p.errorCode())}
	}
	if p.close != CloseCodeFor(ErrBadRequest) {
		return CheckResult{Name: "bounds.oversized_frame", Status: CheckFail,
			Detail: fmt.Sprintf("close code %d, want 4400", p.close)}
	}
	return CheckResult{Name: "bounds.oversized_frame", Status: CheckPass, Detail: fmt.Sprintf("%d bytes refused", size)}
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

// checkBinaryBeforeReady is §4's "the client MUST NOT send binary
// frames before ready", seen from the server's side of the rule.
func (c *Checker) checkBinaryBeforeReady() CheckResult {
	p := c.run(operation{route: RouteDictate, binaryFirst: tone(200)})
	if p.errorCode() != ErrProtocolError {
		return CheckResult{Name: "errors.binary_before_ready", Status: CheckFail,
			Detail: fmt.Sprintf("audio before start produced %q %q, want protocol_error", p.terminalKind(), p.errorCode())}
	}
	if p.close != CloseCodeFor(ErrProtocolError) {
		return CheckResult{Name: "errors.binary_before_ready", Status: CheckFail,
			Detail: fmt.Sprintf("close code %d, want 4400", p.close)}
	}
	return CheckResult{Name: "errors.binary_before_ready", Status: CheckPass}
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
	if p.close != CloseCodeFor(ErrBadRequest) {
		return CheckResult{Name: "errors.bad_request", Status: CheckFail,
			Detail: fmt.Sprintf("close code %d, want 4400", p.close)}
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

// checkReplay is §8's idempotency rule from the client's side. The replay
// sends the same start and then waits: a backend that caches answers with
// the result before any audio goes up. One that does not gets a warning
// (caching is SHOULD), the audio again, and is then held to producing the
// same text.
func (c *Checker) checkReplay() []CheckResult {
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
		return []CheckResult{{Name: "reliability.replay", Status: CheckFail,
			Detail: fmt.Sprintf("the first attempt failed with %q", first.errorCode())}}
	}
	replay := op
	replay.hold = replayHold
	second := c.run(replay)
	if second.terminalKind() != "result" {
		return []CheckResult{{Name: "reliability.replay", Status: CheckFail,
			Detail: fmt.Sprintf("the replay failed with %q", second.errorCode())}}
	}
	firstResult, _ := first.terminal["result"].(map[string]any)
	secondResult, _ := second.terminal["result"].(map[string]any)
	sameText := firstResult["text"] == secondResult["text"]
	if second.fromCache {
		if !sameText {
			return []CheckResult{{Name: "reliability.replay", Status: CheckFail,
				Detail: "the cached result for a replayed client_request_id carries different text"}}
		}
		return []CheckResult{{Name: "reliability.replay", Status: CheckPass, Detail: "cached result delivered after ready"}}
	}
	out := []CheckResult{{Name: "reliability.replay", Status: CheckWarn, Detail: "no cached replay (SHOULD, §8)"}}
	if !sameText {
		out = append(out, CheckResult{Name: "reliability.replay_consistent", Status: CheckFail,
			Detail: "the same client_request_id and the same audio produced different text"})
	} else {
		out = append(out, CheckResult{Name: "reliability.replay_consistent", Status: CheckPass})
	}
	return out
}

// checkCancel is §4's abandon, twice: mid-upload, where the answer is
// unambiguous, and right behind finalize, where the backend may already
// be done and either outcome is fine as long as there is one of them.
func (c *Checker) checkCancel() []CheckResult {
	chunks := chunkAudio(tone(1000), 200)
	p := c.run(operation{
		route:  RouteDictate,
		start:  startFrame(requestID("cancel"), nil),
		audio:  chunks,
		cancel: cancelAfterAudio,
	})
	var out []CheckResult
	switch {
	case p.err != nil:
		out = append(out, CheckResult{Name: "lifecycle.cancel", Status: CheckFail, Detail: p.err.Error()})
	case p.terminals > 0 || p.lateTerminal:
		out = append(out, CheckResult{Name: "lifecycle.cancel", Status: CheckFail,
			Detail: fmt.Sprintf("cancel produced a terminal event (%q %q)", p.terminalKind(), p.errorCode())})
	case p.close != 1000:
		out = append(out, CheckResult{Name: "lifecycle.cancel", Status: CheckFail,
			Detail: fmt.Sprintf("close code %d after cancel, want 1000", p.close)})
	default:
		out = append(out, CheckResult{Name: "lifecycle.cancel", Status: CheckPass, Detail: "close 1000, no terminal event"})
	}

	p = c.run(operation{
		route:    RouteDictate,
		start:    startFrame(requestID("cancel-late"), nil),
		audio:    chunks,
		finalize: map[string]any{"type": "finalize", "audio": totals(chunks)},
		cancel:   cancelAfterFinalize,
	})
	name := "lifecycle.cancel_after_finalize"
	switch {
	case p.terminals > 1:
		out = append(out, CheckResult{Name: name, Status: CheckFail,
			Detail: fmt.Sprintf("%d terminal events after finalize then cancel", p.terminals)})
	case p.lateTerminal:
		out = append(out, CheckResult{Name: name, Status: CheckFail, Detail: "a terminal event arrived after the close"})
	case p.terminals == 0 && p.close == 1000:
		out = append(out, CheckResult{Name: name, Status: CheckPass, Detail: "cancelled: close 1000, no terminal event"})
	case p.terminals == 1 && p.err == nil && p.close != 0:
		out = append(out, CheckResult{Name: name, Status: CheckPass,
			Detail: fmt.Sprintf("already done: one %s then close %d", p.terminalKind(), p.close)})
	default:
		out = append(out, CheckResult{Name: name, Status: CheckFail,
			Detail: fmt.Sprintf("terminal %q, close %d, transport error %v", p.terminalKind(), p.close, p.err)})
	}
	return out
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
	start := textStartFrame(requestID("text"), "lets push the review to thursday")
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
		case p.errorCode() == ErrNotSupported && p.close == CloseCodeFor(ErrNotSupported):
			out = append(out, CheckResult{Name: name, Status: CheckPass, Detail: "not_supported / 4404"})
		case p.errorCode() == ErrNotSupported:
			out = append(out, CheckResult{Name: name, Status: CheckFail,
				Detail: fmt.Sprintf("not_supported event but close code %d, want 4404", p.close)})
		default:
			out = append(out, CheckResult{Name: name, Status: CheckFail,
				Detail: fmt.Sprintf("/%s is advertised off but answered %q %q", route, p.terminalKind(), p.errorCode())})
		}
	}
	return out
}

// routeInput picks the input mode for a served route from its own
// capabilities: text when text_input says so, audio otherwise. A backend
// that honestly advertises text_input false is never failed for it.
func (c *Checker) routeInput(caps map[string]any, route, label, text string) (op operation, chunks [][]byte) {
	op = operation{route: route}
	if supported, _ := nestedCap(caps, "text_input", route); supported {
		op.start = textStartFrame(requestID(label), text)
		op.finalize = map[string]any{"type": "finalize"}
		return op, nil
	}
	chunks = chunkAudio(tone(2000), 200)
	op.start = startFrame(requestID(label), nil)
	op.audio = chunks
	op.finalize = map[string]any{"type": "finalize", "audio": totals(chunks)}
	return op, chunks
}

func (c *Checker) checkAsk(caps map[string]any) CheckResult {
	op, chunks := c.routeInput(caps, RouteAsk, "ask", "tell sam i'm running ten minutes late")
	op.finalize["visible_text"] = ""
	p := c.run(op)
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
	if problem := transcriptEcho(result, chunks); problem != "" {
		return CheckResult{Name: "ask.happy_path", Status: CheckFail, Detail: problem}
	}
	return CheckResult{Name: "ask.happy_path", Status: CheckPass, Detail: inputLabel(chunks)}
}

func (c *Checker) checkImagine(caps map[string]any) CheckResult {
	op, chunks := c.routeInput(caps, RouteImagine, "imagine", "a lighthouse at dusk in watercolour")
	op.finalize["aspect_ratio"] = "3:2"
	op.finalize["quality"] = "low"
	p := c.run(op)
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
	if problem := transcriptEcho(result, chunks); problem != "" {
		return CheckResult{Name: "imagine.happy_path", Status: CheckFail, Detail: problem}
	}
	return CheckResult{Name: "imagine.happy_path", Status: CheckPass, Detail: inputLabel(chunks)}
}

// transcriptEcho is §6's "audio input only" on transcript: present when
// the checker spoke, absent when it typed.
func transcriptEcho(result map[string]any, chunks [][]byte) string {
	_, present := result["transcript"]
	switch {
	case chunks != nil && !present:
		return "audio input but the result carries no transcript"
	case chunks == nil && present:
		return "text input but the result carries a transcript"
	}
	return ""
}

func inputLabel(chunks [][]byte) string {
	if chunks == nil {
		return "text input"
	}
	return "audio input"
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
