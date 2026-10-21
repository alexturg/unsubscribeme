#!/bin/sh
# Install root:root 0755; never execute application code as root.
set -eu
case "${1-}" in
    stop) exec /usr/bin/systemctl stop unsubscribeme.service ;;
    start)
        /usr/bin/systemctl reset-failed unsubscribeme.service
        exec /usr/bin/systemctl start unsubscribeme.service
        ;;
    snapshot)
        if /usr/bin/systemctl is-active --quiet unsubscribeme.service; then
            echo 'Refusing snapshot while the service is running' >&2
            exit 1
        fi
        exec /usr/sbin/runuser -u unsubscribeme -- /usr/bin/python3 -I /usr/local/libexec/unsubscribeme-snapshot.py
        ;;
    *) exit 64 ;;
esac
