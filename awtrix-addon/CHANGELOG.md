# Changelog

Released sections are immutable. Put every new change into a new version section.

## 0.1.25

- Added release script

## 0.1.24

- Add packaged Default PNG/GIF assets for common AWTRIX reminders.
- Allow Default asset filenames to use Unicode letters and digits while keeping path traversal blocked.

## 0.1.23

- New RTTTL Melodies
- Update: README: Repository structure

## 0.1.22

- Enable AWTRIX sound automatically for melody and RTTTL events by publishing `SOUND: true` and `VOL` to the clock settings topic before RTTTL.
- Add optional `sound_volume` REST field, defaulting to `50`, with strict `0..100` validation before any MQTT or EventStore side effects.
- Document that Home Assistant scripts should call only the App for sound events; no separate MQTT `SOUND` or `VOL` action is needed.

## 0.1.21

- Render PNG/GIF assets 1:1 from the top-left origin on the full 32x8 canvas, clipping pixels outside the display instead of resizing to 10x8.
- Composite assets over the clock and weekday bar so transparent pixels preserve the clock, opaque pixels cover it, and partial alpha blends with it.

## 0.1.20

- Place the clock colon in the gap between hours and minutes.
- Explain allowed AWTRIX MQTT prefixes in the App configuration UI and documentation.
- Document a Home Assistant script pattern that exposes REST errors in automation traces.

## 0.1.19

- Match the AWTRIX clock and weekday bar layout with weekday gaps and default colors.
- Pass event `duration_seconds` through to the AWTRIX custom app payload.

## 0.1.18

- Render 10x8 event assets from the left edge of the 32x8 frame.

## 0.1.17

- Align the rendered asset, clock, and weekday bar to the AWTRIX 32x8 layout contract.
- Make same-`event_id` duration refreshes reset their scheduled expiry without stale tasks clearing the replacement event.

## 0.1.16

- Add strict `weekdays` event control for showing or suppressing the weekday bar.
- Render events with local display time while keeping lifecycle timestamps UTC-aware.
- Listen read-only to configured AWTRIX settings topics and persist display palette snapshots under `/data/awtrix-addon-palettes.json`.

## 0.1.15

- Switch each new event to its AWTRIX custom app so the picture is visible immediately, while cleanup still removes only that custom app.

## 0.1.14

- Treat an active `event_id` as a replace key: a new request with the same ID supersedes its current overlay instead of returning `409`.

## 0.1.13

- Start through Home Assistant's documented `with-contenv` Bashio runner so the App process receives the s6 container environment.

## 0.1.12

- Load the s6 container environment before starting the App so `SUPERVISOR_TOKEN` reaches its MQTT startup process.

## 0.1.11

- Complete App startup only after MQTT recovery succeeds, avoiding an inactive background recovery task.

## 0.1.10

- Retry MQTT initialization after Supervisor services become available instead of remaining in a failed startup state.

## 0.1.9

- Refresh Supervisor MQTT credentials and confirm a new connection before retrying after a broker disconnect.
- Document the AWTRIX App's internal Home Assistant DNS URL instead of Core loopback URLs.

## 0.1.8

- Use Supervisor-provided MQTT credentials, wait for an accepted CONNACK, and safely retry events after a failed publish.
- Add managed Default and Personal RTTTL melody references alongside one-off RTTTL expressions.

## 0.1.7

- Use Home Assistant's current Apps terminology in displayed names, documentation, and runtime messages.

## 0.1.6

- Accept inline base64 PNG/GIF event assets so Home Assistant automations can render an image without uploading it to the shared assets directory.

## 0.1.5

- Add App Info README usage instructions with setup, token, REST command, and simple automation examples.

## 0.1.4

- Shorten generated-token startup logs to only the `secrets.yaml` authorization line and `/data/auth.json` location.

## 0.1.3

- Print the generated bearer token and ready-to-copy Home Assistant YAML snippets in the App startup log.

## 0.1.2

- Allow the Home Assistant UI to save blank `auth_token` and `default_clock_prefixes` options while keeping generated tokens and default clock selection.

## 0.1.1

- Build on the Supervisor-provided architecture and remove deprecated architecture entries.
- Add the AWTRIX icon.

## 0.1.0

- Add the AWTRIX event renderer App with authenticated HTTP events, MQTT custom-app rendering, safe restore behavior, and live canvas verification helpers.
