# Changelog

## 1.1.6 — first public release

HaloKey (developed as HaloLink): Kwikset Halo Wi-Fi smart locks in Savant, through a small bridge service on the Savant host.

- Native Savant **Door Lock service**, laid out like Savant's August profile: lock and unlock from the Savant app, with one component for up to 8 locks. Data table: Status State `DoorLockStatus_n`, Error State `DoorLockError_n`.
- **Live status** by Kwikset's real-time push, with a 5-minute safety poll. Lock state is only ever set from what Kwikset reports: `LOCKn_PENDING` shows *Locking*/*Unlocking* until the lock confirms.
- **Who did it:** `LOCKn_LAST_EVENT`, `LOCKn_LAST_SOURCE` (Keypad / Manual / App / Savant), `LOCKn_LAST_USER` (user code or phone name) and `LOCKn_LAST_EVENT_TIME`, from the lock's Kwikset history.
- **Offline and jammed:** an offline lock shows *Unknown* (last report kept in `LOCKn_LAST_KNOWN`); `DoorLockError_n` shows *Lock offline*, *Lock jammed* or why a command failed, in red in the Savant app.
- Battery level and low-battery alerts, door position sensor, status LED, lock sounds, secure screen and auto-lock settings.
- **Full control by default**; the Blueprint **Disable Unlock** variable makes Savant lock-only, and `allow_remote_unlock: false` in `config.json` is a host-level block. `SYS_UNLOCK_ALLOWED` reports the result.
- **Security:** the Kwikset password is never stored (only session tokens, readable by the service user); `allowed_clients` lets only the host connect by default.
- macOS installer and uninstaller for the Savant Mac mini Pro host (launchd service, `halolink` command, upgrades keep settings and sign-in), Linux systemd unit.
- Python 3.9-compatible build of aiokwikset 0.7.3 bundled for the Mac host.
- Released as **HaloKey**. The Blueprint profile (Kwikset HaloLink), bridge service, `halolink` command and `~/HaloLink` folder keep the original name.
- Search aliases in the profile (HaloKey, Kwikset, Halo, Halo Touch, Halo Select, Smart Lock, Deadbolt, ...), so it's easy to find in Blueprint's component search. It adds as **Door Lock Controller**.
- The Blueprint profile ships in Blueprint's import layout, `profile/blueprint-import/`, with this manual on the component's **Documentation** tab and a component icon.

Tested with the original Kwikset Halo Touchscreen Wi-Fi Smart Lock (HALO-01) on a Savant Mac mini Pro host and the Savant Pro app. Pre-release builds (1.1.0–1.1.5) were tested privately.
