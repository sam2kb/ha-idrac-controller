# HA iDRAC Controller

Control one or more Dell PowerEdge servers from Home Assistant over IPMI. The
app reads temperatures, fan speeds, power consumption and PSU status, publishes
them through MQTT, and manages fan speed per server.

## Features

- Several servers from one app instance.
- Per-server fan control with three modes:
    - Simple thresholds: a three-tier system (base, high, critical).
    - Multi-point curve: a custom temperature-to-fan-speed curve.
    - Target temperature: a PID loop that holds a chosen CPU temperature.
- Monitoring: each server becomes an MQTT device with sensors for individual
  CPU temperatures, hottest CPU, inlet and exhaust temperatures, fan RPM, power
  consumption, target fan speed and PSU status.
- A shutdown button for each server.
- A web UI through ingress: a live dashboard and a Manage Servers page.
- MQTT discovery: entities are created automatically.

## Fan control safety

CPU temperatures come from the IPMI processor entity (`3.x`), so `CPU1 Temp` on
13G/iDRAC 8 and `Temp` on 12G/iDRAC 7 both work. Inlet and exhaust names are
anchored, so `PSU1 Inlet Temp` cannot replace the chassis inlet.

A 2 °C hysteresis band stops the fans from flapping between speeds, and requests
below 5% are clamped to 5%. If manual mode is enabled but the speed write fails,
the fans go back to Dell auto. A server that refuses fan control with
`0xc1`/`0xd4`/`0xd5` is detected once, then monitoring continues. An optional
per-server setting disables Dell's third-party PCIe cooling response and
restores it when the app stops.

MQTT outages do not stop a worker. The app reconnects and republishes discovery.
The iDRAC password goes through `ipmitool -E`, so it does not appear in the
process list. The Supervisor watchdog probes `/health`, which fails only when
every server worker has died. The MQTT device carries the iDRAC firmware version
and service tag. Contradictory thresholds are corrected with a warning, and
temperatures outside -20 to 125 °C are ignored.

## Prerequisites

1.  Dell PowerEdge server with iDRAC 7 or 8. Both the 12th-generation sensor
    naming (`Temp`, iDRAC 7, for example R720) and the 13th-generation naming
    (`CPU1 Temp`/`CPU2 Temp`, iDRAC 8, for example T430, R630 and R730) are
    supported and covered by unit tests. The fan commands were originally
    verified on an R720; check the log on first start with a different model.
2.  Network connectivity from Home Assistant to each iDRAC address.
3.  IPMI over LAN enabled on each iDRAC. In the iDRAC web interface, open iDRAC
    Settings, then Network (or Connectivity), then IPMI Settings. Enable IPMI
    over LAN and set the channel privilege limit to Administrator.
4.  An MQTT broker that Home Assistant can reach. The Mosquitto app is the usual
    choice.

## Installation

1.  In Home Assistant, open Settings, then Apps, then Install app.
2.  Open the three-dot menu and choose Repositories.
3.  Paste this URL and add it:

    ```
    https://github.com/sam2kb/ha-idrac-controller
    ```

4.  Install HA iDRAC Controller from the app store.


## Configuration

Most settings live in the app's web UI.

1.  Leave the MQTT host, username and password empty to use Home Assistant's
    registered broker automatically. For an external broker, enter its host,
    port and credentials in the Configuration tab. The fan speed and temperature
    values there are global defaults for new servers. Save the settings.
2.  Start the app, then click Open Web UI.
3.  Open Manage Servers and use the Add New Server form. It is pre-filled with
    the global defaults.
4.  Restart the app after adding or editing servers. The web UI links to the app
    dashboard for this.

### Automatic MQTT setup

The app reads the registered broker's address, port and service credentials from
Home Assistant Supervisor. With the Mosquitto app, this is normally
core-mosquitto. The credentials stay in memory and are not copied to your app
options or logs. The service must use MQTT 3.1.1; TLS is enabled when the service
requires it.

An older core-mosquitto default with no credentials also uses automatic setup.
A manually entered host with credentials, or an external host, takes precedence.
To switch an existing manual setup to automatic, clear the host, username and
password, save, and restart the app.

If the service is unavailable, the app retries while IPMI monitoring continues.
Supervisor requests use a three-second connection timeout. External brokers
configured only in Home Assistant's MQTT integration still need manual
connection settings here.

## Web UI

The dashboard shows live data for every enabled server and refreshes itself.
The Manage Servers page adds, edits and deletes servers, and sets the fan mode
and thresholds for each one. After saving a server, the page offers a link to
the app dashboard to restart it.

## Entities in Home Assistant

Each server becomes a device named `iDRAC (<alias>)` with:

- a Shutdown Server button
- a PSU problem sensor for each power supply
- sensors for CPU temperatures, hottest CPU, inlet, exhaust, fan RPM, power and
  target fan speed

Entity IDs derive from the alias and the sensor names. Rename them in Home
Assistant if you need different IDs.

## Troubleshooting

- Check the app log first. Set the log level to `debug` or `trace` in the
  Configuration tab for more detail.
- For IPMI errors, confirm that IPMI over LAN is enabled and that the
  credentials in the web UI are correct.
- For MQTT errors in automatic mode, check that the Mosquitto app is running
  and provides the MQTT service. For a manual broker, check the connection
  settings in the Configuration tab.
- If sensor values are missing or wrong, the log shows the raw SDR data at
  debug level. Open an issue with the `ipmitool sdr type temperature` lines for
  your model.

## Reporting issues

Open an issue at
[sam2kb/ha-idrac-controller](https://github.com/sam2kb/ha-idrac-controller/issues)
and say which version you are running.

## License

MIT.
