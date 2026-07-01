# AWTRIX Add-on

This add-on lets Home Assistant show a temporary custom picture on AWTRIX, then clears only that custom app when the event ends.

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

Leave `auth_token` empty to let the add-on generate one. After start, copy this line from the add-on log into Home Assistant `secrets.yaml`:

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

Cancel the current add-on display:

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
