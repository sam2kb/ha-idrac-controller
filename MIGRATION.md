# Migrating from an older HA-iDRAC app

HA iDRAC Controller 1.0.0 is a new app with a new repository and its own
history. If you used an earlier HA-iDRAC build, this page maps the old settings
to the new server form and lists the behaviour changes.

That includes the original single-server app from
[Aesgarth/HA-iDRAC](https://github.com/Aesgarth/HA-iDRAC), the 0.2.0 BETA, and
the 0.3/0.4 builds. The fan commands and the sensor parsing are the same. What
changes is where the settings live and how many servers the app can handle.

## Before you start

Write down the settings from the old app:

- iDRAC address, username and password.
- Fan values: base speed, low threshold, high speed, critical threshold.
- MQTT broker, port, username and password.
- Check interval and log level.

The original single-server app kept these in the app Configuration tab. The
0.2.0 BETA kept servers in `/data/servers_config.json` inside the app
container. If you can still open the old web UI, take a screenshot of the
server list.

## Migration steps

1. Note the settings above.
2. Uninstall the old app. This matters even when the slug is the same, because
   the new app uses a different options schema.
3. In Home Assistant, open Settings, then Apps, then Install app. If the old
   repository is still in the store, remove it. Then add:

   ```
   https://github.com/sam2kb/ha-idrac-controller
   ```

4. Install HA iDRAC Controller 1.0.0.
5. Open the app Configuration tab and fill in the MQTT settings. The fan values
   there are global defaults for new servers. Save the settings.
6. Start the app, open the web UI and go to Manage Servers. Add each server
   with its alias, iDRAC address, username, password and fan settings. Choose
   the fan mode and set the per-server thresholds. Save.
7. Restart the app. The web UI links to the app dashboard for this.
8. Open the dashboard and confirm that temperatures, fan RPM and power appear.
   In Home Assistant, the MQTT device is named `iDRAC (<alias>)`.
9. Remove the old device from Home Assistant. The old MQTT discovery messages
   are retained by the broker, so the previous device and its entities can stay
   behind as unavailable. Open Settings, then Devices & Services, then MQTT,
   and delete the old iDRAC device. Deleting the device removes its entities.

## What changed

The old build controlled one server from the Configuration tab. The new one
keeps a list of servers in the web UI, and fan mode, thresholds, monitor-only
and the third-party PCIe option are set per server.

Fan control has three modes: simple thresholds, a multi-point curve and a
target-temperature PID loop. The old build only had the three-tier thresholds.
Monitor-only can be enabled for one server without affecting the others.

Each server device also gets PSU problem sensors and a shutdown button. The
device carries the iDRAC firmware version and service tag, and its identifier
is `idrac_controller_<alias>` instead of the old IP-based identifier, so entity
IDs follow the alias. Pick the alias you want in Home Assistant before adding
the server.

If the MQTT broker restarts, the app re-subscribes and republishes discovery.
The old build needed a restart. The Supervisor watchdog probes `/health`, which
fails only when every server worker has died, so an unreachable iDRAC or broker
does not cause a restart loop.

Servers are stored in `/data/servers_config.json` with the iDRAC password in
plain text. The old build stored them in the app options, also in plain text.
Do not reuse an administrator password from another system.

Adding or changing a server requires an app restart, which the web UI reminds
you about after saving.

## Settings mapping

| Old setting | New location |
|---|---|
| iDRAC address, username, password | Manage Servers, Add New Server |
| Base fan speed, low threshold, high speed, critical threshold | Manage Servers, per-server fan settings. The Configuration tab values are only defaults for new servers. |
| MQTT broker, port, username, password | Configuration tab, global |
| Check interval, log level | Configuration tab, global |
| Temperature unit | Not supported. The app works in Celsius; convert Fahrenheit thresholds before entering them. |
| Advanced fan curve (original single-server build) | Not carried over. The old page saved a curve that the control loop never used. The new app has a real curve mode under Manage Servers. |

## If something goes wrong

- Check the app log with `log_level: debug`. The raw `sdr type temperature`
  output is logged, which is the quickest way to see whether the CPU sensors
  are named as expected.
- If fan control is refused with 0xc1, 0xd4 or 0xd5, the account is below
  Administrator or the server is 14G or newer. The app detects this once and
  keeps monitoring.
- This repository starts at 1.0.0. The old builds, tags and pull requests are
  not part of it and are not maintained.
