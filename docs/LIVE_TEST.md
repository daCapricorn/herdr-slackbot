# Herdr Bridge live verification

Run these checks after [setup](GUIDE.md). Keep terminal effects limited to a read-only command such as `/usr/bin/uname -s` on macOS. Test in an agent configured to ask before running that command; asking the model to request approval may not override its terminal policy.

## 1. Runtime and reads

- Confirm Herdr is running and the former standalone Slack bot is stopped.
- Inspect `openclaw gateway status`, `openclaw channels status` and `openclaw plugins inspect herdr-bridge --runtime --json`.
- In the OpenClaw bot DM, run `/herdr status`, then `/herdr read <exact-pane-id>`.
- Confirm the read matches that pane's visible terminal, not another session with the same working directory.

## 2. Delivery and presentation

Trigger a recognized approval in the selected terminal. Confirm the top-level DM contains one bold name/model/cwd header, a highlighted content summary, terminal evidence and native buttons. `/herdr pending` should show the matching request and options. A sent message or provider receipt only establishes delivery.

## 3. Callback and real dispatch

Click the intended option from the configured owner's DM. Confirm:

- The Gateway logs receipt of the callback and dispatch for that request ID.
- The card is edited to show `dispatched`.
- The terminal actually accepts the option and runs the expected read-only command.
- The command produces its expected result; `dispatched` alone is not proof of command completion.

For a retained duplicate card, a second click must reject the consumed request. For a superseded dialog, the old card must be rejected. Do not approve a card whose command evidence no longer matches the terminal.

The plugin also handles an authenticated `herdr-bridge:probe` no-op callback when a diagnostic probe card is sent. It should edit that card to report a connected callback without operating the terminal. This callback check is separate from real approval verification.

## 4. Phone push

Put Slack in the background and lock the phone before triggering a fresh approval. Confirm a push arrives, then tap the intended button and verify dispatch in the terminal. Notification settings, availability and device policy affect push; delivery to a DM is not proof of push.

## 5. Persistence and local-only cases

With a pending request, restart Gateway and confirm the current request remains available. A restart must not replay a consumed terminal effect. An unsupported dialog should notify for local handling and expose no remote options. At-least-once provider delivery may duplicate messages.

## Recorded result, 2026-10-10

On the tested macOS installation, actual runtime registration, native card delivery, the no-op callback and a real `/usr/bin/uname -s` approval were verified. The command returned `Darwin`; durable state and logs recorded one dispatch. The recipient separately confirmed push with Slack backgrounded and the phone locked, and the card showed `dispatched`. A subsequent real DM preview confirmed the single header and highlighted summary layout. This is evidence for the tested configuration, not every Slack device policy.
