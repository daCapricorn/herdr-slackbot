import json
import sqlite3

from herdr_bridge.metadata import session_model


def test_exact_session_uses_latest_turn_model_not_workspace_default(tmp_path):
    rollout = tmp_path / "session.jsonl"
    rollout.write_text("\n".join(json.dumps({"type": "turn_context", "payload": {"model": model}})
                                 for model in ("previous-model", "current-model")))
    db = sqlite3.connect(tmp_path / "state_5.sqlite")
    db.execute("CREATE TABLE threads (id, model, rollout_path, cwd, archived, agent_path)")
    db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, 0, NULL)", ("session-a", "cached-model", str(rollout), "/project"))
    db.commit()
    db.close()
    (tmp_path / "config.toml").write_text('model = "default-model"')
    info = {"agent": "codex", "cwd": "/project", "agent_session": {"value": "session-a"}}
    assert session_model(info, codex_home=tmp_path) == "current-model"


def test_workspace_fallback_requires_unique_pane_and_unique_root_session(tmp_path):
    db = sqlite3.connect(tmp_path / "state_5.sqlite")
    db.execute("CREATE TABLE threads (id, model, rollout_path, cwd, archived, agent_path)")
    db.executemany("INSERT INTO threads VALUES (?, ?, '', '/project', 0, ?)",
                   [("root-a", "model-a", None), ("child", "child-model", "/root/child")])
    db.commit()
    info = {"agent": "codex", "cwd": "/project"}
    assert session_model(info, codex_home=tmp_path) is None
    assert session_model(info, codex_home=tmp_path, unique_workspace=True) == "model-a"
    db.execute("INSERT INTO threads VALUES ('root-b', 'model-b', '', '/project', 0, NULL)")
    db.commit()
    assert session_model(info, codex_home=tmp_path, unique_workspace=True) is None
    db.close()
