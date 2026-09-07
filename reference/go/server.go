package caretv4

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// Protocol defaults from §2 and §4. A deployment may lower them; it
// advertises whatever it enforces, and clients MUST respect what is
// advertised.
const (
	DefaultMaxAudioSeconds      = 600
	DefaultMaxFrameBytes        = 524288
	DefaultMaxTextChars         = 4000
	DefaultMaxVocabularyEntries = 200

	// startDeadline, frameGap, and progressEvery are the timing rules of
	// §4: start within 10 s of the upgrade, no 60 s hole in the audio,
	// and never 20 s of server silence after finalize.
	startDeadline = 10 * time.Second
	frameGap      = 60 * time.Second
	progressEvery = 5 * time.Second

	// bytesPerSecond is PCM16 mono at 16 kHz — the only codec in V4.
	bytesPerSecond = 32000
)

// Config is everything a reference backend needs to know. The zero value
// is not usable: without at least one API key and a speech lane the
// server starts, serves /health honestly, and refuses every operation.
type Config struct {
	Service string
	Version string

	// APIKeys are the credentials this backend accepts. Empty means the
	// backend fails closed: /health still answers, every operation route
	// answers unauthorized.
	APIKeys []string

	// Lane specs: "", "none", "loopback", "command:<argv>", or an
	// http(s) URL.
	STT     string
	Agent   string
	Image   string
	Cleanup string

	MaxAudioSeconds      int
	MaxFrameBytes        int
	MaxTextChars         int
	MaxVocabularyEntries int

	LaneTimeout    time.Duration
	ResultCacheTTL time.Duration

	// SpecDir overrides the location of spec/cleanup/v1.
	SpecDir string

	Logger *log.Logger
}

func (c *Config) applyDefaults() {
	if c.Service == "" {
		c.Service = "caret-v4-reference"
	}
	if c.Version == "" {
		c.Version = Version
	}
	if c.MaxAudioSeconds <= 0 {
		c.MaxAudioSeconds = DefaultMaxAudioSeconds
	}
	if c.MaxFrameBytes <= 0 {
		c.MaxFrameBytes = DefaultMaxFrameBytes
	}
	if c.MaxTextChars <= 0 {
		c.MaxTextChars = DefaultMaxTextChars
	}
	if c.MaxVocabularyEntries <= 0 {
		c.MaxVocabularyEntries = DefaultMaxVocabularyEntries
	}
	if c.LaneTimeout <= 0 {
		c.LaneTimeout = 120 * time.Second
	}
	if c.ResultCacheTTL <= 0 {
		c.ResultCacheTTL = 5 * time.Minute
	}
	if c.Logger == nil {
		c.Logger = log.New(os.Stderr, "caret-v4 ", log.LstdFlags)
	}
}

// Version is the reference implementation's own version, reported on
// /health. It is not the protocol version.
const Version = "1.0.0"

// Blocker is a machine-readable reason this backend is not fully usable.
type Blocker struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

// Server is a caret/v4 backend. It is safe for concurrent use and holds
// no per-operation state beyond the short-lived idempotency cache.
type Server struct {
	cfg      Config
	stt      STT
	agent    Agent
	image    Image
	cleanup  Cleanup
	spec     *CleanupSpec
	keys     [][32]byte
	blockers []Blocker
	cache    *resultCache
	log      *log.Logger
}

// New resolves the lanes and returns a server. A lane that cannot be
// parsed is a configuration error and fails here; a lane that is simply
// absent is not an error, it is a capability reported off.
func New(cfg Config) (*Server, error) {
	cfg.applyDefaults()
	s := &Server{cfg: cfg, log: cfg.Logger, cache: newResultCache(cfg.ResultCacheTTL)}

	var err error
	if s.stt, err = ResolveSTT(cfg.STT, cfg.LaneTimeout); err != nil {
		return nil, fmt.Errorf("stt lane: %w", err)
	}
	if s.agent, err = ResolveAgent(cfg.Agent, cfg.LaneTimeout); err != nil {
		return nil, fmt.Errorf("agent lane: %w", err)
	}
	if s.image, err = ResolveImage(cfg.Image, cfg.LaneTimeout); err != nil {
		return nil, fmt.Errorf("image lane: %w", err)
	}
	if s.cleanup, err = ResolveCleanup(cfg.Cleanup, cfg.LaneTimeout); err != nil {
		return nil, fmt.Errorf("cleanup lane: %w", err)
	}

	for _, key := range cfg.APIKeys {
		if key = strings.TrimSpace(key); key != "" {
			s.keys = append(s.keys, sha256.Sum256([]byte(key)))
		}
	}

	if s.cleanup != nil {
		spec, specErr := LoadCleanupSpec(cfg.SpecDir)
		if specErr != nil {
			// A broken spec disables polish rather than the backend:
			// §5 says a failed cleanup returns the raw transcript.
			s.cleanup = nil
			s.blockers = append(s.blockers, Blocker{
				Code:    "cleanup_spec_unavailable",
				Message: "transcript cleanup is off: " + specErr.Error(),
			})
		} else {
			s.spec = spec
		}
	}
	if s.stt == nil {
		s.blockers = append(s.blockers, Blocker{
			Code:    "no_stt",
			Message: "no speech recognizer is configured",
		})
	}
	if len(s.keys) == 0 {
		s.blockers = append(s.blockers, Blocker{
			Code:    "no_credentials",
			Message: "no API key is configured; every operation is refused",
		})
	}
	return s, nil
}

// Blockers reports why the backend is not fully usable. Empty is good.
func (s *Server) Blockers() []Blocker { return append([]Blocker{}, s.blockers...) }

// CleanupSpecID is `caret-cleanup/1 <digest>` when a polish lane is
// configured, or the empty string.
func (s *Server) CleanupSpecID() string {
	if s.spec == nil {
		return ""
	}
	return s.spec.SpecID()
}

func (s *Server) routeEnabled(route string) bool {
	switch route {
	case RouteDictate:
		return s.stt != nil
	case RouteAsk:
		return s.agent != nil
	case RouteImagine:
		return s.image != nil
	default:
		return false
	}
}

// Routes lists the WebSocket routes this backend actually serves, in
// protocol order. Operators see it at startup; /health reports the same
// truth as capabilities.
func (s *Server) Routes() []string {
	var routes []string
	for _, route := range []string{RouteDictate, RouteAsk, RouteImagine} {
		if s.routeEnabled(route) {
			routes = append(routes, "/"+route)
		}
	}
	if len(routes) == 0 {
		return []string{"(none — /health only)"}
	}
	return routes
}

func (s *Server) status() string {
	switch {
	case s.stt == nil, len(s.keys) == 0:
		return "not_ready"
	case len(s.blockers) > 0:
		return "degraded"
	default:
		return "ok"
	}
}

// Handler is the whole HTTP surface: /health and the three operation
// routes, mounted at the root. Serve it under a path prefix and the
// prefix becomes the client's base URL.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", s.handleHealth)
	for _, route := range []string{RouteDictate, RouteAsk, RouteImagine} {
		route := route
		mux.HandleFunc("/"+route, func(w http.ResponseWriter, r *http.Request) {
			s.handleRoute(route, w, r)
		})
	}
	return mux
}

// ----------------------------------------------------------------- health

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	presented, valid := s.checkAuth(r.Header.Get("Authorization"))
	served := func(route string) bool { return s.routeEnabled(route) }
	partial := func(route string) bool { return s.stt != nil && s.stt.Streaming() && served(route) }

	body := map[string]any{
		"protocol": ProtocolName,
		"status":   s.status(),
		"service":  s.cfg.Service,
		"version":  s.cfg.Version,
		"time":     time.Now().UTC().Format(time.RFC3339),
		"auth":     authReport(presented, valid),
		"capabilities": map[string]any{
			"dictate": served(RouteDictate),
			"ask":     served(RouteAsk),
			"imagine": served(RouteImagine),
			"text_input": map[string]bool{
				"dictate": served(RouteDictate),
				"ask":     served(RouteAsk),
				"imagine": served(RouteImagine),
			},
			"partials": map[string]bool{
				"dictate": partial(RouteDictate),
				"ask":     partial(RouteAsk),
				"imagine": partial(RouteImagine),
			},
			"vocabulary": s.vocabularyCapable(),
		},
		"limits": map[string]int{
			"max_audio_seconds":      s.cfg.MaxAudioSeconds,
			"max_frame_bytes":        s.cfg.MaxFrameBytes,
			"max_text_chars":         s.cfg.MaxTextChars,
			"max_vocabulary_entries": s.cfg.MaxVocabularyEntries,
		},
		"blockers": s.blockersJSON(),
	}
	// §10 blesses publishing which cleanup wording you run — the digest,
	// never the prompt.
	if s.spec != nil {
		body["cleanup"] = map[string]string{"spec": CleanupSpecName, "digest": s.spec.Digest}
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	if r.Method == http.MethodHead {
		return
	}
	_ = json.NewEncoder(w).Encode(body)
}

// vocabularyCapable is true only when something downstream actually
// consumes the list. Advertising the capability and dropping the list is
// forbidden by §7, so this is computed, never configured.
func (s *Server) vocabularyCapable() bool {
	if s.stt != nil && s.stt.UsesVocabulary() {
		return true
	}
	return s.cleanup != nil && s.spec != nil
}

func (s *Server) blockersJSON() []Blocker {
	if len(s.blockers) == 0 {
		return []Blocker{}
	}
	return s.blockers
}

func authReport(presented, valid bool) map[string]any {
	if !presented {
		return map[string]any{"presented": false, "valid": nil}
	}
	return map[string]any{"presented": true, "valid": valid}
}

// checkAuth reports whether a credential was presented and whether it is
// one of ours. The comparison is over sha256 digests so it is constant
// time in both content and length.
func (s *Server) checkAuth(header string) (presented, valid bool) {
	token := strings.TrimSpace(header)
	if token == "" {
		return false, false
	}
	if len(token) > 7 && strings.EqualFold(token[:7], "bearer ") {
		token = strings.TrimSpace(token[7:])
	}
	if token == "" {
		return false, false
	}
	sum := sha256.Sum256([]byte(token))
	match := 0
	for _, key := range s.keys {
		match |= subtle.ConstantTimeCompare(sum[:], key[:])
	}
	return true, match == 1
}

// ----------------------------------------------------------------- routes

func (s *Server) handleRoute(route string, w http.ResponseWriter, r *http.Request) {
	conn, err := Upgrade(w, r)
	if err != nil {
		return // Upgrade has already answered with an HTTP status.
	}
	sess := &session{
		server:    s,
		conn:      conn,
		route:     route,
		opID:      newID("op"),
		startedAt: time.Now(),
	}
	defer func() {
		if rec := recover(); rec != nil {
			s.log.Printf("op=%s route=%s panic recovered", sess.opID, route)
			sess.fail(opErr(ErrInternalError, "internal error"))
		}
	}()
	sess.run(r)
}

func newID(prefix string) string {
	var raw [6]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return prefix + "_" + fmt.Sprint(time.Now().UnixNano())
	}
	return prefix + "_" + hex.EncodeToString(raw[:])
}

// ---------------------------------------------------------- result cache

// resultCache is the whole of §8's idempotency: a successful terminal
// result, keyed by route and client_request_id, for a few minutes.
// Failures are never cached — retrying a failure is the point of
// retrying.
type resultCache struct {
	ttl     time.Duration
	mu      sync.Mutex
	entries map[string]cacheEntry
}

type cacheEntry struct {
	requestID string
	result    any
	audio     *AudioSpec
	expires   time.Time
}

func newResultCache(ttl time.Duration) *resultCache {
	return &resultCache{ttl: ttl, entries: map[string]cacheEntry{}}
}

func (c *resultCache) key(route, clientRequestID string) string {
	return route + "\x00" + clientRequestID
}

func (c *resultCache) get(route, clientRequestID string) (cacheEntry, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	entry, ok := c.entries[c.key(route, clientRequestID)]
	if !ok || time.Now().After(entry.expires) {
		return cacheEntry{}, false
	}
	return entry, true
}

func (c *resultCache) put(route, clientRequestID, requestID string, result any, audio *AudioSpec) {
	if clientRequestID == "" {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	now := time.Now()
	for key, entry := range c.entries {
		if now.After(entry.expires) {
			delete(c.entries, key)
		}
	}
	c.entries[c.key(route, clientRequestID)] = cacheEntry{
		requestID: requestID,
		result:    result,
		audio:     audio,
		expires:   now.Add(c.ttl),
	}
}
