import importlib.util
import json
from pathlib import Path
import sqlite3
import pytest
from herdr_bridge.agent_hook import report_claude
from herdr_bridge.session_observation import load_observation
from herdr_bridge.agent_adapters import adapters
from herdr_bridge.core import Bridge
from test_claude_sync import TID, NEW, append, user, answer, end


def test_build_preserves_venv_path_and_emits_both_plugin_manifests(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/build-agent-plugins.py"
    spec = importlib.util.spec_from_file_location("build_agent_plugins", script)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    python = tmp_path / "venv/bin/python"; python.parent.mkdir(parents=True)
    python.symlink_to('/usr/bin/python3')
    root = tmp_path / "packages"
    module.build(root, python=python)
    for kind in ('codex', 'claude'):
        runtime = json.loads((root / kind / 'runtime.json').read_text())
        assert runtime['python'] == str(python)
        manifest = json.loads((root / kind / f'.{kind}-plugin/plugin.json').read_text())
        assert manifest['name'] == 'herdr-bridge-' + kind
        hooks = json.loads((root / kind / 'hooks/hooks.json').read_text())['hooks']
        assert set(hooks) == {'SessionStart', 'UserPromptSubmit'}
        assert (root / kind / 'hooks/run.py').is_file()


def test_registry_exposes_same_durable_source_contract_without_recreating_codex_tables(tmp_path):
    db = sqlite3.connect(tmp_path / 'db')
    registry = adapters(db)
    assert set(registry) == {'codex', 'claude'}
    for agent, adapter in registry.items():
        assert adapter.source.agent_kind == agent
        for method in ('sync', 'bind', 'bound_info', 'diagnostic', 'claim_completion'):
            assert callable(getattr(adapter.source, method))
    registry['codex'].source._status({'terminal_id': 'term'}, 'syncing', thread_id=TID)
    db.commit()
    assert adapters(db)['codex'].source.diagnostic({'terminal_id': 'term'})['thread_id'] == TID
    assert registry['claude'].source.diagnostic({'terminal_id': 'term'})['state'] == 'pending'


def test_claude_plugin_proves_ancestor_registry_and_preserves_missing_file_cutoff(tmp_path, monkeypatch):
    monkeypatch.setattr('herdr_bridge.agent_hook.foreground_claude_session', lambda info, process, **kwargs: TID)
    class Client:
        info = {'agent': 'claude', 'pane_id': 'w1:p1', 'terminal_id': 'term-a', 'cwd': '/project'}
        calls = []
        def list_agents(self): return [self.info.copy()]
        def get_agent(self, pane): return self.info.copy()
        def process_info(self, pane): return {'pane_id': pane, 'foreground_processes': [{'pid': 123, 'argv': ['claude']}]}
        def request(self, method, params): self.calls.append((method, params))
    client = Client()
    env = {'HERDR_ENV': '1', 'HERDR_SOCKET_PATH': '/socket', 'HERDR_PANE_ID': 'stale:p9'}
    payload = {'session_id': TID, 'cwd': '/project', 'hook_event_name': 'SessionStart', 'source': 'startup'}
    assert report_claude(payload, env, client, home=tmp_path, seq=10, pids={123})
    assert client.calls[0][1]['pane_id'] == 'w1:p1'
    assert client.calls[0][1]['agent'] == 'claude'
    assert client.calls[0][1]['source'] == 'herdr:claude'
    # SessionStart can precede first JSONL creation, but must not lose a fast turn.
    path = tmp_path / 'projects/-project' / (TID + '.jsonl'); path.parent.mkdir(parents=True)
    append(path, [user(), answer(), end()])
    assert load_observation(tmp_path, client.info, (TID, path, '/project'))['offset'] == 0
    assert not report_claude(payload, env, client, home=tmp_path, seq=9, pids={123})
    assert not report_claude(payload, env, client, home=tmp_path, seq=11, pids={456})
    assert not report_claude({**payload, 'session_id': NEW}, env, client, home=tmp_path, seq=11, pids={123})
    assert len(client.calls) == 1


def test_claude_hook_revalidates_session_after_registry_read(tmp_path, monkeypatch):
    states = iter([TID, NEW])
    monkeypatch.setattr('herdr_bridge.agent_hook.foreground_claude_session', lambda *args, **kwargs: next(states))
    class Client:
        info = {'agent':'claude', 'pane_id':'w1:p1', 'terminal_id':'term-a', 'cwd':'/project'}
        def list_agents(self): return [self.info]
        def get_agent(self, pane): return self.info
        def process_info(self, pane): return {'pane_id':pane,'foreground_processes':[{'pid':123}]}
        def request(self,*args): pytest.fail('stale registration')
    assert not report_claude({'session_id':TID,'cwd':'/project','hook_event_name':'UserPromptSubmit'}, {'HERDR_ENV':'1','HERDR_SOCKET_PATH':'/socket'}, Client(), home=tmp_path, pids={123})


def test_bridge_claude_sync_and_bind_route_to_its_adapter(tmp_path):
    class Client:
        info = {'agent':'claude','pane_id':'w1:p1','terminal_id':'term-a','cwd':'/project','agent_status':'idle','agent_session':{'value':TID}}
        def get_agent(self,pane): return self.info.copy()
        def list_agents(self): return [self.info.copy()]
    client = Client(); bridge = Bridge(client, tmp_path / 'bridge.sqlite')
    home = tmp_path / 'claude'; path = home / 'projects/-project' / (TID + '.jsonl'); path.parent.mkdir(parents=True)
    append(path, [user('old'), answer('old'), end()])
    bridge.adapters['claude'].source.home = home
    bridge.sync_conversation(client.info)
    append(path, [user(), answer(), end()])
    bridge.sync_conversation(client.info)
    rows = [json.loads(r[0]) for r in bridge.db.execute('SELECT event FROM outbox ORDER BY rowid')]
    assert [(r['role'],r['text'],r['agent']['kind']) for r in rows] == [('user','prompt','claude'),('assistant','answer','claude')]
    assert bridge.sync_status('w1:p1')['thread_id'] == TID
    assert bridge.bind_session('w1:p1',TID)['state'] == 'syncing'
    assert not bridge.db.execute('SELECT * FROM codex_sync_cursors').fetchall()
    bridge.close()


def test_periodic_scan_recovers_a_final_written_after_idle_transition_once(tmp_path):
    class Client:
        info = {'agent':'claude','pane_id':'w1:p1','terminal_id':'term-a','cwd':'/project','agent_status':'idle','agent_session':{'value':TID}}
        def get_agent(self,pane): return self.info.copy()
        def list_agents(self): return [self.info.copy()]
        def read_agent_once(self,*args,**kwargs): return 'test answer'
    client = Client(); bridge = Bridge(client, tmp_path / 'bridge.sqlite')
    home = tmp_path / 'claude'; path = home / 'projects/-project' / (TID + '.jsonl'); path.parent.mkdir(parents=True)
    append(path, [user('old'), answer('old'), end()])
    bridge.adapters['claude'].source.home = home
    bridge.discover(client.info)
    append(path, [user(), answer()])
    bridge.discover(client.info)
    assert not bridge.db.execute("SELECT 1 FROM outbox WHERE event LIKE '%agent.done%'").fetchall()
    append(path, [end()])
    bridge.discover(client.info)
    bridge.discover(client.info)
    events = [json.loads(r[0]) for r in bridge.db.execute('SELECT event FROM outbox ORDER BY rowid')]
    assert [e['type'] for e in events] == ['conversation.message','conversation.message','agent.done']
    bridge.close()


def test_retiring_legacy_hook_preserves_managed_other_handlers_and_trust(tmp_path):
    script = Path(__file__).resolve().parents[1] / 'scripts/install-codex-session-hook.py'
    spec = importlib.util.spec_from_file_location('retire_bridge_hooks',script)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    old = {'hooks':{'SessionStart':[{'hooks':[{'command':'managed-herdr'}, {'command':'/python -m herdr_bridge.session_hook'}]}],
                    'UserPromptSubmit':[{'hooks':[{'command':'/python -m herdr_bridge.session_hook'}]}]}}
    path = tmp_path/'hooks.json'; path.write_text(json.dumps(old))
    config = tmp_path/'config.toml'; config.write_text('trusted_hash = "untouched"\n')
    assert module.remove(tmp_path)['changed']
    result = json.loads(path.read_text())
    assert result['hooks']['SessionStart'] == [{'hooks':[{'command':'managed-herdr'}]}]
    assert result['hooks']['UserPromptSubmit'] == []
    assert config.read_text() == 'trusted_hash = "untouched"\n'
    assert not module.remove(tmp_path)['changed']


@pytest.mark.parametrize('agent,fixture', [('codex','dialog_codex_approval.txt'),('claude','dialog_claude_permission.txt')])
def test_both_agents_use_one_shot_approval_contract_with_retained_context(tmp_path,agent,fixture):
    class Client:
        info = {'agent':agent,'pane_id':'w1:p1','terminal_id':'term-a','cwd':'/project',
                'agent_status':'blocked','agent_session':{'value':TID},'state_change_seq':7}
        screen = (Path(__file__).parent/'fixtures/transcripts'/fixture).read_text()
        keys = []
        def get_agent(self,pane): return self.info.copy()
        def list_agents(self): return [self.info.copy()]
        def read_agent_once(self,*args,**kwargs): return self.screen
        def send_keys(self,pane,keys): self.keys.append((pane,keys))
    client = Client(); bridge = Bridge(client,tmp_path/'db',notification_delay=0,confirmation_timeout=0)
    bridge.discover(client.info)
    request = bridge.pending()[0]['id']
    prepared = bridge.prepare_answer(request,0)
    assert not client.keys
    result = bridge.dispatch_reserved(request,prepared['attempt_id'])
    assert result['outcome'] == 'unconfirmed'
    assert result['card_context']['text'] == client.screen
    assert result['card_context']['agent']['kind'] == agent
    assert bridge.answer(request,0)['outcome'] == 'unconfirmed'
    assert client.keys == [('w1:p1',['1'])]
    bridge.close()
