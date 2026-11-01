"""Structured JSONL logging with secret redaction."""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import time
from pathlib import Path

_STD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()) | {"message", "asctime"}
_REDACT = [
    (re.compile(r"(closedcaption(?:/seq)?)\?[^\s\"']*", re.I), r"\1?<redacted>"),
    (re.compile(r"signature=[^&\s\"']+", re.I), "signature=<redacted>"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), "sk-ant-<redacted>"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACT:
        text = pattern.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)) + f".{int(record.msecs):03d}",
            "lvl": record.levelname,
            "log": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _STD and not k.startswith("_"):
                data[k] = v
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(data, ensure_ascii=False, default=str))


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.handlers.TimedRotatingFileHandler(log_dir / "subtalk.jsonl", when="midnight", backupCount=14,
                                                   encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    ch.setLevel(logging.WARNING)
    root.addHandler(ch)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
