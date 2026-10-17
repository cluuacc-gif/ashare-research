#!/usr/bin/env bash
# Initial installation only: never enables timers or sends mail.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: sudo bash deploy/install-linux.sh

Install this reviewed package on a fresh Ubuntu 24.04 systemd host.
Creates /opt/ashare-research and /var/lib/ashare-mail, installs timer
definitions, and creates an empty /etc/ashare-mail/environment if absent.
Refuses an existing installation. Does not enable/start timers, contact
SMTP, buy a server, or print credentials. Configure credentials locally
and follow AUTONOMOUS_OPERATIONS.md before enabling delivery.

  --help  Show this help without modifying the system.
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    '') ;;
    *) usage >&2; exit 2 ;;
esac
[[ $# -eq 0 ]] || { usage >&2; exit 2; }
die() { printf 'Installation stopped: %s\n' "$1" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || die 'run as root on the selected Ubuntu host.'
[[ -d /run/systemd/system ]] || die 'systemd must be running as the host service manager.'
command -v systemctl >/dev/null || die 'systemctl is unavailable.'
command -v systemd-analyze >/dev/null || die 'systemd-analyze is unavailable.'
[[ -f /etc/os-release ]] || die '/etc/os-release is unavailable.'
# This is the host-provided operating system identification file.
. /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || die 'this initial installer targets Ubuntu 24.04 only.'

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SOURCE_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
INSTALL_ROOT=/opt/ashare-research
STATE_ROOT=/var/lib/ashare-mail
CONFIG_ROOT=/etc/ashare-mail
shopt -s nullglob

for directory in "$INSTALL_ROOT" "$STATE_ROOT"; do
    [[ ! -L $directory ]] || die "refusing a symbolic link at $directory."
    if [[ -e $directory ]]; then
        [[ -d $directory ]] || die "refusing an existing file at $directory."
        [[ -z $(find "$directory" -mindepth 1 -maxdepth 1 -print -quit) ]] || die "refusing a nonempty directory at $directory."
    fi
done
[[ ! -L $CONFIG_ROOT ]] || die 'refusing a symbolic link at the configuration directory.'
[[ ! -e $CONFIG_ROOT || -d $CONFIG_ROOT ]] || die 'configuration directory is an existing file.'
[[ ! -L $CONFIG_ROOT/environment ]] || die 'refusing a symbolic link at the environment file.'
[[ ! -e $CONFIG_ROOT/environment || -f $CONFIG_ROOT/environment ]] || die 'environment path is not a regular file.'
if getent passwd ashare-mail >/dev/null || getent group ashare-mail >/dev/null; then
    die 'ashare-mail account/group already exists; review the previous installation manually.'
fi
existing_units=(/etc/systemd/system/ashare-mail-*.service /etc/systemd/system/ashare-mail-*.timer)
[[ ${#existing_units[@]} -eq 0 ]] || die 'A-share service/timer files already exist; no files were overwritten.'
for required in autonomous_mailer.py exchange_calendar_2026.json AUTONOMOUS_OPERATIONS.md deploy/environment.example; do
    [[ -f $SOURCE_ROOT/$required && ! -L $SOURCE_ROOT/$required ]] || die "package is missing regular file $required."
done
services=("$SOURCE_ROOT"/deploy/systemd/*.service)
timers=("$SOURCE_ROOT"/deploy/systemd/*.timer)
[[ ${#services[@]} -eq 2 && ${#timers[@]} -eq 6 ]] || die 'package must contain two services and six timers.'

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv gh ca-certificates tzdata
umask 022
useradd --system --user-group --home-dir "$STATE_ROOT" --shell /usr/sbin/nologin ashare-mail
install -d -m 0755 -o root -g root "$INSTALL_ROOT"
install -d -m 0700 -o ashare-mail -g ashare-mail "$STATE_ROOT"
for source_file in "$SOURCE_ROOT"/*.py "$SOURCE_ROOT"/*.md; do
    [[ -f $source_file && ! -L $source_file ]] || die 'package contains a nonregular source file.'
    install -m 0644 -o root -g root "$source_file" "$INSTALL_ROOT/"
done
install -m 0644 -o root -g root "$SOURCE_ROOT/exchange_calendar_2026.json" "$INSTALL_ROOT/"
cp -R -- "$SOURCE_ROOT/deploy" "$INSTALL_ROOT/deploy"
python3 -m venv "$INSTALL_ROOT/.venv"
# The independent mailer consumes published predictions; model training remains
# on GitHub. Its preparation/delivery import path uses the Python standard library.
"$INSTALL_ROOT/.venv/bin/python" "$INSTALL_ROOT/autonomous_mailer.py" --help >/dev/null
install -d -m 0700 -o root -g root "$CONFIG_ROOT"
if [[ ! -e $CONFIG_ROOT/environment ]]; then
    install -m 0600 -o root -g root "$SOURCE_ROOT/deploy/environment.example" "$CONFIG_ROOT/environment"
fi
chown root:root "$CONFIG_ROOT/environment"
chmod 0600 "$CONFIG_ROOT/environment"
install -m 0644 -o root -g root "${services[@]}" "${timers[@]}" /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/ashare-mail-prepare@.service /etc/systemd/system/ashare-mail-deliver@.service /etc/systemd/system/ashare-mail-*.timer
systemctl daemon-reload
printf '%s\n' 'Installation complete. No timers were enabled or started; no email was sent.'
printf '%s\n' 'Configure /etc/ashare-mail/environment locally, then follow /opt/ashare-research/AUTONOMOUS_OPERATIONS.md.'
