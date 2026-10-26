#!/bin/bash
# zsh is the macOS default shell: if started with "zsh file" or "sh file", relaunch in bash
[ -n "${BASH_VERSION:-}" ] || exec /bin/bash "$0" "$@"
# Savant HaloLink - macOS installer (Savant Mac mini Pro host)
#
#   sudo ./install.sh                 install or upgrade, run as the current user
#   sudo ./install.sh --user RPM      run the service as a specific macOS user
#
# Needs internet access once, to download the Python packages aiokwikset uses
# (aiohttp, boto3, pycognito, attrs) into a private environment in ~/HaloLink/venv.
# Upgrades keep config.json, the Kwikset sign-in and the lock numbering.
set -euo pipefail
trap 'printf "\033[31mERROR:\033[0m install stopped at line %s: %s\n" "$LINENO" "$BASH_COMMAND" >&2' ERR

LABEL="org.halolink.bridge"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/../bridge" && pwd)"
VENDOR="$(cd "$HERE/../vendor" && pwd)"

RUN_USER="${SUDO_USER:-${USER:-$(id -un)}}"
while [ $# -gt 0 ]; do
  case "$1" in
    --user) RUN_USER="$2"; shift 2 ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
as_user() { sudo -u "$RUN_USER" -H "$@"; }

[ "$(uname)" = "Darwin" ] || fail "this installer is for macOS"
[ "$(id -u)" -eq 0 ] || fail "run with sudo: sudo $0"
id "$RUN_USER" >/dev/null 2>&1 || fail "user '$RUN_USER' does not exist"
[ "$RUN_USER" != "root" ] || fail "refusing to run the service as root; use --user <name>"

USER_HOME="$(dscl . -read "/Users/$RUN_USER" NFSHomeDirectory | awk '{print $2}')"
DEST="$USER_HOME/HaloLink"

WHEEL="$(ls -1 "$VENDOR"/aiokwikset-*.whl 2>/dev/null | sort | tail -1 || true)"
[ -n "$WHEEL" ] || fail "no aiokwikset wheel in $VENDOR"

# ---- Python ----------------------------------------------------------------
say "Checking Python"
PY=""
for c in /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
  if [ -x "$c" ] && as_user "$c" -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || fail "Python 3.9+ not found. On macOS 11+ run: xcode-select --install"
# /usr/bin/python3 is Apple's shim; resolve the real binary for the venv and the firewall
REAL_PY="$(as_user "$PY" -c 'import sys; print(sys.executable)')"
echo "    $("$REAL_PY" --version) at $REAL_PY"

# ---- Port ------------------------------------------------------------------
CFG_SRC="$SRC/config.json"
[ -f "$DEST/config.json" ] && CFG_SRC="$DEST/config.json"
TCP_PORT="$("$REAL_PY" -c "import json;print(json.load(open('$CFG_SRC')).get('tcp_port',2370))")"

if launchctl print "system/$LABEL" >/dev/null 2>&1; then
  say "Stopping existing service"
  launchctl bootout "system/$LABEL" 2>/dev/null || true
  for _ in $(seq 1 20); do
    lsof -nP -iTCP:"$TCP_PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 1
  done
fi

say "Checking port $TCP_PORT (Savant)"
if lsof -nP -iTCP:"$TCP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  lsof -nP -iTCP:"$TCP_PORT" -sTCP:LISTEN
  fail "port $TCP_PORT is already in use. Change tcp_port in config.json (and the profile) and rerun."
fi

# ---- Files -----------------------------------------------------------------
ver() { if [ -f "$1" ]; then sed -n 's/^VERSION = "\(.*\)"/\1/p' "$1" | head -1; fi; return 0; }
OLD_VER="$(ver "$DEST/halolink_bridge.py")"
NEW_VER="$(ver "$SRC/halolink_bridge.py")"
say "Installing bridge v${NEW_VER:-?} to $DEST${OLD_VER:+ (replacing v$OLD_VER)}"
mkdir -p "$DEST/logs"
cp "$SRC/halolink_bridge.py" "$DEST/"
cp "$HERE/halolink" "$HERE/uninstall.sh" "$DEST/"
if [ -f "$DEST/config.json" ]; then
  echo "    keeping existing config.json"
else
  BK="$(ls -1d "$(dirname "$DEST")"/HaloLink-backup-* 2>/dev/null | sort | tail -1 || true)"
  if [ -n "$BK" ] && [ -f "$BK/config.json" ]; then
    for f in config.json tokens.json locks.json settings.json; do [ -f "$BK/$f" ] && cp -p "$BK/$f" "$DEST/"; done
    echo "    restored config, sign-in and lock numbering from $BK"
  else
    cp "$SRC/config.json" "$DEST/"
    echo "    new config.json: $DEST/config.json"
  fi
fi
chown -R "$RUN_USER" "$DEST"
chmod 700 "$DEST"          # tokens.json lives here
chmod 755 "$DEST/halolink" "$DEST/halolink_bridge.py" "$DEST/uninstall.sh"
[ -f "$DEST/tokens.json" ] && chmod 600 "$DEST/tokens.json"

# ---- Python environment ----------------------------------------------------
VPY="$DEST/venv/bin/python3"
PYVER='import sys; print("%d.%d" % sys.version_info[:2])'
if [ -x "$VPY" ] && [ "$("$VPY" -c "$PYVER" 2>/dev/null)" = "$("$REAL_PY" -c "$PYVER")" ]; then
  echo "    reusing $DEST/venv"
else
  say "Creating Python environment $DEST/venv"
  rm -rf "$DEST/venv"
  as_user "$REAL_PY" -m venv "$DEST/venv"
fi
say "Installing packages (needs internet; a minute or two the first time)"
as_user "$VPY" -m pip install --quiet --disable-pip-version-check --upgrade pip
as_user "$VPY" -m pip install --quiet --disable-pip-version-check \
  "aiohttp>=3.8" "pycognito>=2024.5.1" "attrs>=21.0.0" boto3
# aiokwikset itself: the bundled Python 3.9-compatible wheel (see vendor/make_py39_wheel.py)
as_user "$VPY" -m pip install --quiet --disable-pip-version-check --no-deps --force-reinstall "$WHEEL"
as_user "$VPY" -c 'import aiokwikset, aiohttp, boto3, pycognito; print("    aiokwikset", aiokwikset.__version__ if hasattr(aiokwikset, "__version__") else "ok", "| aiohttp", aiohttp.__version__)' \
  || fail "Python packages did not import - see the messages above"

ln_bin() {
  CREATED_BIN=0
  [ -d /usr/local/bin ] || { mkdir -p /usr/local/bin; CREATED_BIN=1; }
  ln -sf "$DEST/halolink" /usr/local/bin/halolink
}
ln_bin

# ---- launchd ---------------------------------------------------------------
say "Writing $PLIST"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VPY</string>
    <string>$DEST/halolink_bridge.py</string>
    <string>-c</string><string>$DEST/config.json</string>
    <string>-l</string><string>$DEST/logs/bridge.log</string>
  </array>
  <key>WorkingDirectory</key><string>$DEST</string>
  <key>UserName</key><string>$RUN_USER</string>
  <key>RunAtLoad</key><true/>
  <!-- restart after a crash; a clean stop (exit 0) stays stopped -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ExitTimeOut</key><integer>15</integer>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>5</integer>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>StandardOutPath</key><string>$DEST/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$DEST/logs/launchd.log</string>
</dict>
</plist>
EOF
chown root:wheel "$PLIST"
chmod 644 "$PLIST"
plutil -lint "$PLIST" >/dev/null || fail "plist failed validation"

# ---- Firewall --------------------------------------------------------------
FW=/usr/libexec/ApplicationFirewall/socketfilterfw
FW_ADDED=0
if [ -f "$DEST/.install-manifest" ] && grep -q '^FW_ADDED=1' "$DEST/.install-manifest"; then FW_ADDED=1; fi
if [ -x "$FW" ] && "$FW" --getglobalstate | grep -qi "enabled"; then
  if "$FW" --listapps 2>/dev/null | grep -qF "$REAL_PY"; then
    echo "    Python already has a firewall entry - leaving it as is"
  else
    say "macOS firewall is on - allowing incoming connections for Python"
    "$FW" --add "$REAL_PY" >/dev/null || true
    "$FW" --unblockapp "$REAL_PY" >/dev/null || true
    FW_ADDED=1
  fi
fi

cat > "$DEST/.install-manifest" <<MANIFEST
# written by install.sh $(date '+%Y-%m-%d %H:%M')
LABEL=$LABEL
PLIST=$PLIST
DEST=$DEST
RUN_USER=$RUN_USER
PYTHON=$REAL_PY
FW_ADDED=$FW_ADDED
CREATED_BIN=$CREATED_BIN
MANIFEST
chown "$RUN_USER" "$DEST/.install-manifest"

GOT_VER="$(ver "$DEST/halolink_bridge.py")"
[ "$GOT_VER" = "$NEW_VER" ] || fail "copy failed: $DEST has v$GOT_VER, expected v$NEW_VER"

# ---- Start & verify --------------------------------------------------------
say "Starting service"
launchctl enable "system/$LABEL" 2>/dev/null || true
launchctl bootstrap system "$PLIST"

ok=0
for _ in $(seq 1 20); do
  if lsof -nP -iTCP:"$TCP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ "$ok" -ne 1 ]; then
  tail -20 "$DEST/logs/bridge.log" "$DEST/logs/launchd.log" 2>/dev/null || true
  fail "service did not start - see logs above"
fi

IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<host-ip>')"
say "Installed and running"
cat <<EOF

    Bridge version:  v$NEW_VER
    Savant profile:  wire to this host's IP ($IP), port $TCP_PORT
    Config:          $DEST/config.json   (then: sudo halolink restart)
    Access:          only this host may connect (allowed_clients "auto" in config.json)
    Control:         halolink status | list | logs | sudo halolink stop | start | restart
    Remove:          sudo $DEST/uninstall.sh

EOF
if [ ! -f "$DEST/tokens.json" ]; then
  printf '\033[1mNext:\033[0m sign in to Kwikset (email, password, then the code Kwikset sends):\n\n    halolink login\n\n'
fi
