import sys, asyncio, json, tempfile, builtins, getpass, os
from pathlib import Path
exec(open(str(__import__("pathlib").Path(__file__).resolve().parent / "import39.py")).read().split("import attr")[0])   # same dependency stubs
from aiokwikset import MFAChallengeRequired
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "bridge"))
import halolink_bridge as hb
calls = []
class LoginAPI:
    def __init__(self, *a, **k): self.id_token = self.access_token = self.refresh_token = None
    async def async_login(self, email, pw):
        calls.append(("login", email, pw))
        raise MFAChallengeRequired(mfa_type="CUSTOM_CHALLENGE", mfa_tokens={"Session": "s1"})
    async def async_request_custom_challenge_code(self, medium, tokens):
        calls.append(("request", medium, tokens)); return {"Session": "s2", "ChallengeName": "CUSTOM_CHALLENGE"}
    async def async_respond_to_mfa_challenge(self, code, typ, tokens):
        calls.append(("respond", code, typ, tokens)); self.id_token, self.access_token, self.refresh_token = "I", "A", "R"
    class user:
        @staticmethod
        async def get_homes(): return [{"homeid": "h", "homename": "Main"}]
    class device:
        @staticmethod
        async def get_devices(h): return [{"devicename": "Front Door"}, {"devicename": "Garage"}]
    async def async_close(self): pass
hb.import_kwikset = lambda: LoginAPI
answers = iter(["p", "123456"])
builtins.input = lambda prompt="": (print(prompt, end=""), next(answers))[1] if True else None
getpass.getpass = lambda prompt="": "secret"
home = Path(tempfile.mkdtemp())
cfg = hb.load_config("/x"); cfg["tcp_port"] = 23999
asyncio.get_event_loop().run_until_complete(hb.cli_login(cfg, home, "james@example.com"))
t = json.loads((home / "tokens.json").read_text())
print("\ncalls:", calls)
assert calls[1] == ("request", "phone", {"Session": "s1"}) and calls[2] == ("respond", "123456", "CUSTOM_CHALLENGE", {"Session": "s2", "ChallengeName": "CUSTOM_CHALLENGE"})
assert t["refresh_token"] == "R" and t["email"] == "james@example.com" and "secret" not in json.dumps(t)
assert oct(os.stat(home / "tokens.json").st_mode)[-3:] == "600"
print("LOGIN FLOW OK: code requested by phone, answered with session from the request, password not stored, mode 600")
