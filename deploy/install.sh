#!/bin/bash
#
# Install or update the bridge on a Raspberry Pi. Run as root, from a checkout:
#
#     sudo deploy/install.sh
#
# Idempotent on purpose -- it is the update path as well as the install path, and
# an appliance is updated rarely enough that nobody will remember which one they
# are doing. Every step checks before it acts and says what it decided.
#
# It does not write the configuration file. That belongs on the SD card's boot
# partition, put there from a laptop while the card is out, because that is the
# one channel a machine with no screen or keyboard has. An example is copied next
# to it so it can be edited in place later.

set -euo pipefail

SERVICE_USER=chessnut
INSTALL_DIR=/opt/chessnut-bridge
STATE_DIR=/var/lib/chessnut-bridge
KEY_DIR=/etc/chessnut-bridge
BOOT_DIR=/boot/firmware
UNIT=chessnut-bridge.service

SOURCE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

say() { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    echo "This needs root: sudo $0" >&2
    exit 1
fi

step "Service account"
if id "$SERVICE_USER" >/dev/null 2>&1; then
    say "$SERVICE_USER already exists"
else
    # A system account with no login shell and no password. Its home is the state
    # directory, so nothing it writes by accident lands somewhere surprising.
    useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin \
        --comment "Chessnut bridge" "$SERVICE_USER"
    say "created $SERVICE_USER"
fi
if getent group bluetooth >/dev/null && ! id -nG "$SERVICE_USER" | grep -qw bluetooth; then
    usermod -aG bluetooth "$SERVICE_USER"
    say "added $SERVICE_USER to the bluetooth group"
fi

step "Directories"
# 0700 on both: the key and the encrypted session are the two things here worth
# protecting from other local accounts, and neither is any use to them anyway.
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$STATE_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$KEY_DIR"
install -d -o root -g root -m 0755 "$INSTALL_DIR"
say "$STATE_DIR, $KEY_DIR, $INSTALL_DIR"

step "Code"
# Owned by root and read-only to the service: the process that serves a page which
# can submit moves should not be able to rewrite the program that serves it.
for dir in src deploy; do
    rsync -a --delete --exclude __pycache__ --exclude '*.pyc' \
        "$SOURCE_DIR/$dir/" "$INSTALL_DIR/$dir/"
done
install -m 0644 "$SOURCE_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
chown -R root:root "$INSTALL_DIR/src" "$INSTALL_DIR/deploy"
say "copied src/ and deploy/ from $SOURCE_DIR"

step "Dependencies"
if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
    python3 -m venv "$INSTALL_DIR/.venv"
    say "created a virtualenv"
fi
# Quiet, because on a Zero 2 W this is minutes of wheel downloads and the only
# interesting outcome is the failure.
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
say "installed $(grep -c '^[a-zA-Z]' "$INSTALL_DIR/requirements.txt") pinned packages"

step "Bluetooth adapter"
# Without this the adapter comes up unpowered after a reboot and the appliance
# looks broken in a way that reads as a hardware fault. This is the persistent
# form of `bluetoothctl power on`.
MAIN_CONF=/etc/bluetooth/main.conf
if grep -q '^AutoEnable=true' "$MAIN_CONF" 2>/dev/null; then
    say "AutoEnable is already on"
elif grep -q '^#\s*AutoEnable=' "$MAIN_CONF" 2>/dev/null; then
    sed -i 's/^#\s*AutoEnable=.*/AutoEnable=true/' "$MAIN_CONF"
    say "enabled AutoEnable in $MAIN_CONF"
elif [[ -f "$MAIN_CONF" ]]; then
    printf '\n[Policy]\nAutoEnable=true\n' >> "$MAIN_CONF"
    say "appended [Policy] AutoEnable=true to $MAIN_CONF"
else
    say "WARNING: $MAIN_CONF not found; the adapter may not power on at boot"
fi

step "Configuration"
if [[ -d "$BOOT_DIR" ]]; then
    # cp, not install -m: the boot partition is FAT32, where the mode comes from
    # the mount options and chmod is refused. `install` would abort the script.
    cp "$SOURCE_DIR/deploy/chessnut-bridge.conf.example" \
        "$BOOT_DIR/chessnut-bridge.conf.example"
    say "example at $BOOT_DIR/chessnut-bridge.conf.example"
    if [[ -f "$BOOT_DIR/chessnut-bridge.conf" ]]; then
        say "found $BOOT_DIR/chessnut-bridge.conf"
    else
        say "NOT CONFIGURED: copy the example to chessnut-bridge.conf and fill it in."
        say "Until then the page explains itself and serves nothing else."
    fi
else
    say "WARNING: $BOOT_DIR does not exist; is this a Raspberry Pi?"
fi

step "Service"
install -m 0644 "$SOURCE_DIR/deploy/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null
systemctl restart "$UNIT"
say "enabled and (re)started $UNIT"

# Give it a moment to fail, so this script reports the failure rather than
# reporting success and leaving it to be discovered later.
sleep 3
step "Result"
if systemctl is-active --quiet "$UNIT"; then
    PORT=$(awk -F= '/^web_port=/ {print $2}' "$BOOT_DIR/chessnut-bridge.conf" 2>/dev/null || true)
    # An `[[ ]] && x` one-liner here would exit the script under `set -e` on the
    # common path, where the port is not 80.
    if [[ "$PORT" == "80" ]]; then PORT=""; fi
    say "running. The page is at http://$(hostname).local${PORT:+:$PORT}/"
else
    say "FAILED to start. The last of the journal:"
    journalctl -u "$UNIT" -n 20 --no-pager | sed 's/^/    /'
    exit 1
fi
