#!/bin/bash
#
# gpsdo-monitor installation/upgrade script
#
# Tooling: uv (https://astral.sh/uv) is the canonical venv + installer
# across the sigmond suite.  If it's not present, this script installs
# it system-wide to /usr/local/bin via the Astral installer.
#
# Idempotent.  Installs or upgrades:
#   - uv (if missing) at /usr/local/bin/uv
#   - gpsdo service user (created via systemd-sysusers)
#   - /etc/udev/rules.d/99-gpsdo.rules
#   - Python venv at /opt/gpsdo-monitor/venv (editable install)
#   - /usr/local/bin/gpsdo-monitor -> /opt/gpsdo-monitor/venv/bin/gpsdo-monitor
#   - Rendered config at /etc/gpsdo-monitor/config.toml
#   - Systemd unit (gpsdo-monitor.service)
#
# Always editable: $REPO_ROOT is the canonical source, code edits land
# on the next `systemctl restart gpsdo-monitor.service` with no
# re-install.  Matches the mag/psk/wspr-recorder pattern.
#
# Usage:
#   sudo ./install.sh              # install or upgrade
#   sudo ./install.sh --uninstall  # remove
#
# To force-recreate the venv (e.g. after a Python upgrade):
#   sudo systemctl stop gpsdo-monitor.service
#   sudo rm -rf /opt/gpsdo-monitor/venv
#   sudo ./install.sh   # recreates venv + restarts service
#

set -e

INSTALL_DIR="/opt/gpsdo-monitor"
CONFIG_DIR="/etc/gpsdo-monitor"
SERVICE_USER="gpsdo"
SERVICE_GROUP="gpsdo"

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

check_root() {
    [[ $EUID -eq 0 ]] || error "Run as root (sudo)."
}

# Ensures `uv` is on PATH.  Delegates to sigmond's shared helper if
# present; falls back to an inline copy otherwise (covers the bootstrap
# case where sigmond hasn't been cloned -- gpsdo-monitor doesn't
# otherwise require sigmond, so we don't force-clone it).  Keep the
# inline body in sync with sigmond/scripts/install/ensure_uv.sh.
_ENSURE_UV_SH="/opt/git/sigmond/sigmond/scripts/install/ensure_uv.sh"
if [[ -r "$_ENSURE_UV_SH" ]]; then
    # shellcheck source=/dev/null
    source "$_ENSURE_UV_SH"
else
    # Place uv-managed Pythons in a world-readable shared location so
    # service users can execute the venv interpreter.  Under sudo the
    # default ~/.local/share/uv/python resolves to /root/.local/..., and
    # /root is mode 0700, so the venv's python symlink dangles for them.
    # Same value as sigmond's helper; keep in sync with it.
    export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/opt/uv/python}"

    _ensure_uv() {
        # uv refuses to install into a missing parent, and the directory
        # must be world-readable (0755) for service users to traverse it.
        if [[ ! -d "$UV_PYTHON_INSTALL_DIR" ]]; then
            if [[ $EUID -eq 0 ]]; then
                install -d -m 0755 "$UV_PYTHON_INSTALL_DIR" 2>/dev/null || true
            else
                sudo install -d -m 0755 "$UV_PYTHON_INSTALL_DIR" 2>/dev/null || true
            fi
        fi
        if command -v uv >/dev/null 2>&1; then
            printf '[INFO]  uv %s at %s\n' "$(uv --version 2>/dev/null | awk '{print $2}')" "$(command -v uv)"
            return 0
        fi
        printf '[INFO]  uv not found -- installing system-wide to /usr/local/bin\n'
        command -v curl >/dev/null || { printf '[ERROR] curl not found (apt install curl)\n' >&2; return 1; }
        if ! curl -LsSf https://astral.sh/uv/install.sh | env XDG_BIN_HOME=/usr/local/bin UV_NO_MODIFY_PATH=1 sh; then
            printf '[ERROR] uv installer failed\n' >&2
            return 1
        fi
        command -v uv >/dev/null || { printf '[ERROR] uv installer ran but uv is still not on PATH\n' >&2; return 1; }
        printf '[INFO]  uv %s installed\n' "$(uv --version 2>/dev/null | awk '{print $2}')"
    }
fi

check_dependencies() {
    info "Checking dependencies..."
    command -v python3 >/dev/null || error "python3 not found"
    _ensure_uv || error "_ensure_uv failed"
    # libhidapi-hidraw0 is required by the `hidapi` PyPI wheel at import time.
    # apt-get is best-effort -- some hosts don't have apt; warn rather than error.
    if command -v apt-get >/dev/null 2>&1; then
        apt-get install -y --no-install-recommends libhidapi-hidraw0 >/dev/null 2>&1 || \
            warn "apt-get install libhidapi-hidraw0 failed; install it manually if hidapi import fails."
    else
        warn "no apt-get; ensure libhidapi-hidraw0 (or equivalent) is installed manually."
    fi
}

create_user() {
    info "Creating service user ${SERVICE_USER}..."
    install -d -m 0755 /etc/sysusers.d
    install -m 0644 "$REPO_ROOT/deploy/sysusers.d/gpsdo.conf" /etc/sysusers.d/gpsdo.conf
    systemd-sysusers
    if id "$SERVICE_USER" &>/dev/null; then
        info "  ${SERVICE_USER} ready"
    else
        error "systemd-sysusers did not create ${SERVICE_USER}"
    fi
}

install_udev_rule() {
    info "Installing udev rule for Leo Bodnar GPSDO..."
    install -m 0644 "$REPO_ROOT/deploy/99-gpsdo.rules" /etc/udev/rules.d/99-gpsdo.rules
    udevadm control --reload-rules
    udevadm trigger --attr-match=subsystem=hidraw || true
    udevadm trigger --attr-match=subsystem=tty    || true
}

install_application() {
    info "Installing Python application to ${INSTALL_DIR}..."
    if [[ ! -d "$INSTALL_DIR/venv" ]]; then
        install -d -m 0755 "$INSTALL_DIR"
        # --seed populates pip/setuptools/wheel for compatibility with
        # tooling that shells out to pip; harmless overhead otherwise.
        uv venv "$INSTALL_DIR/venv" --python 3.11 --seed --quiet
    fi
    # Pre-clean any leftover egg-info from prior dev installs in the
    # source tree -- setuptools' "Cannot update time stamp" check
    # inside the build sandbox would abort the editable install if
    # ownership has drifted.  Safe to delete; uv recreates it.
    rm -rf "$REPO_ROOT/src"/*.egg-info "$REPO_ROOT"/*.egg-info 2>/dev/null || true
    # Editable install: $REPO_ROOT is the canonical source.  Mirrors
    # mag/psk/wspr-recorder -- restart-to-pick-up-edits, no
    # re-install required for pure-Python changes.
    uv pip install --quiet --python "$INSTALL_DIR/venv/bin/python3" -e "$REPO_ROOT"
    # The service user must be able to traverse the repo to import
    # the package in editable mode.  $REPO_ROOT under
    # /opt/git/sigmond is group-readable (mode 2775) so this is
    # normally fine; the check catches the case of a repo cloned
    # somewhere unreadable (e.g. under /home with mode 700).
    if ! sudo -u "$SERVICE_USER" test -r "$REPO_ROOT/src/gpsdo_monitor/__init__.py"; then
        error "Service user $SERVICE_USER cannot read $REPO_ROOT/src/gpsdo_monitor/__init__.py.
    Fix: ensure the repo lives at /opt/git/sigmond/gpsdo-monitor (the canonical, group-readable
    location), or chmod g+rx the path and ensure $SERVICE_USER is in the owner's group."
    fi
    # Symlink the venv entry point so `gpsdo-monitor` works on $PATH
    # (and so the unit's ExecStart=/usr/local/bin/gpsdo-monitor resolves
    # to the venv interpreter via the setuptools-generated shebang).
    ln -sfn "$INSTALL_DIR/venv/bin/gpsdo-monitor" /usr/local/bin/gpsdo-monitor
    info "  installed; CLI: $(head -1 "$INSTALL_DIR/venv/bin/gpsdo-monitor")"
}

install_config() {
    info "Installing config template..."
    install -d -m 0755 "$CONFIG_DIR"
    if [[ ! -f "$CONFIG_DIR/config.toml" ]]; then
        install -m 0644 "$REPO_ROOT/deploy/config.example.toml" "$CONFIG_DIR/config.toml"
        info "  rendered $CONFIG_DIR/config.toml"
    else
        info "  $CONFIG_DIR/config.toml already present (not overwritten)"
    fi
}

install_systemd_unit() {
    info "Installing systemd unit..."
    install -m 0644 "$REPO_ROOT/deploy/gpsdo-monitor.service" /etc/systemd/system/gpsdo-monitor.service
    systemctl daemon-reload
    systemctl enable gpsdo-monitor.service
    systemctl restart gpsdo-monitor.service || true
}

uninstall() {
    info "Removing gpsdo-monitor..."
    systemctl disable --now gpsdo-monitor.service 2>/dev/null || true
    rm -f /etc/systemd/system/gpsdo-monitor.service \
          /usr/local/bin/gpsdo-monitor \
          /etc/udev/rules.d/99-gpsdo.rules
    systemctl daemon-reload || true
    udevadm control --reload-rules 2>/dev/null || true
    info "Removed binary, unit, udev rule."
    info "Kept (delete by hand if desired): ${INSTALL_DIR}, ${CONFIG_DIR}, user '${SERVICE_USER}'."
}

main() {
    check_root
    if [[ "${1:-}" == "--uninstall" ]]; then
        uninstall
        return
    fi
    check_dependencies
    create_user
    install_udev_rule
    install_application
    install_config
    install_systemd_unit
    info "Install complete.  Status:"
    systemctl --no-pager status gpsdo-monitor.service || true
}

main "$@"
