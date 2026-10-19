# HA iDRAC Controller

Monitor iDRAC 7/8 PowerEdge servers in Home Assistant, with optional cooling and
power controls. Each server has an MQTT device and a dashboard in the app.

[Screenshots](#screenshots) · [Install](#install) · [Configure](#configure) · [Sensors](#sensors) ·
[Cooling](#cooling) · [Power actions](#power-actions) ·
[Troubleshooting](#troubleshooting) ·
[Technical reference](https://github.com/sam2kb/ha-idrac-controller/blob/main/docs/reference.md)

## Screenshots

| Dashboard | Server settings | Home Assistant |
| --- | --- | --- |
| [<img src="https://raw.githubusercontent.com/sam2kb/ha-idrac-controller/main/assets/screenshots/dashboard.png" alt="Overview with temperatures, power and hardware health" width="250">](https://raw.githubusercontent.com/sam2kb/ha-idrac-controller/main/assets/screenshots/dashboard.png) | [<img src="https://raw.githubusercontent.com/sam2kb/ha-idrac-controller/main/assets/screenshots/server-settings.png" alt="Server settings for cooling, diagnostics and optional power controls" width="250">](https://raw.githubusercontent.com/sam2kb/ha-idrac-controller/main/assets/screenshots/server-settings.png) | [<img src="https://raw.githubusercontent.com/sam2kb/ha-idrac-controller/main/assets/screenshots/home-assistant.png" alt="Home Assistant MQTT sensors, diagnostics and enabled power controls" width="250">](https://raw.githubusercontent.com/sam2kb/ha-idrac-controller/main/assets/screenshots/home-assistant.png) |

Select a screenshot to view it full size.

## Install

Requirements:

- Dell PowerEdge with iDRAC 7 or 8, such as R720, R630, R730 or T430.
- IPMI over LAN enabled and an iDRAC Administrator account. Look under iDRAC
  Settings → Network/Connectivity → IPMI Settings; set the channel privilege
  limit to Administrator.
- Network access from HA to each iDRAC on UDP 623.
- An MQTT broker reachable from HA. Mosquitto is the usual choice.

In Settings → Apps → Install app, open the three-dot menu, choose Repositories,
and add:

```
https://github.com/sam2kb/ha-idrac-controller
```

Install HA iDRAC Controller from the app store. iDRAC 9 and newer are outside scope.

## Configure

1. Leave MQTT host, username and password blank to use Supervisor's registered
   broker, normally core-mosquitto. For a manual broker, enter its connection
   details in Configuration. The other values there are defaults for new servers.
2. Start the app and open Web UI → Manage Servers.
3. Add each server's alias, iDRAC address and credentials. Choose its cooling mode.
4. Choose Save and reload app to apply the configuration.

Regular Save writes the configuration for the next reload. Save and reload app
restarts only this app through Supervisor, then returns to Manage Servers inside
HA ingress. If reload fails, your saved settings remain intact.

Automatic MQTT uses Supervisor's service credentials in memory; they are not
copied to options or logs. Clear an existing manual host, username and password,
save options and restart this app to switch to automatic. Brokers configured
only in HA's MQTT integration need manual settings here. MQTT outages are retried
while IPMI monitoring continues.

Server credentials are stored in plain text in the app's private
`/data/servers_config.json`. Use a dedicated account/password.

## Sensors

Each server appears as `iDRAC (<alias>)` with model, firmware and service tag.
Important readings are included by default, when available:

- CPU1/CPU2 and other temperatures, hottest CPU, fan RPM and cooling status.
- Total power, capacity used, battery health, PSU problems and intrusion.
- Redundancy, named hardware alarms and Hardware threshold status.

Enable Include advanced diagnostics in Edit for voltage/current, battery
voltage, power reference/thresholds, detailed Dell power/headroom and other
signals. Choose Save and reload app. Turning it off removes optional MQTT
entities and dashboard readings; enabling it again reuses their identifiers.

Hardware threshold status summarises readable threshold warnings/critical
alarms; affected readings appear in its alerts attribute. No alerts is not a
whole-server health assessment. Battery, PSU and intrusion states are separate.
Generic Active/Inactive states describe signals, not good/bad health; attributes
retain their raw text.

Unavailable or unrecognised readings stay Unknown. Batteries have health and,
when reported with advanced diagnostics enabled, voltage; no charge percentage
is estimated. An unreported exhaust sensor stays unknown. Target fan speed is
unknown when the app is not setting a manual percentage.

Temperatures and thresholds use °C. HA can convert the display: select °C in each
temperature entity's settings if needed. Existing core unique IDs are preserved;
additional entities use IPMI sensor/entity IDs. You can rename entities in HA.

See the [sensor reference](https://github.com/sam2kb/ha-idrac-controller/blob/main/docs/reference.md#sensors)
for battery states, power calculations and firmware limitations.

## Cooling

New servers default to Dell automatic. Existing custom settings remain active
until changed in Edit.

| Mode | Behaviour |
| --- | --- |
| Dell automatic | Hands cooling to iDRAC at startup and each poll. Ignores custom/PCIe options. |
| Monitor only | Reads sensors without changing cooling settings. |
| Simple thresholds | Selects base/high speed, returning to Dell automatic at the critical threshold. |
| Curve | Interpolates a temperature-to-speed curve. |
| Target temperature | Uses a PID loop to hold a CPU temperature. |

Custom speeds have a 5% floor and simple control has 2 °C hysteresis. Missing CPU
temperatures or a failed manual speed write return control to Dell automatic.
Refused fan commands switch to monitoring. The custom critical temperature is
an app safety setting, not the processor's hardware limit.

Normal app shutdown/reload returns enabled cooling to Dell automatic and restores
any PCIe cooling override. A hard container kill can skip cleanup and leave the
last manual speed active. The Supervisor watchdog restarts the app. Monitor-only
servers are left untouched.

See the [cooling reference](https://github.com/sam2kb/ha-idrac-controller/blob/main/docs/reference.md#cooling)
before choosing a custom profile.

## Power actions

All actions are disabled by default, including on existing servers. In Edit →
Server power actions, enable each action separately, then Save and reload app.
Only enabled actions appear in HA's device configuration controls.

| Action | Effect |
| --- | --- |
| Power On System | Starts a powered-off server. |
| Graceful Shutdown | Requests OS shutdown and power off through ACPI; never falls back to forced off. |
| Reset System (warm boot) | Immediate hardware reset, without OS shutdown. |
| Power Cycle System (cold boot) | Cuts power, then starts the server. |
| Power Off System (forced) | Immediately cuts server power. |

Reset, power cycle and forced off can lose unsaved data. Use the OS for a graceful
reboot. Enabled buttons have no extra confirmation. NMI is not offered.
Power permissions are independent of cooling mode.

Disabled actions are rejected and their discovery entries removed. Retained
commands are cleared/ignored, and repeated commands within 30 seconds are ignored.

## Troubleshooting

- IPMI: check IPMI over LAN, credentials, privilege and UDP 623 access.
- MQTT: check that Mosquitto provides the Supervisor MQTT service, or verify your
  manual broker settings. Automatic discovery requires MQTT 3.1.1; advertised TLS
  is honoured. Custom broker ACLs must allow reading sensor discovery for cleanup.
- Missing sensors: set log_level to debug or trace and inspect the raw SDR output.
  Include the relevant `ipmitool sdr type temperature` or battery rows when
  [reporting an issue](https://github.com/sam2kb/ha-idrac-controller/issues), along
  with app version, server model and firmware. Remove credentials and personal details.
- Unsupported sensors: firmware, installed hardware, permissions and licensing
  determine availability. Tests cover output variants, not every physical model.

[Changelog](https://github.com/sam2kb/ha-idrac-controller/blob/main/ha-idrac-controller/CHANGELOG.md) ·
[Migration guide](https://github.com/sam2kb/ha-idrac-controller/blob/main/MIGRATION.md) ·
[MIT license](https://github.com/sam2kb/ha-idrac-controller/blob/main/LICENSE)
