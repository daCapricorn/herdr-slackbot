# Herdr Bridge behavior contract

Version 0.2.0 replaces the standalone Slack bot runtime with an OpenClaw-managed Bridge. See [architecture](OPENCLAW-BRIDGE.md) for the implementation and [legacy spec](../legacy/docs/SPEC.md) for historical decisions.

## Current behavior

- Herdr owns terminals, agent identity, visible reads, prompt input and key dispatch.
- The Python worker reconciles events and stores approvals/outbound events durably.
- The OpenClaw plugin owns worker lifecycle, exact human commands, owner checks and message presentation. OpenClaw's Slack adapter owns provider credentials and callbacks.
- The supported route is the configured owner's Slack DM. One configured channel/account and an owner allowlist gate commands and interactions.
- `status` and `read` inspect agents; `send` accepts an exact idle/done pane; `answer` accepts an exact request and zero-based option; `check` observes an existing attempt without sending keys.
- Recognized Codex command approvals and Claude single-choice dialogs expose remote options. Other dialogs require local handling.

## Approval invariant

A request binds to terminal/session identity, state sequence, a dialog fingerprint, an expiry and a fixed option set. Slack first persists a reservation without terminal effects and shows progress; commit rechecks all live evidence and persists `dispatching` before sending once. Initial dispatch/confirmation is mutually exclusive per pane. Two consistent observations of an advanced state sequence and the original dialog disappearing, including a recognized new blocked dialog, produce `confirmed`. A still-open original approval is `unconfirmed`; replacement/read failure leaves acceptance unknown. Five-second initial polling is followed by read-only background checks for up to two minutes or expiry, whichever comes first.

Duplicate choices return the current result; choosing a different option after reservation is rejected. An explicit attempt-bound retry permits the same option only when the original identity, sequence and dialog still match, the request remains valid and no attempt is active. Commit rechecks again. Duplicate retry buttons cannot create another attempt. Unknown results permit only checking until fresh evidence establishes eligibility. A crash/restart never automatically sends reserved or consumed keys. Superseded and expired requests cannot authorize a new submission; delayed old input and simultaneous local input remain races.

OpenClaw authentication supplies the sender provenance; button text and model output do not. Optional agent tools are read-only. Herdr approvals are not inserted into OpenClaw's native `exec.approvals` store.

## Messages

Notifications are top-level DM messages. One header shows the Herdr name/tab label, actual model and cwd. A rule-based summary follows in inline-code highlighting, then captured terminal evidence and approval controls. Session ambiguity shows an unknown model rather than a configured default. Terminal capture is visible evidence, not an archive of all off-screen history.

The outbound queue retries delivery until acknowledgement. An ambiguous provider result may duplicate a card; copies share a request ID. Message delivery is at least once, while terminal mutations are never automatically replayed.

During a Gateway process, background results use the authenticated callback editor to update the original card. Editors are not persisted. After restart or editor eviction, a new recovery card carries the same request ID and eligible controls through OpenClaw's portable sender. SQLite stores card locations and latest result events, without response URLs or provider tokens. Results are acknowledged in the outbox only after edit/delivery succeeds.

## Limits

Other messaging channels, agent creation, Home-tab controls, implicit thread routing, arbitrary keypads and auto-approval are not implemented. Typed inputs, multi-select and file-backed plans remain local. The new flow has been verified on macOS; Windows live integration remains unverified. Fingerprint checks cannot eliminate a race with simultaneous local interaction because Herdr has no atomic compare-and-send-keys operation.

See [LIVE_TEST.md](LIVE_TEST.md) for evidence and checks that distinguish delivery, callback handling, dispatch and phone push.
