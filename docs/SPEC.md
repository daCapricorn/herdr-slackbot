# Herdr Bridge behavior contract

Version 0.2.0 replaces the standalone Slack bot runtime with an OpenClaw-managed Bridge. See [architecture](OPENCLAW-BRIDGE.md) for the implementation and [legacy spec](../legacy/docs/SPEC.md) for historical decisions.

## Current behavior

- Herdr owns terminals, agent identity, visible reads, prompt input and key dispatch.
- The Python worker reconciles events and stores approvals/outbound events durably.
- The OpenClaw plugin owns worker lifecycle, exact human commands, owner checks and message presentation. OpenClaw's Slack adapter owns provider credentials and callbacks.
- The supported route is the configured owner's Slack DM. One configured channel/account and an owner allowlist gate commands and interactions.
- `status` and `read` inspect agents; `send` accepts an exact idle/done pane; `answer` accepts an exact pending request and zero-based option.
- Recognized Codex command approvals and Claude single-choice dialogs expose remote options. Other dialogs require local handling.

## Approval invariant

A request binds to terminal/session identity, state sequence, a dialog fingerprint, an expiry and a fixed option set. All are checked against the live terminal before dispatch. The request is consumed durably before keys are sent. After the send acknowledgement, two consistent live observations must show the original approval closed or a subsequent dialog before the result becomes `confirmed`. A still-open original approval is `unconfirmed`; replacement/read failure leaves acceptance unknown. These outcomes remain consumed, and no keys are automatically retried. Superseded, stale, expired and consumed requests cannot be used as fresh authorization.

OpenClaw authentication supplies the sender provenance; button text and model output do not. Optional agent tools are read-only. Herdr approvals are not inserted into OpenClaw's native `exec.approvals` store.

## Messages

Notifications are top-level DM messages. One header shows the Herdr name/tab label, actual model and cwd. A rule-based summary follows in inline-code highlighting, then captured terminal evidence and approval controls. Session ambiguity shows an unknown model rather than a configured default. Terminal capture is visible evidence, not an archive of all off-screen history.

The outbound queue retries delivery until acknowledgement. An ambiguous provider result may duplicate a card; copies share a request ID. Message delivery is at least once, while terminal mutations are never automatically replayed.

## Limits

Other messaging channels, agent creation, Home-tab controls, implicit thread routing, arbitrary keypads and auto-approval are not implemented. Typed inputs, multi-select and file-backed plans remain local. The new flow has been verified on macOS; Windows live integration remains unverified. Fingerprint checks cannot eliminate a race with simultaneous local interaction because Herdr has no atomic compare-and-send-keys operation.

See [LIVE_TEST.md](LIVE_TEST.md) for evidence and checks that distinguish delivery, callback handling, dispatch and phone push.
