"""One JSONL file per run with everything needed to replay what happened.

Only data passed in explicitly is written. The API key never reaches this
module, and tool outputs never contain the webhook URL.
"""

import json
from datetime import datetime
from pathlib import Path

from strom_agent.prices import BERLIN


class Trace:
    def __init__(self, traces_dir: Path, label: str = "run") -> None:
        stamp = datetime.now(BERLIN).strftime("%Y%m%dT%H%M%S%f")
        self.path = traces_dir / f"{stamp}-{label}.jsonl"
        traces_dir.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **fields: object) -> None:
        record = {"time": datetime.now(BERLIN).isoformat(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as file:
            # default=str covers dates and other simple values without custom code.
            file.write(json.dumps(record, default=str) + "\n")
