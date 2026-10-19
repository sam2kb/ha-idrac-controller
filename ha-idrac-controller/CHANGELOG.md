## 1.0.14 - 2026-10-19

- Refresh the web UI with a consistent white/light theme, shared navigation,
  clearer reading tiles and status labels, and grouped server settings.
- Improve mobile forms and server actions, add accessible field labels and
  keyboard focus, and keep tables contained on narrow screens.
- Show monitor-only guidance when cooling control is disabled. Preserve saved
  fan profiles, default-disabled power actions, and the existing save/reload flow.

## 1.0.13 - 2026-10-19

- Fix same-named battery sensors overwriting each other. Ignore disabled spare
  slots during initial discovery, retain separate identities for additional
  batteries, and keep unreadable readings Unknown.
- Use OK for readable battery sensors without a reported fault.
- Log monitor-only cooling at startup instead of repeating it on every poll.
- Shorten both READMEs, add navigation, and move detailed sensor/cooling notes
  into a linked technical reference.

## 1.0.12 - 2026-10-19

- Add a per-server Include advanced diagnostics switch, off by default for new
  and existing servers, controlling both MQTT entities and the dashboard.
- Keep core health/load readings, other temperatures, redundancy, named hardware
  alarms, and a threshold-alert summary enabled. Put voltage/current readings,
  battery voltage, detailed power metadata, and other signals behind the switch.
- Reconcile retained discovery entries when the switch is off, removing only
  this app's optional entities for the selected device. Keep stable identifiers
  when available readings are enabled again.
- Show generic asserted/deasserted signals as Active/Inactive and retain their
  raw text in attributes without inferring hardware health. Continue to filter
  unset, unreadable, opaque, and non-finite data.

## 1.0.11 - 2026-10-19

- Add Save and reload app using Supervisor's self-restart API. Keep the progress
  page inside ingress on web and mobile, confirm a new app process, and return
  to Manage Servers. Regular Save now shows a notice without a restart link.
- Save configuration atomically before reloading. Report rejected or failed
  restarts without exposing credentials or retrying the request automatically.
- Expose available additional SDR readings as MQTT diagnostic sensors and in
  the dashboard, preserving units, zero values, discrete states, and stable
  sensor identities. Skip unset/unreadable/raw-code values and duplicate core
  numeric readings; mark previously seen readings unavailable if they disappear.
- Expose decoded Dell instantaneous power/current and instantaneous/peak
  headroom from the existing power queries, excluding reserved/sentinel fields.

## 1.0.10 - 2026-10-19

- Discover sensor identities using standard IPMI types and entity IDs, retaining
  Dell label fallbacks. Recognize generic battery and PSU labels and standard
  air-inlet entities; ignore unavailable temperature and fan readings.
- Read power thresholds from the discovered system watt sensor instead of the
  Dell CLI that assumes System Level. Keep partial results when optional reads
  fail. Derive capacity from Dell instantaneous power and headroom, avoiding
  errors from rounded SDR readings and rejecting ambiguous system sensors.
- Decode PSU failure, predictive failure, and lost-input events even without a
  presence message. Make missing or unreadable PSU data unavailable in MQTT.
- Decode readable battery sensors with no asserted events as No fault reported.
  Preserve unknown readings and avoid inferring that an optional battery exists.

## 1.0.9 - 2026-10-18

- Show watts alongside capacity used when Dell reports instantaneous power
  headroom. Expose capacity reference and warning/failure thresholds separately;
  never use threshold values or combined PSU ratings as the capacity reference.
- Add System Board Intrusion as a tamper sensor for alerts. Missing readings
  make it unavailable rather than reporting a clear chassis.
- Use iDRAC's Power On System, Graceful Shutdown, Reset System (warm boot),
  Power Cycle System (cold boot), and Power Off System (forced) labels. Every
  action remains independently disabled by default. Reset is a hard reset;
  forced off and power cycle bypass OS shutdown.
- Read optional power details after cooling control, with a five-second timeout
  and at most one query per minute. Keep unsupported measurements unknown.

## 1.0.8 - 2026-10-18

- Disable and hide power actions by default for new and existing servers. Add
  separate per-server opt-ins for power on, graceful shutdown and hard-reset
  reboot. Label reboot explicitly as an immediate reset without OS shutdown.
- Remove disabled buttons from retained MQTT discovery, including the legacy
  shutdown button. Reject disabled actions and retained command replays, clear
  old retained commands before subscribing, and suppress repeats for 30 seconds.
- Add a cooling-status sensor so automatic mode is visible even when the target
  fan percentage is unknown. Keep missing readings as unknown.
- Label CPU temperatures CPU1 and CPU2 while preserving existing entity IDs.
  Keep socket identities stable if readings arrive out of order or disappear.
- Discover battery health from the existing SDR poll and show voltage only when
  reported. Show unavailable readings as unknown; do not infer charge levels.

## 1.0.7 - 2026-10-18

- Link the configuration-saved message directly to this app's Home Assistant
  Info page, where the Restart button is available.
- Discover the installed app ID from Supervisor instead of hardcoding a
  repository prefix. Fall back to the Apps list if metadata is unavailable.

## 1.0.6 - 2026-10-18

- Default new servers to Dell automatic cooling. Hand control to the iDRAC
  before startup lookups and on each poll, without applying a custom fan curve.
- Keep existing saved custom modes and the separate, read-only monitor mode.
- Make custom cooling an explicit per-server choice and hide unused controls
  in automatic mode. Clarify the return-to-Dell temperature label.
- Ignore custom PCIe cooling overrides in Dell automatic mode and report
  failed hand-back attempts without claiming automatic control succeeded.

## 1.0.5 - 2026-10-19

- Read the MQTT broker address, port and service credentials automatically from
  Home Assistant Supervisor when manual MQTT settings are empty.
- Keep explicit manual broker settings and credentials as overrides. Older
  credential-free core-mosquitto defaults also use automatic configuration.
- Retry unavailable service discovery while IPMI monitoring continues. Use TLS
  when the Supervisor service requires it; keep discovered credentials in memory.

## 1.0.4 - 2026-10-19

- Attempt Dell automatic fan control when temperature polling fails. Clear
  stale readings and report failed hand-back attempts.
- Connect the MQTT shutdown handler before connecting to the broker.
- Preserve curve points after rows are deleted, reject unusable curves before
  saving, and restore Dell automatic control for invalid stored curves.
- Fix server add/delete and error redirects through Home Assistant ingress.
- Add worker and web-route regression tests to CI.

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
