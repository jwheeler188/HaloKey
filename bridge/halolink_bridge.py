#!/usr/bin/env python3
"""
Savant HaloLink - Kwikset Halo bridge
=====================================
Brings Kwikset Halo / Halo Touch / Halo Select Wi-Fi smart locks into Savant.

The bridge signs in to the Kwikset cloud (via the aiokwikset library), keeps
every lock's state current (real-time push from Kwikset, plus a safety poll),
and serves it to the Savant host over a plain TCP line protocol: one
KEY=value line per state, so the Blueprint profile needs no HTTPS, JSON or
AWS sign-in of its own. Lock / unlock / settings commands come back the same way.

    halolink_bridge.py --login        one-time interactive sign-in (email, password, Kwikset code)
    halolink_bridge.py --list         show the locks and their Savant numbers
    halolink_bridge.py                run the service

The Kwikset password is never stored: only the session tokens Kwikset issues
(tokens.json, readable by the service user only).

Python 3.9+ (Savant Mac mini Pro host ships 3.9.6). Needs aiokwikset and its
dependencies (aiohttp, pycognito, boto3, attrs) - install.sh sets these up.

Not affiliated with or endorsed by Kwikset or Savant Systems, Inc.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import signal
import socket
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VERSION = "1.1.7"
log = logging.getLogger("halolink")

# Savant decodes profile traffic as Mac Roman; keep everything plain ASCII.
SAVANT_ENCODING = "ascii"

DEFAULT_CONFIG = {
    "tcp_port": 2370,             # Savant connection (the profile expects 2370)
    "max_locks": 8,               # lock slots the profile has states for
    "poll_seconds": 300,          # safety poll; real-time push covers normal changes
    "realtime": True,             # Kwikset real-time push (AppSync WebSocket)
    "home": "",                   # only locks in this Kwikset home (name); blank = every home on the account
    "lock_order": [],             # optional: fix Savant numbering, e.g. ["Front Door", "Garage Entry"]
    "allow_remote_unlock": True,  # false = Savant can lock but never unlock
    "battery_low_percent": 20,    # LOCKn_BATTERY_LOW = 1 at or below this
    "confirm_seconds": 45,        # how long to wait for a lock/unlock to be confirmed
    "log_level": "INFO",
    # Who may connect on tcp_port (and so lock/unlock). Set here on the host only, never from
    # Blueprint: the Savant connection itself is unauthenticated, so it can't be trusted to set
    # its own access. "auto" = this machine only (the usual install: bridge on the Savant host).
    # Bridge on a separate box: list the Savant host, e.g. ["192.168.1.20"] or ["auto", "192.168.1.20"].
    # Subnets work too ("192.168.1.0/24"). "any" = no restriction (not recommended).
    "allowed_clients": "auto",
}

AUTOLOCK_DELAYS = (15, 30, 60, 180, 300, 600, 1500)

# ---------------------------------------------------------------------------
# State definitions. generate_profile.py builds the Blueprint status parsers
# from these, so the profile always matches what the bridge sends.
# (key, savant type, description)
# ---------------------------------------------------------------------------
SYS_STATES = [
    ("SYS_ONLINE", "integer", "1 = bridge is signed in and reaching the Kwikset cloud"),
    ("SYS_AUTH", "string", "ok, login_required or reauth_required"),
    ("SYS_LIVE", "integer", "1 = Kwikset real-time push connected (0 = polling only)"),
    ("SYS_ACCOUNT", "string", "Kwikset account email the bridge is signed in with"),
    ("SYS_LOCK_COUNT", "integer", "Locks found on the account (after the home filter)"),
    ("SYS_ALL_LOCKED", "integer", "1 = every lock reports Locked"),
    ("SYS_UNLOCKED_COUNT", "integer", "Locks not reporting Locked"),
    ("SYS_UNLOCKED_NAMES", "string", "Names of locks not reporting Locked, comma separated"),
    ("SYS_LOW_BATTERY_COUNT", "integer", "Locks at or below battery_low_percent"),
    ("SYS_LOW_BATTERY_NAMES", "string", "Names of low-battery locks"),
    ("SYS_DOOR_OPEN_COUNT", "integer", "Doors reporting Open (locks with a door sensor)"),
    ("SYS_OFFLINE_COUNT", "integer", "Locks the cloud reports offline"),
    ("SYS_LAST_UPDATE", "string", "Time of the last successful update from Kwikset"),
    ("SYS_LAST_COMMAND", "string", "Last command from Savant and its result"),
    ("SYS_ERROR", "string", "Last bridge error, blank when healthy"),
    ("SYS_VERSION", "string", "Bridge version"),
    ("SYS_REJECTED_CLIENT", "string", "Last connection refused by allowed_clients: IP and time"),
    ("SYS_UNLOCK_ALLOWED", "integer", "1 = Savant may unlock (config.json allow_remote_unlock and the Blueprint Disable Unlock variable)"),
]

LOCK_FIELDS = [
    ("PRESENT", "integer", "1 = a lock is assigned to this number"),
    ("NAME", "string", "Lock name from the Kwikset app"),
    ("STATUS", "string", "Locked | Unlocked | Jammed | Unknown (Unknown while the lock is offline)"),
    ("LOCKED", "integer", "1 = Locked"),
    ("LAST_KNOWN", "string", "Last status the lock reported, kept while it is offline"),
    ("PENDING", "string", "Locking | Unlocking while a command is being confirmed, else blank"),
    ("ONLINE", "integer", "1 = Kwikset can reach the lock"),
    ("BATTERY", "integer", "Battery percent"),
    ("BATTERY_LOW", "integer", "1 = battery at or below battery_low_percent"),
    ("DOOR", "string", "Open | Closed | Unknown (door position sensor)"),
    ("DOOR_OPEN", "integer", "1 = door reports Open"),
    ("LED", "integer", "1 = status LED enabled"),
    ("AUDIO", "integer", "1 = lock sounds enabled"),
    ("SECURE_SCREEN", "integer", "1 = secure screen enabled (Halo Touch)"),
    ("AUTOLOCK", "integer", "1 = auto-lock enabled"),
    ("AUTOLOCK_DELAY", "integer", "Auto-lock delay, seconds"),
    ("TAMPER", "string", "Tamper state as reported by the lock"),
    ("LAST_EVENT", "string", "Last lock event in the Kwikset history: Locked | Unlocked"),
    ("LAST_SOURCE", "string", "What did it: Keypad | Manual (thumb turn or key) | App | Savant | Unknown"),
    ("LAST_USER", "string", "Who did it, when Kwikset knows: user code name or phone name, else blank"),
    ("LAST_EVENT_TIME", "string", "Time of the last lock event (lock's local time)"),
    ("LAST_CHANGE", "string", "Time the bridge saw the status last change"),
    ("MODEL", "string", "Model number"),
    ("FIRMWARE", "string", "Firmware version"),
    ("ERROR", "string", "Last command error for this lock, blank when fine"),
]


def lock_key(n, field):
    return f"LOCK{n}_{field}"


def state_defs(max_locks):
    """Every state the bridge can send, in profile order."""
    out = list(SYS_STATES)
    for n in range(1, max_locks + 1):
        for field, typ, desc in LOCK_FIELDS:
            out.append((lock_key(n, field), typ, f"Lock {n}: {desc}"))
        # Savant's native Door Lock service states (entity address = lock number)
        out.append((f"DoorLockStatus_{n}", "string", f"Lock {n}: Savant Door Lock service status (Locked/Unlocked)"))
        out.append((f"IsDoorLocked_{n}", "boolean", f"Lock {n}: Savant Door Lock service flag (true/false)"))
        out.append((f"DoorLockError_{n}", "string", f"Lock {n}: data table Error State: blank when fine, else the problem"))
        out.append((f"DoorStatus_{n}", "string", f"Lock {n}: Savant door state Open/Closed (door position sensor)"))
        out.append((f"IsDoorClosed_{n}", "boolean", f"Lock {n}: Savant door closed flag (sent only when the sensor reports)"))
        out.append((f"BatteryLevel_{n}", "float", f"Lock {n}: Savant battery level, percent"))
    return out


STATE_DEFS = state_defs(DEFAULT_CONFIG["max_locks"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ascii_text(v, limit=200):
    s = "" if v is None else str(v)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[\r\n\t]+", " ", s).strip()
    return s[:limit]


def truthy(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "enabled", "online", "connected")


def to_int(v, default=0):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def norm_status(v):
    s = str(v or "").strip().lower()
    if s in ("locked", "lock"):
        return "Locked"
    if s in ("unlocked", "unlock"):
        return "Unlocked"
    if "jam" in s:
        return "Jammed"
    return "Unknown"


def norm_door(v):
    s = str(v or "").strip().lower()
    if s in ("open", "opened", "ajar"):
        return "Open"
    if s in ("closed", "close", "shut"):
        return "Closed"
    return "Unknown"


def is_online(info):
    """Kwikset reports deviceconnectivitystatus connected/disconnected (older field: connectivitystatus).
    No field at all = assume reachable."""
    v = info.get("deviceconnectivitystatus")
    if v in (None, ""):
        v = info.get("connectivitystatus")
    return truthy(v) if v not in (None, "") else True


def event_source(v):
    """History eventtype -> short source. Seen: "Keypad", "Manual (Turn Piece, Key)",
    "Mobile ( WiFi, LTE, ETC)", "Unknown Source"."""
    s = str(v or "").lower()
    if "keypad" in s:
        return "Keypad"
    if "manual" in s or "turn piece" in s or "key" in s:
        return "Manual"
    if "mobile" in s or "app" in s:
        return "App"
    if not s or "unknown" in s:
        return "Unknown"
    return ascii_text(v, 40)


def history_time(ts):
    """History timestamps are the lock's local wall-clock time stored as if it were UTC."""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(ts)))
    except (TypeError, ValueError, OverflowError):
        return ""


PRIVATE_KEYS = ("email", "owneremail", "phonenumber")


def redact(v):
    """halolink dump output: hide the account's email and phone number."""
    if isinstance(v, dict):
        return {k: ("(hidden)" if k.lower() in PRIVATE_KEYS and v[k] else redact(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [redact(x) for x in v]
    return v


def parse_allowed(value):
    """allowed_clients -> (allow_any, allow_local, [networks]). Raises ValueError on a bad entry."""
    items = value if isinstance(value, list) else [value]
    allow_any, allow_local, nets = False, False, []
    for raw in items:
        item = str(raw).strip().lower()
        if item in ("any", "*", "all"):
            allow_any = True
        elif item in ("auto", "local", "self", ""):
            allow_local = True
        else:
            nets.append(ipaddress.ip_network(item, strict=False))
    return allow_any, allow_local, nets


def is_this_machine(ip: str) -> bool:
    """True if ip is assigned to this machine. Binding only succeeds for local addresses,
    so this follows DHCP changes and extra interfaces without listing them."""
    try:
        addr = ipaddress.ip_address(ip)
        if addr.is_loopback:
            return True
        fam = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
        with socket.socket(fam, socket.SOCK_STREAM) as s:
            s.bind((ip, 0))
        return True
    except (OSError, ValueError):
        return False


def write_private_json(path: Path, data):
    """Atomic write, readable by this user only (tokens)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


def import_kwikset():
    try:
        import aiokwikset  # noqa: F401
        from aiokwikset import API
        return API
    except ImportError as e:
        raise SystemExit(f"aiokwikset is not installed for this Python ({sys.executable}): {e}\n"
                         "Run install.sh, or: pip install aiokwikset") from e


_KW_ERRORS = None


def kw_errors():
    """aiokwikset exception classes, looked up lazily (the tests run without the library)."""
    global _KW_ERRORS
    if _KW_ERRORS is None:
        _KW_ERRORS = _load_kw_errors()
    return _KW_ERRORS


def _load_kw_errors():
    try:
        import aiokwikset.errors as e
        return e
    except ImportError:
        class _E:  # noqa: N801
            class KwiksetError(Exception): ...
            class TokenExpiredError(KwiksetError): ...
            class Unauthenticated(KwiksetError): ...
            class ConnectionError(KwiksetError): ...  # noqa: A001
            class MFAChallengeRequired(KwiksetError): ...
        return _E


# ---------------------------------------------------------------------------
# bridge
# ---------------------------------------------------------------------------
class Lock:
    __slots__ = ("number", "device_id", "home_id", "info", "status", "last_change", "last_event", "last_source",
                 "last_user", "last_event_time", "pending", "pending_target", "pending_since", "error", "cmd_lock")

    def __init__(self, number, device_id, home_id, info):
        self.number = number
        self.device_id = device_id
        self.home_id = home_id
        self.info = dict(info or {})
        self.status = norm_status(self.info.get("doorstatus") or self.info.get("devicestatus"))
        self.last_change = ""
        self.last_event = ""
        self.last_source = ""
        self.last_user = ""
        self.last_event_time = ""
        self.pending = ""
        self.pending_target = ""
        self.pending_since = 0.0
        self.error = ""
        self.cmd_lock = asyncio.Lock()

    @property
    def name(self):
        return ascii_text(self.info.get("devicename") or f"Lock {self.number}", 60)

    @property
    def online(self):
        return is_online(self.info)

    @property
    def shown_status(self):
        """What Savant sees: never a stale Locked/Unlocked while Kwikset can't reach the lock."""
        return self.status if self.online else "Unknown"

    @property
    def door_lock_error(self):
        """The Savant app shows the data table's Error State in red, so blank unless something is wrong."""
        if not self.online:
            return "Lock offline"
        if self.status == "Jammed":
            return "Lock jammed"
        return self.error


class Bridge:
    def __init__(self, cfg, home: Path, api_factory=None):
        self.cfg = cfg
        self.home = Path(home)
        self.tokens_path = self.home / "tokens.json"
        self.numbers_path = self.home / "locks.json"
        self.settings_path = self.home / "settings.json"
        self.api_factory = api_factory
        self.api = None
        self.session = None
        self.user = None
        self.locks = {}             # number -> Lock
        self.by_device = {}         # device_id -> Lock
        self.values = {}
        self.clients = set()
        self.wake = None            # set to poll now
        self.reauth = None          # set when new tokens arrive
        self.tokens_mtime = 0.0
        self.max_locks = max(1, min(32, int(cfg.get("max_locks", 8))))
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kwikset")
        self.stopping = None
        self.config_home = str(cfg.get("home") or "")
        self.allow_any, self.allow_local, self.allow_nets = parse_allowed(cfg.get("allowed_clients", "auto"))
        self.rejected_logged = {}
        self.service = False        # True in run(): background history lookups only in the service, not the CLI
        self.settings = self.load_settings()
        if self.settings.get("home"):
            cfg["home"] = "" if self.settings["home"] == "ALL" else self.settings["home"]   # Kwikset Home sent from Blueprint wins over config.json

    def unlock_block(self):
        """Why unlocking is off, or "" when allowed. config.json is the host-level switch; the Blueprint
        "Disable Unlock" variable can only turn unlocking off on top of it, never back on."""
        if not self.cfg.get("allow_remote_unlock", True):
            return "remote unlock is disabled in the bridge config"
        if self.settings.get("disable_unlock"):
            return "unlock is disabled in Blueprint (Disable Unlock)"
        return ""

    def load_settings(self):
        try:
            return json.loads(self.settings_path.read_text())
        except Exception:
            return {}

    def save_settings(self):
        try:
            tmp = self.settings_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.settings, indent=2))
            os.replace(str(tmp), str(self.settings_path))
        except Exception as e:
            log.error("could not save settings.json: %s", e)

    # ---------- persistence ----------
    def load_tokens(self):
        try:
            data = json.loads(self.tokens_path.read_text())
            self.tokens_mtime = self.tokens_path.stat().st_mtime
            if data.get("refresh_token"):
                return data
        except FileNotFoundError:
            pass
        except Exception as e:
            log.error("tokens.json unreadable: %s", e)
        return None

    async def save_tokens(self, id_token, access_token, refresh_token):
        data = self.load_tokens() or {}
        data.update(id_token=id_token, access_token=access_token, refresh_token=refresh_token, saved=now_text())
        if self.api is not None and getattr(self.api, "username", None):
            data.setdefault("email", self.api.username)
        try:
            write_private_json(self.tokens_path, data)
            self.tokens_mtime = self.tokens_path.stat().st_mtime
            log.debug("tokens refreshed and saved")
        except Exception as e:
            log.error("could not save tokens: %s", e)

    def load_numbers(self):
        try:
            return {k: int(v) for k, v in json.loads(self.numbers_path.read_text()).items()}
        except FileNotFoundError:
            return {}
        except Exception as e:
            log.warning("locks.json unreadable (%s); numbering from scratch", e)
            return {}

    def save_numbers(self, mapping):
        try:
            tmp = self.numbers_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(mapping, indent=2))
            os.replace(str(tmp), str(self.numbers_path))
        except Exception as e:
            log.error("could not save locks.json: %s", e)

    # ---------- state publishing ----------
    async def publish(self, updates):
        lines = []
        for k, v in updates.items():
            if isinstance(v, bool):
                v = "true" if v else "false"
            v = ascii_text(v)
            if self.values.get(k) != v:
                self.values[k] = v
                lines.append(f"{k}={v}\n")
        if lines and self.clients:
            data = "".join(lines).encode(SAVANT_ENCODING, "replace")
            for w in list(self.clients):
                try:
                    w.write(data)
                except Exception:
                    self.clients.discard(w)
            for w in list(self.clients):
                try:
                    await asyncio.wait_for(w.drain(), 5)
                except Exception:
                    self.clients.discard(w)

    def lock_states(self, n):
        lk = self.locks.get(n)
        if lk is None:
            empty = {lock_key(n, f): (0 if t == "integer" else "") for f, t, _ in LOCK_FIELDS}
            empty[lock_key(n, "STATUS")] = "Unknown"
            empty[f"DoorLockStatus_{n}"] = "Unknown"
            empty[f"IsDoorLocked_{n}"] = False
            empty[f"DoorLockError_{n}"] = ""
            empty[f"DoorStatus_{n}"] = "Unknown"
            empty[f"BatteryLevel_{n}"] = 0
            return empty
        i = lk.info
        battery = to_int(i.get("batterypercentage"), -1)
        door = norm_door(i.get("doorposition") or i.get("doorstatusposition"))
        status = lk.shown_status
        return {
            lock_key(n, "PRESENT"): 1,
            lock_key(n, "NAME"): lk.name,
            lock_key(n, "STATUS"): status,
            lock_key(n, "LOCKED"): 1 if status == "Locked" else 0,
            lock_key(n, "LAST_KNOWN"): lk.status,
            lock_key(n, "PENDING"): lk.pending,
            lock_key(n, "ONLINE"): 1 if lk.online else 0,
            lock_key(n, "BATTERY"): max(battery, 0),
            lock_key(n, "BATTERY_LOW"): 1 if 0 <= battery <= int(self.cfg["battery_low_percent"]) else 0,
            lock_key(n, "DOOR"): door,
            lock_key(n, "DOOR_OPEN"): 1 if door == "Open" else 0,
            lock_key(n, "LED"): 1 if truthy(i.get("ledstatus")) else 0,
            lock_key(n, "AUDIO"): 1 if truthy(i.get("audiostatus")) else 0,
            lock_key(n, "SECURE_SCREEN"): 1 if truthy(i.get("securescreenstatus")) else 0,
            lock_key(n, "AUTOLOCK"): 1 if truthy(i.get("autolockstate")) else 0,
            lock_key(n, "AUTOLOCK_DELAY"): to_int(i.get("autolockdelay")),
            lock_key(n, "TAMPER"): ascii_text(i.get("locktamperstate"), 40),
            lock_key(n, "LAST_EVENT"): lk.last_event,
            lock_key(n, "LAST_SOURCE"): lk.last_source,
            lock_key(n, "LAST_USER"): lk.last_user,
            lock_key(n, "LAST_EVENT_TIME"): lk.last_event_time,
            lock_key(n, "LAST_CHANGE"): lk.last_change,
            lock_key(n, "MODEL"): ascii_text(i.get("modelnumber"), 40),
            lock_key(n, "FIRMWARE"): ascii_text(i.get("firmwareversion"), 40),
            lock_key(n, "ERROR"): lk.error,
            f"DoorLockStatus_{n}": status if status in ("Locked", "Unlocked") else "Unknown",
            f"IsDoorLocked_{n}": status == "Locked",
            f"DoorLockError_{n}": lk.door_lock_error,
            f"DoorStatus_{n}": door,
            f"BatteryLevel_{n}": max(battery, 0),
            # only when the door sensor actually reports, so "no sensor" never reads as "open"
            **({f"IsDoorClosed_{n}": door == "Closed"} if door != "Unknown" else {}),
        }

    def summary_states(self):
        present = [self.locks[n] for n in sorted(self.locks)]
        unlocked = [lk for lk in present if lk.shown_status != "Locked"]
        low = [lk for lk in present if 0 <= to_int(lk.info.get("batterypercentage"), -1) <= int(self.cfg["battery_low_percent"])]
        door_open = [lk for lk in present if norm_door(lk.info.get("doorposition")) == "Open"]
        offline = [lk for lk in present if not lk.online]
        return {
            "SYS_LOCK_COUNT": len(present),
            "SYS_ALL_LOCKED": 1 if present and not unlocked else 0,
            "SYS_UNLOCKED_COUNT": len(unlocked),
            "SYS_UNLOCKED_NAMES": ", ".join(lk.name for lk in unlocked),
            "SYS_LOW_BATTERY_COUNT": len(low),
            "SYS_LOW_BATTERY_NAMES": ", ".join(lk.name for lk in low),
            "SYS_DOOR_OPEN_COUNT": len(door_open),
            "SYS_OFFLINE_COUNT": len(offline),
        }

    async def publish_lock(self, lk):
        await self.publish({**self.lock_states(lk.number), **self.summary_states()})

    async def publish_all(self):
        upd = {}
        for n in range(1, self.max_locks + 1):
            upd.update(self.lock_states(n))
        upd.update(self.summary_states())
        await self.publish(upd)

    # ---------- Kwikset ----------
    def new_api(self):
        if self.api_factory:
            return self.api_factory(self)
        API = import_kwikset()
        import aiohttp
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return API(websession=self.session, token_update_callback=self.save_tokens, executor=self.executor)

    async def close_api(self):
        if self.api is not None:
            try:
                await self.api.async_close()
            except Exception as e:
                log.debug("api close: %s", e)
        self.api = None

    async def sign_in(self, tokens):
        E = kw_errors()
        await self.close_api()
        self.api = self.new_api()
        try:
            await self.api.async_authenticate_with_tokens(
                id_token=tokens["id_token"], access_token=tokens["access_token"],
                refresh_token=tokens["refresh_token"], username=tokens.get("email"))
        except (E.TokenExpiredError, E.Unauthenticated) as e:
            await self.close_api()
            raise PermissionError(str(e)) from e
        self.user = await self.api.user.get_info() or {}
        email = tokens.get("email") or self.user.get("email") or ""
        await self.publish({"SYS_AUTH": "ok", "SYS_ACCOUNT": email, "SYS_ERROR": ""})
        log.info("signed in to Kwikset as %s", email or "(unknown email)")
        return email

    def assign_numbers(self, devices):
        """devices: list of (device_id, home_id, info). Stable numbering:
        lock_order in config first, then numbers saved in locks.json, then the next free number."""
        saved = self.load_numbers()
        order = [str(x).strip().lower() for x in self.cfg.get("lock_order") or []]
        taken, result = {}, {}
        for idx, key in enumerate(order, start=1):
            for dev_id, _, info in devices:
                names = {dev_id.lower(), str(info.get("devicename", "")).strip().lower(),
                         str(info.get("serialnumber", "")).strip().lower()}
                if key in names and dev_id not in result and idx not in taken:
                    result[dev_id], taken[idx] = idx, dev_id
        for dev_id, _, _ in devices:
            n = saved.get(dev_id)
            if dev_id not in result and n and n not in taken:
                result[dev_id], taken[n] = n, dev_id
        for dev_id, _, info in sorted(devices, key=lambda d: str(d[2].get("devicename", "")).lower()):
            if dev_id in result:
                continue
            n = next(i for i in range(1, 1000) if i not in taken)
            result[dev_id], taken[n] = n, dev_id
        merged = dict(saved)
        merged.update(result)
        if merged != saved:
            self.save_numbers(merged)
        return result

    async def discover(self):
        homes = await self.api.user.get_homes() or []
        want = str(self.cfg.get("home") or "").strip().lower()
        if want:
            homes = [h for h in homes if str(h.get("homename", "")).strip().lower() == want]
            if not homes:
                raise RuntimeError(f"home {self.cfg['home']!r} not found on this Kwikset account")
        devices = []
        for h in homes:
            for d in await self.api.device.get_devices(h["homeid"]) or []:
                dev_id = d.get("deviceid")
                if not dev_id:
                    continue
                info = dict(d)
                try:
                    full = await self.api.device.get_device_info(dev_id)
                    if full:
                        info.update(full)
                except Exception as e:
                    log.warning("device info for %s failed: %s", d.get("devicename", dev_id), e)
                devices.append((dev_id, h["homeid"], info))
        numbers = self.assign_numbers(devices)
        old = self.by_device
        self.locks, self.by_device = {}, {}
        for dev_id, home_id, info in devices:
            n = numbers[dev_id]
            if n > self.max_locks:
                log.warning("lock %r is number %d but the profile only has %d slots (raise max_locks)",
                            info.get("devicename"), n, self.max_locks)
                continue
            lk = old.get(dev_id)
            if lk is None:
                lk = Lock(n, dev_id, home_id, info)
                if self.service:    # fill LAST_EVENT etc. for a newly found lock
                    self.spawn(self.fetch_last_event(lk, delays=(0,)))
            if lk.number != n:
                lk.number = n
            self.apply_info(lk, info, "poll")
            self.locks[n] = lk
            self.by_device[dev_id] = lk
        log.info("locks: %s", ", ".join(f"{n}={self.locks[n].name} ({self.locks[n].status})" for n in sorted(self.locks)) or "none")

    def apply_info(self, lk, info, source):
        """Merge new fields; track status changes. Returns True if the status changed.
        Who/what changed the lock isn't in the push message, so a change triggers a history lookup."""
        was_online = lk.online
        lk.info.update({k: v for k, v in info.items() if v is not None})
        ev = str(info.get("eventtype") or "").strip().lower()
        if ev in ("connected", "disconnected"):     # connectivity push, not a lock event
            lk.info["deviceconnectivitystatus"] = ev
        if lk.online != was_online:
            log.info("lock %d %s: %s (%s)", lk.number, lk.name, "back online" if lk.online else "offline", source)
        raw = info.get("doorstatus") or info.get("devicestatus") or info.get("deviceStatus")
        changed = False
        if raw is not None:
            st = norm_status(raw)
            if st != lk.status:
                log.info("lock %d %s: %s -> %s (%s)", lk.number, lk.name, lk.status, st, source)
                lk.status, lk.last_change, changed = st, now_text(), True
                if not lk.pending:
                    lk.error = ""   # a fresh report from the lock supersedes an old command error
            if not lk.last_change:
                lk.last_change = now_text()
            if lk.pending and st == lk.pending_target:
                lk.pending, lk.pending_target, lk.error = "", "", ""
        if changed and self.service and self.api is not None:
            self.spawn(self.fetch_last_event(lk, want=lk.status))
        return changed

    async def fetch_last_event(self, lk, want=None, delays=(2, 5, 10)):
        """LAST_EVENT / LAST_SOURCE / LAST_USER from the newest Lock Mechanism event in Kwikset's history.
        History can lag the push, so retry until the newest event matches the new status."""
        for delay in delays:
            await asyncio.sleep(delay)
            try:
                hist = await self.api.device.get_device_history(lk.info.get("serialnumber") or lk.device_id, top=5)
            except Exception as e:
                log.debug("history for lock %d failed: %s", lk.number, e)
                continue
            events = (hist or {}).get("data") or []
            ev = next((e for e in events if str(e.get("eventcategory", "")).lower() == "lock mechanism"), None)
            if ev is None or (want and norm_status(ev.get("event")) != want):
                continue
            lk.last_event = norm_status(ev.get("event"))
            lk.last_source = event_source(ev.get("eventtype"))
            lk.last_user = ascii_text(ev.get("user"), 60)
            if "apikwik" in lk.last_user.lower():
                # commands through the Kwikset API (this bridge) are logged as "<first name>  apikwik"
                lk.last_source = "Savant"
                lk.last_user = re.sub(r"\s*apikwik\s*", " ", lk.last_user, flags=re.I).strip()
            lk.last_event_time = history_time(ev.get("timestamp"))
            log.info("lock %d %s: %s by %s%s", lk.number, lk.name, lk.last_event, lk.last_source,
                     f" ({lk.last_user})" if lk.last_user else "")
            await self.publish_lock(lk)
            return

    async def poll(self):
        for lk in list(self.locks.values()):
            info = await self.api.device.get_device_info(lk.device_id)
            if info:
                self.apply_info(lk, info, "poll")
        await self.publish_all()
        await self.publish({"SYS_ONLINE": 1, "SYS_LAST_UPDATE": now_text()})

    def on_push(self, name, data):
        """Real-time callback from aiokwikset (sync, on the event loop)."""
        if isinstance(data, dict) and isinstance(data.get(name), dict):
            data = data[name]          # GraphQL wraps the payload in the subscription name
        if not isinstance(data, dict):
            return
        dev_id = data.get("deviceid") or data.get("deviceId")
        lk = self.by_device.get(dev_id)
        if lk is None:
            if dev_id:
                log.info("push for unknown device %s - rediscovering", dev_id)
                self.wake.set()
            return
        log.debug("push %s for lock %d: %s", name, lk.number, {k: v for k, v in data.items() if v is not None})
        self.apply_info(lk, data, "push")
        asyncio.ensure_future(self.publish_lock(lk))
        asyncio.ensure_future(self.publish({"SYS_LAST_UPDATE": now_text()}))

    async def start_push(self, email):
        if not self.cfg.get("realtime", True) or not email:
            await self.publish({"SYS_LIVE": 0})
            return
        try:
            subs = self.api.subscriptions
            subs.set_callback(self.on_push)
            subs.set_on_disconnect(lambda: asyncio.ensure_future(self._live(0)))
            subs.set_on_reconnect(lambda: asyncio.ensure_future(self._live(1, resync=True)))
            await subs.async_subscribe_device(email)
            await self.publish({"SYS_LIVE": 1})
            log.info("real-time push connected")
        except Exception as e:
            log.warning("real-time push unavailable, polling every %ss: %s", self.cfg["poll_seconds"], e)
            await self.publish({"SYS_LIVE": 0})

    async def _live(self, up, resync=False):
        log.info("real-time push %s", "reconnected" if up else "dropped")
        await self.publish({"SYS_LIVE": up})
        if resync:
            self.wake.set()   # catch anything missed while it was down

    async def kwikset_loop(self):
        """Sign in, discover, push + poll; recover from token and network problems."""
        E = kw_errors()
        backoff = 30
        while not self.stopping.is_set():
            tokens = self.load_tokens()
            if not tokens:
                await self.publish({"SYS_AUTH": "login_required", "SYS_ONLINE": 0, "SYS_LIVE": 0,
                                    "SYS_ERROR": "not signed in: run  halolink login  on the host"})
                log.warning("no Kwikset sign-in yet: run  halolink login  on this host")
                await self.wait_for_tokens()
                continue
            try:
                email = await self.sign_in(tokens)
                await self.discover()
                await self.publish_all()
                await self.publish({"SYS_ONLINE": 1, "SYS_LAST_UPDATE": now_text()})
                await self.start_push(email)
                backoff = 30
                await self.poll_loop()
            except PermissionError as e:
                log.error("Kwikset sign-in expired or revoked (%s): run  halolink login  again", e)
                await self.publish({"SYS_AUTH": "reauth_required", "SYS_ONLINE": 0, "SYS_LIVE": 0,
                                    "SYS_ERROR": "Kwikset sign-in expired: run  halolink login  on the host"})
                await self.wait_for_tokens()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if isinstance(e, E.Unauthenticated):
                    log.error("Kwikset rejected the session: %s", e)
                    await self.publish({"SYS_AUTH": "reauth_required", "SYS_ONLINE": 0, "SYS_LIVE": 0,
                                        "SYS_ERROR": "Kwikset sign-in rejected: run  halolink login  on the host"})
                    await self.wait_for_tokens()
                    continue
                log.warning("Kwikset unreachable (%s: %s); retrying in %ss", type(e).__name__, e, backoff)
                await self.publish({"SYS_ONLINE": 0, "SYS_ERROR": ascii_text(f"Kwikset unreachable: {e}", 120)})
                await self.sleep_or_wake(backoff)
                backoff = min(backoff * 2, 600)
            finally:
                if self.stopping.is_set():
                    await self.close_api()

    async def poll_loop(self):
        """Runs while signed in. Returns/raises to make kwikset_loop re-sign-in."""
        E = kw_errors()
        failures = 0
        while not self.stopping.is_set():
            woke = await self.sleep_or_wake(max(30, int(self.cfg["poll_seconds"])))
            if self.reauth.is_set():
                self.reauth.clear()
                log.info("new sign-in tokens - reconnecting")
                return
            try:
                if woke == "rediscover":
                    await self.discover()
                await self.poll()
                failures = 0
            except E.Unauthenticated:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                log.warning("poll failed (%d): %s", failures, e)
                await self.publish({"SYS_ONLINE": 0, "SYS_ERROR": ascii_text(f"Kwikset unreachable: {e}", 120)})
                if failures >= 3:
                    raise

    async def sleep_or_wake(self, seconds):
        """Sleep up to `seconds`; return early on REFRESH, new tokens, stop. Also watches tokens.json."""
        end = time.monotonic() + seconds
        while not self.stopping.is_set():
            if self.wake.is_set():
                self.wake.clear()
                return "rediscover"
            if self.reauth.is_set():
                return "reauth"
            try:
                m = self.tokens_path.stat().st_mtime
                if m != self.tokens_mtime and self.tokens_mtime:
                    self.tokens_mtime = m
                    self.reauth.set()
                    return "reauth"
            except FileNotFoundError:
                pass
            left = end - time.monotonic()
            if left <= 0:
                return "timer"
            try:
                await asyncio.wait_for(self.wake.wait(), min(left, 5))
            except asyncio.TimeoutError:
                pass
        return "stop"

    async def wait_for_tokens(self):
        start = self.tokens_mtime
        while not self.stopping.is_set():
            if self.reauth.is_set():
                self.reauth.clear()
                return
            try:
                m = self.tokens_path.stat().st_mtime
                if m != start:
                    return
            except FileNotFoundError:
                pass
            try:
                await asyncio.wait_for(self.wake.wait(), 5)   # RELOAD / stop set wake
                self.wake.clear()
            except asyncio.TimeoutError:
                pass

    # ---------- commands ----------
    def find_lock(self, ref):
        ref = (ref or "").strip()
        if ref.isdigit():
            return self.locks.get(int(ref))
        low = ref.lower()
        for lk in self.locks.values():
            if low in (lk.device_id.lower(), lk.name.lower(), str(lk.info.get("serialnumber", "")).lower()):
                return lk
        return None

    async def command_result(self, lk, what, res):
        ok = bool(getattr(res, "success", False))
        err = "" if ok else ascii_text(getattr(res, "error_message", None) or "command failed", 120)
        lk.error = err
        await self.publish({"SYS_LAST_COMMAND": ascii_text(f"{what} {lk.number} {lk.name}: {'ok' if ok else err}", 160)})
        if not ok:
            log.warning("%s lock %d %s failed: %s", what, lk.number, lk.name, err)
        return ok

    async def do_lock(self, lk, target):
        """LOCK/UNLOCK with confirmation: PENDING until the lock reports the new state."""
        verb = "Locking" if target == "Locked" else "Unlocking"
        what = "LOCK" if target == "Locked" else "UNLOCK"
        if lk.cmd_lock.locked():
            log.info("%s lock %d: previous command still running, queued", what, lk.number)
        async with lk.cmd_lock:
            api = self.api
            if api is None:
                lk.error = "bridge not signed in to Kwikset"
                await self.publish_lock(lk)
                return
            lk.pending, lk.pending_target, lk.pending_since, lk.error = verb, target, time.monotonic(), ""
            await self.publish_lock(lk)
            log.info("%s lock %d %s (was %s)", what, lk.number, lk.name, lk.status)
            if target == "Locked":
                res = await api.device.lock_device(lk.info, self.user or {})
            else:
                res = await api.device.unlock_device(lk.info, self.user or {})
            if not await self.command_result(lk, what, res):
                lk.pending, lk.pending_target = "", ""
                await self.publish_lock(lk)
                return
            # confirm: push usually lands first; poll as a backstop
            deadline = time.monotonic() + int(self.cfg.get("confirm_seconds", 45))
            for delay in (2, 4, 6, 10, 15, 20):
                left = deadline - time.monotonic()
                if not lk.pending or left <= 0:
                    break
                await asyncio.sleep(min(delay, left))
                if not lk.pending:
                    break
                try:
                    info = await api.device.get_device_info(lk.device_id)
                    if info:
                        self.apply_info(lk, info, "confirm")
                        await self.publish_lock(lk)
                except Exception as e:
                    log.debug("confirm poll failed: %s", e)
            if lk.pending:
                lk.pending, lk.pending_target = "", ""
                lk.error = f"no confirmation from the lock ({target.lower()} not reported)"
                log.warning("lock %d %s: %s", lk.number, lk.name, lk.error)
            await self.publish_lock(lk)
            await self.publish(self.summary_states())

    async def do_setting(self, lk, what, arg):
        on = arg.strip().upper() in ("ON", "1", "TRUE", "ENABLE", "ENABLED")
        off = arg.strip().upper() in ("OFF", "0", "FALSE", "DISABLE", "DISABLED")
        if self.api is None:
            return
        api = self.api
        dev = api.device
        async with lk.cmd_lock:
            if what == "AUTOLOCK":
                if off:
                    res = await dev.set_autolock_enabled(lk.info, False)
                    lk.info.update(autolockstate="false", autolockdelay=0)
                else:
                    secs = to_int(arg, 30) if not on else 30
                    if secs not in AUTOLOCK_DELAYS:
                        lk.error = f"auto-lock delay must be one of {', '.join(map(str, AUTOLOCK_DELAYS))} seconds"
                        await self.publish_lock(lk)
                        return
                    res = await dev.set_autolock_enabled(lk.info, True, secs)
                    lk.info.update(autolockstate="true", autolockdelay=secs)
            else:
                if not (on or off):
                    lk.error = f"{what} needs ON or OFF"
                    await self.publish_lock(lk)
                    return
                fn = {"LED": dev.set_led_enabled, "AUDIO": dev.set_audio_enabled,
                      "SECURESCREEN": dev.set_secure_screen_enabled}[what]
                field = {"LED": "ledstatus", "AUDIO": "audiostatus", "SECURESCREEN": "securescreenstatus"}[what]
                res = await fn(lk.info, on)
                lk.info[field] = "true" if on else "false"
            if not await self.command_result(lk, what, res):
                # fetch the real value back since we updated optimistically
                try:
                    info = await api.device.get_device_info(lk.device_id)
                    if info:
                        self.apply_info(lk, info, "poll")
                except Exception:
                    pass
            await self.publish_lock(lk)

    async def refresh_one(self, lk):
        try:
            info = await self.api.device.get_device_info(lk.device_id)
            if info:
                self.apply_info(lk, info, "query")
                await self.publish_lock(lk)
        except Exception as e:
            log.debug("status query for lock %d failed: %s", lk.number, e)

    def spawn(self, coro):
        t = asyncio.ensure_future(coro)
        t.add_done_callback(lambda f: f.cancelled() or not f.exception() or
                            log.error("command error: %r", f.exception()))
        return t

    async def handle_line(self, line, writer):
        line = line.strip()
        if not line:
            return
        verb, _, arg = line.partition(" ")
        verb, arg = verb.upper(), arg.strip()
        log.debug("savant: %s", line)
        if verb == "PING":
            writer.write(b"PONG\n")
        elif verb == "STATUS":
            # STATUS <n>: the Door Lock entity's query (GetLockStatus). Send that lock's
            # current states to this connection, and refresh it from Kwikset in the background.
            lk = self.find_lock(arg.split(",")[0].strip())
            if lk is not None:
                keys = self.lock_states(lk.number)
                writer.write("".join(f"{k}={self.values[k]}\n" for k in keys if k in self.values)
                             .encode(SAVANT_ENCODING, "replace"))
                if self.api is not None:
                    self.spawn(self.refresh_one(lk))
        elif verb == "DUMP":
            writer.write("".join(f"{k}={v}\n" for k, v in self.values.items()).encode(SAVANT_ENCODING, "replace"))
        elif verb == "REFRESH":
            self.wake.set()
        elif verb == "HOME":
            # HOME <Kwikset home name>   (blank = config.json "home"; ALL = every home on the account)
            if arg.upper() == "ALL":
                val, saved = "", "ALL"
            elif arg.upper() in ("", "CLEAR", "NONE"):
                val, saved = self.config_home, ""
            else:
                val, saved = arg, arg
            if val != str(self.cfg.get("home") or "") or saved != self.settings.get("home", ""):
                log.info("Kwikset home filter from Savant: %s", val or "(all homes)")
                self.cfg["home"] = val
                self.settings["home"] = saved
                self.save_settings()
                self.wake.set()
        elif verb == "DISABLEUNLOCK":
            # DISABLEUNLOCK YES|NO from the Blueprint "Disable Unlock" variable (blank = NO)
            on = arg.strip().upper() in ("YES", "Y", "ON", "1", "TRUE", "DISABLE", "DISABLED")
            if on != bool(self.settings.get("disable_unlock")):
                log.info("Disable Unlock from Savant: %s", "YES (lock only)" if on else "NO")
                self.settings["disable_unlock"] = on
                self.save_settings()
            await self.publish({"SYS_UNLOCK_ALLOWED": 0 if self.unlock_block() else 1})
        elif verb == "RELOAD":
            log.info("RELOAD requested")
            self.reauth.set()
            self.wake.set()
        elif verb in ("LOCK", "UNLOCK", "LOCKALL", "LED", "AUDIO", "SECURESCREEN", "AUTOLOCK"):
            if self.api is None or self.values.get("SYS_AUTH") != "ok":
                await self.publish({"SYS_LAST_COMMAND": f"{line}: ignored, bridge not signed in to Kwikset"})
                log.warning("%s ignored: not signed in", line)
            elif verb == "LOCKALL":
                await self.publish({"SYS_LAST_COMMAND": "LOCKALL"})
                for lk in self.locks.values():
                    if lk.status != "Locked":
                        self.spawn(self.do_lock(lk, "Locked"))
            else:
                if verb in ("LOCK", "UNLOCK"):
                    ref, rest = arg, ""
                elif "," in arg:                      # from the profile: LED 3,ON
                    ref, _, rest = arg.rpartition(",")
                else:                                 # typed by hand: LED 3 ON / LED Front Door ON
                    ref, _, rest = arg.rpartition(" ")
                ref, rest = ref.strip(), rest.strip()
                lk = self.find_lock(ref)
                if lk is None:
                    await self.publish({"SYS_LAST_COMMAND": ascii_text(f"{line}: no lock {ref or '(blank)'}")})
                    log.warning("%s: no lock %r", line, ref)
                elif verb == "LOCK":
                    self.spawn(self.do_lock(lk, "Locked"))
                elif verb == "UNLOCK":
                    why = self.unlock_block()
                    if why:
                        lk.error = why
                        await self.publish_lock(lk)
                        await self.publish({"SYS_LAST_COMMAND": f"UNLOCK {lk.number}: refused ({why})"})
                        log.warning("UNLOCK %d refused: %s", lk.number, why)
                    else:
                        self.spawn(self.do_lock(lk, "Unlocked"))
                else:
                    self.spawn(self.do_setting(lk, verb, rest))
        else:
            writer.write(f"SYS_ERROR=unknown command {ascii_text(verb, 40)}\n".encode(SAVANT_ENCODING, "replace"))
        await writer.drain()

    def client_allowed(self, ip):
        if self.allow_any:
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if getattr(addr, "ipv4_mapped", None):
            addr = addr.ipv4_mapped
        if addr.is_loopback:
            return True            # the halolink command on this machine
        if any(addr in net for net in self.allow_nets):
            return True
        return self.allow_local and is_this_machine(str(addr))

    async def handle_tcp(self, reader, writer):
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else ""
        if not self.client_allowed(ip):
            # refuse before reading or sending anything
            now = time.monotonic()
            if now - self.rejected_logged.get(ip, -1e9) > 300:      # log each IP at most every 5 min
                self.rejected_logged[ip] = now
                log.warning("refused connection from %s (not in allowed_clients %s)", ip,
                            json.dumps(self.cfg.get("allowed_clients")))
            await self.publish({"SYS_REJECTED_CLIENT": f"{ip} {now_text()}"})
            try:
                writer.close()
            except Exception:
                pass
            return
        log.info("Savant connected from %s", peer)
        self.clients.add(writer)
        try:
            writer.write("".join(f"{k}={v}\n" for k, v in self.values.items()).encode(SAVANT_ENCODING, "replace"))
            await writer.drain()
            while not self.stopping.is_set():
                raw = await reader.readline()
                if not raw:
                    break
                for part in raw.decode("utf-8", "replace").replace("\r", "\n").split("\n"):
                    await self.handle_line(part, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as e:
            log.warning("client %s: %s", peer, e)
        finally:
            self.clients.discard(writer)
            try:
                writer.close()
            except Exception:
                pass
            log.info("Savant disconnected %s", peer)

    def request_stop(self, why):
        self.stop_reason = why
        self.stopping.set()
        if self.wake:
            self.wake.set()

    async def run(self):
        loop = asyncio.get_event_loop()
        self.stopping, self.wake, self.reauth = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.stop_reason = ""
        self.service = True
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                loop.add_signal_handler(sig, self.request_stop, "signal " + sig.name)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        await self.publish({"SYS_UNLOCK_ALLOWED": 0 if self.unlock_block() else 1})
        await self.publish({"SYS_VERSION": VERSION, "SYS_ONLINE": 0, "SYS_LIVE": 0, "SYS_AUTH": "login_required",
                            "SYS_ERROR": "", "SYS_LAST_COMMAND": "", "SYS_ACCOUNT": "", "SYS_LAST_UPDATE": ""})
        await self.publish_all()
        srv = await asyncio.start_server(self.handle_tcp, "0.0.0.0", int(self.cfg["tcp_port"]))
        log.info("Savant TCP on :%s, pid %s", self.cfg["tcp_port"], os.getpid())
        if self.allow_any:
            log.warning("allowed_clients is \"any\": anything on the network can lock and unlock")
        else:
            who = (["this machine"] if self.allow_local else []) + [str(n) for n in self.allow_nets]
            log.info("accepting Savant connections from: %s", ", ".join(who) or "this machine's halolink command only")
        task = asyncio.ensure_future(self.kwikset_loop())
        await self.stopping.wait()
        log.info("shutting down (%s)", self.stop_reason)
        task.cancel()
        srv.close()
        for w in list(self.clients):
            try:
                w.close()
            except Exception:
                pass
        await asyncio.gather(task, return_exceptions=True)
        await self.close_api()
        if self.session is not None:
            await self.session.close()
        try:
            await asyncio.wait_for(srv.wait_closed(), 3)
        except Exception:
            pass
        self.executor.shutdown(wait=False)
        log.info("stopped")
        return 0


# ---------------------------------------------------------------------------
# CLI: login / list / dump
# ---------------------------------------------------------------------------
async def cli_login(cfg, home: Path, email=None):
    API = import_kwikset()
    from aiokwikset import MFAChallengeRequired
    E = kw_errors()
    print("Kwikset sign-in. Use the account that owns (or was shared) the locks in the Kwikset app.")
    print("The password is used once and is not saved; only Kwikset's session tokens are stored.\n")
    email = (email or input("Kwikset email: ")).strip()
    password = getpass.getpass("Kwikset password: ")
    api = API()
    try:
        try:
            await api.async_login(email, password)
        except MFAChallengeRequired as ch:
            if ch.mfa_type == "CUSTOM_CHALLENGE":
                medium = ""
                while medium not in ("email", "phone"):
                    c = input("Kwikset will send a verification code. Send it by (e)mail or (p)hone text? [e]: ").strip().lower()
                    medium = "phone" if c.startswith("p") else "email" if c in ("", "e", "email") else ""
                resp = await api.async_request_custom_challenge_code(medium, ch.mfa_tokens)
                code = input(f"Code from the {medium}: ").strip()
                await api.async_respond_to_mfa_challenge(code, "CUSTOM_CHALLENGE", resp)
            else:
                code = input("Verification code (authenticator app / text): ").strip()
                await api.async_respond_to_mfa_challenge(code, ch.mfa_type, ch.mfa_tokens)
        except E.Unauthenticated as e:
            raise SystemExit(f"\nSign-in failed: {e}") from e
        write_private_json(home / "tokens.json", {
            "email": email, "id_token": api.id_token, "access_token": api.access_token,
            "refresh_token": api.refresh_token, "saved": now_text()})
        print(f"\nSigned in. Tokens saved to {home / 'tokens.json'}")
        homes = await api.user.get_homes()
        for h in homes:
            devs = await api.device.get_devices(h["homeid"])
            print(f"  home {h.get('homename')!r}: {len(devs)} lock(s): " + ", ".join(d.get("devicename", "?") for d in devs))
    finally:
        await api.async_close()
    # nudge a running service to pick up the new tokens right away
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", int(cfg["tcp_port"])), 3)
        w.write(b"RELOAD\n")
        await w.drain()
        w.close()
        print("Told the running bridge to reconnect.")
    except Exception:
        print("(bridge service not running here; it will use the tokens when it starts)")


async def cli_list(cfg, home: Path, dump=None):
    b = Bridge(cfg, home)
    b.stopping, b.wake, b.reauth = asyncio.Event(), asyncio.Event(), asyncio.Event()
    tokens = b.load_tokens()
    if not tokens:
        raise SystemExit("not signed in: run  halolink login  first")
    try:
        await b.sign_in(tokens)
        if dump:
            homes = await b.api.user.get_homes()
            print(json.dumps(redact({"user": b.user, "homes": homes}), indent=2, default=str))
        await b.discover()
        print(f"\n{'#':>3}  {'name':24} {'status':9} {'batt':>4}  door     device id")
        for n in sorted(b.locks):
            lk = b.locks[n]
            print(f"{n:>3}  {lk.name:24} {lk.status:9} {str(lk.info.get('batterypercentage', '?')):>4}  "
                  f"{norm_door(lk.info.get('doorposition')):8} {lk.device_id}")
        print("\nSavant uses these numbers (DoorLockAddress / LOCKn_ states). Saved in", b.numbers_path)
        if dump:
            lk = b.find_lock(dump)
            if lk:
                print(f"\nRaw device info for lock {lk.number}:")
                print(json.dumps(redact(lk.info), indent=2, default=str))
                try:
                    hist = await b.api.device.get_device_history(lk.info.get("serialnumber") or lk.device_id, top=5)
                    print("\nLast 5 history events:")
                    print(json.dumps(hist, indent=2, default=str))
                except Exception as e:
                    print("history unavailable:", e)
    finally:
        await b.close_api()
        if b.session is not None:
            await b.session.close()
        b.executor.shutdown(wait=False)


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    p = Path(path)
    if p.exists():
        cfg.update({k: v for k, v in json.loads(p.read_text()).items() if not k.startswith("_")})
    cfg["poll_seconds"] = max(30, int(cfg["poll_seconds"]))
    try:
        parse_allowed(cfg.get("allowed_clients", "auto"))
    except ValueError as e:
        raise SystemExit(f"config.json allowed_clients: {e}")
    return cfg


def main():
    ap = argparse.ArgumentParser(description="Savant HaloLink - Kwikset Halo bridge")
    ap.add_argument("-c", "--config", default=str(Path(__file__).parent / "config.json"))
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-l", "--logfile", help="log to a rotating file (1 MB x 3) instead of the console")
    ap.add_argument("--login", action="store_true", help="interactive Kwikset sign-in (saves tokens.json)")
    ap.add_argument("--email", help="with --login: account email")
    ap.add_argument("--list", action="store_true", help="list locks and their Savant numbers")
    ap.add_argument("--dump", metavar="LOCK", help="with --list: print raw Kwikset data for a lock (number or name)")
    ap.add_argument("--version", action="version", version=VERSION)
    a = ap.parse_args()
    cfg = load_config(a.config)
    home = Path(a.config).resolve().parent
    level = logging.DEBUG if a.verbose else getattr(logging, str(cfg.get("log_level", "INFO")).upper(), logging.INFO)
    if a.login or a.list or a.dump:
        logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")
        loop = asyncio.get_event_loop()
        if a.login:
            loop.run_until_complete(cli_login(cfg, home, a.email))
        else:
            loop.run_until_complete(cli_list(cfg, home, a.dump))
        return
    handlers = [logging.handlers.RotatingFileHandler(a.logfile, maxBytes=1000000, backupCount=3, encoding="utf-8")] \
        if a.logfile else [logging.StreamHandler()]
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s", handlers=handlers)
    logging.getLogger("aiokwikset").setLevel(logging.DEBUG if a.verbose else logging.WARNING)
    for noisy in ("botocore", "boto3", "urllib3", "pycognito"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log.info("Savant HaloLink bridge %s, Python %s", VERSION, sys.version.split()[0])
    loop = asyncio.get_event_loop()
    try:
        code = loop.run_until_complete(Bridge(cfg, home).run())
    except OSError as e:
        log.error("could not start: %s (is port %s already in use?)", e, cfg["tcp_port"])
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
