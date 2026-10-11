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
- The original options are replaced with checking before any key write; failure to display progress must leave the attempt unsent.
- The card is edited to show `confirmed` only after the original approval is observed closed.
- The terminal actually accepts the option and runs the expected read-only command.
- The command produces its expected result; `confirmed` alone is not proof of command completion.

If the original approval stays open, expect `unconfirmed`, **检查状态** and, when all original live checks match, an attempt-bound **重新提交** button. Background checks continue for up to two minutes or expiry. Checking never writes keys; only an explicit retry sends the same option after revalidation. A read/replacement failure reports unknown acceptance and cannot enable retry until fresh checks establish eligibility. Previous input can arrive late even when retry is eligible. The old `dispatched` label only described send acknowledgement.

For a retained duplicate card, a second choice click must return the current attempt without another write; a different option must be rejected. Repeating an old retry button cannot create another attempt. For a superseded dialog, the old card must be rejected. Do not approve a card whose command evidence no longer matches the terminal.

The plugin also handles an authenticated `herdr-bridge:probe` no-op callback when a diagnostic probe card is sent. It should edit that card to report a connected callback without operating the terminal. This callback check is separate from real approval verification.

## 4. Phone push

Put Slack in the background and lock the phone before triggering a fresh approval. Confirm a push arrives, then tap the intended button and verify dispatch in the terminal. Notification settings, availability and device policy affect push; delivery to a DM is not proof of push.

## 5. Persistence and local-only cases

With a pending request, restart Gateway and confirm it remains available. With an unresolved consumed/reserved attempt, restart and confirm observation resumes without any key write. Results update the original card while its authenticated callback editor remains available; after restart they arrive in a replacement recovery card with the same request ID. An unsupported dialog should notify for local handling and expose no remote options. At-least-once provider delivery may duplicate messages.

## Recorded result, 2026-10-10

On the tested macOS installation, actual runtime registration, native card delivery, the no-op callback and a real `/usr/bin/uname -s` approval were verified. The command returned `Darwin`; durable state and logs recorded one dispatch. The recipient separately confirmed push with Slack backgrounded and the phone locked, and the card showed `dispatched`. A subsequent real DM preview confirmed the single header and highlighted summary layout. This is evidence for the tested configuration, not every Slack device policy.

After terminal-confirmation checks were added, a second real Slack approval for `/usr/bin/uname -m` returned `arm64`. The worker sent one digit, observed the original approval closed after about 630 ms, and recorded `confirmed`. Simulated dropped-input cases report `unconfirmed` without retry; the intermittent live failure itself has not yet been reproduced.

After recovery was added, a real `/usr/bin/uname -s` approval at 22:57 Dubai time returned `Darwin`. The authenticated Slack callback reserved and dispatched attempt `38af9b333a794b4bb87be03c311ebf06` for request `c2e5ffe54096436397386da9aaacdec6`; one digit was sent, and stable terminal progression confirmed closure after 644 ms. Slack readback verified `confirmed` on the original card. A Gateway restart subsequently delivered the persisted recovery result and cleared the outbox without replaying input. Explicit retry and the reported intermittent input loss remain simulated coverage rather than live failure reproduction.
