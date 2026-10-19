<p align="center">
  <img src="ha-idrac-controller/logo.png" alt="HA iDRAC Controller" width="500">
</p>

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

New servers use Dell automatic cooling by default. Custom control is optional:
choose simple thresholds, a temperature curve, or a target-temperature PID loop
for each server. Existing saved custom modes keep their settings. A 2 °C hysteresis band keeps the fans from
flapping between speeds, and requests below 5% are clamped to 5%. If manual
mode is enabled but the speed write fails, the fans go back to Dell auto. A
server that refuses fan control with `0xc1`, `0xd4` or `0xd5` (iDRAC 9 or 10, or
an account below Administrator) is detected once, then monitoring continues.
An optional per-server setting disables Dell's default cooling response for
third-party PCIe cards and restores Dell's setting when the app stops.

In Home Assistant, each server becomes an MQTT device with sensors for CPU
temperatures, hottest CPU, inlet and exhaust, fan RPM, power, target fan speed
and PSU status. Power on, graceful shutdown, reset, power cycle and forced-off buttons are
optional per server and disabled by default. The device carries
the iDRAC firmware version and service tag. The web UI shows a dashboard for all
servers and a Manage Servers page for adding, editing and deleting them.

Temperature readings and cooling thresholds use Celsius. Home Assistant may
convert sensor displays to your preferred unit. To keep server sensors in °C,
open each temperature entity's settings and select °C as its unit of measurement.
MQTT discovery does not expose a per-device display-unit override.

CPU temperatures are labeled CPU1, CPU2 and so on, using the socket number
reported by IPMI. Existing MQTT unique IDs stay unchanged to preserve automations.
A missing CPU reading stays unknown and does not shift the other socket's label.

Battery health appears only when the SDR data exposes a battery, such as the
CMOS battery. Voltage appears only if a numeric voltage is reported. These
sensors use the existing SDR poll and do not estimate charge percentages.
An unavailable battery reading is shown as Unknown. A readable discrete sensor
with no asserted event shows No fault reported; that does not confirm a battery
is installed.

The dashboard shows watts and capacity used when the server reports enough data.
Capacity reference uses Dell's instantaneous power reading plus instantaneous
headroom. The ordinary SDR watt reading can be rounded and is not used to
calculate that reference. PSU nameplate ratings and warning/failure thresholds
are separate measurements. Missing or unsupported values remain unknown.
Dell total-power reads work independently of sensor labels and also supply
watts when no recognized system SDR sensor is available. An arbitrary board or
PSU watt sensor is never treated as total server power. Power details refresh
at most once a minute, after cooling control. The threshold
query times out after five seconds; each of the two Dell read queries times out
after three seconds. These queries do not change power caps or server settings.

Sensor discovery uses standard IPMI types and entity IDs where available, with
Dell label fallbacks for older firmware. Battery, Power Supply, and Physical
Security identities refresh in rotation, one type per minute with a five-second
timeout, after cooling control. Values always come from the current SDR poll.
CPU and inlet identification also recognizes standard processor and air-inlet
entities. Exhaust labels remain a fallback because generic IPMI has no universal
exhaust-temperature entity. An absent exhaust sensor stays unknown; target fan
speed stays unknown when the app is not setting a manual speed.

PSU problem sensors decode asserted failure and lost-input events even when a
presence message is missing. Unreadable or missing readings make a previously
discovered PSU unavailable. An empty PSU bay does not create a failure sensor.
Firmware, installed hardware, privileges, and licensing determine which optional
measurements are available. Parser tests cover iDRAC 7/8 output variants; they do
not establish that every model and firmware version has been tested on hardware.

Available SDR readings also appear as MQTT diagnostic sensors and in the
dashboard's Additional Sensor Readings table. This includes voltage, current,
other temperatures, and discrete status messages. Existing CPU, inlet/exhaust,
fan RPM, and total-power readings keep their entity IDs and are not duplicated.
New entities require an actual reading: blank, unset, unreadable, opaque raw
codes, and non-finite values are skipped. Zero is a valid reading. If a reading
later disappears, its entity becomes unavailable and keeps its history.

Additional sensor IDs use the IPMI sensor number and entity ID, so identical
labels remain distinct. Sensor attributes include the source, IPMI status,
entity ID, and sensor number. Decoded Dell instantaneous power, current, and
instantaneous/peak headroom are also exposed when available; reserved fields
are excluded. No extra per-sensor polling is needed.

System Board Intrusion is an MQTT tamper sensor for alerts. On means intrusion
is asserted; off means clear. Missing readings make the sensor unavailable.
It uses the existing SDR poll and does not clear or acknowledge intrusion events.

## Saving changes

Save writes your server configuration and shows a notice to reload or restart
the app. Save and reload app saves first, then asks Home Assistant Supervisor
to restart HA iDRAC Controller. The progress page stays inside ingress on both
web and mobile and returns to Manage Servers when the new app process responds.
A failed reload leaves the saved configuration intact and shows an error.

This restarts only the controller app. Home Assistant and your servers keep
running. Normal shutdown cleanup returns custom cooling control to Dell auto;
monitor-only servers keep their existing settings. The reload button is
available when running as a Home Assistant app and needs no extra API privileges.

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

1. Leave the MQTT host, username and password empty to use Home Assistant's
   registered broker automatically. For an external broker, enter its host,
   port and credentials in the Configuration tab. Save the settings.
2. Start the app, then click Open Web UI.
3. Open Manage Servers and add each server: alias, iDRAC address, username,
   password, fan mode and thresholds.
4. Choose Save and reload app to save the server and apply its settings. Regular
   Save writes the configuration and shows a notice to reload later.

## Server power actions

Power buttons are disabled for every server by default, including servers saved
before this option existed. In Manage Servers, open Edit and expand Server
power actions. Enable each action separately, then choose Save and reload app.
Only enabled actions appear in the Home Assistant device's configuration controls.

- Power On System starts a server that is off.
- Graceful Shutdown sends an ACPI request for the OS to shut down and power off.
  The OS must support the request. This action never falls back to forced off.
- Reset System (warm boot) is an immediate hardware reset, without an OS
  shutdown. Use the server's OS for a graceful reboot.
- Power Cycle System (cold boot) cuts power, then starts the server again.
- Power Off System (forced) immediately cuts server power.

Reset, power cycle and forced off can lose unsaved data. Each has its own
opt-in. NMI is a diagnostic interrupt, not a normal power action.

On update and reconnect, the app removes disabled buttons from MQTT discovery,
including the old shutdown button. Commands for disabled actions are rejected.
Retained command replays are ignored, old retained commands are cleared before
subscription, and repeated power commands within 30 seconds are ignored.
There is no extra confirmation when pressing an enabled Home Assistant button.
These permissions are independent of cooling control.

The cooling-status sensor reports Dell Auto, Monitor only, custom control, or a
fallback/error status. Target fan speed is a percentage only when the app sets
one; it is unknown in automatic mode. An unknown exhaust temperature means the
app has no usable exhaust reading, rather than a measured zero.

## Fan control notes

Dell automatic mode hands cooling to the iDRAC at startup and on each poll.
Custom speed and temperature settings are ignored in this mode. Monitor-only
mode is separate and leaves the current iDRAC cooling settings untouched.

For custom modes, fan values are percentages of maximum fan speed, not RPM.
The app defaults are `base_fan_speed_percent: 20`,
`low_temp_threshold: 45`, `high_temp_fan_speed_percent: 50`
and `critical_temp_threshold: 65`. Check CPU temperatures alongside disk,
memory and adapter temperatures when choosing a custom profile.

The critical_temp_threshold option is the app's temperature for returning to
Dell automatic cooling. It is not the processor's hardware critical limit.
The polling interval is also an app setting; it does not come from iDRAC.

The account needs Administrator privilege. A lower account gets the fan commands
refused with `0xd4`, and the app switches to monitoring only.

The app stores servers in `/data/servers_config.json`, which is private to the
app, with the iDRAC password in plain text. Do not reuse an administrator
password from another system.

When cooling control is enabled and the app stops, it hands the fans back to
Dell's automatic profile. If the container is killed without running its
shutdown handler, the iDRAC can stay in
manual mode at the last speed. The Supervisor watchdog restarts the app in that
case, and the configured profile is applied again.

## Good to know

- The third-party PCIe cooling command is a Dell OEM command for 12G and 13G.
  Some firmware does not accept it; the app logs that once and leaves the
  setting untouched. It only runs in an enabled custom cooling mode.
- iDRAC 9 (14G) and newer are outside the scope of this app.
- The temperature parsing is covered by unit tests and a fake-IPMI run. It has
  not been tested on every PowerEdge model, so watch the first start on new
  hardware.
- If the log reports "No CPU temperature sensor found", set `log_level: debug`
  and check the raw SDR output. Open an issue with the
  `ipmitool sdr type temperature` lines for your model.

## Development

```sh
python -m pip install pytest -r ha-idrac-controller/app/requirements.txt
python -m pytest -q
```

The tests use the app dependencies but need no iDRAC or MQTT broker. They
cover the SDR parsers, fan RPM, power and PSU parsing, the fan floor and
hysteresis, temperature-read failures, power permissions and MQTT replay rejection, curve editing
and validation, and redirects through Home Assistant ingress. Subprocess and
network calls are blocked during tests. GitHub Actions runs the same suite
on Python 3.14 for every push.

## Credits

The IPMI fan-control approach and the original Home Assistant app came from
[Aesgarth/HA-iDRAC](https://github.com/Aesgarth/HA-iDRAC). This project continues
that work as a standalone app. If you are coming from that project,
[MIGRATION.md](MIGRATION.md) has the settings mapping and the behaviour
differences. MIT license.
