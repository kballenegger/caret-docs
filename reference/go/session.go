package caretv4

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"strings"
	"time"
)

// The shared live-audio lifecycle, once.
//
// §4 of the protocol says the three routes differ only in their
// finalizer and their terminal result type. This file is that claim made
// structural: one state machine reads start, streams audio, verifies the
// finalize totals, and hands off to exactly one route-specific finish
// function. If a rule lives here, it applies to /dictate, /ask, and
// /imagine identically, because there is nowhere else for it to live.

type session struct {
	server    *Server
	conn      *Conn
	route     string
	opID      string
	requestID string
	startedAt time.Time

	start      *StartFrame
	textInput  bool
	inputText  string
	vocabulary []string

	audio      []byte
	frames     int
	stream     STTStream
	partialSeq int

	terminated bool
}

func (s *session) run(r *http.Request) {
	defer func() { _ = s.conn.Close(1000, "") }()

	if _, valid := s.server.checkAuth(r.Header.Get("Authorization")); !valid {
		s.fail(opErr(ErrUnauthorized, "missing or invalid credential"))
		return
	}
	if !s.server.routeEnabled(s.route) {
		s.fail(opErr(ErrNotSupported, "this backend does not serve /"+s.route))
		return
	}
	if err := s.readStart(); err != nil {
		s.fail(err)
		return
	}

	// §8 idempotency: a replayed client_request_id gets ready and then
	// the cached result immediately, so a retry after a dropped
	// connection is not a second bill.
	if entry, ok := s.server.cache.get(s.route, s.start.ClientRequestID); ok {
		s.sendReady()
		s.requestID = entry.requestID
		s.sendResult(entry.result, entry.audio)
		return
	}

	s.sendReady()

	if !s.textInput {
		if err := s.openStream(); err != nil {
			s.fail(err)
			return
		}
	}
	final, err := s.readInput()
	if err != nil {
		s.abortStream()
		s.fail(err)
		return
	}
	if final == nil {
		s.abortStream() // cancelled: no terminal event, close 1000
		return
	}
	if err := s.checkFinalize(final); err != nil {
		s.abortStream()
		s.fail(err)
		return
	}
	s.finish(final)
}

// ------------------------------------------------------------------ start

func (s *session) readStart() *OpError {
	_ = s.conn.SetReadDeadline(time.Now().Add(startDeadline))
	msg, err := s.conn.ReadMessage()
	if err != nil {
		if errors.Is(err, os.ErrDeadlineExceeded) {
			return opErr(ErrTimeout, "no start frame within 10 seconds")
		}
		return nil // the peer went away; nothing to answer
	}
	if msg.Binary {
		return opErr(ErrProtocolError, "audio arrived before start")
	}
	kind, err := controlType(msg.Data)
	if err != nil {
		return opErr(ErrProtocolError, "the first frame is not JSON")
	}
	if kind != "start" {
		return opErr(ErrProtocolError, "the first frame must be start")
	}
	var frame StartFrame
	if err := json.Unmarshal(msg.Data, &frame); err != nil {
		return opErr(ErrProtocolError, "malformed start frame")
	}
	if frame.Protocol == nil || *frame.Protocol != ProtocolVersion {
		return opErr(ErrProtocolError, "this backend speaks protocol 4")
	}
	if frame.ClientRequestID == "" || len(frame.ClientRequestID) > 128 {
		return opErr(ErrBadRequest, "client_request_id is required and at most 128 characters")
	}
	if len([]rune(frame.LanguageHint)) > 16 {
		return opErr(ErrBadRequest, "language_hint is at most 16 characters")
	}
	if len([]rune(frame.AppHint)) > 200 {
		return opErr(ErrBadRequest, "app_hint is at most 200 characters")
	}
	if frame.Input == nil {
		return opErr(ErrBadRequest, "start needs an input object")
	}
	switch frame.Input.Type {
	case "audio":
		if frame.Input.Codec != "" && frame.Input.Codec != "pcm16" {
			return opErr(ErrBadRequest, "the only codec is pcm16")
		}
		if frame.Input.SampleRateHz != 0 && frame.Input.SampleRateHz != 16000 {
			return opErr(ErrBadRequest, "audio must be 16 kHz")
		}
		if frame.Input.Channels != 0 && frame.Input.Channels != 1 {
			return opErr(ErrBadRequest, "audio must be mono")
		}
	case "text":
		text := frame.Input.Text
		if n := len([]rune(text)); n < 1 || n > s.server.cfg.MaxTextChars {
			return opErr(ErrBadRequest, "text input is 1 to advertised max_text_chars characters")
		}
		s.textInput = true
		s.inputText = text
	default:
		return opErr(ErrBadRequest, "input.type is audio or text")
	}
	vocabulary, verr := normalizeVocabulary(frame.Vocabulary, s.server.cfg.MaxVocabularyEntries)
	if verr != nil {
		var oe *OpError
		if errors.As(verr, &oe) {
			return oe
		}
		return opErr(ErrBadRequest, "invalid vocabulary")
	}
	s.vocabulary = vocabulary
	s.start = &frame
	return nil
}

func (s *session) sendReady() {
	stt := any(nil)
	if !s.textInput {
		if s.server.stt != nil && s.server.stt.Streaming() {
			stt = "streaming"
		} else {
			stt = "buffered"
		}
	}
	s.send(map[string]any{
		"event":    "ready",
		"protocol": ProtocolVersion,
		"op_id":    s.opID,
		"stt":      stt,
	})
}

// openStream asks the speech lane for a live recognizer. A provider
// without one is not a failure: §8 says the operation lives on, audio is
// buffered, and the transcript is produced at finalize.
func (s *session) openStream() *OpError {
	if s.server.stt == nil {
		return nil
	}
	stream, err := s.server.stt.Open(context.Background(), s.sttOptions(), s.emitPartial)
	if err != nil {
		if errors.Is(err, ErrNoStreaming) {
			return nil
		}
		s.server.log.Printf("op=%s route=%s streaming recognizer unavailable", s.opID, s.route)
		return nil
	}
	s.stream = stream
	return nil
}

func (s *session) sttOptions() STTOptions {
	return STTOptions{
		Vocabulary:   s.vocabulary,
		LanguageHint: s.start.LanguageHint,
		SampleRateHz: 16000,
	}
}

func (s *session) emitPartial(text string) {
	if s.terminated || text == "" {
		return
	}
	s.partialSeq++
	s.send(map[string]any{
		"event":  "partial",
		"op_id":  s.opID,
		"seq":    s.partialSeq,
		"frames": s.frames,
		"bytes":  len(s.audio),
		"text":   text,
	})
}

// ------------------------------------------------------------------ input

// readInput runs until finalize (returned), cancel (nil, nil), or a
// terminal error.
func (s *session) readInput() (*FinalizeFrame, *OpError) {
	for {
		_ = s.conn.SetReadDeadline(time.Now().Add(frameGap))
		msg, err := s.conn.ReadMessage()
		if err != nil {
			if errors.Is(err, os.ErrDeadlineExceeded) {
				return nil, opErr(ErrTimeout, "no audio for 60 seconds")
			}
			// The socket died before the terminal event. §8: the server
			// discards the operation; the client replays it.
			s.terminated = true
			return nil, nil
		}
		if msg.Binary {
			if oe := s.consumeAudio(msg.Data); oe != nil {
				return nil, oe
			}
			continue
		}
		kind, jerr := controlType(msg.Data)
		if jerr != nil {
			return nil, opErr(ErrProtocolError, "control frame is not JSON")
		}
		switch kind {
		case "finalize":
			var frame FinalizeFrame
			if err := json.Unmarshal(msg.Data, &frame); err != nil {
				return nil, opErr(ErrProtocolError, "malformed finalize frame")
			}
			return &frame, nil
		case "cancel":
			s.terminated = true
			return nil, nil
		case "start":
			return nil, opErr(ErrProtocolError, "start arrived twice")
		default:
			return nil, opErr(ErrProtocolError, "unknown control frame")
		}
	}
}

func (s *session) consumeAudio(chunk []byte) *OpError {
	if s.textInput {
		return opErr(ErrProtocolError, "binary frames are not allowed with text input")
	}
	if len(chunk) > s.server.cfg.MaxFrameBytes {
		return opErr(ErrBadRequest, "audio frame exceeds the advertised max_frame_bytes")
	}
	if len(chunk) == 0 {
		return nil // empty frames are ignored, not errors
	}
	if len(s.audio)+len(chunk) > s.server.cfg.MaxAudioSeconds*bytesPerSecond {
		return opErr(ErrAudioTooLong, "audio exceeded the advertised max_audio_seconds")
	}
	s.frames++
	s.audio = append(s.audio, chunk...)
	if s.stream != nil {
		if err := s.stream.Write(chunk); err != nil {
			// §8: streaming failure is not operation failure. Partials
			// stop; the buffered audio still gets transcribed.
			s.server.log.Printf("op=%s route=%s streaming recognizer died mid-stream", s.opID, s.route)
			s.stream.Abort()
			s.stream = nil
		}
	}
	return nil
}

// checkFinalize is the V4 reliability contract: the client's own totals
// must match what the server received, or the operation is retried
// rather than transcribed from silently truncated audio.
func (s *session) checkFinalize(frame *FinalizeFrame) *OpError {
	if s.textInput {
		if frame.Audio != nil {
			return opErr(ErrBadRequest, "finalize must not carry audio totals for text input")
		}
		return nil
	}
	if frame.Audio == nil {
		return opErr(ErrBadRequest, "finalize needs the audio totals")
	}
	if frame.Audio.Frames != s.frames || frame.Audio.Bytes != int64(len(s.audio)) {
		s.server.log.Printf("op=%s route=%s audio mismatch: client %d/%d, server %d/%d",
			s.opID, s.route, frame.Audio.Frames, frame.Audio.Bytes, s.frames, len(s.audio))
		return opErr(ErrAudioIncomplete, "the audio the server received does not match what the client sent")
	}
	if s.durationMS() < 300 {
		return opErr(ErrAudioTooShort, "that was too short to transcribe")
	}
	return nil
}

func (s *session) durationMS() int64 {
	return int64(len(s.audio)) * 1000 / bytesPerSecond
}

func (s *session) consumedAudio() *AudioSpec {
	if s.textInput {
		return nil
	}
	return &AudioSpec{Frames: s.frames, Bytes: int64(len(s.audio)), DurationMS: s.durationMS()}
}

// ------------------------------------------------------------- finalizers

func (s *session) finish(frame *FinalizeFrame) {
	stop := s.startProgress()
	defer stop()

	var (
		result any
		oe     *OpError
	)
	switch s.route {
	case RouteDictate:
		result, oe = s.finishDictate(frame)
	case RouteAsk:
		result, oe = s.finishAsk(frame)
	case RouteImagine:
		result, oe = s.finishImagine(frame)
	default:
		oe = opErr(ErrNotSupported, "unknown route")
	}
	stop()
	if oe != nil {
		s.fail(oe)
		return
	}
	s.requestID = newID("req")
	s.server.cache.put(s.route, s.start.ClientRequestID, s.requestID, result, s.consumedAudio())
	s.sendResult(result, s.consumedAudio())
}

// startProgress keeps the socket honest while a lane works. §4 requires
// a progress event or a ping at least every 20 seconds; this sends one
// every 5.
func (s *session) startProgress() func() {
	done := make(chan struct{})
	stopped := false
	go func() {
		ticker := time.NewTicker(progressEvery)
		defer ticker.Stop()
		began := time.Now()
		for {
			select {
			case <-done:
				return
			case <-ticker.C:
				s.send(map[string]any{
					"event":      "progress",
					"op_id":      s.opID,
					"stage":      s.stage(),
					"elapsed_ms": time.Since(began).Milliseconds(),
				})
			}
		}
	}()
	return func() {
		if !stopped {
			stopped = true
			close(done)
		}
	}
}

func (s *session) stage() string {
	switch s.route {
	case RouteImagine:
		return "generating"
	case RouteAsk:
		return "answering"
	default:
		return "transcribing"
	}
}

func (s *session) finishDictate(frame *FinalizeFrame) (any, *OpError) {
	raw, route, oe := s.transcript()
	if oe != nil {
		return nil, oe
	}
	polish := frame.Polish == nil || *frame.Polish
	text, applied := raw, false
	if polish {
		if polished, ok := s.polish(raw); ok {
			text, applied = polished, true
		}
	}
	result := DictationResult{
		Type:          "dictation",
		Text:          text,
		RawTranscript: raw,
		PolishApplied: applied,
		STTRoute:      route,
	}
	return result, nil
}

// polish runs caret-cleanup/1 through the configured lane. It is
// best-effort by contract: every failure returns the raw transcript, and
// polish_applied says which happened.
func (s *session) polish(raw string) (string, bool) {
	if s.server.cleanup == nil || s.server.spec == nil || strings.TrimSpace(raw) == "" {
		return raw, false
	}
	framing := s.server.spec.SystemPrompt(s.vocabulary)
	ctx, cancel := context.WithTimeout(context.Background(), s.server.cfg.LaneTimeout)
	defer cancel()
	text, err := s.server.cleanup.Polish(ctx, framing, raw)
	if err != nil || strings.TrimSpace(text) == "" {
		if err != nil {
			s.server.log.Printf("op=%s cleanup failed, returning the raw transcript", s.opID)
		}
		return raw, false
	}
	return text, true
}

func (s *session) finishAsk(frame *FinalizeFrame) (any, *OpError) {
	if len([]rune(frame.VisibleText)) > s.server.cfg.MaxTextChars {
		return nil, opErr(ErrBadRequest, "visible_text is too long")
	}
	prompt, transcript, oe := s.promptText()
	if oe != nil {
		return nil, oe
	}
	ctx, cancel := context.WithTimeout(context.Background(), s.server.cfg.LaneTimeout)
	defer cancel()
	text, err := s.server.agent.Respond(ctx, prompt, frame.VisibleText, s.vocabulary)
	if err != nil || strings.TrimSpace(text) == "" {
		s.server.log.Printf("op=%s route=ask agent lane failed", s.opID)
		return nil, opErr(ErrGenerationFailed, "the agent could not answer")
	}
	return MessageResult{Type: "message", Text: text, Transcript: transcript}, nil
}

var (
	validAspectRatios = map[string]bool{"1:1": true, "3:2": true, "2:3": true}
	validQualities    = map[string]bool{"low": true, "medium": true, "high": true}
)

func (s *session) finishImagine(frame *FinalizeFrame) (any, *OpError) {
	aspect := frame.AspectRatio
	if aspect == "" {
		aspect = "1:1"
	}
	if !validAspectRatios[aspect] {
		return nil, opErr(ErrBadRequest, "aspect_ratio is 1:1, 3:2, or 2:3")
	}
	quality := frame.Quality
	if quality == "" {
		quality = "high"
	}
	if !validQualities[quality] {
		return nil, opErr(ErrBadRequest, "quality is low, medium, or high")
	}
	prompt, transcript, oe := s.promptText()
	if oe != nil {
		return nil, oe
	}
	ctx, cancel := context.WithTimeout(context.Background(), s.server.cfg.LaneTimeout)
	defer cancel()
	mime, data, err := s.server.image.Generate(ctx, prompt, aspect, quality)
	if err != nil || len(data) == 0 {
		s.server.log.Printf("op=%s route=imagine image lane failed", s.opID)
		return nil, opErr(ErrGenerationFailed, "the image could not be generated")
	}
	sum := sha256.Sum256(data)
	return ImageResult{
		Type:       "image",
		MimeType:   mime,
		ByteLength: len(data),
		SHA256:     hex.EncodeToString(sum[:]),
		DataBase64: base64.StdEncoding.EncodeToString(data),
		Transcript: transcript,
		Provider:   s.server.image.Name(),
	}, nil
}

// promptText is the instruction the route works from, plus the
// transcript to echo back when the input was speech.
func (s *session) promptText() (prompt, transcript string, oe *OpError) {
	if s.textInput {
		return s.inputText, "", nil
	}
	text, _, oe := s.transcript()
	if oe != nil {
		return "", "", oe
	}
	return text, text, nil
}

// transcript resolves the audio to words: the live recognizer's flush
// when there was one, the batch route otherwise. An empty transcript
// from a healthy route is no_speech_detected — silence is an answer, not
// an error, and never a reason to fall back.
func (s *session) transcript() (string, string, *OpError) {
	if s.textInput {
		return s.inputText, "", nil
	}
	if s.server.stt == nil {
		return "", "", opErr(ErrNotSupported, "this backend has no speech recognizer")
	}
	route := "fallback"
	var text string
	if s.stream != nil {
		flushed, err := s.stream.Finish()
		s.stream = nil
		if err == nil {
			text, route = flushed, "stream"
		} else {
			s.server.log.Printf("op=%s streaming flush failed, falling back to batch", s.opID)
		}
	}
	if route == "fallback" {
		ctx, cancel := context.WithTimeout(context.Background(), s.server.cfg.LaneTimeout)
		defer cancel()
		batch, err := s.server.stt.Transcribe(ctx, s.audio, s.sttOptions())
		if err != nil {
			s.server.log.Printf("op=%s every transcription route failed", s.opID)
			return "", "", opErr(ErrTranscriptionFailed, "speech recognition is unavailable")
		}
		text = batch
	}
	if strings.TrimSpace(text) == "" {
		return "", "", opErr(ErrNoSpeechDetected, "no speech was detected")
	}
	return text, route, nil
}

func (s *session) abortStream() {
	if s.stream != nil {
		s.stream.Abort()
		s.stream = nil
	}
}

// ------------------------------------------------------------- terminals

func (s *session) send(event map[string]any) {
	if err := s.conn.WriteText(eventJSON(event)); err != nil {
		s.terminated = true
	}
}

func (s *session) sendResult(result any, audio *AudioSpec) {
	if s.terminated {
		return
	}
	s.terminated = true
	if s.requestID == "" {
		s.requestID = newID("req")
	}
	event := map[string]any{
		"event":      "result",
		"op_id":      s.opID,
		"request_id": s.requestID,
		"result":     result,
	}
	if audio != nil {
		event["audio"] = audio
	}
	s.send(event)
	s.server.log.Printf("op=%s route=%s ok in %dms", s.opID, s.route, time.Since(s.startedAt).Milliseconds())
	_ = s.conn.Close(1000, "")
}

// fail sends the one terminal error event and closes with its mapped
// code. A nil error means the peer vanished: there is nobody to tell.
func (s *session) fail(oe *OpError) {
	if oe == nil || s.terminated {
		return
	}
	s.terminated = true
	if s.requestID == "" {
		s.requestID = newID("req")
	}
	s.send(map[string]any{
		"event":      "error",
		"op_id":      s.opID,
		"request_id": s.requestID,
		"code":       oe.Code,
		"message":    oe.Message,
		"retryable":  RetryableFor(oe.Code),
	})
	s.server.log.Printf("op=%s route=%s error=%s in %dms", s.opID, s.route, oe.Code, time.Since(s.startedAt).Milliseconds())
	_ = s.conn.Close(CloseCodeFor(oe.Code), oe.Code)
}
