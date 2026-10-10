# Graph Report - project-graph  (2026-10-10)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 2416 nodes · 6629 edges · 98 communities (75 shown, 23 thin omitted)
- Extraction: 93% EXTRACTED · 7% INFERRED · 0% AMBIGUOUS · INFERRED: 481 edges (avg confidence: 0.88)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `ad36ce3a`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Community 0
- Community 1
- Community 2
- Community 3
- Community 4
- Community 5
- Community 6
- Community 7
- Community 8
- Community 9
- Community 10
- Community 11
- Community 12
- Community 13
- Community 14
- Community 15
- Community 16
- Community 17
- Community 18
- Community 19
- Community 20
- Community 21
- Community 22
- Community 23
- Community 24
- Community 25
- Community 26
- Community 27
- Community 28
- Community 29
- Community 30
- Community 31
- Community 32
- Community 33
- Community 34
- Community 35
- Community 36
- Community 37
- Community 38
- Community 39
- Community 40
- Community 41
- Community 42
- Community 43
- Community 44
- Community 45
- Community 46
- Community 47
- Community 48
- Community 49
- Community 50
- Community 51
- Community 52
- Community 53
- Community 54
- Community 55
- Community 56
- Community 58
- Community 59
- Community 60
- Community 61
- Community 62
- Community 63
- Community 64
- Community 65
- Community 66
- Community 67
- Community 68
- Community 69
- Community 70
- Community 71
- Community 72
- Community 73
- Community 74
- Community 75
- Community 76
- Community 77
- Community 78
- Community 79
- Community 80
- Community 81
- Community 82
- Community 83
- Community 84
- Community 85
- Community 86
- Community 87
- Community 88
- Community 89
- Community 90
- Community 91
- Community 92
- Community 94
- Community 95
- Community 96

## God Nodes (most connected - your core abstractions)
1. `Bridge` - 156 edges
2. `StateStore` - 63 edges
3. `load_config()` - 56 edges
4. `transition()` - 55 edges
5. `HerdrError` - 50 edges
6. `run()` - 49 edges
7. `block()` - 48 edges
8. `SubscriptionManager` - 47 edges
9. `FakeClient` - 47 edges
10. `FakeCli` - 46 edges

## Surprising Connections (you probably didn't know these)
- `test_prompt_visible()` --calls--> `prompt_visible()`  [EXTRACTED]
  tests/test_bridge.py → herdr_slackbot/bridge.py
- `test_env_file_with_bom()` --calls--> `load_config()`  [EXTRACTED]
  tests/test_config.py → herdr_slackbot/config.py
- `test_load_config_defaults_to_repo_root()` --calls--> `load_config()`  [EXTRACTED]
  tests/test_config.py → herdr_slackbot/config.py
- `test_load_config_from_plugin_config_dir()` --calls--> `load_config()`  [EXTRACTED]
  tests/test_config.py → herdr_slackbot/config.py
- `test_default_manifest_uses_username()` --calls--> `load_config()`  [EXTRACTED]
  tests/test_slack_manifest.py → herdr_slackbot/config.py

## Import Cycles
- None detected.

## Communities (98 total, 23 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (65): _actions(), agent_line(), agent_list_blocks(), _button(), chunk_lines(), clock_time(), _code(), context() (+57 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (61): _Resume, _after_cursor(), _final_index(), _latest_turn_start(), newer_cursor(), _norm(), _number(), range_markdown() (+53 more)

### Community 2 - "Community 2"
Cohesion: 0.11
Nodes (55): becomes(), block(), click(), keys_sent(), last_text(), message(), record(), shows() (+47 more)

### Community 3 - "Community 3"
Cohesion: 0.05
Nodes (45): DeferredExecutor, test_codex_start_without_trust_screen_prompts_directly(), test_muted_pc_agent_gets_no_dialog(), test_resume_with_an_open_dialog_posts_nothing_new(), test_send_command_still_rejects_a_blocked_agent(), test_n2_matching_terminal_entry_elsewhere_is_found(), run(), _stalled() (+37 more)

### Community 4 - "Community 4"
Cohesion: 0.07
Nodes (45): default_bot_display_name(), parse_env_text(), lifecycle_lock(), _active_key(), _atomic_write(), env_lock(), EnvLockTimeout, format_env_value() (+37 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (43): _block_text(), _clean(), _collect_recap(), drop_sticky_headers(), drop_trailing_notices(), extract_result(), fallback_tail(), find_prompt_box() (+35 more)

### Community 6 - "Community 6"
Cohesion: 0.06
Nodes (4): Bridge, run(), _Reconcile, HistoryRange

### Community 7 - "Community 7"
Cohesion: 0.07
Nodes (27): load_env_file(), bridge_readiness(), stdin_is_interactive(), Clock, FakeBridgeStart, make_wizard(), _reset_calls(), Script (+19 more)

### Community 8 - "Community 8"
Cohesion: 0.10
Nodes (9): _close_quietly(), _new_state(), _PaneState, _Recheck, _same_agent(), _Stats, _Sub, SubscriptionManager (+1 more)

### Community 9 - "Community 9"
Cohesion: 0.08
Nodes (4): _display(), _result_op(), _seq(), _strip_mrkdwn()

### Community 10 - "Community 10"
Cohesion: 0.06
Nodes (20): HomeRefresher, _agent(), FakeTimer, _home_event(), Published, refresher(), test_debounce_keeps_at_least_the_interval_between_publishes(), test_h1_manual_refresh_during_a_publish_becomes_the_pending_one() (+12 more)

### Community 11 - "Community 11"
Cohesion: 0.09
Nodes (37): agent_result(), cwd_slug(), last_answer_from_lines(), test_result_prefers_claude_jsonl(), _final_message_id(), lines(), projects(), prompt_indices() (+29 more)

### Community 12 - "Community 12"
Cohesion: 0.09
Nodes (27): _blocks(), _bookkeeping_only(), ClaudeAnswer, conversation_from_lines(), _decode(), _duration_ms(), evaluate_lines(), _event_text() (+19 more)

### Community 13 - "Community 13"
Cohesion: 0.07
Nodes (24): SlackPermanentError, test_activation_is_retried_then_succeeds(), flaky(), _fences_balanced(), _hashing(), HashSlack, _payload(), _preview_text() (+16 more)

### Community 14 - "Community 14"
Cohesion: 0.08
Nodes (24): code_banner(), generate_code(), PairResult, PairStatus, read_pairing(), result_text(), test_a_guessing_user_is_throttled_alone_and_released_after_the_window(), test_activation_failure_is_visible_and_needs_restart() (+16 more)

### Community 15 - "Community 15"
Cohesion: 0.12
Nodes (31): load_config(), home_app(), test_home_buttons_are_acked_and_owner_guarded(), test_buttons_modals_messages_home_rejected_with_notice(), test_failed_activation_is_reported_to_the_paired_user(), test_other_commands_get_not_paired_notice(), test_pair_command_with_right_code_pairs_and_switches_owner(), test_pair_wrong_code_replies_ephemeral() (+23 more)

### Community 16 - "Community 16"
Cohesion: 0.09
Nodes (27): build_agent_args(), codex_home(), codex_model_options(), KindSpec, LaunchError, load_codex_default_model(), load_codex_models(), auto_agent_name() (+19 more)

### Community 17 - "Community 17"
Cohesion: 0.20
Nodes (34): ev(), FakeClient, make_manager(), _move(), _moved_working(), simple(), status_env(), test_new_agent_detected_then_pane_closed() (+26 more)

### Community 18 - "Community 18"
Cohesion: 0.09
Nodes (9): delete_pairing(), Pairing, _write_pairing(), Codes, gate(), GateBridge, machine(), test_bridge_activation_resumes_after_a_failed_step() (+1 more)

### Community 19 - "Community 19"
Cohesion: 0.07
Nodes (14): Config, resolve_config_dir(), activating_text(), activation_failed_text(), not_paired_text(), build_app(), current_owner(), notify() (+6 more)

### Community 20 - "Community 20"
Cohesion: 0.07
Nodes (7): ResultStore, FakeManager, FakeTransport, SyncExecutor, make_env(), wire(), test_bridge_stop_in_pairing_mode_does_not_touch_the_manager()

### Community 21 - "Community 21"
Cohesion: 0.11
Nodes (6): PluginOps, run_plugin_command(), shell_kind(), test_plugin_status_shows_waiting_for_pairing(), test_is_held_does_not_create_or_write(), test_open_wizard_types_the_wizard_into_a_new_focused_tab()

### Community 22 - "Community 22"
Cohesion: 0.08
Nodes (15): all_options(), block_ids(), _buttons(), _dialog(), _mrkdwn(), test_chunk_lines(), test_dialog_blocks_escape_and_limits(), test_dialog_blocks_keypad_when_not_parsed() (+7 more)

### Community 23 - "Community 23"
Cohesion: 0.12
Nodes (24): _child_handoff(), FakeCli, last_launch(), make_ops(), test_concurrent_starts_create_one_workspace_and_type_once(), test_config_dir_env_verbatim_prefix_is_stripped(), test_failed_pane_run_releases_the_reservation(), test_launch_creates_workspace_without_focus_and_runs_bridge() (+16 more)

### Community 24 - "Community 24"
Cohesion: 0.13
Nodes (19): run(), activate(), ack_launch(), bridge_command(), clear_ready_marker(), clear_stop_request(), launch_admitted(), module_command() (+11 more)

### Community 25 - "Community 25"
Cohesion: 0.08
Nodes (22): HerdrError, replies(), start_blocked_on(), start(), read(), test_claude_startup_trust_answered_on_pc_sends_the_prompt(), test_codex_trust_drawn_during_the_prompt_delay(), test_deferred_prompt_waits_for_a_second_dialog() (+14 more)

### Community 26 - "Community 26"
Cohesion: 0.22
Nodes (28): assistant(), _lines(), _linked(), _pairs(), queued(), _rewound(), test_conversation_from_synthetic_lines(), test_conversation_skips_garbage_and_a_partial_first_line() (+20 more)

### Community 27 - "Community 27"
Cohesion: 0.12
Nodes (20): NewArgs, parse_command(), parse_new_args(), parse_send_args(), ParseError, resolve_agent(), resolve_workspace(), _scan() (+12 more)

### Community 28 - "Community 28"
Cohesion: 0.15
Nodes (21): Action, check_send_allowed(), check_thread_target(), decide_notification(), decide_resume(), Decision, SendCheck, test_blocked_muted_pc_agent_is_silent() (+13 more)

### Community 29 - "Community 29"
Cohesion: 0.09
Nodes (18): classify_slack_exception(), _definitely_unsent(), SlackTransientError, SlackUncertainError, test_n4_absence_right_after_the_attempt_defers(), test_r9_definite_transient_error_is_still_retried(), test_review9_completion_retried_until_slack_recovers(), test_review9_notifier_thread_retries_in_background() (+10 more)

### Community 30 - "Community 30"
Cohesion: 0.12
Nodes (5): Admission, DeliveryResult, Target, target_of(), session_of()

### Community 31 - "Community 31"
Cohesion: 0.14
Nodes (18): SlackLookupIncomplete, _fail_once(), _msg(), PagedWeb, roots(), test_n4_failed_page_is_unknown(), test_n4_marker_on_a_later_page_is_found(), test_n4_truncated_scan_is_unknown() (+10 more)

### Community 32 - "Community 32"
Cohesion: 0.12
Nodes (14): ConfigError, current_username(), default_slash_command(), _float(), _int(), normalize_slash_command(), sanitize_username(), test_default_slash_command_is_capped() (+6 more)

### Community 33 - "Community 33"
Cohesion: 0.12
Nodes (11): redact(), RedactingFormatter, RedactSecrets, setup_logging(), slack_failure_reason(), test_slack_failure_reason_classification(), _render(), test_log_redaction() (+3 more)

### Community 34 - "Community 34"
Cohesion: 0.12
Nodes (17): _attach_continuations(), _Block, _build_option(), _cursor_menu(), sibling(), _fingerprint(), _has_cursor(), _is_rule() (+9 more)

### Community 35 - "Community 35"
Cohesion: 0.11
Nodes (17): cli_args(), _cli_source(), pipe_path(), _auto_with_fake_pipe(), EofAfterWriteHandle, _read(), ScriptedTransport, test_cli_args() (+9 more)

### Community 36 - "Community 36"
Cohesion: 0.14
Nodes (10): _in_background(), test_recheck2_r6_old_lookup_does_not_clear_new_runs_dirty_flag(), test_recheck_n1_threaded_back_to_back_events(), test_recheck_r6_old_dispatcher_cannot_touch_restarted_manager(), block_dispatcher(), test_review6_stop_start_joins_old_workers_and_drops_old_callbacks(), test_threaded_end_to_end(), _wait() (+2 more)

### Community 37 - "Community 37"
Cohesion: 0.13
Nodes (6): HerdrClient, _looks_complete(), test_cli_transport_success_error_and_plain_read(), test_client_input_request_shapes(), test_client_request_shapes(), test_cli_matches_pipe()

### Community 38 - "Community 38"
Cohesion: 0.09
Nodes (11): cfg_dir(), _Proc, _record_previous(), test_cli_timeout_is_uncertain_but_a_missing_cli_is_not(), test_discover_config_dir(), test_n1_nonzero_exit_is_uncertain_unless_a_known_refusal(), test_plugin_command_errors_are_one_line(), test_r2_early_exit_ack_closes_previous_tab_only_for_our_own_active_start() (+3 more)

### Community 39 - "Community 39"
Cohesion: 0.09
Nodes (16): _apply_config_dir(), _close_previous_tab(), main(), _safe_stdio(), default_config_dir(), discover_config_dir(), plugin_root_from_env(), state_dir_for() (+8 more)

### Community 40 - "Community 40"
Cohesion: 0.13
Nodes (17): pairing_path(), _Handler, make_ops(), _run_bridge(), fake_stop_requested(), slack_line(), state(), test_run_in_owner_mode_marks_ready_and_clears_on_exit() (+9 more)

### Community 41 - "Community 41"
Cohesion: 0.14
Nodes (16): _classify(), Dialog, _index(), keys_for(), _moves(), Option, _rows(), submit_keys() (+8 more)

### Community 43 - "Community 43"
Cohesion: 0.14
Nodes (6): read_pairing_record(), mask_token(), TokenSpec, Wizard, test_mask_token_never_shows_the_whole_token(), test_refuses_without_a_terminal()

### Community 44 - "Community 44"
Cohesion: 0.17
Nodes (21): typed_text(), labels(), parse(), test_ask_boxed_question(), test_ask_multi_select(), test_ask_review(), test_ask_single_select_with_tabs(), test_ask_type_something_with_cursor() (+13 more)

### Community 45 - "Community 45"
Cohesion: 0.16
Nodes (7): StateStore, test_n5_result_commit_interrupted_then_restart_does_not_repost(), test_default_origin_is_pc(), test_rekey_and_find_provisional(), test_review11_nested_input_is_not_retained(), test_review7_failed_save_keeps_memory_and_disk_consistent(), test_thread_map_roundtrip()

### Community 46 - "Community 46"
Cohesion: 0.12
Nodes (9): _slack_error(), TokenError, verify_app_token(), verify_bot_token(), FakeResp, FakeWebClient, test_verification_errors_never_print_the_token(), test_verify_helpers() (+1 more)

### Community 47 - "Community 47"
Cohesion: 0.12
Nodes (10): make_pairing(), announce(), show(), persist(), close_previous_tab(), HerdrCli, PluginError, PluginUncertain (+2 more)

### Community 48 - "Community 48"
Cohesion: 0.18
Nodes (15): StateError, _failing_write(), test_counter_is_monotonic_and_persisted(), test_counter_thread_safe(), test_recheck2_r8_floor_write_failure_aborts_load(), test_recheck2_r8_marker_write_failure_aborts_load_and_keeps_evidence(), test_recheck2_r8_salvaged_counter_survives_restart(), test_recheck_r8_disabled_state_survives_second_restart() (+7 more)

### Community 49 - "Community 49"
Cohesion: 0.14
Nodes (7): Delivery, _norm(), prompt_visible(), _text_hash(), screen_fingerprint(), tail_lines(), test_tail_lines_and_screen_fingerprint()

### Community 50 - "Community 50"
Cohesion: 0.13
Nodes (18): Turn, _preview(), test_busy_claude_agent_shows_history_and_a_note(), test_codex_and_missing_session_fall_back_to_the_last_response(), test_conversation_blocks_budget_with_many_turns(), test_conversation_blocks_char_budget_and_huge_messages(), test_conversation_blocks_note_and_omitted_marks(), test_conversation_blocks_one_huge_answer_alone_fits() (+10 more)

### Community 51 - "Community 51"
Cohesion: 0.11
Nodes (13): _dedent(), parse_dialog(), _parse_tabs(), _plan_excerpt(), _split_generic(), test_empty_screen(), test_fingerprint_changes_with_the_command(), test_fingerprint_covers_the_plan_text() (+5 more)

### Community 52 - "Community 52"
Cohesion: 0.13
Nodes (3): NewAgentRequest, Reply, agent_display_name()

### Community 55 - "Community 55"
Cohesion: 0.16
Nodes (8): HerdrOutcomeUnknown, PipeTransport, _read_line(), _unwrap(), test_new_agent_start_errors_are_reported_via_response_url(), test_pipe_transport_requires_socket(), test_read_line_handles_chunks_and_leftovers(), test_unwrap()

### Community 56 - "Community 56"
Cohesion: 0.14
Nodes (8): AlreadyRunning, InstanceLock, test_run_reports_slack_auth_failure_cleanly(), test_run_with_a_cancelled_launch_id_does_not_start(), test_marker_counts_only_for_the_live_lock_holder(), test_instance_lock_across_processes(), test_instance_lock_in_process(), test_review12_pid_write_failure_releases_lock()

### Community 58 - "Community 58"
Cohesion: 0.13
Nodes (9): AgentTransition, start_codex_sessionless_trust(), test_answer_lock_is_stable_across_rekeying(), test_ended_transition_of_a_provisional_thread_takes_the_click_lock(), test_thread_reply_after_a_rekey(), test_ended_before_completion_notice(), test_provisional_thread_found_after_move_by_terminal(), test_review6_live_transition_without_terminal_evidence_does_not_adopt() (+1 more)

### Community 59 - "Community 59"
Cohesion: 0.27
Nodes (3): WebClientTransport, test_r9a_transport_disables_sdk_retries_so_the_bridge_sees_uncertain(), test_web_client_transport_maps_calls()

### Community 60 - "Community 60"
Cohesion: 0.12
Nodes (3): HomeBridge, PreviewBridge, RecordingBridge

### Community 61 - "Community 61"
Cohesion: 0.16
Nodes (4): atomic_write_json(), mutate(), test_atomic_write_leaves_no_temp_files(), test_recheck2_r8_leftover_corrupt_backup_counts_as_evidence()

### Community 62 - "Community 62"
Cohesion: 0.22
Nodes (8): busy_children(), child_processes(), current_start_time(), process_start_time(), _PROCESSENTRY32W, _win_children(), _win_start_time(), test_real_process_helpers()

### Community 63 - "Community 63"
Cohesion: 0.18
Nodes (3): _inspect(), marker_check(), SlackTransportError

### Community 65 - "Community 65"
Cohesion: 0.22
Nodes (10): Conversation, find_session_file(), last_answer(), projects_dir(), read_tail(), _read_tail(), read_tail_lines(), test_last_answer_from_file() (+2 more)

### Community 66 - "Community 66"
Cohesion: 0.23
Nodes (11): bridge_ws(), hold_bridge(), test_launch_is_a_noop_while_the_bridge_holds_the_lock(), test_r1_bridge_exit_during_stop_puts_nothing_into_its_old_terminal(), test_status_running_shows_verified_location(), test_stop_rechecks_the_process_before_terminating(), test_stop_refuses_when_the_pid_is_not_the_recorded_process(), test_stop_refuses_when_the_record_is_from_another_process() (+3 more)

### Community 67 - "Community 67"
Cohesion: 0.16
Nodes (7): Clock, _concurrently(), honor_stop(), _previous_launch(), test_concurrent_start_and_restart_type_once(), test_restart_stops_then_launches_in_a_fresh_tab(), test_stop_sends_an_addressed_request_and_never_types()

### Community 68 - "Community 68"
Cohesion: 0.23
Nodes (5): ReadResult, moved_env(), HerdrForBridge, test_moved_session_with_failed_destination_keeps_pending_muted_task(), test_provisional_codex_moves_and_reports_session_at_completion()

### Community 69 - "Community 69"
Cohesion: 0.18
Nodes (5): with_block_marker(), MarkerSlack, test_marker_check_reports_round_trip(), test_recheck3_marker_check_reports_slack_errors(), boom()

### Community 70 - "Community 70"
Cohesion: 0.24
Nodes (5): test_default_manifest_uses_username(), test_every_transport_call_has_a_scope_entry(), test_manifest_follows_config(), test_no_direct_web_api_calls_outside_transport(), _transport_calls()

### Community 71 - "Community 71"
Cohesion: 0.33
Nodes (5): AutoTransport, HerdrUnavailable, test_auto_transport_falls_back_to_cli(), test_review1_unsent_request_falls_back_for_mutations(), cannot_open()

### Community 72 - "Community 72"
Cohesion: 0.27
Nodes (6): build_manifest(), manifest_for_config(), required_scopes(), manifest_url(), test_manifest_url_roundtrips_and_fits_the_browser(), test_manifest_url_too_long_falls_back()

### Community 73 - "Community 73"
Cohesion: 0.22
Nodes (5): HerdrEvent, normalize_event_name(), parse_event(), test_normalize_event_name(), test_parse_event_real_envelopes()

### Community 75 - "Community 75"
Cohesion: 0.32
Nodes (5): message_markers(), test_r9_find_message_by_marker(), conversations_history(), conversations_replies(), _messages()

### Community 76 - "Community 76"
Cohesion: 0.29
Nodes (3): CliTransport, test_cli_transport_missing_binary(), test_review1_cli_timeout_is_unknown_outcome()

### Community 77 - "Community 77"
Cohesion: 0.33
Nodes (3): on_keys(), test_a_click_in_flight_does_not_block_sends_to_other_agents(), test_double_click_sends_keys_once()

### Community 79 - "Community 79"
Cohesion: 0.40
Nodes (4): FakeProc, test_cli_send_text_prints_nothing_send_keys_prints_the_envelope(), fake_run(), fake_run()

### Community 85 - "Community 85"
Cohesion: 0.50
Nodes (3): _sdk_client(), test_r9a_connection_refused_is_retryable_not_uncertain(), test_r9a_sdk_retry_would_duplicate_without_the_transport()

### Community 86 - "Community 86"
Cohesion: 0.50
Nodes (3): test_review2_buffered_events_older_than_snapshot_are_ignored(), buffer_changes(), wait_queue()

## Knowledge Gaps
- **1 isolated node(s):** `herdr-slackbot`
  These have ≤1 connection - possible missing edges. (Counts symbols only; 752 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **23 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Bridge` connect `Community 6` to `Community 1`, `Community 2`, `Community 3`, `Community 8`, `Community 9`, `Community 10`, `Community 13`, `Community 14`, `Community 15`, `Community 16`, `Community 18`, `Community 19`, `Community 20`, `Community 24`, `Community 25`, `Community 27`, `Community 28`, `Community 29`, `Community 30`, `Community 31`, `Community 33`, `Community 41`, `Community 42`, `Community 45`, `Community 48`, `Community 49`, `Community 52`, `Community 55`, `Community 57`, `Community 58`, `Community 68`, `Community 90`?**
  _High betweenness centrality (0.148) - this node is a cross-community bridge._
- **Are the 16 inferred relationships involving `Bridge` (e.g. with `LaunchError` and `Config`) actually correct?**
  _`Bridge` has 16 INFERRED edges - model-reasoned connections that need verification._
- **What connects `herdr-slackbot` to the rest of the system?**
  _1 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.05133161512027491 - nodes in this community are weakly interconnected._
- **Why does `build_app()` connect `Community 19` to `Community 33`, `Community 6`, `Community 70`, `Community 27`, `Community 10`, `Community 13`, `Community 14`, `Community 15`, `Community 18`, `Community 24`, `Community 88`?**
  _High betweenness centrality (0.031) - this node is a cross-community bridge._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.05255613951266125 - nodes in this community are weakly interconnected._
- **Why does `HerdrError` connect `Community 25` to `Community 2`, `Community 3`, `Community 6`, `Community 8`, `Community 10`, `Community 17`, `Community 20`, `Community 24`, `Community 30`, `Community 33`, `Community 35`, `Community 36`, `Community 49`, `Community 54`, `Community 55`, `Community 71`, `Community 78`, `Community 79`, `Community 83`?**
  _High betweenness centrality (0.030) - this node is a cross-community bridge._