# AWTRIX App

Home Assistant App service that publishes and immediately shows short AWTRIX custom-app overlays without changing AWTRIX brightness, palettes, or the forced Clock app. It writes AWTRIX settings only to enable sound and set volume when an event requests a melody or RTTTL.

This repository follows the Home Assistant [app repository structure](https://developers.home-assistant.io/docs/apps/repository/): root `repository.yaml`, with the app itself in its own `awtrix-addon/` folder.

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
  This is the MQTT topic prefix allowlist. These are the clock topic roots the
  App may publish to, for example `bedroom-clock` for
  `bedroom-clock/custom/awtrix_addon` and `bedroom-clock/switch`. Requests for
  any prefix outside this list return `400 invalid_clock_prefixes` before MQTT publish,
  so a typo cannot write to an unintended topic.
- `default_clock_prefixes`: optional subset of `clock_prefixes` used when a REST
  request omits `clock_prefixes`; omitted or empty means all allowed clocks.
- `assets_dir`: directory for PNG/GIF assets rendered 1:1 from `(0,0)` on the `32x8` canvas.
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
        "weekdays": {{ weekdays | default(true) | to_json }},
        "asset": "{{ asset | default('') }}",
        "asset_base64": "{{ asset_base64 | default('') }}",
        "melody": "{{ melody | default('') }}",
        "rtttl": "{{ rtttl | default('') }}",
        "sound_volume": {{ sound_volume | default(50) | int }}
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
  weekdays: true
  asset: doorbell.gif
  melody: "Default/Arkanoid"
  sound_volume: 50
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

To make REST failures visible in the automation trace, capture the response and
raise an error after emitting a trace event with the API response details:

```yaml
sequence:
  - action: rest_command.awtrix_event
    response_variable: awtrix_response
    data:
      event_id: doorbell
      clock_prefixes:
        - awtrix
      duration_seconds: 30
      weekdays: true
      asset_base64: ""
      melody: Default/Arkanoid
      sound_volume: 50

  - if:
      - condition: template
        value_template: "{{ awtrix_response.status != 201 }}"
    then:
      - event: awtrix_request_failed
        event_data:
          status: "{{ awtrix_response.status }}"
          content: "{{ awtrix_response.content | to_json }}"

      - stop: "AWTRIX request failed"
        error: true
```

For sound, send only `melody` or `rtttl` to this App. Do not add a separate
Home Assistant `mqtt.publish` action for `SOUND` or `VOL`; the App publishes
`{"SOUND":true,"VOL":<sound_volume>}` to each target `<prefix>/settings` topic
before `<prefix>/rtttl`. Omit `sound_volume` to use the default `50`.

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
{"error":"bad_request","message":"weekdays must be a boolean","details":{}}
```

```json
{"error":"managed_by_options","message":"Token is managed by App options","details":{}}
```

```json
{"error":"startup_config_failed","message":"Startup configuration failed","details":{"config_error":{"code":"invalid_app_name","details":{}}}}
```

Invalid request targets publish zero MQTT payloads, including zero restore clears.

An `event_id` is a replace key, not a uniqueness constraint. A new request with
the same ID replaces the active event immediately; clocks omitted from the new
request are cleared.

`weekdays` is optional and defaults to `true`. Set `"weekdays": false` to hide
the seven-pixel weekday bar while keeping the clock. Any non-boolean value is
rejected before an event is created or MQTT is published.

## MQTT behavior

The App only publishes to:

- `<prefix>/custom/<app_name>`
- `<prefix>/switch` with `{"name":"<app_name>","fast":true}` once for each new event
- `<prefix>/settings` with `{"SOUND":true,"VOL":<sound_volume>}` immediately before RTTTL, only when the event has `melody` or `rtttl`
- `<prefix>/rtttl`

The switch makes the new custom page visible immediately. Restore is only an
empty payload to `<prefix>/custom/<app_name>`, which removes that page and
returns control to AWTRIX's normal app loop. Other than the explicit sound
settings payload for melody/RTTTL events, the App never publishes AWTRIX
settings, brightness, palette, moodlight, indicators, or a forced `Clock` command.

The App subscribes read-only to each configured `<prefix>/settings` topic to
learn AWTRIX display colors. This listener is read-only; it does not mutate
palette, brightness, moodlight, indicators, or the forced Clock app.

## Display palette

Settings messages may provide `TCOL`, `TIME_COL`, `WDCA`, `WDCI`, `CHCOL`,
`CBCOL`, and `CTCOL` as `[r,g,b]` integers from `0` to `255`, `#RRGGBB`, or
`RRGGBB`. `TIME_COL: 0` is a sentinel that falls back to `TCOL` or white.

The event renderer uses only time color and weekday colors now. Calendar header,
body, and text colors are parsed and persisted for future calendar rendering;
they are not rendered by this version.

Fallback colors are:

- time: `#FFFFFF`
- active weekday: `#FFFFFF`
- inactive weekday: `#666666`
- calendar header: `#FF0000`
- calendar body: `#FFFFFF`
- calendar text: `#000000`

## Melodies

Use `melody` for a managed melody name, for example `Default/Arkanoid` or `Personal/My_chime`. Names are case-sensitive and always include the namespace. The bundled Default library is updated with the App; manually add Personal UTF-8 `.rtttl` files under `/data/library/melodies/Personal`, for example `/data/library/melodies/Personal/My_chime.rtttl`. Personal files survive App restarts and updates.

Use `rtttl` for a one-off RTTTL expression such as `chime:d=4,o=5,b=120:c,e,g,c6,g,e,c,p,c,p`. Specify either `melody` or `rtttl`, not both. A missing library name returns `404 melody_not_found` before any event or MQTT publication. When sound is requested, the App first publishes `{"SOUND":true,"VOL":<sound_volume>}` to `<prefix>/settings`, then publishes the resolved RTTTL text once to `<prefix>/rtttl`. `sound_volume` is optional, defaults to `50`, and must be an integer from `0` through `100`. The sound settings and RTTTL are not replayed while the event is rendered, canceled, expired, or shut down.

Runtime events are in memory only. Refresh, restart, or version update does not
resurrect old workflow state. Generated auth, Personal melody files, and palette
snapshots persist under `/data`.

Palette snapshots are stored at `/data/awtrix-addon-palettes.json`. Refresh does
not clear them, and restart or version update preserves the file. To reset
colors, delete `/data/awtrix-addon-palettes.json` and restart the App; fallback
colors apply until new AWTRIX settings arrive.

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
| invalid `sound_volume` | `400 {"error":"bad_request","message":"sound_volume must be an integer from 0 through 100","details":{}}` |

Every melody/RTTTL error creates no event and publishes no MQTT payload, so the same `event_id` can be retried safely with a corrected request.

## Assets

PNG/GIF files loaded through `asset` and inline PNG/GIF payloads sent through `asset_base64` are rendered 1:1 from the top-left origin `(0,0)` on the full `32x8` AWTRIX canvas. Pixels outside the `32x8` canvas are clipped and ignored; x=0..31 and y=0..7 are visible. There is no resize, stretch, padding, or 10x8 normalization.

The clock and weekday bar are drawn first, then the asset is composited over them. Transparent pixels preserve the clock pixels underneath, opaque pixels cover them, opaque black hides them, and 50% alpha blends with the clock pixels underneath. RGB assets are treated as fully opaque.

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
