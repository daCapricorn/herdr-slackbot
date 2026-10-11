"""Durable pane destinations, separate from per-agent approval identities."""
import json
import uuid


class PaneChannels:
    def __init__(self, db):
        self.db = db
        db.execute("""CREATE TABLE IF NOT EXISTS pane_channels
                      (id TEXT PRIMARY KEY, account_id TEXT NOT NULL, record TEXT NOT NULL)""")

    def all(self, account_id):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT record FROM pane_channels WHERE account_id=? ORDER BY rowid", (account_id,))]

    def get(self, binding_id):
        row = self.db.execute("SELECT record FROM pane_channels WHERE id=?", (binding_id,)).fetchone()
        if not row:
            raise ValueError("Unknown pane channel binding")
        return json.loads(row[0])

    def save(self, record):
        self.db.execute("INSERT OR REPLACE INTO pane_channels VALUES (?,?,?)",
                        (record["id"], record["account_id"], json.dumps(record)))
        return record

    def reconcile(self, account_id, panes):
        terminals = [p["terminal_id"] for p in panes if p.get("terminal_id")]
        if len(terminals) != len(set(terminals)):
            raise ValueError("Ambiguous pane inventory")
        records = self.all(account_id)
        seen = set()
        for pane in panes:
            terminal = pane.get("terminal_id")
            if not terminal:
                continue
            matches = [r for r in records if r["terminal_id"] == terminal and not r.get("closed")]
            if len(matches) > 1:
                raise ValueError("Ambiguous terminal channel binding")
            rec = matches[0] if matches else {
                "id": uuid.uuid4().hex, "account_id": account_id, "terminal_id": terminal,
                "channel_id": None, "state": "creating", "closed": False,
            }
            if not matches:
                records.append(rec)
            # A moved terminal adopts its existing channel under the new pane ID.
            rec.update({k: pane.get(k) for k in ("pane_id", "name", "cwd", "model")})
            seen.add(rec["id"])
            self.save(rec)
        for rec in records:
            if rec["id"] not in seen and not rec.get("closed"):
                rec["closed"] = True
                self.save(rec)
        return records

    def route(self, account_id, pane_id, terminal_id):
        matches = [r for r in self.all(account_id) if r["terminal_id"] == terminal_id
                   and r["state"] == "ready" and r.get("channel_id")]
        if len(matches) != 1:
            return None
        rec = matches[0]
        return {"channel": "slack", "accountId": account_id, "to": "channel:" + rec["channel_id"],
                "conversationId": rec["channel_id"], "binding_id": rec["id"], "team_id": rec["team_id"]}

    def from_channel(self, account_id, conversation_id):
        matches = [r for r in self.all(account_id) if r.get("channel_id") == conversation_id]
        if len(matches) > 1:
            raise ValueError("Ambiguous pane channel")
        return matches[0] if matches else None
