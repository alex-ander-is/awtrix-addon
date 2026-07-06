# AWTRIX App TODO

- Add a future web UI device picker backed by Home Assistant's AWTRIX device
  registry, so users choose a clock rather than typing its MQTT prefix.
- Add future web UI forms for supported AWTRIX custom-app payloads, while
  preserving the current managed event lifecycle and safety boundaries.
- Add optional AWTRIX HTTP API integration for clocks with explicit
  per-prefix configuration, for example `clock_http` mapping MQTT prefix to
  URL or mDNS service plus Basic Auth credentials. Use it only as an optional
  initial `/api/settings` palette fetch and diagnostics path; keep the current
  MQTT settings listener as the default because it does not need clock IPs,
  mDNS, or AWTRIX web credentials.
- If HTTP API support is implemented, keep credentials redacted, never log
  them, tolerate unavailable mDNS/HTTP by falling back to MQTT/fallback
  palette, and document that MQTT prefix does not reliably identify an HTTP
  hostname.
