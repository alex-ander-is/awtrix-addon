# AWTRIX App

This App lets Home Assistant show a temporary custom picture on AWTRIX, then clears only that custom app when the event ends.

It does not change AWTRIX brightness, palette, settings, or force the Clock app.

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

Also add these URLs to `secrets.yaml`:

```yaml
awtrix_addon_events_url: http://127.0.0.1:8099/api/events
awtrix_addon_current_event_url: http://127.0.0.1:8099/api/events/current
```

## Home Assistant REST Commands

Add this to Home Assistant configuration:

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
        "clock_prefixes": {{ clock_prefixes | default([]) | to_json }},
        "duration_seconds": {{ duration_seconds | int(20) }},
        "asset": "{{ asset | default('') }}",
        "asset_base64": "{{ asset_base64 | default('') }}",
        "melody": "{{ melody | default('') }}",
        "rtttl": "{{ rtttl | default('') }}"
      }

  awtrix_cancel_current:
    url: !secret awtrix_addon_current_event_url
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

## Assets

Put PNG or GIF files in:

```text
/share/awtrix-addon/assets
```

The visible asset area is exactly `10x8` pixels on the left side of the `32x8` AWTRIX canvas. Use `10x8` images when you want pixel-perfect output.

Larger or smaller PNG/GIF files are accepted, but every frame is resized to `10x8` with nearest-neighbor scaling. The app does not crop, pad, or preserve aspect ratio, so a larger picture may look squeezed or stretched.

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
