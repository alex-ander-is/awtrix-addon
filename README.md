# AWTRIX Add-on

Home Assistant add-on service that publishes short AWTRIX custom-app overlays without changing AWTRIX settings, brightness, palettes, or the forced Clock app.

## Install in Home Assistant

1. Open Home Assistant.
2. Go to `Settings` -> `Apps` -> `Install app` (`App store`).
3. Open the three-dot menu in the top right and choose `Repositories`.
4. Paste this URL:

```text
https://github.com/alex-ander-is/awtrix-addon
```

5. Click `Add`, close the dialog, and wait until `AWTRIX Add-on` appears in the store.
6. Open `AWTRIX Add-on`, click `Install`, then open the `Configuration` tab.
7. Set at least `clock_prefixes`, `default_clock_prefixes`, and `auth_token`.
8. Click `Save`, then `Start`.

## Add-on options

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
- `auth_token`: optional fixed bearer token. If omitted, the add-on generates one in `/data/auth.json`.

Unknown option keys or unsafe configured values fail startup before MQTT is started.

## HTTP API

The service binds `0.0.0.0:8099` in the container and exposes host port `8099`.

- `GET /health` is public.
- Every `/api/*` route requires `Authorization: Bearer <token>`.
- No ingress is used. Home Assistant `rest_command` needs a stable service URL and bearer contract, not a browser ingress session.

Use one of these URL forms from Home Assistant:

- `http://homeassistant.local:8099/api/events`
- `http://<home_assistant_host_ip>:8099/api/events`

When using secrets, store the full scalar value in `secrets.yaml`:

```yaml
awtrix_addon_events_url: http://homeassistant.local:8099/api/events
awtrix_addon_current_event_url: http://homeassistant.local:8099/api/events/current
awtrix_addon_event_url: http://homeassistant.local:8099/api/events/{{ event_id }}
awtrix_addon_authorization: Bearer optional-fixed-token
```

## Token recovery

If `auth_token` is set in add-on options, that option always wins and `/api/auth/regenerate` returns `409 managed_by_options`.

If `auth_token` is omitted, the generated token is stored only in `/data/auth.json`. To recover, read that file from the add-on data directory. To rotate it:

```bash
curl -X POST \
  -H "Authorization: Bearer <current-token>" \
  http://homeassistant.local:8099/api/auth/regenerate
```

## Home Assistant examples

```yaml
rest_command:
  awtrix_event:
    url: !secret awtrix_addon_events_url
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
        "sound": "{{ sound | default('') }}"
      }

  awtrix_cancel_current:
    url: !secret awtrix_addon_current_event_url
    method: DELETE
    headers:
      Authorization: !secret awtrix_addon_authorization
      Content-Type: "application/json"
    payload: >
      {"clock_prefixes": {{ clock_prefixes | default([]) | to_json }}}

  awtrix_delete_event:
    url: !secret awtrix_addon_event_url
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
  sound: ding
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
{"error":"managed_by_options","message":"Token is managed by add-on options","details":{}}
```

```json
{"error":"duplicate_event_id","message":"event_id already exists","details":{}}
```

```json
{"error":"startup_config_failed","message":"Startup configuration failed","details":{"config_error":{"code":"invalid_app_name","details":{}}}}
```

Invalid request targets publish zero MQTT payloads, including zero restore clears.

## MQTT behavior

The add-on only publishes to:

- `<prefix>/custom/<app_name>`
- `<prefix>/sound`
- `<prefix>/rtttl`

Restore is only an empty payload to `<prefix>/custom/<app_name>`. The add-on never publishes AWTRIX `settings`, brightness, palette, or forced `Clock` commands.

Runtime events are in memory only. Refresh, restart, or version update does not resurrect old workflow state. Generated auth survives restart because `/data/auth.json` is the only persisted runtime file.

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
