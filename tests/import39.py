import sys, types, inspect
# stub the third-party deps (not downloadable here) so aiokwikset's own code is what's tested
def mod(name, **kw):
    m = types.ModuleType(name); m.__dict__.update(kw); sys.modules[name] = m; return m
class Anything:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return Anything
aiohttp = mod("aiohttp", ClientSession=Anything, ClientTimeout=Anything, WSMsgType=Anything, ClientWebSocketResponse=Anything)
mod("aiohttp.client_exceptions", ClientConnectorError=Exception, ClientPayloadError=Exception, ClientResponseError=Exception, ServerConnectionError=Exception)
mod("boto3", Session=Anything, session=types.SimpleNamespace(Session=Anything))
bc = mod("botocore", UNSIGNED=object(), config=types.SimpleNamespace(Config=Anything))
mod("botocore.config", Config=Anything)
mod("botocore.exceptions", BotoCoreError=Exception, ClientError=Exception, ConnectTimeoutError=Exception, ReadTimeoutError=Exception, EndpointConnectionError=Exception)
mod("pycognito", Cognito=Anything)
mod("pycognito.aws_srp", AWSSRP=Anything)
mod("pycognito.exceptions", SMSMFAChallengeException=Exception, SoftwareTokenMFAChallengeException=Exception)
import attr  # real attrs is needed for the API class
import aiokwikset
from aiokwikset import API, MFAChallengeRequired
import aiokwikset.errors as E
print("aiokwikset", "imported on Python", sys.version.split()[0])
for name in ["async_authenticate_with_tokens", "async_login", "async_request_custom_challenge_code", "async_respond_to_mfa_challenge", "async_close"]:
    print(" ", name, inspect.signature(getattr(API, name)))
from aiokwikset.device import Device
for name in ["get_devices", "get_device_info", "lock_device", "unlock_device", "set_led_enabled", "set_audio_enabled", "set_secure_screen_enabled", "set_autolock_enabled", "get_device_history"]:
    print(" ", name, inspect.signature(getattr(Device, name)))
a = API(websession=None, token_update_callback=None, executor=None)
print("  API(websession, token_update_callback, executor) ok:", a)
for n in ("TokenExpiredError", "Unauthenticated", "ConnectionError", "MFAChallengeRequired"):
    print("  errors.%s base %s" % (n, getattr(E, n).__mro__[1].__name__))
