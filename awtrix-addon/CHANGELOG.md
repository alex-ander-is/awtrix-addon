# Changelog

Released sections are immutable. Put every new change into a new version section.

## 0.1.4

- Shorten generated-token startup logs to only the `secrets.yaml` authorization line and `/data/auth.json` location.

## 0.1.3

- Print the generated bearer token and ready-to-copy Home Assistant YAML snippets in the add-on startup log.

## 0.1.2

- Allow the Home Assistant UI to save blank `auth_token` and `default_clock_prefixes` options while keeping generated tokens and default clock selection.

## 0.1.1

- Build on the Supervisor-provided architecture and remove deprecated architecture entries.
- Add the AWTRIX icon.

## 0.1.0

- Add the AWTRIX event renderer add-on with authenticated HTTP events, MQTT custom-app rendering, safe restore behavior, and live canvas verification helpers.
