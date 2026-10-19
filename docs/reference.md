# Technical reference

[Setup guide](../ha-idrac-controller/README.md) · [Sensors](#sensors) ·
[Cooling](#cooling) · [Storage and runtime](#storage-and-runtime)

## Sensors

### Identity and availability

The app reads sensors with ipmitool over IPMI. Standard sensor types and entity
IDs identify batteries, power supplies and physical security sensors; Dell name
fallbacks support older firmware. Those identities refresh in rotation, one type
per minute, with a five-second timeout after cooling control. Health always
comes from the current full SDR poll, never a cached healthy value.

Processor entities identify CPU sockets, including 12G `Temp` and 13G `CPU1 Temp` /
`CPU2 Temp`. Processor entities 3/65 and air-inlet entities 55/64 are recognised.
Inlet/exhaust name matching is anchored to avoid confusing PSU inlet readings
with chassis inlet temperature. Generic IPMI has no universal exhaust entity;
exhaust names remain a fallback. Missing CPU readings keep the other socket's
label and existing cpu_0_temp/cpu_1_temp unique IDs.

The app ignores temperatures outside -20 to 125 °C. Missing enabled readings
become unavailable; an absent exhaust sensor is not a measured zero.

### Batteries and health

Battery sensors report health events, not charge percentage. Explicit healthy
presence, a good reported voltage, or a readable OK sensor with no asserted
fault maps to OK. OK describes the reported health; it alone does not establish
presence. Low, Failed, Missing and Warning are preserved; unreadable or
unrecognised states remain Unknown. Voltage is shown only when supplied by the
server and advanced diagnostics is enabled. Disabled spare slots are omitted
from initial discovery. Same-named battery sensors are kept separate using their
IPMI entity/sensor identity; a disabled slot cannot overwrite an active reading.
The primary keeps the existing MQTT name. Additional slots get an entity/sensor
suffix; previously discovered slots remain Unknown if a later poll loses them.

PSU problem sensors decode asserted failure, predictive failure and lost-input
states even without a presence message. Previously discovered unreadable/missing
PSUs become unavailable. An empty PSU bay does not create a failure sensor.

System Board Intrusion is an MQTT tamper sensor: on means asserted, off means
clear, and a missing reading makes it unavailable. Polling never acknowledges or
clears intrusion events.

Hardware threshold status reports Warning/Critical from current readable SDR
threshold statuses and lists affected readings in its alerts attribute. No
alerts means no readable threshold alarm was reported, not that every component
is healthy. A failed full-SDR read makes the summary unknown. Separate battery,
PSU, intrusion and digital alarm sensors still apply.

### Power

Capacity reference is Dell's precise instantaneous watts plus instantaneous
headroom. Capacity used is the displayed current power divided by that reference.
The ordinary SDR reading may be rounded and is never used to construct the
reference. PSU nameplate ratings and warning/failure thresholds are separate.
The app never treats an arbitrary board or PSU watt sensor as total server power.

Independent Dell reads supply total watts even without a recognised system SDR
label. Optional details refresh at most once a minute, after cooling control:
the named SDR threshold query has a five-second timeout; each Dell power/headroom
query has a three-second timeout. Unsupported fields stay unknown. No power cap
or server setting is changed. Reserved fields and sentinel values are omitted.

### Advanced diagnostics

Include advanced diagnostics defaults to Off for new and existing servers and
controls both MQTT and the dashboard. Default readings include core health/load,
additional numeric temperatures, redundancy and clearly named hardware alarms.
Advanced adds voltages/current, battery voltage, power capacity reference and
thresholds, detailed Dell power/current/headroom, and other signal readings.

Blank, unset, unreadable, opaque raw codes and non-finite readings are omitted;
zero is valid. Generic State Asserted/State Deasserted displays Active/Inactive,
with raw_reading retained in attributes. A signal's meaning determines whether
it represents a problem.

Additional IDs use the sensor number and entity ID; core numeric readings are
reused to avoid duplicates. Disabling extras removes this device's optional
retained discovery entries, including older entries; enabling them restores
available readings with the same IDs. Ownership checks ignore other devices'
discovery payloads. Custom MQTT ACLs must allow subscribing to sensor discovery
messages for cleanup. These readings reuse the existing SDR/Dell polls.

Availability depends on firmware, hardware, privileges and licensing. Tests
cover iDRAC 7/8 output variants; not every model/firmware is hardware-tested.
The fan commands were originally verified on an R720; watch the first start on
new hardware.

## Cooling

Dell automatic hands cooling to iDRAC at startup and each poll. Monitor only
makes no cooling changes. Existing custom modes retain their settings on upgrade.

Custom fan values are percentages, not RPM. Defaults are
`base_fan_speed_percent: 20`, `low_temp_threshold: 45`,
`high_temp_fan_speed_percent: 50`, `critical_temp_threshold: 65`, with a
`check_interval_seconds: 30` polling interval. These are app policies, not values
read from iDRAC. The critical temperature returns control to Dell automatic;
it is not the processor's hardware critical limit. Consider disk, memory and
adapter temperatures as well as CPUs before lowering fans.

Simple control uses 2 °C hysteresis, and requested speeds below 5% are clamped.
Contradictory thresholds are corrected with a warning. Curve mode validates and
interpolates points; target mode uses a PID loop and persists its state.
A failed speed write after entering manual mode triggers a return to Dell auto.
Failures in temperature/CPU reads also trigger hand-back when cooling is enabled.

Refusal codes 0xc1/0xd4/0xd5 disable further fan-control attempts for that session
while monitoring continues. Administrator privilege is required; lower privilege
can produce 0xd4. iDRAC 9/10 fan controls are outside scope.

The optional third-party PCIe cooling override is a Dell 12G/13G OEM command. It
runs only in enabled custom modes, restores Dell's setting on normal shutdown,
and logs rejection once when unsupported. Dell automatic ignores this option.

Normal stop/reload returns enabled cooling to Dell auto. A hard container kill
can bypass cleanup and leave the last manual fan speed set. The Supervisor
watchdog restarts the app and its configured profile is applied again.
Monitor-only mode leaves existing fan settings untouched throughout.

## Storage and runtime

Server configurations, including plain-text iDRAC passwords, live in the app's
private `/data/servers_config.json`. Use dedicated credentials. ipmitool reads
the password through its environment (-E), not a command-line password argument.

Automatic MQTT gets the registered broker, port and credentials from Supervisor,
keeps them in memory and enables TLS when advertised. MQTT 3.1.1 is required.
Credential-free legacy core-mosquitto options also use automatic setup; explicit
manual/external settings take precedence. Supervisor service requests use a
three-second connection timeout. Service failures are retried without stopping
IPMI workers. Reconnection republishes discovery.

Save atomically writes configuration; Save and reload app requests only this
app's Supervisor restart. It needs no extra API role and stays inside ingress
on web/mobile. Failed reloads preserve saved configuration and show an error.
Normal cooling cleanup still runs; monitor-only servers are untouched.

The watchdog probes /health, which fails when every configured server worker has
died. MQTT outages alone do not fail it. Tests cover parsers, fan safety, power
permissions/replay rejection, curve editing, ingress and reload behaviour, with
external subprocess/network calls blocked. CI runs Python 3.14 and Node UI tests.
