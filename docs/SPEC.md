# Herdr Bridge behavior contract

Version 0.2.0 replaces the standalone Slack bot runtime with an OpenClaw-managed Bridge. See [architecture](OPENCLAW-BRIDGE.md) for the implementation and [legacy spec](../legacy/docs/SPEC.md) for historical decisions.

## Current behavior

- Herdr owns terminals, agent identity, visible reads, prompt input and key dispatch.
- The Python worker reconciles events and stores approvals/outbound events durably.
- The OpenClaw plugin owns worker lifecycle, exact human commands, owner checks and message presentation. OpenClaw's Slack adapter owns provider credentials and callbacks.
- The supported route is the configured owner's Slack DM. One configured channel/account and an owner allowlist gate commands and interactions.
- `status` and `read` inspect agents; `send` accepts an exact idle/done pane; `answer` accepts an exact request and zero-based option; `check` observes an existing attempt without sending keys.
- Recognized Codex command approvals and Claude single-choice dialogs expose remote options. Codex async questions expose suggested choices and explicit free-text `reply` commands, including while working. Other dialogs require local handling.

## Approval invariant

A request binds to terminal/session identity, state sequence, a dialog fingerprint, an expiry and a fixed option set. Slack first persists a reservation without terminal effects and shows progress; commit rechecks all live evidence and persists `dispatching` before sending once. Initial dispatch/confirmation is mutually exclusive per pane. Two consistent observations of an advanced state sequence and the original dialog disappearing, including a recognized new blocked dialog, produce `confirmed`. A still-open original approval is `unconfirmed`; replacement/read failure leaves acceptance unknown. Five-second initial polling is followed by read-only background checks for up to two minutes or expiry, whichever comes first.

Duplicate choices return the current result; choosing a different option after reservation is rejected. An explicit attempt-bound retry permits the same option only when the original identity, sequence and dialog still match, the request remains valid and no attempt is active. Commit rechecks again. Duplicate retry buttons cannot create another attempt. Unknown results permit only checking until fresh evidence establishes eligibility. A crash/restart never automatically sends reserved or consumed keys. Superseded and expired requests cannot authorize a new submission; delayed old input and simultaneous local input remain races.

OpenClaw authentication supplies the sender provenance; button text and model output do not. Optional agent tools are read-only. Herdr approvals are not inserted into OpenClaw's native `exec.approvals` store.

## Async question invariant

Codex async question discovery reads completed structured `AgentMessage` items with `delivery: async` from the exactly matched local session. IDs derive from the message ID and question index. Model prose, tool-call text and terminal status alone are not question evidence. Pending questions are scoped to their originating turn; ordinary status sequence changes do not invalidate them. Turn end, expiry, an exact user reply or session replacement invalidates outstanding question submissions.

Choice callbacks reuse reserve/progress/revalidate/one-shot dispatch. `/herdr reply` permits explicit owner-authenticated free text for this request kind only. The write path requires an exact visible expanded menu or the known empty main composer with the matching pending count. Custom answers require the composer path. Prompt mode sends Codex's native reply envelope; a verified collapsed composer reported blocked uses one encoded `pane.send_input` submission because `agent.prompt` rejects blocked agents; key mode sends one suggestion digit. Unknown layouts, drafts or another open approval fail closed. Discovery never navigates the UI or sends input.

Confirmation requires two observations of a durable Codex user message containing the exact question ID and selected answer. Disappearance, `working` status, an input acknowledgement or a different local answer is insufficient. Same-answer duplicates return the current attempt; changes after reservation are rejected. Read-only follow-up, explicit attempt-bound retries and no-replay restart recovery apply to both approvals and questions. Session files and databases are read-only; ambiguous session matches disable the adapter. The verified Codex CLI source is 0.162.1; this is an agent-specific adapter, not universal question compatibility.

## Messages

Notifications are top-level DM messages. One header shows the Herdr name/tab label, actual model and cwd. A rule-based summary follows in inline-code highlighting, then captured terminal evidence and approval controls. Session ambiguity shows an unknown model rather than a configured default. Terminal capture is visible evidence, not an archive of all off-screen history.

Approval cards, unsupported blocked-dialog notices and Codex async question cards wait **10 seconds from first discovery**. Bridge retains the pending request immediately, but releases its notification only if the same request is still unresolved. Local answers, turn completion, expiry or replacement cancel the queued card. A replacement request starts its own delay; duplicate scans and Gateway restarts preserve the existing deadline. Live-read failures defer delivery until evidence is available. Completion notices and answer-result updates have no added delay. The five-second delivery poll can make a card arrive a few seconds after the deadline; this is a minimum grace period, not an exact phone push timer.

The plugin asks the worker to recheck each attention event again immediately before OpenClaw handoff. Delayed events do not occupy the ready batch ahead of completion/results. This read-and-send sequence cannot eliminate a local answer racing with an already-started provider request.

The outbound queue retries delivery until acknowledgement. An ambiguous provider result may duplicate a card; copies share a request ID. Message delivery is at least once, while terminal mutations are never automatically replayed.

During a Gateway process, background results use the authenticated callback editor to update the original card. Editors are not persisted. After restart or editor eviction, a new recovery card carries the same request ID and eligible controls through OpenClaw's portable sender. SQLite stores card locations and latest result events, without response URLs or provider tokens. Results are acknowledged in the outbox only after edit/delivery succeeds.

## Limits

Other messaging channels, agent creation, Home-tab controls, implicit thread routing, arbitrary keypads and auto-approval are not implemented. Typed approval inputs, multi-select, file-backed plans and non-async question tools remain local. Ordinary queued message editing is not implemented. The new flow has been verified on macOS; Windows live integration remains unverified. Fingerprint checks cannot eliminate a race with simultaneous local interaction because Herdr has no atomic compare-and-send-keys operation.

See [LIVE_TEST.md](LIVE_TEST.md) for evidence and checks that distinguish delivery, callback handling, dispatch and phone push.
