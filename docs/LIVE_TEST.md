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

## 6. Codex async questions

In Codex CLI 0.162.1, ask the agent to call `request_user_input_async` with a harmless label question and two suggestions. Keep the turn active until answering. Without opening the local menu, verify a top-level question card arrives while Herdr is working, with the full title/options, terminal capture and `/herdr reply` guidance. Click only in Slack. Confirm checking precedes the one input submission, then the exact native question ID and answer appear in Codex's durable user record and the card shows confirmed. Herdr need not leave working.

For free text, leave the main composer empty and use `/herdr reply <request-id> <text>`. Confirm the recorded answer matches exactly. Repeated same-answer clicks/commands must not submit again. Answer locally or end the turn before clicking a different question card: the old request must fail without input. Leave a local draft: a remote submission must fail without appending to it. Discovery/restart alone must never open a menu or send an answer. Compare ordinary approval tests separately.

## 7. Unattended-request delay

Resolve a newly discovered approval/question on the computer within 10 seconds: its attention card must not be sent. A separate completion notification remains allowed. Leave another harmless question unanswered: its queued event must stay hidden until `notify_after`, then deliver while the same exact question remains pending. Verify the discovery timestamp, persisted deadline and actual delivery timestamp independently. A provider receipt is not phone-push evidence. Restarting during the grace period must keep the original deadline; replacing/ending the request must cancel its queued card. A long delayed queue must not postpone ready completion/result messages.

## Recorded result, 2026-10-10

On the tested macOS installation, actual runtime registration, native card delivery, the no-op callback and a real `/usr/bin/uname -s` approval were verified. The command returned `Darwin`; durable state and logs recorded one dispatch. The recipient separately confirmed push with Slack backgrounded and the phone locked, and the card showed `dispatched`. A subsequent real DM preview confirmed the single header and highlighted summary layout. This is evidence for the tested configuration, not every Slack device policy.

After terminal-confirmation checks were added, a second real Slack approval for `/usr/bin/uname -m` returned `arm64`. The worker sent one digit, observed the original approval closed after about 630 ms, and recorded `confirmed`. Simulated dropped-input cases report `unconfirmed` without retry; the intermittent live failure itself has not yet been reproduced.

After recovery was added, a real `/usr/bin/uname -s` approval at 22:57 Dubai time returned `Darwin`. The authenticated Slack callback reserved and dispatched attempt `38af9b333a794b4bb87be03c311ebf06` for request `c2e5ffe54096436397386da9aaacdec6`; one digit was sent, and stable terminal progression confirmed closure after 644 ms. Slack readback verified `confirmed` on the original card. A Gateway restart subsequently delivered the persisted recovery result and cleared the outbox without replaying input. Explicit retry and the reported intermittent input loss remain simulated coverage rather than live failure reproduction.

## Codex async question verification, 2026-10-11

- Full regression after the final input-state fix: **940 Python tests passed**, 5 opt-in live tests skipped; **25 JavaScript tests passed**.
- The first live color question exposed Herdr reporting a collapsed queued question as `blocked` while the TUI displayed `Working`. The Slack choice was rejected before input. The final adapter distinguishes that verified empty-composer UI from unrelated approvals and chooses one encoded pane submission before dispatch.
- At 10:19 Dubai time, the owner answered the second color question only through Slack. Request `c8fcfee6f9bb4803a3a84199794c8e94`, attempt `e74ae554e90b442bbd2f2fe40dd4358b`, submitted the native answer envelope once. Codex recorded `紫色标签` for exact question ID `["request_user_input_async","call_ae330806efd14d2cb0f7444c6210f004",0]`; two observations produced `confirmed` after 649 ms. No terminal command was executed by this test.
- Independent Slack readback of original card `1791699571.404789` confirmed “Codex recorded the answer to this question” and no remaining choice buttons. The durable result update was delivered and its outbox event cleared.
- This live result covers collapsed async questions on Codex CLI 0.162.1, Herdr 0.9.3 and OpenClaw 2026.9.9. Expanded-menu selection, free-text commands, drafts, explicit retries and restart no-replay have deterministic test coverage; they were not all exercised live in this run. A separate locked-phone push test was not repeated for question cards.

## Attention delay verification, 2026-10-11

- Full regression: **956 Python tests passed**, 5 opt-in live tests skipped; **27 JavaScript tests passed**. Deterministic clock tests cover all three attention types, local resolution before the deadline, replacement requests, restart persistence, failed reads, pre-handoff cancellation and immediate completion/result delivery behind a delayed queue.
- At 10:38 Dubai time, harmless question request `1e3f75bf46e041e995f0b3c9c3988121` was discovered at `2026-10-11T06:38:01.342503Z`. Slack card `1791700695.523849` was created **14.181 seconds later**, consistent with the 10-second grace period plus polling. The queued deadline itself was not captured live before delivery; its exact persistence is covered by the clock tests.
- The owner answered only through Slack with `延迟卡片已收到`. Attempt `09eef459a9df4a8bbe719420f86a85ce` reached `confirmed` after **441 ms** with reason `question_answered`. Independent Slack readback confirmed the original card's final result and removal of choice buttons. No terminal command was executed by this test.
- Local handling within 10 seconds and restart during the delay were not repeated live in this run. Phone lock-screen push was not retested; this live check establishes delayed card delivery and answer confirmation.

## Pane channels verification, 2026-10-11

Enable `paneChannels` after adding `groups:write` and `groups:write.invites` to the OpenClaw Slack bot and reinstalling it. Run `node scripts/verify-pane-channels.mjs <openclaw-config-path> <bridge-db-path>` for a read-only check of actual Slack channel names, private visibility and owner membership. It never prints credentials. Test a harmless question and a read-only approval in the matching channel, then independently check durable acceptance and the resulting Slack card. Avoid opening another command approval while answering an async question.

- Regression: **965 Python tests passed**, 5 opt-in live tests skipped; **43 JavaScript tests passed**. Coverage includes routing isolation, durable provisioning recovery, rename/move identity, explicit archive, source-channel recovery and delivery while another channel is still provisioning.
- All four live panes acquired distinct private channels containing the configured owner: `w3:p1` → `C0C8E6PKE01`, `w4:p1` → `C0C86V356SH`, `w4:p2` → `C0C861RS4SX`, `w5:p8` → `C0C86V3F44V`. Independent Slack API readback verified their names and membership. A Gateway restart preserved all four IDs.
- Temporarily renaming `w5:p8` to `bridge-channel-test` changed its Slack channel name while retaining `C0C86V3F44V`. Clearing the pane label restored the inherited name `bridge`; Slack readback verified restoration. No terminal input was sent by the rename operation.
- The first provisioning run exposed a Slack `invalid_arguments` response for the JSON-encoded members call. Switching management calls to form encoding resumed provisioning from the persisted channel IDs without creating duplicates.
- A real channel approval, request `c11003c3626d4b7aa26a18646d7a915d`, attempt `a72a76f99f5b46979de9e90fbb5f0d27`, eventually reached `confirmed` after read-only follow-up observed the original approval closed. The read-only channel verifier completed successfully. Independent Slack readback of card `1791702688.444389` verified the confirmed result and removal of buttons; this was not an immediate confirmation.
- The first question click was rejected because another command approval was open. The later label answer had no Slack dispatch record and is not counted as successful callback verification.
- The isolated retest succeeded through Slack: request `b74710ff10814746855faae1dc9c12bc`, attempt `ae7516663f0f401189cf4f954700f7d9`, recorded `频道回调成功` against the exact native question ID once and reached `confirmed` after **425 ms** (`question_answered`). Independent Slack readback of original channel card `1791703230.820889` verified the confirmed result and no remaining choice buttons. No terminal command was executed by this question test.
- Cross-workspace moves, closed-pane archival and invalid-channel rejection have deterministic coverage; they were not all exercised live. Phone lock-screen push was not retested for these channels. Continuous conversation streaming remains deferred.

## Result-card context rendering, 2026-10-11

- The first context-preservation implementation passed mocked rendering tests but failed a real Slack question. OpenClaw 2026.9.9 flattened a confirmed presentation with `presentationTextMode: "fallback"` and no controls into a text segment. The edit rejected that segment, and a response-URL error reply replaced the original card. This failure was reproduced against the actual installed renderer offline.
- Context-bearing result payloads now have empty authored text and native presentation blocks, preventing that fallback optimization. Owner callback errors use separate outbound messages; unauthorized callbacks neither edit nor replace the original card. `node scripts/verify-card-rendering.mjs <installed-Slack-outbound-adapter-module>` verifies checking, confirmation and recovery using the real OpenClaw renderer without network requests.
- All **44 JavaScript tests passed** after this correction. The preceding durable-context worker change passed **966 Python tests**, with 5 opt-in live tests skipped; the correction changes only JavaScript rendering/callback presentation.
- A real Slack-only retest, request `b23e6465ed464b9b84ec7d1b079188d4`, attempt `48b523d7008541e99a8a09e2466dd02f`, reached `confirmed` after **1869 ms** with reason `question_answered`. Independent readback of original card `1791704538.173749` verified the native header, highlighted summary, confirmed status, retained code-block transcript and zero action blocks. No terminal command was executed by the question test.
- Previously delivered cards without saved context cannot reconstruct their original capture. This live check covers the shared question/result rendering path; a separate command approval and locked-phone push were not repeated for this layout change.

## Request lifetime, 2026-10-11

- Removed the Bridge's fixed 900-second TTL from approval and async-question requests. Old persisted records with `expires_at` are accepted under the same live pane, approval fingerprint/sequence and native-question checks. Exact duplicate clicks remain idempotent; explicit retry still requires fresh live evidence.
- Removed the generated UTC expiry timestamp from new Slack cards. The previous timestamp came from `Date.toISOString()` and displayed UTC (`Z`), not the user's local timezone.
- Full regression: **965 Python tests passed**, 5 opt-in live tests skipped; **44 JavaScript tests passed**. `tests/test_openclaw_bridge.py` verifies that an old request whose stored deadline has passed can still dispatch when its exact live approval matches. Slack UI removal of the timestamp was verified by the presentation test; no new live card was sent solely for this copy change.

### 2026-10-11 Codex conversation session fallback

- Shared exact/unique session resolution and explicit terminal-scoped binding regression: **975 Python tests passed**, 5 opt-in live tests skipped; **46 JavaScript tests passed**. macOS socket/process tests passed outside the filesystem sandbox.
- Gateway plugin reload operation `9adcd519-8cfa-4e98-bd7b-732dddf3b4ba`, generation 4, loaded this checkout. The `codex-slackbot` pane resolved thread `01a124c3-81cd-7871-83ba-b5ffe7a0b369` without a Herdr session ID, created a durable cursor, and emitted `conversation.message` events. Gateway delivery logs and Slack `conversations.history` independently confirmed tool-summary messages in the bound channel; the conversation outbox had no outstanding messages at readback.
- The wasm-agent and almanack directories had multiple candidate root threads, so sync correctly reported `unavailable` rather than guessing. Explicit binding is covered by persistence/identity and authenticated command tests. Live explicit-binding and new final-answer readback remained pending at this point.

### 2026-10-11 Native Slack binding and final-message verification

- The owner executed `/openclaw /herdr bind 01a12091-da21-7800-a5b6-b02fff2d0af0` in the codex-wa channel and received `syncing`. OpenClaw native commands lowercase sender IDs and provide the source conversation through `from`, while `channelId` identifies the provider. The Bridge now normalizes stable Slack user IDs and maps the authenticated origin; conflicting origins, unauthorized senders and cross-pane commands remain rejected.
- A first fixed-response test revealed that real Codex `AgentMessage.content` uses `type: "Text"`. The reader skipped this case, so only the user message and separate completion card arrived. The parser now accepts this exact content variant, with a regression fixture matching the observed event. Already-consumed historical lines are not replayed.
- Runtime reload operation `a23a7e23-ec06-4627-a85a-d67ab6078d35`, generation 7, loaded the correction. A second harmless prompt requested only `HERDR_SYNC_FINAL_OK_20261011` and no tools, commands or file changes. Independent Slack history readback in `C0C86V356SH` verified one user conversation message (`1791722120.858049`) and one distinct assistant conversation message (`1791722125.525059`), followed by the existing completion card (`1791722125.804659`). The durable cursor advanced to 17522951 and no conversation events remained in the outbox. Completion notifications continue separately from conversation sync.
- Full final regression: **976 Python tests passed**, 5 opt-in live tests skipped; **47 JavaScript tests passed**. Phone lock-screen push was not retested in this run.

### 2026-10-11 Sync recovery and session-switch implementation

- Full implementation regression: **997 Python tests passed**, 5 opt-in live tests skipped; **48 JavaScript tests passed**. Added real SQLite close/reopen coverage for pending and acknowledged messages, read/enqueue rollback with injected failures, per-thread cursor switching, exact root-session hook validation, stale overlapping hook rejection, hook cutoff recovery after a turn finishes before first scan, and installer preservation/idempotency. The plugin pump test injects a provider failure before handoff, keeps later messages from that pane queued, lets a separate pane progress, and verifies ordered recovery after service restart without resending acknowledged messages. This simulates disconnection rather than disabling the computer's network.
- A real Gateway restart returned `ok: true` / `restarted`. Independent history reads of codex-wa channel `C0C86V356SH` before and after verified unchanged timestamps and one copy each of the `HERDR_RECOVERY_BEFORE_20261011` user (`1791723001.306009`) and assistant (`1791723001.824689`) messages. A new post-restart fixed-response prompt delivered one user (`1791723400.732459`) and assistant (`1791723405.739649`) message with marker `HERDR_RECOVERY_AFTER_20261011`. Both prompts explicitly forbade tools, commands and file changes. Completion cards remained separate. The session binding stayed `01a12091-da21-7800-a5b6-b02fff2d0af0`, its cursor advanced to 17547417, and no conversation events were pending at readback.
- Companion `SessionStart` and `UserPromptSubmit` hooks were added beside the existing Herdr hook in the owner's Codex hooks.json; a backup was saved and trust settings were not changed. At this intermediate checkpoint, actual `/new` and resume hook execution awaited the owner's review in the Codex CLI `/hooks` menu. The final results are recorded below.
- Provider delivery remains at least once. A message accepted immediately before its provider/worker acknowledgement is lost can still be duplicated on retry; the tests do not claim exactly-once Slack delivery or exercise a real ambiguous send. Phone push was not retested.

### 2026-10-11 Exact hook context and completed switch/resume verification

- Both Bridge hooks were subsequently trusted by the owner. A shared-daemon `/new` created exact thread `01a12b10-dbd3-78a1-8dc3-92394713c05a`, independently identified from the unique test prompt in its rollout. Hook diagnostics showed an inherited `w1:p1`, while the actual TUI was in `w4:p1`; Herdr rejected the old ID. The hook payload fields contained no client/pane ID. This failure is not counted as successful automatic association. The hook now requires an exact foreground ancestor running `codex --no-daemon`, and refuses shared-daemon association even if its inherited pane exists.
- Only codex-wa was restarted normally using `codex --no-daemon resume 01a12091-da21-7800-a5b6-b02fff2d0af0`; the shared daemon and other panes remained running. Its prompt hook reported `true`, and independent Herdr `agent.get` exposed the exact original session ID with source `herdr:codex`.
- In that pane-local runtime, `/new` with the current worktree created `01a12b26-1bbe-7942-b246-9e69d201e259`. Without a manual bind, Bridge adopted that exact ID and kept channel `C0C86V356SH`. Slack readback verified one `HERDR_SESSION_LOCAL_SWITCH_20261011` user message (`1791725286.757219`) and one final assistant message (`1791725291.693559`), plus the separate completion card. The new cursor was 87915, with no pending conversation events.
- A startup/resume transition exposed an extra completion card for an already displayed old answer (`1791724642.155349`). Conversation messages themselves were not replayed. The correction stores completed-answer event IDs and durable completion claims, suppressing an old answer's repeated completion notification without removing normal completion cards.
- The exact original thread was then restored again in the same pane with `--no-daemon`. After the correction, `HERDR_LOCAL_RESTORED_20261011` arrived once as a user message (`1791728748.559989`), once as a final assistant message (`1791728753.369319`) and once as the new completion card (`1791728753.839199`). The old completion-marker message count did not increase during this restoration. Herdr and Bridge both reported original thread `01a12091-da21-7800-a5b6-b02fff2d0af0`; the original cursor continued to 17577610, the channel ID remained unchanged, and no conversation messages were pending. Two new test conversations remain saved; the original conversation is active.
- Final full regression: **1001 Python tests passed**, 5 opt-in live tests skipped; **48 JavaScript tests passed**. Native process lookup, same-directory multiple panes, shared-daemon refusal, moved-pane identity and completion-claim persistence have deterministic coverage. The live checks cover same-pane `/new`, normal CLI resume, Gateway restart and Slack readback; moving a running standalone pane was not repeated live.

## 2026-10-11: Codex + Claude agent plugins

Implemented on `feature/agent-plugin-adapters` with Herdr 0.9.3, Codex CLI 0.162.1, Claude Code 2.1.287 and OpenClaw 2026.9.9. Both agent plugins use the same session-registration API and the Bridge adapter contract; the durable reader preserves existing Codex tables and adds an independent Claude namespace.

- Claude `w4:p5`, terminal `term_65d918bb7507810`, session `3fbc438b-2720-4718-8eb2-a9fdf402ebfd`, channel `C0C97RURGDN` (`claude-slackbot`): fixed-text transcript delivery was read back from Slack. Marker `HERDR_CLAUDE_PLUGIN_20261011_OK` has user timestamp `1791730720.361789` and assistant timestamp `1791730725.413449`. Herdr subsequently exposed exact native `agent_session`, source `herdr:claude`; the private cutoff file matches this terminal/session/path. PID registry validation also matched the real foreground process's creation time, including UTC/local timezone representation.
- Plugin runtime locator initially resolved the venv symlink to its base interpreter. This was corrected and both installed packages updated to 0.1.1. A regression checks that the original venv path is retained. Both plugin handlers were enabled; the user explicitly reviewed and trusted Codex's two plugin hooks.
- Codex `w4:p1`, original session `01a12091-da21-7800-a5b6-b02fff2d0af0`, channel `C0C86V356SH`: after retiring this project's legacy companion hooks (managed Herdr and other hooks preserved), the new plugin diagnostic recorded `UserPromptSubmit`, `reported: true`, no error, sequence `1791731127495874000`. Slack readback for `HERDR_CODEX_PLUGIN_ONLY_20261011_OK` contains user `1791731130.303839`, assistant `1791731135.480699`, completion `1791731135.956299`. The original terminal/session/channel were retained. This live pane remains a previously started `--no-daemon` process; this test does not establish automatic association in shared-daemon mode.
- The second Claude fixed-text prompt was appended to a pre-existing `/pstack:p` draft. The reply completed, but this is not evidence of safe submission into arbitrary drafts. User then explicitly requested no further input while using Claude. Claude Slack choice callbacks and live `/clear`/resume tests are deferred; no additional test input was submitted to that pane after that request.
- Deterministic tests cover both agents' one-shot approval reservation/dispatch, retained context, exact session switches with independent cursors, native/manual identity validation, staged split Claude answers across SQLite reopen, late final blocks, partial/malformed records, PID reuse, nested agents, registry changes during registration, and read/enqueue rollback. Periodic discovery claims a late completion once after its durable boundary; it does not depend on an idle transition arriving again.
- Final Bridge runtime reload: generation 7, operation `aed652f2-74e1-4575-a79f-807f8d44ecf8`, selected checkout `openclaw/index.js`, source digest `4d64e7b2cb40f777403b88b731ea9d82bb68fb4b2fbc05e0da82dd1b6dea77c2`.

The normal Codex launcher no longer forces `--no-daemon`. Shared-daemon sync retains exact/manual binding, but hooks still lack reliable client/pane context for automatic `/new` association. Claude registry access is a version-specific compatibility reader; missing/invalid records require exact native/manual identity rather than newest-file guessing. At-least-once provider delivery and the existing unsupported dialog limits remain.

The current default-runtime Codex pane `w4:p2` (`argv: ["codex"]`) became ambiguous after another same-directory Codex session appeared. The exact current thread was verified from `CODEX_THREAD_ID` and the matching durable user-message item, then saved as an administrative explicit Bridge binding without terminal input. Worker readback returned `syncing`, thread `01a124c3-81cd-7871-83ba-b5ffe7a0b369`, offset `48313317`. This proves exact binding works in the default runtime; it does not prove automatic pane discovery.

Final regression: **1020 Python tests passed, 5 opt-in live tests skipped; 48 JavaScript tests passed**. The first sandboxed full run had host socket/process permission failures; the final full suite was rerun with the required local permissions and passed. Remaining Claude interactive cases are explicitly deferred, not counted as passing live tests.
