# Connect Caret to GrokBot

Read this after [connect-caret.md](https://docs.typewithcaret.com/agent-prompts/connect-caret.md).
Use the Caret reference backend. Do not build a new backend.

## 1. Get the reference backend

```sh
git clone https://github.com/kballenegger/caret-docs.git
cd caret-docs/reference-backend
```

Read its README and the GrokBot adapter contract before configuring anything.
Keep the backend's public contract and safety checks intact.

## 2. Choose phone access

Ask the user which option they want:

- **Public HTTPS — recommended.** It is simpler for the phone. Protect the
  Caret backend with its API key and keep the backend itself bound to
  loopback behind TLS.
- **Tailscale only.** It is private, but the phone must join and stay
  connected to the same Tailnet for the keyboard to work.

If they choose Tailscale, configure it and guide them through signing in
and connecting Tailscale on the phone. Test phone-to-backend access before
moving on.

## 3. Configure GrokBot capabilities

Ask whether the user has an xAI API key. Do not assume a Grok subscription
includes API access. Store supplied keys securely; never commit or log them.

- **Dictation:** If they have an xAI API key, connect Grok native STT. If
  not, offer local OpenWhisper. Test one real transcription end to end.
- **Cleanup:** Send the transcript to Grok with the reference backend's
  cleanup system prompt. Keep this request short and text-only.
- **Ask:** Create or configure the user's Grok bot through supported Grok
  tools. Send completed Ask turns there as a record, but keep recording
  asynchronous: the keyboard must return its answer without waiting for it.
  A recording failure must not fail or slow an Ask.
- **Imagine:** Connect Grok Imagine and test a real image request.

Use the reference backend's GrokBot interface exactly. Do not claim a
capability is enabled until its endpoint has been tested.

## 4. Verify before handoff

Run the backend check. Then verify from the phone:

- the final HTTPS URL works;
- requests without the Caret API key get `401`;
- Ask, cleanup, dictation, and Imagine work only when configured;
- the resolved `routes` in `/v1/health` match the live setup;
- Ask returns quickly even if Grok bot recording is unavailable.

Give the user the final base URL and Caret API key to enter in the app.
