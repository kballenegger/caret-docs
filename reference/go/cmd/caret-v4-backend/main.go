// Command caret-v4-backend serves the caret/v4 protocol.
//
//	go run ./cmd/caret-v4-backend -addr 127.0.0.1:8080 -keys dev-key
//
// With no lane flags it serves /dictate with the built-in loopback
// providers: no model, no network, no API keys of anyone's. That is
// enough to point the keyboard at, to run the conformance checker
// against, and to develop a client against on a plane. Wire real
// providers with -stt, -agent, -image, and -cleanup; see the package
// README for the lane grammar.
//
// TLS: pass -tls-cert and -tls-key to serve HTTPS directly, or leave
// them off and terminate TLS in front. Clients require https/wss, so
// plain HTTP is for loopback development only.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	caretv4 "caret.dev/reference/v4"
)

func envOr(name, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		return v
	}
	return fallback
}

func envInt(name string, fallback int) int {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return fallback
}

func main() {
	var (
		addr     = flag.String("addr", envOr("CARET_ADDR", "127.0.0.1:8080"), "listen address")
		keys     = flag.String("keys", envOr("CARET_API_KEYS", ""), "comma-separated bearer credentials; empty means the backend reports not_ready and refuses every operation")
		sttSpec  = flag.String("stt", envOr("CARET_STT", "loopback"), "speech-to-text lane: loopback, command:<argv>, or an https URL")
		agent    = flag.String("agent", envOr("CARET_AGENT", ""), "agent lane for /ask; empty turns /ask off")
		image    = flag.String("image", envOr("CARET_IMAGE", ""), "image lane for /imagine; empty turns /imagine off")
		cleanup  = flag.String("cleanup", envOr("CARET_CLEANUP", ""), "cleanup lane; empty means dictation is returned unpolished")
		service  = flag.String("service", envOr("CARET_SERVICE", "caret-v4-reference-go"), "service name reported on /health")
		specDir  = flag.String("cleanup-spec-dir", envOr("CARET_CLEANUP_SPEC_DIR", ""), "path to spec/cleanup/v1; found automatically when run from the repository")
		certFile = flag.String("tls-cert", envOr("CARET_TLS_CERT", ""), "TLS certificate file; serves HTTPS when set with -tls-key")
		keyFile  = flag.String("tls-key", envOr("CARET_TLS_KEY", ""), "TLS private key file")
		maxAudio = flag.Int("max-audio-seconds", envInt("CARET_MAX_AUDIO_SECONDS", caretv4.DefaultMaxAudioSeconds), "longest single operation, in seconds")
		maxFrame = flag.Int("max-frame-bytes", envInt("CARET_MAX_FRAME_BYTES", caretv4.DefaultMaxFrameBytes), "largest accepted binary audio frame")
		timeout  = flag.Duration("lane-timeout", 120*time.Second, "how long a provider may take before the operation fails")
	)
	flag.Parse()

	logger := log.New(os.Stderr, "", log.LstdFlags|log.LUTC)

	var credentials []string
	for _, k := range strings.Split(*keys, ",") {
		if k = strings.TrimSpace(k); k != "" {
			credentials = append(credentials, k)
		}
	}
	if len(credentials) == 0 {
		logger.Print("no credentials configured: /health will report not_ready and every operation will be refused. Pass -keys or set CARET_API_KEYS")
	}

	server, err := caretv4.New(caretv4.Config{
		Service:         *service,
		APIKeys:         credentials,
		STT:             *sttSpec,
		Agent:           *agent,
		Image:           *image,
		Cleanup:         *cleanup,
		MaxAudioSeconds: *maxAudio,
		MaxFrameBytes:   *maxFrame,
		LaneTimeout:     *timeout,
		SpecDir:         *specDir,
		Logger:          logger,
	})
	if err != nil {
		logger.Fatalf("cannot start: %v", err)
	}

	httpServer := &http.Server{
		Addr:              *addr,
		Handler:           server.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
		// No WriteTimeout: a hijacked WebSocket outlives any deadline
		// the HTTP server would impose, and the session applies its
		// own per-frame deadlines.
	}

	scheme := "http"
	if *certFile != "" && *keyFile != "" {
		scheme = "https"
	}
	logger.Printf("caret/v4 reference backend on %s://%s — service %q", scheme, *addr, *service)
	logger.Printf("routes: %s", strings.Join(server.Routes(), " "))
	if scheme == "http" {
		logger.Print("serving plain HTTP: a conforming client requires https/wss, so put TLS in front before this faces a keyboard")
	}

	errs := make(chan error, 1)
	go func() {
		if scheme == "https" {
			errs <- httpServer.ListenAndServeTLS(*certFile, *keyFile)
			return
		}
		errs <- httpServer.ListenAndServe()
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	select {
	case err := <-errs:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Fatalf("listen failed: %v", err)
		}
	case sig := <-stop:
		logger.Printf("%v — shutting down", sig)
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := httpServer.Shutdown(ctx); err != nil {
			fmt.Fprintf(os.Stderr, "shutdown: %v\n", err)
		}
	}
}
