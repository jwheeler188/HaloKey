"""End-to-end test: bridge + simulated Kwikset cloud + every profile action over TCP."""
import asyncio, json, sys, tempfile, time, os
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "bridge"))
import halolink_bridge as hb

E = hb.kw_errors()
PORT = 23600
FAILS = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class Result:
    def __init__(self, ok, err=None):
        self.success, self.error_message = ok, err


class Cloud:
    def __init__(self):
        self.devices = {
            "dev-a": dict(deviceid="dev-a", devicename="Front Door", serialnumber="SN-A", doorstatus="Locked",
                          batterypercentage="88", ledstatus="true", audiostatus="true", securescreenstatus="false",
                          autolockstate="false", autolockdelay=0, modelnumber="99390-001", firmwareversion="1.2.3",
                          connectivitystatus="online", doorposition="closed"),
            "dev-b": dict(deviceid="dev-b", devicename="Garage Entrée", serialnumber="SN-B", doorstatus="Unlocked",
                          batterypercentage="15", ledstatus="false", audiostatus="true", securescreenstatus="false",
                          autolockstate="true", autolockdelay=30, deviceconnectivitystatus="connected", doorposition="open"),
            "dev-c": dict(deviceid="dev-c", devicename="Back Door", serialnumber="SN-C", doorstatus="Unlocked",
                          batterypercentage="60", deviceconnectivitystatus="disconnected"),   # offline: never responds to commands
        }
        self.calls = []
        self.push_cb = None
        self.history = {d: [] for d in self.devices}   # newest first, like Kwikset

    def event(self, dev, event, eventtype, user=None):
        # history timestamps: local wall-clock time stored as UTC (Kwikset quirk)
        self.history[dev].insert(0, {"event": event, "eventtype": eventtype, "user": user,
                                     "eventcategory": "Lock Mechanism", "timestamp": 1792959618})


class FakeAPI:
    def __init__(self, cloud, bridge):
        self.c, self.b = cloud, bridge
        self.username = None
        self.user = self.U(self)
        self.device = self.D(self)
        self.subscriptions = self.S(self)

    async def async_authenticate_with_tokens(self, id_token, access_token, refresh_token, username=None):
        if refresh_token == "expired":
            raise E.TokenExpiredError("expired")
        self.username = username
        await self.b.save_tokens("id2", "acc2", refresh_token)   # library refreshes on sign-in

    async def async_close(self):
        pass

    class U:
        def __init__(s, a): s.a = a
        async def get_info(s): return {"firstname": "James", "lastname": "Owner", "email": "test@example.com"}
        async def get_homes(s): return [{"homeid": "h1", "homename": "Main House"}, {"homeid": "h2", "homename": "Lake"}]

    class D:
        def __init__(s, a): s.a = a
        async def get_devices(s, home):
            return [{"deviceid": d, "devicename": v["devicename"]} for d, v in s.a.c.devices.items()
                    if (home == "h1") != (d == "dev-c")]  # dev-c lives in "Lake"
        async def get_device_info(s, dev):
            return dict(s.a.c.devices[dev])
        async def get_device_history(s, dev, top=200):
            dev = next(d for d, v in s.a.c.devices.items() if dev in (d, v.get("serialnumber")))
            return {"data": s.a.c.history[dev][:top], "total": len(s.a.c.history[dev])}
        async def _act(s, info, target):
            dev = info["deviceid"]
            s.a.c.calls.append((target, dev))
            if dev == "dev-c":
                return Result(True)          # cloud accepts, lock never reports back
            async def later():
                await asyncio.sleep(1.0)
                s.a.c.devices[dev]["doorstatus"] = target
                s.a.c.event(dev, target, "Mobile ( WiFi, LTE, ETC)", "James  apikwik")   # how Kwikset logs API commands
                if s.a.c.push_cb:
                    s.a.c.push_cb("onManageDevice", {"onManageDevice": {"deviceid": dev, "devicestatus": target}})
            asyncio.ensure_future(later())
            return Result(True)
        async def lock_device(s, info, user): return await s._act(info, "Locked")
        async def unlock_device(s, info, user): return await s._act(info, "Unlocked")
        async def set_led_enabled(s, info, on):
            s.a.c.calls.append(("led", info["deviceid"], on)); s.a.c.devices[info["deviceid"]]["ledstatus"] = str(on).lower(); return Result(True)
        async def set_audio_enabled(s, info, on):
            s.a.c.calls.append(("audio", info["deviceid"], on)); return Result(False, "Response error 500")
        async def set_secure_screen_enabled(s, info, on):
            s.a.c.calls.append(("screen", info["deviceid"], on)); return Result(True)
        async def set_autolock_enabled(s, info, on, delay=None):
            s.a.c.calls.append(("autolock", info["deviceid"], on, delay)); return Result(True)

    class S:
        def __init__(s, a): s.a = a
        def set_callback(s, cb): s.a.c.push_cb = cb
        def set_on_disconnect(s, cb): s.dis = cb
        def set_on_reconnect(s, cb): s.rec = cb
        async def async_subscribe_device(s, email): return "sub1"


def profile_commands():
    """Every action in the generated profile -> the exact line Savant would send."""
    r = ET.parse(str(__import__("pathlib").Path(__file__).resolve().parent.parent / "profile" / "blueprint-import" / "kwikset_halolink.xml")).getroot()
    sample = {"Lock": "2", "DoorLockAddress": "1", "State": "ON", "Seconds": "60"}
    svars = {"Kwikset Home": "", "Disable Unlock": ""}
    out = []
    for a in r.iter("action"):
        cmd = a.find(".//command")
        line = cmd.find("command_string").text or ""
        for p in cmd.iter("parameter"):
            if p.get("action_argument"):
                line += sample[p.get("action_argument")]
            elif p.get("state_variable"):
                line += svars[p.get("state_variable")]
            else:
                line += p.text or ""
        out.append((a.get("name"), line))
    parsers = {sm.find("constant").text for sm in r.iter("status_message")}
    return out, parsers


def unit():
    print("0. helpers")
    r = hb.redact({"user": {"email": "a@b.c", "phonenumber": "+1555", "firstname": "J"}, "homes": [{"email": "a@b.c", "homename": "Main"}]})
    check(r["user"]["email"] == "(hidden)" and r["user"]["phonenumber"] == "(hidden)" and r["homes"][0]["email"] == "(hidden)"
          and r["homes"][0]["homename"] == "Main" and r["user"]["firstname"] == "J", "dump hides email and phone")
    check([hb.event_source(x) for x in ("Keypad", "Manual (Turn Piece, Key)", "Mobile ( WiFi, LTE, ETC)", "Unknown Source", None)]
          == ["Keypad", "Manual", "App", "Unknown", "Unknown"], "event sources")
    check(hb.is_online({"deviceconnectivitystatus": "disconnected", "connectivitystatus": "online"}) is False
          and hb.is_online({}) is True, "deviceconnectivitystatus wins; missing = online")


async def main():
    home = Path(tempfile.mkdtemp())
    cfg = hb.load_config("/nonexistent")
    cfg.update(tcp_port=PORT, confirm_seconds=8, poll_seconds=30)
    cloud = Cloud()
    b = hb.Bridge(cfg, home, api_factory=lambda br: FakeAPI(cloud, br))
    task = asyncio.ensure_future(b.run())
    await asyncio.sleep(0.5)

    rd, wr = await asyncio.open_connection("127.0.0.1", PORT)
    got = {}
    raw_lines = []

    async def reader():
        while True:
            line = await rd.readline()
            if not line:
                return
            t = line.decode("ascii").rstrip("\n")
            raw_lines.append(t)
            if "=" in t:
                k, v = t.split("=", 1)
                got[k] = v

    asyncio.ensure_future(reader())

    async def send(line, wait=0.3):
        wr.write((line + "\n").encode())
        await wr.drain()
        await asyncio.sleep(wait)

    print("1. not signed in yet")
    await asyncio.sleep(0.5)
    check(got.get("SYS_AUTH") == "login_required", f"SYS_AUTH=login_required (got {got.get('SYS_AUTH')})")
    check(got.get("LOCK1_PRESENT") == "0" and got.get("LOCK1_STATUS") == "Unknown", "empty slots published")
    await send("LOCK 1")
    check("not signed in" in got.get("SYS_LAST_COMMAND", ""), "commands refused while signed out")

    print("2. login writes tokens.json -> bridge picks it up")
    hb.write_private_json(home / "tokens.json", {"email": "test@example.com", "id_token": "i", "access_token": "a", "refresh_token": "r"})
    await send("RELOAD", 2.0)
    check(got.get("SYS_AUTH") == "ok" and got.get("SYS_ONLINE") == "1", "signed in, online")
    check(got.get("SYS_LIVE") == "1", "real-time push up")
    check(oct((home / "tokens.json").stat().st_mode)[-3:] == "600", "tokens.json is mode 600")
    check(json.loads((home / "tokens.json").read_text())["access_token"] == "acc2", "refreshed tokens saved by callback")
    names = {got.get(f"LOCK{n}_NAME") for n in (1, 2, 3)}
    check(names == {"Back Door", "Front Door", "Garage Entree"}, f"3 locks, ASCII names {names}")
    nums = json.loads((home / "locks.json").read_text())
    check(sorted(nums.values()) == [1, 2, 3], f"numbering saved {nums}")
    n_front, n_garage, n_back = nums["dev-a"], nums["dev-b"], nums["dev-c"]
    check(got[f"LOCK{n_front}_STATUS"] == "Locked" and got[f"IsDoorLocked_{n_front}"] == "true", "Front Door Locked + native flag")
    check(got[f"DoorLockStatus_{n_garage}"] == "Unlocked" and got[f"LOCK{n_garage}_BATTERY_LOW"] == "1", "Garage unlocked, battery low")
    check(got[f"LOCK{n_garage}_DOOR"] == "Open" and got["SYS_DOOR_OPEN_COUNT"] == "1", "door sensor open")
    check(got[f"LOCK{n_back}_ONLINE"] == "0" and got["SYS_OFFLINE_COUNT"] == "1", "offline lock flagged (deviceconnectivitystatus)")
    check(got[f"LOCK{n_back}_STATUS"] == "Unknown" and got[f"LOCK{n_back}_LAST_KNOWN"] == "Unlocked"
          and got[f"DoorLockStatus_{n_back}"] == "Unknown", "offline lock shows Unknown, last known kept")
    check(got[f"DoorLockError_{n_back}"] == "Lock offline" and got[f"DoorLockError_{n_front}"] == "",
          "Error State: Lock offline / blank when fine")
    check(got[f"LOCK{n_front}_ONLINE"] == "1" and got[f"LOCK{n_garage}_ONLINE"] == "1", "online via old and new field names")
    check(got["SYS_ALL_LOCKED"] == "0" and got["SYS_UNLOCKED_COUNT"] == "2", "summary: 2 unlocked")
    check(got["LOCK4_PRESENT"] == "0", "unused slot 4 empty")
    check(got[f"DoorStatus_{n_garage}"] == "Open" and got[f"IsDoorClosed_{n_garage}"] == "false", "Savant DoorStatus/IsDoorClosed (Open)")
    check(got[f"DoorStatus_{n_front}"] == "Closed" and got[f"IsDoorClosed_{n_front}"] == "true", "Savant DoorStatus/IsDoorClosed (Closed)")
    check(got[f"DoorStatus_{n_back}"] == "Unknown" and f"IsDoorClosed_{n_back}" not in got, "no door sensor -> IsDoorClosed never sent")
    check(got[f"BatteryLevel_{n_front}"] == "88", "Savant BatteryLevel")
    before_q = len(raw_lines)
    await send(f"STATUS {n_front}", 0.5)
    q = raw_lines[before_q:]
    check(f"DoorLockStatus_{n_front}=Locked" in q and f"IsDoorLocked_{n_front}=true" in q, f"GetLockStatus query answered ({len(q)} lines)")

    print("3. lock with confirmation via push")
    await send(f"LOCK {n_garage}", 0.4)
    check(got[f"LOCK{n_garage}_PENDING"] == "Locking", "PENDING=Locking while waiting")
    check(got[f"LOCK{n_garage}_STATUS"] == "Unlocked", "status NOT assumed before the lock reports")
    await asyncio.sleep(1.5)
    check(got[f"LOCK{n_garage}_STATUS"] == "Locked" and got[f"LOCK{n_garage}_PENDING"] == "", "confirmed Locked, pending cleared")
    await asyncio.sleep(2.5)
    check(got[f"LOCK{n_garage}_LAST_EVENT"] == "Locked" and got[f"LOCK{n_garage}_LAST_SOURCE"] == "Savant"
          and got[f"LOCK{n_garage}_LAST_USER"] == "James", "who did it: bridge command -> Savant (James)")
    check(got[f"LOCK{n_garage}_LAST_EVENT_TIME"] == "2026-10-25 20:20:18", "history time read as local wall clock")
    cloud.devices["dev-b"]["doorstatus"] = "Unlocked"
    cloud.event("dev-b", "Unlocked", "Keypad", "Guest Code")
    cloud.push_cb("onManageDevice", {"onManageDevice": {"deviceid": "dev-b", "devicestatus": "Unlocked"}})
    await asyncio.sleep(2.5)
    check(got[f"LOCK{n_garage}_STATUS"] == "Unlocked" and got[f"LOCK{n_garage}_LAST_SOURCE"] == "Keypad"
          and got[f"LOCK{n_garage}_LAST_USER"] == "Guest Code", "keypad unlock: source + code name")
    cloud.devices["dev-b"]["doorstatus"] = "Locked"
    cloud.event("dev-b", "Locked", "Manual (Turn Piece, Key)")
    cloud.push_cb("onManageDevice", {"onManageDevice": {"deviceid": "dev-b", "devicestatus": "Locked"}})
    await asyncio.sleep(2.5)
    check(got[f"LOCK{n_garage}_LAST_SOURCE"] == "Manual" and got[f"LOCK{n_garage}_LAST_USER"] == "", "thumb turn: Manual, no user")
    cloud.push_cb("onManageDevice", {"onManageDevice": {"deviceid": "dev-a", "eventtype": "disconnected"}})
    await asyncio.sleep(0.3)
    check(got[f"LOCK{n_front}_ONLINE"] == "0" and got[f"LOCK{n_front}_STATUS"] == "Unknown", "connectivity push -> offline")
    check(got[f"LOCK{n_front}_LAST_EVENT"] != "disconnected", "connectivity push is not a lock event")
    cloud.push_cb("onManageDevice", {"onManageDevice": {"deviceid": "dev-a", "eventtype": "connected"}})
    await asyncio.sleep(0.3)
    check(got[f"LOCK{n_front}_ONLINE"] == "1" and got[f"LOCK{n_front}_STATUS"] == "Locked", "connectivity push -> back online")

    print("4. lock that never confirms")
    await send(f"LOCK {n_back}", 9.5)
    check(got[f"LOCK{n_back}_PENDING"] == "" and "no confirmation" in got[f"LOCK{n_back}_ERROR"],
          f"timeout -> ERROR ({got[f'LOCK{n_back}_ERROR']})")
    check(got[f"LOCK{n_back}_STATUS"] == "Unknown" and got[f"LOCK{n_back}_LAST_KNOWN"] == "Unlocked", "offline lock still Unknown (truthful)")

    print("5. lock by name, unlock, settings")
    await send("UNLOCK Front Door", 1.6)
    check(got[f"LOCK{n_front}_STATUS"] == "Unlocked", "UNLOCK by name")
    await send(f"LED {n_garage},ON")
    check(got[f"LOCK{n_garage}_LED"] == "1" and ("led", "dev-b", True) in cloud.calls, "LED on (profile comma form)")
    await send(f"AUDIO {n_garage} OFF")
    check("500" in got[f"LOCK{n_garage}_ERROR"] and got[f"LOCK{n_garage}_AUDIO"] == "1", "failed setting reported, value re-read")
    await send(f"AUTOLOCK {n_front},45")
    check("must be one of" in got[f"LOCK{n_front}_ERROR"], "bad auto-lock delay rejected")
    await send(f"AUTOLOCK {n_front},300")
    check(got[f"LOCK{n_front}_AUTOLOCK"] == "1" and got[f"LOCK{n_front}_AUTOLOCK_DELAY"] == "300", "auto-lock 300 s")
    await send(f"AUTOLOCK {n_front},OFF")
    check(got[f"LOCK{n_front}_AUTOLOCK"] == "0", "auto-lock off")
    await send("LOCK 9")
    check("no lock 9" in got["SYS_LAST_COMMAND"], "unknown lock number reported")

    print("6. LOCKALL")
    before = len([c for c in cloud.calls if c[0] == "Locked"])
    await send("LOCKALL", 2.0)
    locked_now = [c for c in cloud.calls if c[0] == "Locked"][before:]
    check(sorted(d for _, d in locked_now) == ["dev-a", "dev-c"], f"LOCKALL only sent to unlocked locks {locked_now}")
    check(got[f"LOCK{n_front}_STATUS"] == "Locked", "front locked")

    print("7. unlock controls: config.json and Blueprint Disable Unlock")
    b.cfg["allow_remote_unlock"] = False
    calls = len(cloud.calls)
    await send(f"UNLOCK {n_front}")
    check(len(cloud.calls) == calls and "refused" in got["SYS_LAST_COMMAND"], "unlock refused, nothing sent to Kwikset")
    check("disabled" in got[f"DoorLockError_{n_front}"], f"refusal shown in Error State ({got[f'DoorLockError_{n_front}']})")
    cloud.devices["dev-a"]["doorstatus"] = "Unlocked"
    cloud.event("dev-a", "Unlocked", "Keypad", "James")
    cloud.push_cb("onManageDevice", {"onManageDevice": {"deviceid": "dev-a", "devicestatus": "Unlocked"}})
    await asyncio.sleep(0.3)
    check(got[f"DoorLockError_{n_front}"] == "" and got[f"DoorLockStatus_{n_front}"] == "Unlocked",
          "next real report from the lock clears the old error")
    b.cfg["allow_remote_unlock"] = True
    check(got["SYS_UNLOCK_ALLOWED"] == "1", "full control by default")
    await send("DISABLEUNLOCK YES")
    calls = len(cloud.calls)
    await send(f"UNLOCK {n_front}")
    check(len(cloud.calls) == calls and "Blueprint" in got[f"DoorLockError_{n_front}"] and got["SYS_UNLOCK_ALLOWED"] == "0",
          "Blueprint Disable Unlock = YES refuses unlock")
    check(json.loads((home / "settings.json").read_text()).get("disable_unlock") is True, "Disable Unlock saved for restarts")
    await send(f"LOCK {n_front}", 1.6)
    check(got[f"LOCK{n_front}_STATUS"] == "Locked", "still locks while unlock is disabled")
    b.cfg["allow_remote_unlock"] = False
    await send("DISABLEUNLOCK ")
    calls = len(cloud.calls)
    await send(f"UNLOCK {n_front}")
    check(len(cloud.calls) == calls and got["SYS_UNLOCK_ALLOWED"] == "0", "Blueprint can't override config.json false")
    b.cfg["allow_remote_unlock"] = True
    await send("DISABLEUNLOCK NO")
    check(got["SYS_UNLOCK_ALLOWED"] == "1", "blank/NO = full control again")

    print("8. HOME filter from Blueprint")
    await send("HOME Main House", 1.5)
    check(got["SYS_LOCK_COUNT"] == "2" and got[f"LOCK{n_back}_PRESENT"] == "0", "home filter -> 2 locks, Back Door slot emptied")
    check(json.loads((home / "settings.json").read_text())["home"] == "Main House", "home saved")
    await send("HOME ", 1.5)
    check(got["SYS_LOCK_COUNT"] == "3", "blank home -> back to config (all homes)")
    check(got.get(f"LOCK{n_back}_NAME") == "Back Door", "Back Door keeps its number after re-adding")

    print("9. every profile action, as Savant sends it")
    cmds, parsers = profile_commands()
    errs_before = len([l for l in raw_lines if l.startswith("SYS_ERROR=unknown")])
    for name, line in cmds:
        await send(line, 0.05)
    await asyncio.sleep(2.5)
    errs = [l for l in raw_lines if l.startswith("SYS_ERROR=unknown")][errs_before:]
    check(not errs, f"{len(cmds)} actions accepted, no unknown commands {errs}")
    check("PONG" in raw_lines, "PING answered")
    unparsed = sorted({l.split("=", 1)[0] for l in raw_lines if "=" in l} - {p.rstrip("=") for p in parsers})
    check(not unparsed, f"every state the bridge sent has a profile parser {unparsed[:5]}")

    print("10. tokens expire -> reauth_required, then new login recovers")
    hb.write_private_json(home / "tokens.json", {"email": "x", "id_token": "i", "access_token": "a", "refresh_token": "expired"})
    await send("RELOAD", 2.5)
    check(got["SYS_AUTH"] == "reauth_required" and got["SYS_ONLINE"] == "0", "reauth_required shown")
    hb.write_private_json(home / "tokens.json", {"email": "x", "id_token": "i", "access_token": "a", "refresh_token": "r"})
    await asyncio.sleep(6)
    check(got["SYS_AUTH"] == "ok", "tokens.json change picked up without RELOAD")

    print("11. second Savant connection gets a full dump; clean shutdown")
    r2, w2 = await asyncio.open_connection("127.0.0.1", PORT)
    await asyncio.sleep(0.3)
    dump = (await asyncio.wait_for(r2.read(200000), 1)).decode()
    check(dump.count("\n") == len(hb.state_defs(8)) - 7,  # IsDoorClosed_n x6 (no door sensor) + SYS_REJECTED_CLIENT (nothing refused)
           f"dump has {dump.count(chr(10))} lines")
    w2.close()
    b.request_stop("test")
    code = await asyncio.wait_for(task, 10)
    check(code == 0, "clean exit")
    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}  (Python {sys.version.split()[0]})")
    sys.stdout.flush(); os._exit(1 if FAILS else 0)

unit()
asyncio.get_event_loop().run_until_complete(main())
