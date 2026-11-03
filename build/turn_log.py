"""Local JSONL pipeline events. Never records API credentials or HTTP bodies."""

from datetime import datetime, timezone
import json
from pathlib import Path
import threading


class TurnLog:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.failed = False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=False)

    def write(self, event: str, **data):
        if self.failed:
            return
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **data}
        try:
            with self.lock, self.path.open("a") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        except (OSError, ValueError):
            self.failed = True
            print("WARNING: writing the session log failed. Terminal output remains available.", flush=True)
