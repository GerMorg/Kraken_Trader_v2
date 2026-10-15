from __future__ import annotations

import argparse
from datetime import datetime, timezone
import signal
import sys
import time
from types import FrameType

from app import __version__
from app.audit import AuditLogger
from app.config import Settings
from app.persistence import Persistence
from app.runtime import TraderRuntime

def main() -> int:
    parser=argparse.ArgumentParser(description="Kraken Trader v2 HA app")
    parser.add_argument("--healthcheck",action="store_true")
    parser.add_argument("--options",default="/data/options.json")
    args=parser.parse_args()
    settings=Settings.from_file(args.options)
    db=Persistence("/data/kraken_trader.sqlite3")
    audit=AuditLogger("/data/logs/events.jsonl")
    audit.emit("STARTUP_BEGIN",version=__version__,started_at=datetime.now(timezone.utc).isoformat(),
               config=settings.safe_summary())
    if args.healthcheck:
        audit.emit("HEALTHCHECK_OK",version=__version__,database="OPEN")
        audit.close(); db.close()
        return 0
    runtime=TraderRuntime(settings,db,audit)
    running=True
    def stop_handler(signum: int, frame: FrameType | None) -> None:
        nonlocal running
        running=False
        audit.emit("SHUTDOWN_SIGNAL_RECEIVED",signal=signum)
    signal.signal(signal.SIGTERM,stop_handler)
    signal.signal(signal.SIGINT,stop_handler)
    audit.emit("STARTUP_STATUS",mode=settings.mode,credentials_present=settings.credentials_present,
               live_requested=settings.live_requested,internal_live_release=False,
               log_path="/data/logs/events.jsonl",database="/data/kraken_trader.sqlite3",
               note="Unattended live order execution is not released in this version.")
    try:
        while running:
            started=time.monotonic()
            runtime.cycle()
            remaining=max(0.0,settings.cycle_interval_seconds-(time.monotonic()-started))
            while running and remaining>0:
                interval=min(1.0,remaining); time.sleep(interval); remaining-=interval
    finally:
        audit.emit("SHUTDOWN_COMPLETE")
        audit.close(); db.close()
    return 0

if __name__=="__main__":
    sys.exit(main())