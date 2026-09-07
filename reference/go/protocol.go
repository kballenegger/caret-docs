package caretv4

import (
	"encoding/json"
	"fmt"
	"strings"
)

// Protocol identifiers. `ProtocolName` is what /health reports and how a
// client recognizes a V4 backend at an arbitrary URL; `ProtocolVersion`
// is the in-band integer every start frame carries.
const (
	ProtocolName    = "caret/v4"
	ProtocolVersion = 4
)

// Routes. Dictate is mandatory; the other two are optional and are
// advertised only when a lane resolves for them.
const (
	RouteDictate = "dictate"
	RouteAsk     = "ask"
	RouteImagine = "imagine"
)

// Error codes from §9 of the protocol. The list is append-only: a client
// meeting an unknown code falls back to the retryable flag.
const (
	ErrUnauthorized        = "unauthorized"
	ErrRateLimited         = "rate_limited"
	ErrNotSupported        = "not_supported"
	ErrProtocolError       = "protocol_error"
	ErrBadRequest          = "bad_request"
	ErrTimeout             = "timeout"
	ErrAudioIncomplete     = "audio_incomplete"
	ErrAudioTooLong        = "audio_too_long"
	ErrAudioTooShort       = "audio_too_short"
	ErrNoSpeechDetected    = "no_speech_detected"
	ErrTranscriptionFailed = "transcription_failed"
	ErrGenerationFailed    = "generation_failed"
	ErrInternalError       = "internal_error"
)

// closeCodes maps every error code to its WebSocket close code. A code
// missing from this table is a bug, not a default: CloseCodeFor panics
// rather than inventing 4500 for a typo.
var closeCodes = map[string]uint16{
	ErrUnauthorized:        4401,
	ErrRateLimited:         4429,
	ErrNotSupported:        4404,
	ErrProtocolError:       4400,
	ErrBadRequest:          4400,
	ErrTimeout:             4408,
	ErrAudioIncomplete:     4409,
	ErrAudioTooLong:        4413,
	ErrAudioTooShort:       4422,
	ErrNoSpeechDetected:    4422,
	ErrTranscriptionFailed: 4503,
	ErrGenerationFailed:    4503,
	ErrInternalError:       4500,
}

// retryableCodes is the truthful value of the flag per code. It is the
// only field a future client can rely on when it meets a code it has
// never heard of, so it is a table, not a guess at the call site.
var retryableCodes = map[string]bool{
	ErrUnauthorized:        false,
	ErrRateLimited:         true,
	ErrNotSupported:        false,
	ErrProtocolError:       false,
	ErrBadRequest:          false,
	ErrTimeout:             true,
	ErrAudioIncomplete:     true,
	ErrAudioTooLong:        false,
	ErrAudioTooShort:       false,
	ErrNoSpeechDetected:    false,
	ErrTranscriptionFailed: true,
	ErrGenerationFailed:    true,
	ErrInternalError:       true,
}

// CloseCodeFor returns the WebSocket close code an error code maps to.
func CloseCodeFor(code string) uint16 {
	c, ok := closeCodes[code]
	if !ok {
		panic(fmt.Sprintf("caretv4: no close code for %q", code))
	}
	return c
}

// RetryableFor reports whether retrying the operation could plausibly
// succeed.
func RetryableFor(code string) bool { return retryableCodes[code] }

// OpError is a terminal error on its way to the client: a code from the
// table, a short message the user sees verbatim, and nothing internal.
type OpError struct {
	Code    string
	Message string
}

func (e *OpError) Error() string { return e.Code + ": " + e.Message }

func opErr(code, message string) *OpError { return &OpError{Code: code, Message: message} }

// ---------------------------------------------------------------- frames

// AudioSpec is the audio accounting that appears in finalize (client
// totals) and in the result event (what the server actually consumed).
type AudioSpec struct {
	Frames     int   `json:"frames"`
	Bytes      int64 `json:"bytes"`
	DurationMS int64 `json:"duration_ms"`
}

// Input is the discriminated input object on the start frame: exactly
// two shapes, audio and text.
type Input struct {
	Type         string `json:"type"`
	Codec        string `json:"codec,omitempty"`
	SampleRateHz int    `json:"sample_rate_hz,omitempty"`
	Channels     int    `json:"channels,omitempty"`
	Text         string `json:"text,omitempty"`
}

// StartFrame is the client's first text frame, identical on every route.
type StartFrame struct {
	Type            string   `json:"type"`
	Protocol        *int     `json:"protocol"`
	ClientRequestID string   `json:"client_request_id"`
	Input           *Input   `json:"input"`
	Vocabulary      []string `json:"vocabulary,omitempty"`
	LanguageHint    string   `json:"language_hint,omitempty"`
	AppHint         string   `json:"app_hint,omitempty"`
}

// FinalizeFrame ends input. The common half is the client's own audio
// accounting; the rest is the route's own finalizer (§5).
type FinalizeFrame struct {
	Type  string     `json:"type"`
	Audio *AudioSpec `json:"audio"`

	// /dictate
	Polish *bool `json:"polish,omitempty"`
	// /ask
	VisibleText string `json:"visible_text,omitempty"`
	// /imagine
	AspectRatio string `json:"aspect_ratio,omitempty"`
	Quality     string `json:"quality,omitempty"`
}

// controlType peeks at the "type" field of a control frame without
// committing to a shape, so an unknown type is a protocol_error rather
// than a decode failure.
func controlType(data []byte) (string, error) {
	var probe struct {
		Type string `json:"type"`
	}
	if err := json.Unmarshal(data, &probe); err != nil {
		return "", err
	}
	return probe.Type, nil
}

// ---------------------------------------------------------------- events

func eventJSON(fields map[string]any) string {
	b, err := json.Marshal(fields)
	if err != nil {
		// Every value here is protocol-shaped; a failure is a bug.
		panic(fmt.Sprintf("caretv4: cannot encode event: %v", err))
	}
	return string(b)
}

// DictationResult is the /dictate terminal result.
type DictationResult struct {
	Type          string `json:"type"`
	Text          string `json:"text"`
	RawTranscript string `json:"raw_transcript"`
	PolishApplied bool   `json:"polish_applied"`
	STTRoute      string `json:"stt_route"`
}

// MessageResult is the /ask terminal result.
type MessageResult struct {
	Type       string `json:"type"`
	Text       string `json:"text"`
	Transcript string `json:"transcript,omitempty"`
}

// ImageResult is the /imagine terminal result. data_base64 is the only
// payload transport in V4 — no URL form, no data-URL form.
type ImageResult struct {
	Type       string `json:"type"`
	MimeType   string `json:"mime_type"`
	ByteLength int    `json:"byte_length"`
	SHA256     string `json:"sha256"`
	DataBase64 string `json:"data_base64"`
	Transcript string `json:"transcript,omitempty"`
	Provider   string `json:"provider,omitempty"`
}

// normalizeVocabulary validates the shape rules of §7 and returns the
// list unchanged when it passes. Earlier entries are higher priority, so
// order is preserved and duplicates are an error rather than a silent
// dedup.
func normalizeVocabulary(entries []string, limit int) ([]string, error) {
	if len(entries) == 0 {
		return nil, nil
	}
	if len(entries) > limit {
		return nil, opErr(ErrBadRequest, fmt.Sprintf("at most %d vocabulary entries", limit))
	}
	// §7 says unique strings of 1-64 characters, with earlier entries
	// higher priority. Surrounding whitespace is a client-side accident
	// rather than a term, so it is trimmed, and a repeat is dropped
	// rather than refused: the list is priority-ordered, so first
	// occurrence wins and the meaning is unchanged. Anything that is
	// still out of range after that is the client's mistake.
	seen := make(map[string]bool, len(entries))
	out := make([]string, 0, len(entries))
	for _, entry := range entries {
		entry = strings.TrimSpace(entry)
		if n := len([]rune(entry)); n < 1 || n > 64 {
			return nil, opErr(ErrBadRequest, "vocabulary entries are 1-64 characters")
		}
		if seen[entry] {
			continue
		}
		seen[entry] = true
		out = append(out, entry)
	}
	return out, nil
}
