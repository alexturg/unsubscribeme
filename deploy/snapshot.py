"""Run as the service user, using only the system standard library."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

source = Path('/var/lib/unsubscribeme/data/bot.sqlite')
if source.is_file():
    directory = Path('/var/lib/unsubscribeme/backups')
    directory.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination = directory / f'before-deploy-{stamp}.sqlite'
    with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
    destination.chmod(0o600)
    print(f'Database snapshot: {destination.name}', flush=True)
