# HaloKey states (v1.1.6)

Every state the bridge sends. n = lock number (1 to max_locks) from `halolink list`.

## System

| State | Type | Meaning |
|---|---|---|
| `SYS_ONLINE` | integer | 1 = bridge is signed in and reaching the Kwikset cloud |
| `SYS_AUTH` | string | ok, login_required or reauth_required |
| `SYS_LIVE` | integer | 1 = Kwikset real-time push connected (0 = polling only) |
| `SYS_ACCOUNT` | string | Kwikset account email the bridge is signed in with |
| `SYS_LOCK_COUNT` | integer | Locks found on the account (after the home filter) |
| `SYS_ALL_LOCKED` | integer | 1 = every lock reports Locked |
| `SYS_UNLOCKED_COUNT` | integer | Locks not reporting Locked |
| `SYS_UNLOCKED_NAMES` | string | Names of locks not reporting Locked, comma separated |
| `SYS_LOW_BATTERY_COUNT` | integer | Locks at or below battery_low_percent |
| `SYS_LOW_BATTERY_NAMES` | string | Names of low-battery locks |
| `SYS_DOOR_OPEN_COUNT` | integer | Doors reporting Open (locks with a door sensor) |
| `SYS_OFFLINE_COUNT` | integer | Locks the cloud reports offline |
| `SYS_LAST_UPDATE` | string | Time of the last successful update from Kwikset |
| `SYS_LAST_COMMAND` | string | Last command from Savant and its result |
| `SYS_ERROR` | string | Last bridge error, blank when healthy |
| `SYS_VERSION` | string | Bridge version |
| `SYS_REJECTED_CLIENT` | string | Last connection refused by allowed_clients: IP and time |
| `SYS_UNLOCK_ALLOWED` | integer | 1 = Savant may unlock (config.json allow_remote_unlock and the Blueprint Disable Unlock variable) |

## Per lock (LOCKn_...)

| State | Type | Meaning |
|---|---|---|
| `LOCKn_PRESENT` | integer | 1 = a lock is assigned to this number |
| `LOCKn_NAME` | string | Lock name from the Kwikset app |
| `LOCKn_STATUS` | string | Locked | Unlocked | Jammed | Unknown (Unknown while the lock is offline) |
| `LOCKn_LOCKED` | integer | 1 = Locked |
| `LOCKn_LAST_KNOWN` | string | Last status the lock reported, kept while it is offline |
| `LOCKn_PENDING` | string | Locking | Unlocking while a command is being confirmed, else blank |
| `LOCKn_ONLINE` | integer | 1 = Kwikset can reach the lock |
| `LOCKn_BATTERY` | integer | Battery percent |
| `LOCKn_BATTERY_LOW` | integer | 1 = battery at or below battery_low_percent |
| `LOCKn_DOOR` | string | Open | Closed | Unknown (door position sensor) |
| `LOCKn_DOOR_OPEN` | integer | 1 = door reports Open |
| `LOCKn_LED` | integer | 1 = status LED enabled |
| `LOCKn_AUDIO` | integer | 1 = lock sounds enabled |
| `LOCKn_SECURE_SCREEN` | integer | 1 = secure screen enabled (Halo Touch) |
| `LOCKn_AUTOLOCK` | integer | 1 = auto-lock enabled |
| `LOCKn_AUTOLOCK_DELAY` | integer | Auto-lock delay, seconds |
| `LOCKn_TAMPER` | string | Tamper state as reported by the lock |
| `LOCKn_LAST_EVENT` | string | Last lock event in the Kwikset history: Locked | Unlocked |
| `LOCKn_LAST_SOURCE` | string | What did it: Keypad | Manual (thumb turn or key) | App | Savant | Unknown |
| `LOCKn_LAST_USER` | string | Who did it, when Kwikset knows: user code name or phone name, else blank |
| `LOCKn_LAST_EVENT_TIME` | string | Time of the last lock event (lock's local time) |
| `LOCKn_LAST_CHANGE` | string | Time the bridge saw the status last change |
| `LOCKn_MODEL` | string | Model number |
| `LOCKn_FIRMWARE` | string | Firmware version |
| `LOCKn_ERROR` | string | Last command error for this lock, blank when fine |

## Savant Door Lock service

| State | Type | Meaning |
|---|---|---|
| `DoorLockStatus_n` | string | Locked / Unlocked / Unknown (Jammed shows as Unknown) |
| `IsDoorLocked_n` | boolean | true when Locked |
| `DoorLockError_n` | string | Door Lock data table Error State: blank, or Lock offline / Lock jammed / last command error |
| `DoorStatus_n` | string | Open / Closed / Unknown (door position sensor) |
| `IsDoorClosed_n` | boolean | true when Closed; only sent when the sensor reports |
| `BatteryLevel_n` | float | Battery percent |
