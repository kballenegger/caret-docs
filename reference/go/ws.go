// Package caretv4 is the caret/v4 reference backend: the transport core,
// the shared live-audio lifecycle, the pluggable lanes, and the
// black-box conformance checker.
//
// This file is the WebSocket layer. RFC 6455 is small enough that
// hand-writing the useful half of it costs less than a dependency: a
// handshake, a frame header, a mask, and the four opcodes the protocol
// uses. Nothing here is general-purpose — it does exactly what caret/v4
// needs (text control frames, binary audio, ping/pong keep-alive, a
// close code) and nothing else. Extensions and continuation-heavy
// senders are handled, compression is not negotiated.
package caretv4

import (
	"bufio"
	"crypto/rand"
	"crypto/sha1"
	"crypto/tls"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// wsGUID is the RFC 6455 handshake constant.
// closeDrain bounds how long Close spends reading the peer out before
// closing the socket. Skipped once the peer's own close frame has
// arrived, since there is then nothing left in flight.
const closeDrain = 500 * time.Millisecond

const wsGUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

// WebSocket opcodes.
const (
	opContinuation = 0x0
	opText         = 0x1
	opBinary       = 0x2
	opClose        = 0x8
	opPing         = 0x9
	opPong         = 0xA
)

// maxPayload caps a single inbound message. The protocol's own frame
// bound is smaller (524 288 bytes by default); this is the transport
// backstop that keeps a hostile length header from allocating a gigabyte.
const maxPayload = 8 << 20

// ErrClosed is returned by ReadMessage when the peer sent a close frame.
// The close code, if any, is carried by CloseError.
var ErrClosed = errors.New("websocket: peer closed")

// CloseError reports the code and reason the peer closed with.
type CloseError struct {
	Code   uint16
	Reason string
}

func (e *CloseError) Error() string {
	return fmt.Sprintf("websocket: closed %d %q", e.Code, e.Reason)
}

func (e *CloseError) Unwrap() error { return ErrClosed }

// Conn is one WebSocket connection. Reads happen on one goroutine;
// writes are serialized by a mutex so a progress ticker and a partial
// emitter can share the socket.
type Conn struct {
	conn     net.Conn
	br       *bufio.Reader
	isClient bool

	wmu      sync.Mutex
	closed   bool
	sawClose bool
	closeMu  sync.Mutex
}

// Message is one complete inbound WebSocket message.
type Message struct {
	Binary bool
	Data   []byte
}

func acceptKey(key string) string {
	sum := sha1.Sum([]byte(key + wsGUID))
	return base64.StdEncoding.EncodeToString(sum[:])
}

// Upgrade completes the server side of the handshake and hijacks the
// connection. On failure it has already written an HTTP error response.
func Upgrade(w http.ResponseWriter, r *http.Request) (*Conn, error) {
	if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") ||
		!headerContainsToken(r.Header.Get("Connection"), "upgrade") {
		http.Error(w, "expected a websocket upgrade", http.StatusBadRequest)
		return nil, errors.New("websocket: not an upgrade request")
	}
	if r.Header.Get("Sec-WebSocket-Version") != "13" {
		w.Header().Set("Sec-WebSocket-Version", "13")
		http.Error(w, "unsupported websocket version", http.StatusUpgradeRequired)
		return nil, errors.New("websocket: bad version")
	}
	key := r.Header.Get("Sec-WebSocket-Key")
	if key == "" {
		http.Error(w, "missing Sec-WebSocket-Key", http.StatusBadRequest)
		return nil, errors.New("websocket: missing key")
	}
	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "connection cannot be hijacked", http.StatusInternalServerError)
		return nil, errors.New("websocket: response writer is not a Hijacker")
	}
	netConn, rw, err := hj.Hijack()
	if err != nil {
		return nil, fmt.Errorf("websocket: hijack: %w", err)
	}
	_ = netConn.SetDeadline(time.Time{})
	resp := "HTTP/1.1 101 Switching Protocols\r\n" +
		"Upgrade: websocket\r\n" +
		"Connection: Upgrade\r\n" +
		"Sec-WebSocket-Accept: " + acceptKey(key) + "\r\n\r\n"
	if _, err := rw.WriteString(resp); err != nil {
		_ = netConn.Close()
		return nil, fmt.Errorf("websocket: write handshake: %w", err)
	}
	if err := rw.Flush(); err != nil {
		_ = netConn.Close()
		return nil, fmt.Errorf("websocket: flush handshake: %w", err)
	}
	return &Conn{conn: netConn, br: rw.Reader}, nil
}

func headerContainsToken(header, token string) bool {
	for _, part := range strings.Split(header, ",") {
		if strings.EqualFold(strings.TrimSpace(part), token) {
			return true
		}
	}
	return false
}

// DialOptions configures the client handshake.
type DialOptions struct {
	Header    http.Header
	TLSConfig *tls.Config
	Timeout   time.Duration
}

// Dial opens a client connection to a ws:// or wss:// URL. It returns
// the handshake response so a caller can inspect a non-101 status —
// caret/v4 backends are allowed to refuse the upgrade with HTTP 401.
func Dial(rawURL string, opts DialOptions) (*Conn, *http.Response, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, nil, fmt.Errorf("websocket: bad url: %w", err)
	}
	secure := false
	switch u.Scheme {
	case "ws":
	case "wss":
		secure = true
	default:
		return nil, nil, fmt.Errorf("websocket: unsupported scheme %q", u.Scheme)
	}
	host := u.Host
	if u.Port() == "" {
		if secure {
			host = net.JoinHostPort(u.Hostname(), "443")
		} else {
			host = net.JoinHostPort(u.Hostname(), "80")
		}
	}
	timeout := opts.Timeout
	if timeout <= 0 {
		timeout = 15 * time.Second
	}
	dialer := &net.Dialer{Timeout: timeout}
	var netConn net.Conn
	if secure {
		cfg := opts.TLSConfig
		if cfg == nil {
			cfg = &tls.Config{}
		}
		cfg = cfg.Clone()
		if cfg.ServerName == "" {
			cfg.ServerName = u.Hostname()
		}
		netConn, err = tls.DialWithDialer(dialer, "tcp", host, cfg)
	} else {
		netConn, err = dialer.Dial("tcp", host)
	}
	if err != nil {
		return nil, nil, fmt.Errorf("websocket: dial: %w", err)
	}

	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		_ = netConn.Close()
		return nil, nil, err
	}
	key := base64.StdEncoding.EncodeToString(raw[:])

	path := u.RequestURI()
	var req strings.Builder
	fmt.Fprintf(&req, "GET %s HTTP/1.1\r\n", path)
	fmt.Fprintf(&req, "Host: %s\r\n", u.Host)
	req.WriteString("Upgrade: websocket\r\nConnection: Upgrade\r\n")
	fmt.Fprintf(&req, "Sec-WebSocket-Key: %s\r\n", key)
	req.WriteString("Sec-WebSocket-Version: 13\r\n")
	for name, values := range opts.Header {
		for _, v := range values {
			fmt.Fprintf(&req, "%s: %s\r\n", name, v)
		}
	}
	req.WriteString("\r\n")

	_ = netConn.SetDeadline(time.Now().Add(timeout))
	if _, err := io.WriteString(netConn, req.String()); err != nil {
		_ = netConn.Close()
		return nil, nil, fmt.Errorf("websocket: write handshake: %w", err)
	}
	br := bufio.NewReader(netConn)
	httpReq, _ := http.NewRequest(http.MethodGet, rawURL, nil)
	resp, err := http.ReadResponse(br, httpReq)
	if err != nil {
		_ = netConn.Close()
		return nil, nil, fmt.Errorf("websocket: read handshake: %w", err)
	}
	if resp.StatusCode != http.StatusSwitchingProtocols {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		resp.Body = io.NopCloser(strings.NewReader(string(body)))
		_ = netConn.Close()
		return nil, resp, fmt.Errorf("websocket: handshake refused with HTTP %d", resp.StatusCode)
	}
	if resp.Header.Get("Sec-WebSocket-Accept") != acceptKey(key) {
		_ = netConn.Close()
		return nil, resp, errors.New("websocket: bad Sec-WebSocket-Accept")
	}
	_ = netConn.SetDeadline(time.Time{})
	return &Conn{conn: netConn, br: br, isClient: true}, resp, nil
}

// SetReadDeadline bounds the next ReadMessage. A zero time clears it.
func (c *Conn) SetReadDeadline(t time.Time) error { return c.conn.SetReadDeadline(t) }

// RemoteAddr reports the peer address, for logging ids only.
func (c *Conn) RemoteAddr() net.Addr { return c.conn.RemoteAddr() }

// ReadMessage returns the next complete text or binary message,
// answering pings and skipping pongs on the way. A close frame from the
// peer surfaces as a *CloseError wrapping ErrClosed.
func (c *Conn) ReadMessage() (Message, error) {
	var assembled []byte
	var msgBinary bool
	inFragment := false
	for {
		fin, opcode, payload, err := c.readFrame()
		if err != nil {
			return Message{}, err
		}
		switch opcode {
		case opPing:
			_ = c.writeFrame(opPong, payload)
			continue
		case opPong:
			continue
		case opClose:
			code := uint16(1005)
			reason := ""
			if len(payload) >= 2 {
				code = binary.BigEndian.Uint16(payload[:2])
				reason = string(payload[2:])
			}
			c.closeMu.Lock()
			c.sawClose = true
			c.closeMu.Unlock()
			return Message{}, &CloseError{Code: code, Reason: reason}
		case opText, opBinary:
			if inFragment {
				return Message{}, errors.New("websocket: interleaved data frame")
			}
			msgBinary = opcode == opBinary
			assembled = payload
		case opContinuation:
			if !inFragment {
				return Message{}, errors.New("websocket: continuation without start")
			}
			assembled = append(assembled, payload...)
		default:
			return Message{}, fmt.Errorf("websocket: unknown opcode %d", opcode)
		}
		if fin {
			return Message{Binary: msgBinary, Data: assembled}, nil
		}
		inFragment = true
	}
}

func (c *Conn) readFrame() (fin bool, opcode byte, payload []byte, err error) {
	var head [2]byte
	if _, err = io.ReadFull(c.br, head[:]); err != nil {
		return
	}
	fin = head[0]&0x80 != 0
	opcode = head[0] & 0x0F
	masked := head[1]&0x80 != 0
	length := uint64(head[1] & 0x7F)
	switch length {
	case 126:
		var ext [2]byte
		if _, err = io.ReadFull(c.br, ext[:]); err != nil {
			return
		}
		length = uint64(binary.BigEndian.Uint16(ext[:]))
	case 127:
		var ext [8]byte
		if _, err = io.ReadFull(c.br, ext[:]); err != nil {
			return
		}
		length = binary.BigEndian.Uint64(ext[:])
	}
	if length > maxPayload {
		err = fmt.Errorf("websocket: frame of %d bytes exceeds the transport cap", length)
		return
	}
	if !c.isClient && !masked {
		err = errors.New("websocket: client frame was not masked")
		return
	}
	var mask [4]byte
	if masked {
		if _, err = io.ReadFull(c.br, mask[:]); err != nil {
			return
		}
	}
	payload = make([]byte, length)
	if _, err = io.ReadFull(c.br, payload); err != nil {
		return
	}
	if masked {
		for i := range payload {
			payload[i] ^= mask[i%4]
		}
	}
	return
}

func (c *Conn) writeFrame(opcode byte, payload []byte) error {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	header := make([]byte, 0, 14)
	header = append(header, 0x80|opcode)
	maskBit := byte(0)
	if c.isClient {
		maskBit = 0x80
	}
	switch n := len(payload); {
	case n < 126:
		header = append(header, maskBit|byte(n))
	case n <= 0xFFFF:
		header = append(header, maskBit|126, byte(n>>8), byte(n))
	default:
		header = append(header, maskBit|127)
		var ext [8]byte
		binary.BigEndian.PutUint64(ext[:], uint64(n))
		header = append(header, ext[:]...)
	}
	body := payload
	if c.isClient {
		var mask [4]byte
		if _, err := rand.Read(mask[:]); err != nil {
			return err
		}
		header = append(header, mask[:]...)
		body = make([]byte, len(payload))
		for i := range payload {
			body[i] = payload[i] ^ mask[i%4]
		}
	}
	if _, err := c.conn.Write(append(header, body...)); err != nil {
		return err
	}
	return nil
}

// WriteText sends one text frame.
func (c *Conn) WriteText(s string) error { return c.writeFrame(opText, []byte(s)) }

// WriteBinary sends one binary frame.
func (c *Conn) WriteBinary(b []byte) error { return c.writeFrame(opBinary, b) }

// WritePing sends an empty ping, the cheap keep-alive the protocol
// accepts in place of a progress event.
func (c *Conn) WritePing() error { return c.writeFrame(opPing, nil) }

// Close sends a close frame with the given code and shuts the socket
// down. Calling it twice is harmless.
func (c *Conn) Close(code uint16, reason string) error {
	c.closeMu.Lock()
	if c.closed {
		c.closeMu.Unlock()
		return nil
	}
	c.closed = true
	drain := !c.sawClose
	c.closeMu.Unlock()

	payload := make([]byte, 2, 2+len(reason))
	binary.BigEndian.PutUint16(payload, code)
	payload = append(payload, reason...)
	_ = c.writeFrame(opClose, payload)
	// Drain whatever the peer is still sending before closing the
	// socket. This is not politeness: a TCP socket closed with unread
	// inbound data is reset, and a reset can discard the peer's receive
	// buffer along with the terminal event we just wrote. §8 lets a
	// backend answer a replayed operation from cache the moment it says
	// ready, while the client is mid-upload, so this path is ordinary
	// rather than exotic.
	if drain {
		deadline := time.Now().Add(closeDrain)
		_ = c.conn.SetReadDeadline(deadline)
		scratch := make([]byte, 4096)
		for time.Now().Before(deadline) {
			if _, err := c.conn.Read(scratch); err != nil {
				break
			}
		}
	}
	_ = c.conn.SetWriteDeadline(time.Now().Add(time.Second))
	return c.conn.Close()
}
