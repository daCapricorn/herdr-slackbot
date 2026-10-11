import copy
import json
import sqlite3

import pytest

from herdr_bridge.channels import PaneChannels
from herdr_bridge.core import Bridge, BridgeError
from test_openclaw_bridge import Client, request


PANE = {"pane_id":"w4:p2", "terminal_id":"term-a", "name":"codex-slackbot", "cwd":"/project"}


def ready(store, rec, channel="C123"):
    return store.save({**rec,"state":"ready","channel_id":channel,"team_id":"T1"})


def test_rename_move_and_agent_replacement_retain_the_same_channel(tmp_path):
    db = sqlite3.connect(tmp_path / "channels.sqlite")
    store = PaneChannels(db)
    first = ready(store, store.reconcile("default", [PANE])[0])
    renamed = store.reconcile("default", [{**PANE,"name":"new-name","model":"new-model"}])[0]
    moved = store.reconcile("default", [{**PANE,"pane_id":"w7:p9"}])[0]
    assert renamed["id"] == moved["id"] == first["id"]
    assert renamed["name"] == "new-name" and renamed["channel_id"] == "C123"
    assert moved["pane_id"] == "w7:p9" and moved["channel_id"] == "C123"
    assert len(store.all("default")) == 1
    db.close()


def test_restart_preserves_binding_and_empty_panes_are_also_registered(tmp_path):
    path = tmp_path / "channels.sqlite"
    db = sqlite3.connect(path)
    store = PaneChannels(db)
    first = ready(store, store.reconcile("default", [PANE])[0])
    db.commit()
    db.close()
    db = sqlite3.connect(path)
    store = PaneChannels(db)
    assert store.reconcile("default", [PANE])[0] == first
    assert first["id"] == store.route("default",PANE["pane_id"],"term-a")["binding_id"]
    db.close()


def test_closed_and_reused_pane_id_does_not_reuse_a_channel(tmp_path):
    db = sqlite3.connect(tmp_path / "channels.sqlite")
    store = PaneChannels(db)
    first = ready(store,store.reconcile("default",[PANE])[0])
    store.reconcile("default",[])
    records = store.reconcile("default",[{**PANE,"terminal_id":"term-b"}])
    new = next(r for r in records if not r["closed"])
    assert new["id"] != first["id"] and new["channel_id"] is None
    assert store.from_channel("default","C123")["closed"]
    # Delayed completion/results retain their historical destination.
    assert store.route("default","w4:p2","term-a")["conversationId"] == "C123"
    assert store.route("default","w4:p2","term-b") is None
    assert store.from_channel("other","C123") is None
    db.close()


def test_ambiguous_inventory_cannot_close_existing_bindings(tmp_path):
    db = sqlite3.connect(tmp_path / "channels.sqlite")
    store = PaneChannels(db)
    store.reconcile("default",[PANE])
    with pytest.raises(ValueError,match="Ambiguous"):
        store.reconcile("default",[PANE,{**PANE,"pane_id":"w5:p1"}])
    assert not store.all("default")[0]["closed"]
    db.close()


class InventoryClient(Client):
    def list_panes(self):
        return [{**PANE,"tab_id":"w4:t2","workspace_id":"w4"},
                {"pane_id":"w4:p3","terminal_id":"term-shell","tab_id":"w4:t3","workspace_id":"w4"}]

    def list_workspaces(self):
        return [{"workspace_id":"w4"}]

    def list_tabs(self, workspace_id):
        return [{"tab_id":"w4:t2","label":"tab-name"},{"tab_id":"w4:t3","label":"empty-shell"}]


@pytest.fixture
def core(tmp_path):
    client = InventoryClient()
    bridge = Bridge(client,tmp_path / "bridge.sqlite",clock=lambda:100,notification_delay=0)
    yield client,bridge
    bridge.close()


def test_inventory_includes_non_agent_panes_and_failed_reads_preserve_bindings(core):
    client,bridge = core
    bindings = bridge.pane_channels("default")
    assert {r["name"] for r in bindings} == {"codex-slackbot","empty-shell"}
    def unreadable():
        raise OSError("disconnected")
    client.list_panes = unreadable
    with pytest.raises(OSError):
        bridge.pane_channels("default")
    assert all(not r["closed"] for r in bridge.channels.all("default"))


def test_delivery_route_is_persisted_and_wrong_channel_callbacks_cannot_bind(core):
    client,bridge = core
    token = request(client,bridge)
    event = bridge.events()[0]
    binding = bridge.pane_channels("default")[0]
    assert bridge.route_event(event["id"],"default") is None
    bridge.channel_provisioned(binding["id"],"C123","T1")
    assert bridge.route_event(event["id"],"default") is None
    bridge.channel_ready(binding["id"],"herdr-name","topic")
    route = bridge.route_event(event["id"],"default")
    assert route["to"] == "channel:C123"
    card = {"channel":"slack","accountId":"default","conversationId":"C123","messageTs":"123.456"}
    bridge.bind_card(token,card)
    assert bridge.result_cards(token)["cards"] == [card]
    for account,channel in [("other","C123"),("default","C456"),("default","D123")]:
        with pytest.raises(BridgeError,match="different pane channel"):
            bridge.authorize_request(token,account,channel)
    with bridge.db:
        update = {"id":"result","type":"approval.updated","result":{"request_id":token}}
        bridge.db.execute("INSERT INTO outbox VALUES (?,?)",("result",json.dumps(update)))
    assert bridge.route_event("result","default") == route
    assert bridge.route_event(event["id"],"default") == route
    assert not client.keys and not client.prompts


def test_channel_binding_cannot_change_workspace_and_active_channel_cannot_archive(core):
    _,bridge = core
    binding = bridge.pane_channels("default")[0]
    bridge.channel_provisioned(binding["id"],"C123","T1")
    with pytest.raises(BridgeError,match="another channel"):
        bridge.channel_provisioned(binding["id"],"C123","T2")
    with pytest.raises(BridgeError,match="Close"):
        bridge.channel_archived(binding["id"])


def test_legacy_dm_cards_still_work_but_cannot_be_answered_in_a_channel(core):
    client,bridge = core
    token = request(client,bridge)
    bridge.bind_card(token,{"channel":"slack","accountId":"default","conversationId":"D123","messageTs":"123.456"})
    with pytest.raises(BridgeError,match="Legacy"):
        bridge.authorize_request(token,"default","C123")
    assert not client.keys


def test_unprovisioned_events_cannot_starve_another_ready_channel(core):
    client,bridge = core
    bindings = bridge.pane_channels("default")
    shell = next(b for b in bindings if b["terminal_id"] == "term-shell")
    bridge.channel_provisioned(shell["id"],"C456","T1")
    bridge.channel_ready(shell["id"],"shell","topic")
    with bridge.db:
        for _ in range(25):
            bridge._queue("agent.done",client.info,text="unprovisioned")
        bridge._queue("agent.done",{"pane_id":"w4:p3","terminal_id":"term-shell"},text="ready")
    assert [e["text"] for e in bridge.events(account_id="default")] == ["ready"]
