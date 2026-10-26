import asyncio, sys, tempfile, socket, os
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "bridge"))
import halolink_bridge as hb
FAILS = []
def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m); c or FAILS.append(m)
# a non-loopback address of this machine (the "host IP" a Savant profile would use)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("10.255.255.255", 1)); LAN = s.getsockname()[0]; s.close()
print("this machine's LAN IP:", LAN)

async def try_connect(port, src_ip, dst_ip):
    """Connect from a chosen local source address; True if the bridge talks to us."""
    sock = socket.socket(); sock.bind((src_ip, 0)); sock.setblocking(False)
    await asyncio.get_event_loop().sock_connect(sock, (dst_ip, port))
    r, w = await asyncio.open_connection(sock=sock)
    try:
        data = await asyncio.wait_for(r.read(4096), 1.0)
    except asyncio.TimeoutError:
        data = b""
    w.close()
    return b"SYS_VERSION=" in data

async def run_case(allowed, port):
    cfg = hb.load_config("/x"); cfg.update(tcp_port=port, allowed_clients=allowed)
    b = hb.Bridge(cfg, tempfile.mkdtemp(), api_factory=lambda br: None)
    t = asyncio.ensure_future(b.run()); await asyncio.sleep(0.4)
    res = {"loopback": await try_connect(port, "127.0.0.1", "127.0.0.1"),
           "host LAN IP": await try_connect(port, LAN, LAN)}
    rej = b.values.get("SYS_REJECTED_CLIENT", "")
    b.request_stop("test"); await asyncio.wait_for(t, 5)
    return res, rej

async def main():
    print("1. default auto (bridge on the Savant host)")
    r, rej = await run_case("auto", 24001)
    check(r["loopback"] and r["host LAN IP"], f"this machine accepted {r}")
    print("2. explicit list of a different IP (bridge on a separate box, Savant host elsewhere)")
    r, rej = await run_case(["192.0.2.50"], 24002)
    check(r["loopback"], "halolink command (loopback) still works")
    check(not r["host LAN IP"], "unlisted IP refused")
    check(rej.startswith(LAN), f"SYS_REJECTED_CLIENT = {rej}")
    print("3. subnet entry covering this machine")
    net = ".".join(LAN.split(".")[:3]) + ".0/24"
    r, _ = await run_case([net], 24003)
    check(r["host LAN IP"], f"{net} accepted")
    print("4. parsing")
    check(hb.parse_allowed("any")[0] is True, "any = unrestricted")
    check(hb.parse_allowed(["auto", "192.168.1.20"])[1:] == (True, [hb.ipaddress.ip_network("192.168.1.20/32")]), "auto + IP")
    check(not hb.is_this_machine("192.0.2.50") and hb.is_this_machine(LAN), "is_this_machine")
    try:
        cfgp = tempfile.mktemp(); open(cfgp, "w").write('{"allowed_clients": ["192.168.1.300"]}'); hb.load_config(cfgp); check(False, "bad IP rejected")
    except SystemExit as e:
        check("allowed_clients" in str(e), f"bad entry stops startup: {e}")
    print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED"); sys.stdout.flush(); os._exit(1 if FAILS else 0)
asyncio.get_event_loop().run_until_complete(main())
