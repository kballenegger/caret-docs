// The caret/v4 reference backend and conformance checker.
//
// No `require` block, and there never will be one: everything here is
// the Go standard library, including the WebSocket implementation in
// ws.go. `go build ./...` works offline on a fresh checkout.
module caret.dev/reference/v4

go 1.22
