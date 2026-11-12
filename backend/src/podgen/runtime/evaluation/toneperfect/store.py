import fcntl
import json
from contextlib import contextmanager
from pathlib import Path

from podgen.domain.errors import Conflict
from podgen.runtime.evaluation.audio.immutable_export import write_bytes, write_json


class Store:
    def __init__(self, root):
        self.root = Path(root)
        if self.root.exists() and not (self.root / "binding.json").exists():
            if any(p.name != ".toneperfect.lock" for p in self.root.iterdir()):
                raise Conflict("toneperfect_foreign_directory")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def lock(self):
        with (self.root / ".toneperfect.lock").open("ab") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def get(self, name):
        path = self.root / name
        return json.loads(path.read_bytes()) if path.exists() else None

    def put(self, name, value):
        write_json(self.root / name, value)

    def bytes(self, name):
        return (self.root / name).read_bytes()

    def blob(self, name, body):
        write_bytes(self.root / name, body)

    def attempts(self, case):
        result = []
        for number in range(1, 4):
            key = f"cases/{case['case_id']}/attempt-{number}"
            intent = self.get(key + "/submit.json")
            if intent is None:
                break
            result.append(
                dict(
                    key=key,
                    intent=intent,
                    receipt=self.get(key + "/receipt.json"),
                    result=self.get(key + "/result.json"),
                    failure=self.get(key + "/failure.json"),
                )
            )
        return result
