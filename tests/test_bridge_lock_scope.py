"""Real blocked fake evidence reads must not serialize unrelated state work."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from herdr_bridge.core import Bridge
from test_openclaw_bridge import Client, request


def blocked_call(bridge, monkeypatch, target, action, competing):
    entered, release = Event(), Event()
    original = getattr(bridge.client, target)
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'blocked fake was not released'
        return original(*args, **kwargs)
    monkeypatch.setattr(bridge.client, target, blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = pool.submit(action)
        try:
            assert entered.wait(2)
            other = pool.submit(competing)
            assert other.result(timeout=.5) is not None
        finally:
            release.set()
        return work.result(timeout=2)


@pytest.mark.parametrize('case', ['pending', 'delivery', 'prepare'])
def test_blocked_external_evidence_does_not_block_ack(tmp_path, monkeypatch, case):
    client = Client()
    bridge = Bridge(client, tmp_path / 'state.sqlite', confirmation_timeout=0, notification_delay=0)
    try:
        token = request(client, bridge)
        event = bridge.events()[0]
        target, action = {
            'pending': ('list_agents', bridge.pending),
            'delivery': ('read_agent_once', lambda: bridge.event_for_delivery(event['id'])),
            'prepare': ('read_agent_once', lambda: bridge.prepare_answer(token, option=0)),
        }[case]
        result = blocked_call(bridge, monkeypatch, target, action, lambda: bridge.ack(event['id']))
        assert bridge.db.execute('SELECT id FROM outbox WHERE id=?', (event['id'],)).fetchall() == []
        if case == 'delivery':
            assert result is None
    finally:
        bridge.close()


def test_ready_result_events_bypass_attention_probe(tmp_path, monkeypatch):
    client = Client()
    bridge = Bridge(client, tmp_path / 'state.sqlite', notification_delay=0)
    try:
        request(client, bridge)
        ready = {'id': 'ready', 'type': 'input.updated', 'result': {'outcome': 'confirmed'}}
        with bridge.lock, bridge.db:
            bridge.db.execute('INSERT INTO outbox VALUES (?,?)', ('ready', json.dumps(ready)))
        def forbidden(*args, **kwargs):
            raise AssertionError('ready results must not wait for attention inventory')
        monkeypatch.setattr(client, 'list_agents', forbidden)
        monkeypatch.setattr(client, 'get_agent', forbidden)
        assert ready in bridge.events()
    finally:
        bridge.close()


def test_prepare_owner_is_nonblocking_and_same_state_mutation_discards_proof(tmp_path, monkeypatch):
    client = Client()
    bridge = Bridge(client, tmp_path / 'state.sqlite', confirmation_timeout=0, notification_delay=0)
    entered, release = Event(), Event()
    original = client.read_agent_once
    try:
        token = request(client, bridge)
        def blocked(*args, **kwargs):
            assert not bridge.lock._is_owned() and not bridge.db.in_transaction
            entered.set()
            assert release.wait(3)
            return original(*args, **kwargs)
        monkeypatch.setattr(client, 'read_agent_once', blocked)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(bridge.prepare_answer, token, 0)
            try:
                assert entered.wait(2)
                duplicate = pool.submit(bridge.prepare_answer, token, 0).result(timeout=.5)
                assert duplicate['outcome'] == 'checking' and not duplicate['ready_to_dispatch']
                with bridge.lock, bridge.db:
                    rec, state = bridge._record(token)
                    rec['fingerprint'] = 'new dialog, same pending state'
                    bridge._save(rec, state)
            finally:
                release.set()
            assert not first.result(timeout=2)['ready_to_dispatch']
        assert not bridge.operations and not client.keys
        with bridge.lock:
            assert bridge._record(token)[1] == 'pending'
    finally:
        release.set()
        bridge.close()


def test_pending_old_proof_cannot_supersede_concurrent_reservation(tmp_path, monkeypatch):
    client = Client()
    bridge = Bridge(client, tmp_path / 'state.sqlite', confirmation_timeout=0, notification_delay=0)
    entered, release = Event(), Event()
    try:
        token = request(client, bridge)
        original = client.list_agents
        def obsolete_inventory():
            stale = original()
            stale[0]['agent_status'] = 'done'
            entered.set()
            assert release.wait(3)
            return stale
        monkeypatch.setattr(client, 'list_agents', obsolete_inventory)
        with ThreadPoolExecutor(max_workers=2) as pool:
            work = pool.submit(bridge.pending)
            try:
                assert entered.wait(2)
                reservation = bridge.prepare_answer(token, 0)
                assert reservation['ready_to_dispatch']
            finally:
                release.set()
            work.result(timeout=2)
        with bridge.lock:
            assert bridge._record(token)[1] == 'reserved'
    finally:
        release.set()
        bridge.close()


from test_durable_completions import native_bridge, NATIVE, write_rows


def test_source_native_read_is_unlocked_and_old_cursor_batch_cannot_regress(native_bridge, monkeypatch):
    bridge, client, path, _ = native_bridge
    source = bridge.adapters['codex'].source
    write_rows(path, NATIVE[4:8])
    entered, release = Event(), Event()
    original = source.read_sync_batch
    calls = []
    def blocked(snapshot):
        assert not bridge.lock._is_owned() and not bridge.db.in_transaction
        batch = original(snapshot)
        calls.append(batch)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return batch
    monkeypatch.setattr(source, 'read_sync_batch', blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(bridge.sync_conversation, client.info)
        try:
            assert entered.wait(2)
            bridge.ack('absent')
            write_rows(path, NATIVE[8:12])
            assert bridge.sync_conversation(client.info) > 0
            with bridge.lock:
                cursor = bridge.db.execute('SELECT offset FROM codex_sync_cursors').fetchone()[0]
        finally:
            release.set()
        assert old.result(timeout=2) == 0
    with bridge.lock:
        assert bridge.db.execute('SELECT offset FROM codex_sync_cursors').fetchone()[0] == cursor
        assert bridge.db.execute('SELECT COUNT(*) FROM codex_completions').fetchone()[0] == 2


from test_codex_fresh_input import fresh
from test_codex_input_readiness import persist
from test_terminal_inputs import ingest


def test_native_readiness_scan_is_unlocked_and_cache_compare_rejects_old_proof(fresh, monkeypatch):
    home, native, client, bridge = fresh
    persist(home)
    source = bridge.adapters['codex'].source
    entered, release = Event(), Event()
    original = source._input_lifecycle
    def blocked(*args, **kwargs):
        assert not bridge.lock._is_owned() and not bridge.db.in_transaction
        evidence = original(*args, **kwargs)
        entered.set()
        assert release.wait(3)
        return evidence
    monkeypatch.setattr(source, '_input_lifecycle', blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = pool.submit(ingest, bridge, '100.1')
        try:
            assert entered.wait(2)
            pool.submit(bridge.ack, 'absent').result(timeout=.5)
            from herdr_bridge.events import session_of
            info = bridge._agent('w1:p1')
            key = f"{info['terminal_id']}:{session_of(info)}:owner"
            with bridge.lock, bridge.db:
                bridge.db.execute('INSERT OR REPLACE INTO codex_input_readiness VALUES (?,?)', (key, '{}'))
        finally:
            release.set()
        result = work.result(timeout=2)
    assert result['outcome'] == 'queued' and not client.prompts
    assert not bridge.operations


def test_slow_terminal_A_does_not_block_status_or_terminal_B_dispatch(tmp_path, monkeypatch):
    import copy
    client = Client()
    agents = {'w4:p2': client.info, 'w4:p3': {**client.info, 'pane_id': 'w4:p3', 'terminal_id': 'term-b', 'agent_session': {'value': 'session-b'}}}
    client.get_agent = lambda pane: copy.deepcopy(agents[pane])
    client.list_agents = lambda: copy.deepcopy(list(agents.values()))
    bridge = Bridge(client, tmp_path / 'state.sqlite', confirmation_timeout=0, notification_delay=0)
    entered, release = Event(), Event()
    try:
        bridge.discover(agents['w4:p2'])
        bridge.discover(agents['w4:p3'])
        pending = {rec['pane_id']: rec['id'] for rec in bridge.pending()}
        original = client.read_agent_once
        def read(pane, *args, **kwargs):
            assert not bridge.lock._is_owned() and not bridge.db.in_transaction
            if pane == 'w4:p2':
                entered.set()
                assert release.wait(3)
            return original(pane, *args, **kwargs)
        monkeypatch.setattr(client, 'read_agent_once', read)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(bridge.prepare_answer, pending['w4:p2'], 0)
            try:
                assert entered.wait(2)
                assert len(pool.submit(bridge.status).result(timeout=.5)) == 2
                second = pool.submit(bridge.answer, pending['w4:p3'], 0).result(timeout=.5)
                assert second['outcome'] == 'unconfirmed'
                assert len(client.keys) == 1 and client.keys[0][0] == 'w4:p3'
            finally:
                release.set()
            assert first.result(timeout=2)['ready_to_dispatch']
    finally:
        release.set()
        bridge.close()


@pytest.mark.parametrize('partial', [False, True])
def test_oversized_transcript_row_preserves_boundary_and_following_completion(native_bridge, partial):
    bridge, client, path, _ = native_bridge
    huge = json.dumps({'type': 'event_msg', 'payload': {'type': 'noise', 'data': 'x' * (16 * 1024 * 1024 + 16)}}).encode()
    original = path.stat().st_size
    with path.open('ab') as stream:
        stream.write(huge + (b'' if partial else b'\n'))
    assert bridge.sync_conversation(client.info) == (0 if partial else len(huge) + 1)
    with bridge.lock:
        assert bridge.db.execute('SELECT offset FROM codex_sync_cursors').fetchone()[0] == (original if partial else original + len(huge) + 1)
    if partial:
        with path.open('ab') as stream:
            stream.write(b'\n')
        assert bridge.sync_conversation(client.info) == len(huge) + 1
    write_rows(path, NATIVE[4:8])
    assert bridge.sync_conversation(client.info) > 0
    with bridge.lock:
        assert bridge.db.execute('SELECT COUNT(*) FROM codex_completions').fetchone()[0] == 1


def test_question_cache_different_sources_do_not_share_slow_reader_lock(tmp_path, monkeypatch):
    from herdr_bridge import questions
    source = questions.CodexQuestionSource(tmp_path)
    entered, release = Event(), Event()
    paths = {tid: tmp_path / f'{tid}.jsonl' for tid in ('A', 'B')}
    for tid, path in paths.items():
        path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': tid, 'cwd': '/project'}}) + '\n')
    monkeypatch.setattr(questions, 'resolve_session', lambda info, **kwargs: (info['tid'], paths[info['tid']], '/project'))
    original = questions._Rollout.read
    def read(reader):
        assert not source.lock._is_owned()
        if reader.thread_id == 'A':
            entered.set()
            assert release.wait(3)
        return original(reader)
    monkeypatch.setattr(questions._Rollout, 'read', read)
    info = {'agent': 'codex', 'cwd': '/project'}
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(source.snapshot, {**info, 'tid': 'A'})
        try:
            assert entered.wait(2)
            assert pool.submit(source.snapshot, {**info, 'tid': 'B'}).result(timeout=.5)['thread_id'] == 'B'
        finally:
            release.set()
        assert slow.result(timeout=2)['thread_id'] == 'A'


def test_watch_expiry_is_durable_and_repeat_reconciliation_emits_no_update(tmp_path, monkeypatch):
    import sqlite3
    client = Client()
    now = [100]
    state_path = tmp_path / 'state.sqlite'
    bridge = Bridge(client, state_path, clock=lambda: now[0], confirmation_timeout=0,
                    notification_delay=0, followup_timeout=1)
    try:
        token = request(client, bridge)
        assert bridge.answer(token, 0)['outcome'] == 'unconfirmed'
        now[0] += 2
        stores = []
        original = bridge._store_verification
        def store(rec, verification):
            stores.append(rec['id'])
            return original(rec, verification)
        monkeypatch.setattr(bridge, '_store_verification', store)
        bridge.reconcile_answers()
        with sqlite3.connect(state_path) as observer:
            raw, state = observer.execute('SELECT record,state FROM approvals WHERE id=?', (token,)).fetchone()
            assert json.loads(raw)['watch_expired'] is True and state == 'unconfirmed'
            events = observer.execute("SELECT id,event FROM outbox WHERE id LIKE 'approval-update-%'").fetchall()
        bridge.reconcile_answers()
        assert stores == [token]
        with sqlite3.connect(state_path) as observer:
            assert observer.execute("SELECT id,event FROM outbox WHERE id LIKE 'approval-update-%'").fetchall() == events
        assert len(events) == 1 and len(client.keys) == 1
    finally:
        bridge.close()


def test_check_preserves_route_written_after_its_initial_snapshot(tmp_path, monkeypatch):
    client = Client()
    bridge = Bridge(client, tmp_path / 'state.sqlite', clock=lambda: 100,
                    confirmation_timeout=0, notification_delay=0)
    try:
        token = request(client, bridge)
        event_id = bridge.events()[0]['id']
        assert bridge.answer(token, 0)['outcome'] == 'unconfirmed'
        route = {'accountId': 'default', 'conversationId': 'C1'}
        monkeypatch.setattr(bridge.channels, 'route', lambda *args: route)
        original = bridge._claim_operation
        def claim(rec):
            owner = original(rec)
            assert bridge.route_event(event_id, 'default') == route
            return owner
        monkeypatch.setattr(bridge, '_claim_operation', claim)
        bridge.check(token, timeout=0)
        rec, _ = bridge._record(token)
        assert rec['delivery_route'] == route
    finally:
        bridge.close()
