# AWTRIX App

Home Assistant App service that publishes short AWTRIX custom-app overlays without changing AWTRIX settings, brightness, palettes, or the forced Clock app.

## Install in Home Assistant

1. Open Home Assistant.
2. Go to `Settings` -> `Apps` -> `Install app` (`App store`).
3. Open the three-dot menu in the top right and choose `Repositories`.
4. Paste this URL:

```text
https://github.com/alex-ander-is/awtrix-addon
```

5. Click `Add`, close the dialog, and wait until `AWTRIX App` appears in the store.
6. Open `AWTRIX App`, click `Install`, then open the `Configuration` tab.
7. Set at least `clock_prefixes`, `default_clock_prefixes`, and `auth_token`.
8. Click `Save`, then `Start`.

## App options

```yaml
app_name: awtrix_addon
clock_prefixes:
  - awtrix
default_clock_prefixes:
  - awtrix
assets_dir: /share/awtrix-addon/assets
auth_token: "optional-fixed-token"
```

- `app_name`: one MQTT segment, `A-Z`, `a-z`, `0-9`, `_`, `-`, max 40 chars.
- `clock_prefixes`: required non-empty allowlist of AWTRIX MQTT topic prefixes.
- `default_clock_prefixes`: optional subset of `clock_prefixes`; omitted means all clocks.
- `assets_dir`: directory for PNG/GIF assets normalized to a 10x8 left-side area.
- `auth_token`: optional fixed bearer token. If omitted, the App generates one in `/data/auth.json`.

Unknown option keys or unsafe configured values fail startup before MQTT is started.

## HTTP API

The service binds `0.0.0.0:8099` in the container and exposes host port `8099`.

- `GET /health` is public.
- Every `/api/*` route requires `Authorization: Bearer <token>`.
- No ingress is used. Home Assistant `rest_command` needs a stable service URL and bearer contract, not a browser ingress session.

From Home Assistant Core, use the App's internal DNS name:

`http://35664e22-awtrix-addon:8099/api/events`

`127.0.0.1:8099` is the Core container itself, not this App. The URL is not
sensitive; store only the bearer authorization value in `secrets.yaml`:

```yaml
awtrix_addon_authorization: Bearer optional-fixed-token
```

## Token recovery

If `auth_token` is set in App options, that option always wins and `/api/auth/regenerate` returns `409 managed_by_options`.

If `auth_token` is omitted, the generated token is stored only in `/data/auth.json`. To recover, read that file from the App data directory. To rotate it:

```bash
curl -X POST \
  -H "Authorization: Bearer <current-token>" \
  http://35664e22-awtrix-addon:8099/api/auth/regenerate
```

## Home Assistant examples

```yaml
rest_command:
  awtrix_event:
    url: http://35664e22-awtrix-addon:8099/api/events
    method: POST
    headers:
      Authorization: !secret awtrix_addon_authorization
      Content-Type: "application/json"
    payload: >
      {
        "event_id": "{{ event_id }}",
        "clock_prefixes": {{ clock_prefixes | to_json }},
        "duration_seconds": {{ duration_seconds | int(30) }},
        "asset": "{{ asset | default('') }}",
        "asset_base64": "{{ asset_base64 | default('') }}",
        "melody": "{{ melody | default('') }}",
        "rtttl": "{{ rtttl | default('') }}"
      }

  awtrix_cancel_current:
    url: http://35664e22-awtrix-addon:8099/api/events/current
    method: DELETE
    headers:
      Authorization: !secret awtrix_addon_authorization
      Content-Type: "application/json"
    payload: >
      {"clock_prefixes": {{ clock_prefixes | default([]) | to_json }}}

  awtrix_delete_event:
    url: http://35664e22-awtrix-addon:8099/api/events/{{ event_id }}
    method: DELETE
    headers:
      Authorization: !secret awtrix_addon_authorization
      Content-Type: "application/json"
```

```yaml
action: rest_command.awtrix_event
data:
  event_id: doorbell
  clock_prefixes:
    - awtrix
  duration_seconds: 20
  asset: doorbell.gif
  melody: "Default/Arkanoid"
```

```yaml
action: rest_command.awtrix_cancel_current
data:
  clock_prefixes:
    - awtrix
```

```yaml
action: rest_command.awtrix_delete_event
data:
  event_id: doorbell
```

## API errors

All API errors are JSON:

```json
{"error":"auth_required","message":"Bearer token is required","details":{}}
```

```json
{"error":"auth_failed","message":"Bearer token is invalid","details":{}}
```

```json
{"error":"invalid_clock_prefixes","message":"clock_prefixes must be unique, valid, and allowlisted","details":{"invalid":["missing"],"allowed":["awtrix"]}}
```

```json
{"error":"managed_by_options","message":"Token is managed by App options","details":{}}
```

```json
{"error":"duplicate_event_id","message":"event_id already exists","details":{}}
```

```json
{"error":"startup_config_failed","message":"Startup configuration failed","details":{"config_error":{"code":"invalid_app_name","details":{}}}}
```

Invalid request targets publish zero MQTT payloads, including zero restore clears.

## MQTT behavior

The App only publishes to:

- `<prefix>/custom/<app_name>`
- `<prefix>/rtttl`

Restore is only an empty payload to `<prefix>/custom/<app_name>`. The App never publishes AWTRIX `settings`, brightness, palette, or forced `Clock` commands.

## Melodies

Use `melody` for a managed melody name, for example `Default/Arkanoid` or `Personal/My_chime`. Names are case-sensitive and always include the namespace. The bundled Default library is updated with the App; manually add Personal UTF-8 `.rtttl` files under `/data/library/melodies/Personal`, for example `/data/library/melodies/Personal/My_chime.rtttl`. Personal files survive App restarts and updates.

Use `rtttl` for a one-off RTTTL expression such as `chime:d=4,o=5,b=120:c,e,g,c6,g,e,c,p,c,p`. Specify either `melody` or `rtttl`, not both. A missing library name returns `404 melody_not_found` before any event or MQTT publication. The resolved RTTTL text is published once to `<prefix>/rtttl` immediately after event creation; it is not replayed while the event is rendered, canceled, expired, or shut down.

Runtime events are in memory only. Refresh, restart, or version update does not resurrect old workflow state. Generated auth and Personal melody files persist under `/data`.

### Melody error contract

`melody` is case-sensitive: only `Default/<name>` and `Personal/<name>` are valid references. An empty `melody` or `rtttl` means no melody. A reference cannot contain an extra `/`, a path traversal, or a name outside letters, digits, `_`, and `-`. Personal files must be non-empty UTF-8 RTTTL expressions.

RTTTL defaults must include exactly `d`, `o`, and `b`: duration is `1`, `2`, `4`, `8`, `16`, or `32`; octave is `4` through `7`; tempo is `25` through `900`. Notes use `a`–`g` or `p`, with `#` only on `a`, `c`, `d`, `f`, or `g`.

| Request problem | Status and JSON error |
| --- | --- |
| non-string `melody` | `400 {"error":"invalid_melody","message":"melody must be a string","details":{}}` |
| invalid, unreadable, empty, or malformed `melody` file | `400 {"error":"invalid_melody","message":"melody must be a valid library reference","details":{}}` |
| `melody: "Default/Missing"` | `404 {"error":"melody_not_found","message":"Melody was not found","details":{"melody":"Default/Missing"}}` |
| non-string `rtttl` | `400 {"error":"invalid_rtttl","message":"rtttl must be a string","details":{}}` |
| malformed `rtttl` | `400 {"error":"invalid_rtttl","message":"rtttl must be a valid RTTTL expression","details":{}}` |
| both non-empty `melody` and `rtttl` | `400 {"error":"invalid_melody","message":"melody and rtttl are mutually exclusive","details":{}}` |

Every melody/RTTTL error creates no event and publishes no MQTT payload, so the same `event_id` can be retried safely with a corrected request.

## Assets

The left asset area is exactly `10x8` pixels inside the full `32x8` AWTRIX canvas. PNG/GIF files loaded through `asset` and inline PNG/GIF payloads sent through `asset_base64` are both resized to `10x8` with nearest-neighbor scaling. The App does not crop, pad, or preserve aspect ratio.

Use either:

- `asset`: file name under `assets_dir`.
- `asset_base64`: plain base64 PNG/GIF data, or a `data:image/png;base64,...` / `data:image/gif;base64,...` URL.

Do not send both `asset` and `asset_base64` in the same event.

## Local safety

Tests use a fake publisher and do not touch live Home Assistant or live MQTT. Docker context excludes `.codex-audit` and `.git`.

Run the local smoke suite with:

```bash
python3 awtrix-addon/scripts/smoke.py
```

If dependencies are missing, create a venv and install the project first:

```bash
cd awtrix-addon
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[test]'
```

## Manual bedroom-clock live test

The live canvas test is manual and opt-in only. It is hard-limited to `bedroom-clock`, publishes the custom app only to `bedroom-clock/custom/awtrix_addon_live_test`, and uses `bedroom-clock/switch` only with `{"name":"awtrix_addon_live_test","fast":true}` to show that test app. Cleanup clears only that test custom app with an empty payload. It never publishes AWTRIX settings, palette, brightness, moodlight, or forced Clock commands.

Run it only when you intend to touch the real bedroom clock:

```bash
AWTRIX_LIVE_BEDROOM_CLOCK=1 python3 awtrix-addon/scripts/live_bedroom_clock.py
```

The runner asks for MQTT/screen credentials or reads them from environment variables, keeps them only in process memory, and does not print or store them. It opens `http://bedroom-clock.ander.is/screen` through `~/Applications/Playwright`, verifies all 80 pixels of the 10x8 custom pattern through the canvas, clears the custom app, and verifies that the native-clock palette is compatible with the pre-test baseline.
