# Changelog

Released sections are immutable. Put every new change into a new version section.

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
