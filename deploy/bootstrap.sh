#!/bin/bash
# One-time migration for the existing Ubuntu server. Run from a reviewed source tree.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run as root' >&2; exit 1; }
source_dir=$(cd -- "$(dirname -- "$0")/.." && pwd)
legacy=/root/unsubscribeme
base=/opt/unsubscribeme
runtime=/var/lib/unsubscribeme
[[ ! -e "$base/current" ]] || { echo 'Already migrated; use regular deployment' >&2; exit 1; }
[[ -f "$source_dir/deploy/deploy-key.pub" ]] || { echo 'Missing deploy/deploy-key.pub' >&2; exit 1; }
# Read only path settings using the OLD, already running installation.
legacy_db=$(cd "$legacy" && .venv/bin/python -c 'from rssbot.config import Settings; print(Settings().DB_PATH.resolve())')
legacy_output=$(cd "$legacy" && .venv/bin/python -c 'from rssbot.config import Settings; print(Settings().AI_SUMMARIZER_OUTPUT_DIR.resolve())')
[[ -f "$legacy_db" ]]
id unsubscribeme >/dev/null 2>&1 || useradd --system --user-group --home-dir "$runtime" --shell /usr/sbin/nologin unsubscribeme
id unsubscribeme-deploy >/dev/null 2>&1 || useradd --system --user-group --create-home --home-dir /var/lib/unsubscribeme-deploy --shell /bin/bash unsubscribeme-deploy
install -d -m 755 -o unsubscribeme-deploy -g unsubscribeme-deploy "$base" "$base/releases"
install -d -m 700 -o unsubscribeme -g unsubscribeme "$runtime" "$runtime/data" "$runtime/backups"
install -d -m 700 /etc/unsubscribeme
install -m 600 "$legacy/.env" /etc/unsubscribeme/environment
install -m 600 "$source_dir/deploy/paths.env" /etc/unsubscribeme/paths
install -d -m 755 /usr/local/libexec
install -m 755 "$source_dir/deploy/service-control.sh" /usr/local/sbin/unsubscribeme-service
install -m 644 "$source_dir/deploy/receive.py" /usr/local/libexec/unsubscribeme-receive.py
install -m 644 "$source_dir/deploy/snapshot.py" /usr/local/libexec/unsubscribeme-snapshot.py
visudo -cf "$source_dir/deploy/sudoers"
install -m 440 "$source_dir/deploy/sudoers" /etc/sudoers.d/unsubscribeme-deploy
install -d -m 755 /etc/ssh/authorized_keys
{ printf 'restrict '; cat "$source_dir/deploy/deploy-key.pub"; } > /etc/ssh/authorized_keys/unsubscribeme-deploy
chmod 644 /etc/ssh/authorized_keys/unsubscribeme-deploy
cat > /etc/ssh/sshd_config.d/60-unsubscribeme-deploy.conf <<'SSH'
Match User unsubscribeme-deploy
    AuthorizedKeysFile /etc/ssh/authorized_keys/unsubscribeme-deploy
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitTTY no
    DisableForwarding yes
    ForceCommand /usr/bin/python3 -I /usr/local/libexec/unsubscribeme-receive.py
Match all
SSH
/usr/sbin/sshd -t
systemctl reload ssh

release="$base/releases/initial-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 755 -o unsubscribeme-deploy -g unsubscribeme-deploy "$release"
tar -C "$source_dir" --exclude=deploy/deploy-key.pub -cf - . | tar -C "$release" -xf -
chown -R unsubscribeme-deploy:unsubscribeme-deploy "$release"
runuser -u unsubscribeme-deploy -- python3 -m venv "$release/.venv"
runuser -u unsubscribeme-deploy -- "$release/.venv/bin/python" -m pip install --require-hashes -r "$release/deploy/requirements.lock"
runuser -u unsubscribeme-deploy -- "$release/.venv/bin/python" -m pip install --no-deps --no-build-isolation "$release"
runuser -u unsubscribeme-deploy -- "$release/.venv/bin/python" -m pip check
( cd "$release" && runuser -u unsubscribeme-deploy -- env TELEGRAM_BOT_TOKEN=123456:TEST_ONLY_NO_NETWORK "$release/.venv/bin/python" -m pytest -q )

# From here the legacy service is stopped. A trap restores its exact unit on failure.
install -m 600 /etc/systemd/system/unsubscribeme.service /etc/unsubscribeme/legacy.service
rollback() {
    trap - ERR HUP INT TERM
    systemctl stop unsubscribeme || true
    install -m 644 /etc/unsubscribeme/legacy.service /etc/systemd/system/unsubscribeme.service
    systemctl daemon-reload
    systemctl reset-failed unsubscribeme
    systemctl start unsubscribeme
    echo 'Migration failed; legacy service restored' >&2
    exit 1
}
trap rollback ERR HUP INT TERM
systemctl stop unsubscribeme
# Preserve the whole runtime data tree, but take the DB from its actual configured path.
cp -a "$legacy/data/." "$runtime/data/"
if [[ -d "$legacy_output" && "$legacy_output" != "$legacy/data/ai_summaries" ]]; then
    mkdir -p "$runtime/data/ai_summaries"
    cp -a "$legacy_output/." "$runtime/data/ai_summaries/"
fi
/usr/bin/python3 - "$legacy_db" "$runtime/data/bot.sqlite" <<'PY'
import sqlite3, sys
from pathlib import Path
for suffix in ('', '-wal', '-shm', '-journal'):
    Path(sys.argv[2] + suffix).unlink(missing_ok=True)
with sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True) as source:
    with sqlite3.connect(sys.argv[2]) as destination:
        source.backup(destination)
PY
chown -R unsubscribeme:unsubscribeme "$runtime"
chmod -R go-rwx "$runtime"
runuser -u unsubscribeme -- /usr/bin/python3 -I /usr/local/libexec/unsubscribeme-snapshot.py
runuser -u unsubscribeme-deploy -- ln -s "$release" "$base/current"
install -m 644 "$source_dir/deploy/systemd/unsubscribeme.service" /etc/systemd/system/unsubscribeme.service
systemctl daemon-reload
systemctl reset-failed unsubscribeme
systemctl start unsubscribeme
runuser -u unsubscribeme-deploy -- /usr/bin/python3 -I -c 'import runpy; runpy.run_path("/usr/local/libexec/unsubscribeme-receive.py")["wait_ready"]()'
trap - ERR HUP INT TERM
echo 'Migration complete; legacy repository and database retained for recovery.'
