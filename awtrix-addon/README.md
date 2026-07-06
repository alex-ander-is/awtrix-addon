# AWTRIX App

This App lets Home Assistant show a temporary custom picture on AWTRIX immediately, then clears only that custom app when the event ends.

It switches to its own custom app with `<prefix>/switch` and
`{"name":"<app_name>","fast":true}`. It does not change AWTRIX brightness,
palette, settings, or publish a forced `Clock` command. Removing the custom app
at event end returns control to AWTRIX's normal app loop.

## Basic Configuration

For the bedroom clock:

```yaml
app_name: awtrix_addon
clock_prefixes:
  - bedroom-clock
default_clock_prefixes: []
assets_dir: /share/awtrix-addon/assets
auth_token: ""
```

Leave `auth_token` empty to let the App generate one. After start, copy this line from the App log into Home Assistant `secrets.yaml`:

```yaml
awtrix_addon_authorization: Bearer <generated-token>
```

The App is reachable from Home Assistant Core at
`http://35664e22-awtrix-addon:8099`. The URL is not sensitive; keep only the
authorization value in `secrets.yaml`.

`clock_prefixes` is the MQTT topic prefix allowlist for clocks this App may
publish to. Add every AWTRIX clock you want to target, for example
`bedroom-clock` and `kids-room-clock`. A request for any other prefix returns
`400 invalid_clock_prefixes` before MQTT publish, so typos and unintended
topics are rejected. `default_clock_prefixes` is the subset used when a REST request
omits `clock_prefixes`; leave it empty to target all allowed clocks by default.

## Home Assistant REST Commands

Add this to Home Assistant configuration:

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
        "clock_prefixes": {{ clock_prefixes | default([]) | to_json }},
        "duration_seconds": {{ duration_seconds | int(20) }},
        "weekdays": {{ weekdays | default(true) | to_json }},
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
```

Reload Home Assistant YAML or restart Home Assistant after adding the commands.

## Simple Action

Use this action in an automation to show the default clock-style event for 20 seconds:

```yaml
action: rest_command.awtrix_event
data:
  event_id: doorbell
  clock_prefixes:
    - bedroom-clock
  duration_seconds: 20
  weekdays: true
  asset: ""
  melody: "Default/Arkanoid"
```

Cancel the current App display:

```yaml
action: rest_command.awtrix_cancel_current
data:
  clock_prefixes:
    - bedroom-clock
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
        - bedroom-clock
      duration_seconds: 30
      weekdays: true
      asset_base64: ""
      rtttl: ""

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

An `event_id` is a replace key, not a uniqueness constraint. Sending the same
ID again replaces its active event immediately; clocks omitted from the new
request are cleared.

`weekdays` is optional and defaults to `true`. Set it to `false` to hide the
seven-pixel weekday bar while keeping the clock. Any non-boolean value is
rejected before an event is created or MQTT is published.

## Display Palette

The App subscribes read-only to each configured `<prefix>/settings` topic so it
can learn AWTRIX display colors. It never writes settings, palette, brightness,
moodlight, indicators, or a forced `Clock` command.

Supported color fields are `TCOL`, `TIME_COL`, `WDCA`, `WDCI`, `CHCOL`,
`CBCOL`, and `CTCOL` as `[r,g,b]` integers from `0` to `255`, `#RRGGBB`, or
`RRGGBB`. `TIME_COL: 0` falls back to `TCOL` or white.

The renderer uses time color and weekday colors now. Calendar header, body, and
text colors are persisted for future calendar rendering, but are not rendered
by this version.

Fallback colors are time `#FFFFFF`, active weekday `#FFFFFF`, inactive weekday
`#666666`, calendar header `#FF0000`, calendar body `#FFFFFF`, and calendar
text `#000000`.

Palette snapshots are stored at `/data/awtrix-addon-palettes.json`. Refresh does
not clear them, and restart or version update preserves the file. To reset
colors, delete `/data/awtrix-addon-palettes.json` and restart the App; fallback
colors apply until new AWTRIX settings arrive.

Runtime events are in memory only. Refresh, restart, or version update does not
resurrect old workflow state.

## Assets

Put PNG or GIF files in:

```text
/share/awtrix-addon/assets
```

PNG/GIF files are rendered 1:1 from the top-left origin `(0,0)` on the full `32x8` AWTRIX canvas. Pixels outside the `32x8` canvas are clipped and ignored; x=0..31 and y=0..7 are visible. There is no resize, stretch, padding, or 10x8 normalization.

The clock and weekday bar are drawn first, then the asset is composited over them. Transparent pixels preserve the clock pixels underneath, opaque pixels cover them, opaque black hides them, and 50% alpha blends with the clock pixels underneath. RGB assets are treated as fully opaque.

Then pass the file name in `asset`, for example:

```yaml
action: rest_command.awtrix_event
data:
  event_id: washing_done
  clock_prefixes:
    - bedroom-clock
  duration_seconds: 30
  asset: washing.gif
  rtttl: ""
```

You can also send a PNG or GIF directly in the action without uploading a file. Put a plain base64 string or a `data:image/png;base64,...` / `data:image/gif;base64,...` URL in `asset_base64`:

```yaml
action: rest_command.awtrix_event
data:
  event_id: inline_icon
  clock_prefixes:
    - bedroom-clock
  duration_seconds: 20
  asset: ""
  asset_base64: "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAICAIAAACgHXkX..."
  rtttl: ""
```

Use either `asset` or `asset_base64`, not both.

## Melodies

Use `melody` for a managed reference such as `Default/Arkanoid` or `Personal/My_chime`. Names are case-sensitive and always include the namespace. Default melodies ship with the App and update with it. Add Personal UTF-8 `.rtttl` files manually under `/data/library/melodies/Personal`, for example `/data/library/melodies/Personal/My_chime.rtttl`; they survive restarts and App updates.

Use `rtttl` for a direct one-off expression, for example `chime:d=4,o=5,b=120:c,e,g,c6,g,e,c,p,c,p`. Specify either `melody` or `rtttl`, not both. A missing library name returns `404 melody_not_found` without creating an event or publishing MQTT. The resolved RTTTL text is published once to `<prefix>/rtttl` immediately after creating the event. It is not replayed when the event is rendered, canceled, expires, or shuts down.

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

## Local development

From this App source directory, run the local smoke suite with:

```bash
python3 scripts/smoke.py
```
