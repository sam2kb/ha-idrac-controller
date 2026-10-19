# HA iDRAC Controller

A Home Assistant app for Dell PowerEdge servers. It reads temperatures, fan
speeds, power consumption and PSU status through the iDRAC's IPMI interface,
publishes the data to Home Assistant over MQTT, and controls fan speed per
server.

## What it does

The app connects to each iDRAC over IPMI and reads the sensor data with
`ipmitool`. It supports 12th- and 13th-generation PowerEdge servers (iDRAC 7 and
8), including the R720, R630, R730 and T430. CPU temperatures come from the
IPMI processor entity (`3.x`), so both the 12G `Temp` naming and the 13G
`CPU1 Temp`/`CPU2 Temp` naming work. PSU health uses `PSn Status` and
`PSn PG Fail`, and inlet and exhaust sensors are matched at the start of the
name so a PSU inlet reading cannot replace the chassis inlet.

Fan control has three per-server modes: simple thresholds, a temperature curve,
and a target-temperature PID loop. A 2 °C hysteresis band keeps the fans from
flapping between speeds, and requests below 5% are clamped to 5%. If manual
mode is enabled but the speed write fails, the fans go back to Dell auto. A
server that refuses fan control with `0xc1`, `0xd4` or `0xd5` (iDRAC 9 or 10, or
an account below Administrator) is detected once, then monitoring continues.
An optional per-server setting disables Dell's default cooling response for
third-party PCIe cards and restores Dell's setting when the app stops.

In Home Assistant, each server becomes an MQTT device with sensors for CPU
temperatures, hottest CPU, inlet and exhaust, fan RPM, power, target fan speed
and PSU status. There is also a shutdown button per server. The device carries
the iDRAC firmware version and service tag. The web UI shows a dashboard for all
servers and a Manage Servers page for adding, editing and deleting them.

## Requirements

- A Dell PowerEdge server with iDRAC 7 or 8.
- IPMI over LAN enabled on the iDRAC, with an account that has Administrator
  privilege. In the iDRAC web interface, open iDRAC Settings, then Network (or
  Connectivity), then IPMI Settings.
- Network access from Home Assistant to each iDRAC on UDP port 623.
- An MQTT broker that Home Assistant can reach. The Mosquitto app is the usual
  choice.

## Install

1. In Home Assistant, open Settings, then Apps, then Install app.
2. Open the three-dot menu and choose Repositories.
3. Paste this URL and add it:

   ```
   https://github.com/sam2kb/ha-idrac-controller
   ```

4. Install HA iDRAC Controller from the app store.

## Configure

1. On the app page, open the Configuration tab and fill in the MQTT broker
   details. The fan values there are defaults for new servers. Save.
2. Start the app, then click Open Web UI.
3. Open Manage Servers and add each server: alias, iDRAC address, username,
   password, fan mode and thresholds.
4. Restart the app after adding or editing servers. The web UI links to the app
   dashboard for this.

## Fan control notes

Fan values are percentages of the iDRAC's maximum fan speed, not RPM. Start with
`base_fan_speed_percent: 20`, `low_temp_threshold: 45`,
`high_temp_fan_speed_percent: 50` and `critical_temp_threshold: 65`, then watch
the temperatures for a day before lowering anything.

The account needs Administrator privilege. A lower account gets the fan commands
refused with `0xd4`, and the app switches to monitoring only.

The app stores servers in `/data/servers_config.json`, which is private to the
app, with the iDRAC password in plain text. Do not reuse an administrator
password from another system.

When the app stops, it hands the fans back to Dell's automatic profile. If the
container is killed without running its shutdown handler, the iDRAC can stay in
manual mode at the last speed. The Supervisor watchdog restarts the app in that
case, and the configured profile is applied again.

## Good to know

- The third-party PCIe cooling command is a Dell OEM command for 12G and 13G.
  Some firmware does not accept it; the app logs that once and leaves the
  setting untouched. It only runs when fan control is enabled.
- iDRAC 9 (14G) and newer are outside the scope of this app.
- The temperature parsing is covered by unit tests and a fake-IPMI run. It has
  not been tested on every PowerEdge model, so watch the first start on new
  hardware.
- If the log reports "No CPU temperature sensor found", set `log_level: debug`
  and check the raw SDR output. Open an issue with the
  `ipmitool sdr type temperature` lines for your model.

## Development

```sh
python -m pytest -q
```

The tests need no iDRAC, Flask or paho. They cover the SDR parsers, fan RPM,
power and PSU parsing, the fan floor and the hysteresis logic. GitHub Actions
runs the same suite on every push.

## Credits

The IPMI fan-control approach and the original Home Assistant app came from
[Aesgarth/HA-iDRAC](https://github.com/Aesgarth/HA-iDRAC). This project continues
that work as a standalone app. If you are coming from that project,
[MIGRATION.md](MIGRATION.md) has the settings mapping and the behaviour
differences. MIT license.
