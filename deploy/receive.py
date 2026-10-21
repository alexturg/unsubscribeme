#!/usr/bin/python3
"""Root-owned forced SSH command, executed as unsubscribeme-deploy (never root)."""
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

BASE = Path('/opt/unsubscribeme')
CONTROL = '/usr/local/sbin/unsubscribeme-service'


def run(*args):
    subprocess.run(args, check=True)


def control(command):
    run('/usr/bin/sudo', '-n', CONTROL, command)


def switch(target):
    pending = BASE / 'next'
    pending.unlink(missing_ok=True)
    pending.symlink_to(target)
    pending.replace(BASE / 'current')


def wait_ready(timeout=180):
    deadline = time.monotonic() + timeout
    stable_pid = None
    stable_since = None
    while time.monotonic() < deadline:
        try:
            pid = subprocess.check_output(
                ['/usr/bin/systemctl', 'show', '-p', 'MainPID', '--value', 'unsubscribeme.service'],
                text=True,
            ).strip()
            with urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3) as response:
                health = json.load(response)
            if health.get('ready') and str(health.get('pid')) == pid and int(pid) > 0:
                if stable_pid != pid:
                    stable_pid, stable_since = pid, time.monotonic()
                if time.monotonic() - stable_since >= 15:
                    return
            else:
                stable_pid = None
        except Exception:
            stable_pid = None
        time.sleep(2)
    raise RuntimeError('Service did not become ready; check journalctl -u unsubscribeme')


def unpack(stream, release):
    # Only regular files/directories; no links, devices, traversal or arbitrary modes.
    with tarfile.open(fileobj=stream, mode='r|') as archive:
        total = 0
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts:
                raise ValueError('Unsafe archive path')
            if path.parts[0] in {'.env', '.venv', '.git'}:
                raise ValueError('Forbidden archive path')
            target = release.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                total += member.size
                if total > 100 * 1024 * 1024:
                    raise ValueError('Archive exceeds 100 MiB')
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
                target.chmod(0o644)
            else:
                raise ValueError('Archive links and special files are forbidden')


def main():
    command = shlex.split(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    if len(command) != 2 or command[0] != 'deploy' or not re.fullmatch('[0-9a-f]{40}', command[1]):
        raise ValueError('Only deploy <40-character commit SHA> is accepted')
    os.umask(0o022)
    def interrupted(signum, frame):
        raise RuntimeError(f'Deployment interrupted by signal {signum}')
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    with (BASE / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release = Path(tempfile.mkdtemp(prefix=command[1] + '-', dir=BASE / 'releases'))
        release.chmod(0o755)
        unpack(sys.stdin.buffer, release)
        (release / 'REVISION').write_text(command[1] + '\n')
        run('/usr/bin/python3', '-m', 'venv', str(release / '.venv'))
        python = str(release / '.venv/bin/python')
        run(python, '-m', 'pip', 'install', '--require-hashes', '-r', str(release / 'deploy/requirements.lock'))
        run(python, '-m', 'pip', 'install', '--no-deps', '--no-build-isolation', str(release))
        run(python, '-m', 'pip', 'check')
        previous = (BASE / 'current').resolve(strict=True)
        stopped = False
        try:
            stopped = True
            control('stop')
            control('snapshot')
            switch(release)
            control('start')
            wait_ready()
        except BaseException:
            # Let rollback finish even if the SSH connection closes again.
            for sig in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, signal.SIG_IGN)
            if stopped:
                control('stop')
                switch(previous)
                control('start')
                wait_ready()
                print('Previous code restored. Database snapshot retained; schema is NOT automatically reverted.', flush=True)
            raise
        pending = BASE / 'previous-next'
        pending.unlink(missing_ok=True)
        pending.symlink_to(previous)
        pending.replace(BASE / 'previous')
        # Keep five recent releases plus both rollback targets. Never follow links.
        releases = sorted(
            (p for p in (BASE / 'releases').iterdir()
             if not p.is_symlink() and p.is_dir() and re.fullmatch('[0-9a-f]{40}-.+', p.name)),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        for old in releases[5:]:
            if old not in (previous, release):
                shutil.rmtree(old)
        print(f'Deployed {command[1]}', flush=True)


if __name__ == '__main__':
    main()
