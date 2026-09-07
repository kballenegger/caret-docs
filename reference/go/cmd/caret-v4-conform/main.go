// Command caret-v4-conform interrogates a live caret/v4 backend.
//
//	go run ./cmd/caret-v4-conform -url https://caret.example.com -key "$CARET_KEY"
//
// It is a client, not a linter: it opens real WebSocket connections,
// streams real PCM, lies in a finalize frame to see whether the backend
// notices, replays a client_request_id, and holds the backend to its own
// health document. It knows nothing about this repository's server, and
// works against a backend in any language.
//
// Exit 0 means every required check passed. Exit 1 means at least one
// failed. Warnings never fail the run: they mark behaviour the protocol
// permits but a well-behaved backend usually gets right.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	caretv4 "caret.dev/reference/v4"
)

func main() {
	var (
		base     = flag.String("url", strings.TrimSpace(os.Getenv("CARET_BASE_URL")), "backend base URL, without a route (https://host/path)")
		key      = flag.String("key", strings.TrimSpace(os.Getenv("CARET_API_KEY")), "bearer credential")
		insecure = flag.Bool("insecure", false, "allow an http:// base URL (loopback development only)")
		skipTLS  = flag.Bool("insecure-skip-verify", false, "do not verify the TLS certificate (development certificate only)")
		asJSON   = flag.Bool("json", false, "write the report as JSON")
		timeout  = flag.Duration("timeout", 30*time.Second, "per-operation timeout")
	)
	flag.Parse()

	if *base == "" {
		fmt.Fprintln(os.Stderr, "caret-v4-conform: -url is required (or set CARET_BASE_URL)")
		flag.Usage()
		os.Exit(2)
	}

	checker := &caretv4.Checker{
		BaseURL:       *base,
		APIKey:        *key,
		AllowInsecure: *insecure,
		InsecureTLS:   *skipTLS,
		Timeout:       *timeout,
	}
	results, err := checker.Run()
	if err != nil {
		fmt.Fprintf(os.Stderr, "caret-v4-conform: %v\n", err)
		os.Exit(2)
	}

	if *asJSON {
		encoder := json.NewEncoder(os.Stdout)
		encoder.SetIndent("", "  ")
		ok := true
		for _, r := range results {
			if r.Status == caretv4.CheckFail {
				ok = false
			}
		}
		_ = encoder.Encode(map[string]any{"base_url": *base, "pass": ok, "checks": results})
		if !ok {
			os.Exit(1)
		}
		return
	}

	fmt.Printf("caret/v4 conformance — %s\n\n", *base)
	if !caretv4.Report(os.Stdout, results) {
		os.Exit(1)
	}
}
