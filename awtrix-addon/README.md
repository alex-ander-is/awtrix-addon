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
awtrix_addon_events_url: http://homeassistant.local:8099/api/events
awtrix_addon_current_event_url: http://homeassistant.local:8099/api/events/current
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
  sound: ""
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
  sound: ""
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
  sound: ""
```

Use either `asset` or `asset_base64`, not both.
