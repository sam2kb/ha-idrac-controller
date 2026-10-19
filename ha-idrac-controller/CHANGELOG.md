## 1.0.2 - 2026-10-19

- Add an app icon and logo for the Home Assistant app store.
- Rename `changelog.md` to `CHANGELOG.md`, the name the app store reads.

## 1.0.1 - 2026-10-19

- Move the app image to Python 3.14, the version current Home Assistant runs
  on. Flask moves to 3.1.3 for Python 3.14 support.
- No behaviour changes.

## 1.0.0 - 2026-10-19

First release.

- Monitor one or more Dell PowerEdge servers with iDRAC 7 or 8 over IPMI.
- Per-server fan control with simple thresholds, a multi-point curve, or a
  target-temperature PID loop.
- 2 °C hysteresis, a 5% fan floor, a hand-back to Dell auto when a speed write
  fails, and detection of servers that refuse fan control.
- Optional third-party PCIe cooling response, restored when the app stops.
- MQTT discovery with CPU, inlet and exhaust temperatures, fan RPM, power,
  target fan speed, PSU status, firmware, service tag and a shutdown button.
- Web UI with a live dashboard and a Manage Servers page.
- `/health` endpoint for the Supervisor watchdog, MQTT reconnect handling, and
  `ipmitool -E` password handling.
- 26 tests and a GitHub Actions workflow.
