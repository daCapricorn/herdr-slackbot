# Changelog

## 0.2.0 — Herdr Bridge (development)

This fork changes purpose from a standalone Slack bot to a local Herdr integration supervised by OpenClaw. Package/plugin version 0.2.0 is under development; no tagged release is implied.

- OpenClaw now owns Slack credentials, message delivery, native presentation and authenticated callbacks. The Bridge worker uses private NDJSON stdio and needs no Slack token.
- Added exact-pane status/read/send commands and request-ID-based human approvals, backed by durable pending requests and an outbound queue.
- Approval dispatch revalidates live evidence and consumes authorization before sending keys. Uncertain writes are never automatically retried.
- Approval results now distinguish observed terminal confirmation from a send acknowledgement. Still-open approvals report `unconfirmed`, with persisted diagnostics and no automatic retry.
- Slack clicks reserve an attempt and show progress before dispatch. Added per-pane dispatch exclusion, idempotent repeated clicks, read-only status checks, bounded durable background follow-up and explicit attempt-bound retries after live revalidation. Recovery results update the original card while its callback editor is available, or send a portable recovery card after Gateway restart.
- Added Codex CLI async questions from Queued follow-up inputs, including discovery during working, native suggestion buttons and `/herdr reply` free-text answers. Confirmation requires the exact recorded question answer; drafts, ambiguity, expiry and changed menus prevent dispatch.
- Approval, unsupported-blocked and async-question notifications now wait 10 seconds and recheck whether the exact request is still unresolved before delivery. Local handling cancels queued cards; durable deadlines survive rescans/restarts, while completion and result notifications remain immediate.
- Private-channel completion messages no longer mention owners; approvals, blocked dialogs and questions retain mentions.
- Removed the fixed 15-minute approval/question request expiry and its UTC timestamp from Slack cards. Existing records with the old deadline remain governed by live pane, dialog and native question checks.
- Fixed OpenClaw's text-only fallback for confirmed cards: retained context renders as native blocks even without buttons. Callback errors no longer replace the source card.
- Approval/question card updates now retain the original header, summary and captured conversation through checking, confirmation and restart recovery, replacing only the choices with status and applicable recovery controls.
- Messages contain the Herdr name/tab label, actual session model, cwd, a rule-based highlighted summary and captured terminal conversation.
- Added opt-in private Slack channels per Herdr pane, durable channel IDs, rename/move reconciliation, owner invitations/mentions, channel-scoped commands and approvals, and source-channel result recovery. Closed panes retain channel history until explicit archival; continuous conversation streaming is deferred. Channel lifecycle management reuses the current OpenClaw bot credential while messages remain on OpenClaw's outbound adapter.
- Added owner-gated read tools for OpenClaw agents. No model-facing approval or terminal-write tool is exposed.
- Verified the new flow on macOS, including a real read-only approval and locked-phone Slack push.

Migration requires installing the OpenClaw plugin, configuring owner/route and stopping the former standalone bot. Old tokens, pairing and thread state are not imported. Home-tab controls, agent creation and implicit thread routing are not implemented in Bridge. Historical code and setup guides remain available under `herdr_slackbot/` and `legacy/`.

The pre-migration fork baseline is `c9f17c8`, preserving the macOS port and Slack conversation formatting work. Development follows Git Flow on `develop`; `main` retains the pre-migration version until a release is made.
