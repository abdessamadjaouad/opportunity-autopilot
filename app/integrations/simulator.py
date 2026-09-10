"""Test-only persistent mail simulator. No sockets or live provider credentials."""
import json
from pathlib import Path
from threading import Lock

from app.applications.contracts import DefinitiveRejection, ProviderAcceptance


class MailSimulator:
    is_simulator = True

    def __init__(self, path: Path, outcome="accepted"):
        self.path, self.outcome, self.lock = path, outcome, Lock()
        if not path.exists():
            path.write_text(json.dumps({"sent": [], "incoming": []}))
            path.chmod(0o600)

    def send(self, envelope):
        if not envelope.recipient.endswith((".example", ".test", ".invalid")):
            raise DefinitiveRejection("simulator_requires_nonlive_destination")
        with self.lock:
            if self.outcome == "rejected":
                raise DefinitiveRejection("fixture_not_accepted")
            state = json.loads(self.path.read_text())
            identity = f"simulated-{len(state['sent']) + 1}"
            record = {"provider_id": identity, "thread_id": f"thread-{identity}", "message_id": envelope.message_id,
                "recipient": envelope.recipient, "subject": envelope.subject, "text": envelope.text,
                "attachments": [x.filename for x in envelope.attachments], "simulation": True}
            state["sent"].append(record)
            self.path.write_text(json.dumps(state))
            if self.outcome == "timeout_after_acceptance":
                raise TimeoutError("Simulated timeout after acceptance")
            return ProviderAcceptance(identity, record["thread_id"], {"simulation": True})

    def find_sent(self, message_id):
        return [x for x in json.loads(self.path.read_text())["sent"] if x["message_id"] == message_id]

    def poll_messages(self, cursor=None):
        messages = json.loads(self.path.read_text())["incoming"]
        offset = int(cursor or 0)
        return {"messages": messages[offset:], "cursor": str(len(messages))}

    def add_incoming(self, message):
        with self.lock:
            state = json.loads(self.path.read_text())
            state["incoming"].append({**message, "simulation": True})
            self.path.write_text(json.dumps(state))

    @property
    def sent(self):
        return json.loads(self.path.read_text())["sent"]
