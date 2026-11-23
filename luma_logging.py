"""How the application logs: INFO by default, a bounded file, DEBUG only when asked for.

The application used to configure the root logger at DEBUG unconditionally. Nothing was written to disk by
the packaged build (it is a windowed program with no stderr and no file handler), so the cost was a record
formatted and handed to a stream that is not there for every device read; but anyone who ran the server
from a terminal and redirected it kept every DEBUG line, unbounded -- about 55 MB an hour with 200 units
that did not answer.

The default is now INFO, written to a rotating file in the data folder (a few megabytes in all, however
long the application runs and however noisy the network is) as well as to stderr when there is one.
The per-request access lines (werkzeug) are quiet below WARNING, and the per-read success lines of the
device transport are DEBUG: steady-state polling is not logged, changes and failures are.
`LUMA_LOG_LEVEL=DEBUG` restores the detail for a diagnosis; `LUMA_LOG_FILE=0` writes no file. Warnings and
errors are never filtered out by any of this.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

FORMAT = "[%(asctime)s] %(levelname)s - %(message)s"
#: One file is at most this large, and this many older ones are kept: the folder never holds more than
#: (BACKUPS + 1) * MAX_BYTES, whatever is logged.
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 3
LOG_NAME = "lumasuite.log"


def level_from(value, default=logging.INFO):
    """A level from its name (`debug`, `INFO`, ...). Anything else is the default, never an error."""
    name = str(value or "").strip().upper()
    level = logging.getLevelName(name) if name else None
    return level if isinstance(level, int) else default


def configure(data_dir, env=None, stream=None):
    """Configure the root logger. Returns what was done, for the startup line and for tests."""
    env = os.environ if env is None else env
    level = level_from(env.get("LUMA_LOG_LEVEL"))
    handlers = []
    out = stream if stream is not None else sys.stderr
    if out is not None:
        handlers.append(logging.StreamHandler(out))
    path = None
    if str(env.get("LUMA_LOG_FILE", "1")).strip().lower() not in ("0", "false", "no", "off"):
        try:
            folder = os.path.join(str(data_dir), "logs")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, LOG_NAME)
            handlers.append(logging.handlers.RotatingFileHandler(
                path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True))
        except OSError:
            path = None            # the application must start without a log file
    if not handlers:
        handlers.append(logging.NullHandler())
    for handler in handlers:
        handler.setFormatter(logging.Formatter(FORMAT))
    logging.basicConfig(level=level, handlers=handlers, force=True)
    # werkzeug writes one INFO access line per request. The page polls (a TCP probe, a heartbeat, a scan
    # status) so those lines were ~85% of a log that held eight minutes of history (measured: ~58 MB/h).
    # Mutations are in the audit log; refusals, errors and warnings still pass. DEBUG restores them.
    logging.getLogger("werkzeug").setLevel(
        logging.NOTSET if level <= logging.DEBUG else max(level, logging.WARNING))
    return {"level": logging.getLevelName(level), "file": path}
