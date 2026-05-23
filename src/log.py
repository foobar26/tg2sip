from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "lvl": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k, v in getattr(record, "extra", {}).items():
            payload[k] = v
        return json.dumps(payload, default=str)


def setup(level: str = "INFO") -> None:
    fmt = JsonFormatter()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    # Optional file log (for /var/log + logrotate). WatchedFileHandler reopens
    # the file after logrotate moves it, so no copytruncate is needed.
    log_file = os.environ.get("LOG_FILE", "").strip()
    if log_file:
        try:
            d = os.path.dirname(log_file)
            if d:
                os.makedirs(d, exist_ok=True)
            handlers.append(logging.handlers.WatchedFileHandler(log_file))
        except OSError as e:
            print(f"log: cannot open LOG_FILE {log_file!r}: {e}", file=sys.stderr)
    for h in handlers:
        h.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    # ntgcalls routes WebRTC's internal logs to these loggers, but silences them
    # (sets CRITICAL) unless their level is set before the call object is built.
    # Surface them only under DEBUG — they show codec negotiation, video tracks,
    # ICE, etc. (WebRTC's own min severity is INFO in the release wheel).
    if str(level).upper() == "DEBUG":
        logging.getLogger("ntgcalls").setLevel(logging.DEBUG)
        logging.getLogger("webrtc").setLevel(logging.INFO)
