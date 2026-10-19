# HA-iDRAC/ha-idrac-controller/app/main.py
import json
import os
import re
import signal
import threading
import time

from .fan_logic import CUSTOM_FAN_MODES, SimpleFanState, clamp_speed, validate_fan_config, validate_fan_curve
from .ipmi_manager import IPMIManager
from .mqtt_client import MqttClient
from .mqtt_config import resolve_mqtt_config
from .power_control import POWER_ACTIONS
from .sensor_readings import parse_sdr_readings, is_important_reading, is_advanced_core_slug
from .pid_controller import PIDController
from . import web_server

# --- Global Variables ---
running = True
threads = []
status_lock = threading.Lock()
ALL_SERVERS_STATUS = {}
STATUS_FILE = "/data/current_status.json"
PID_STATE_FILE = "/data/pid_states.json"


# --- Graceful Shutdown ---
def graceful_shutdown(signum, frame):
    global running
    print("[MAIN] Shutdown signal received. Cleaning up...", flush=True)
    running = False


signal.signal(signal.SIGTERM, graceful_shutdown)
signal.signal(signal.SIGINT, graceful_shutdown)


# --- Server Worker Class ---
class ServerWorker:
    def __init__(self, server_config, global_opts):
        self.config = server_config
        self.global_opts = global_opts
        self.alias = self.config['alias']
        self.log_level = self.global_opts['log_level']
        self.running = True

        self.ipmi = IPMIManager(
            ip=self.config['idrac_ip'],
            user=self.config['idrac_username'],
            password=self.config['idrac_password'],
            log_level=self.log_level,
        )
        self.mqtt = MqttClient(client_id=f"ha_idrac_{self.alias}")
        self.pid = PIDController()
        self.fan_state = SimpleFanState()
        self.fan_mode = self.config.get('fan_mode', 'auto')

        self.server_info = {}
        self.discovered_sensors = set()
        self._known_batteries = {}
        self._known_psus = {}
        self._known_readings = {}
        self._threshold_status = None
        self._threshold_alerts = []
        self._reading_metadata = {}
        self._next_sensor_inventory = 0.0
        self._sensor_inventory_index = 0
        self._intrusion_seen = False
        self._power_details = {}
        self._next_power_details = 0.0
        self._next_mqtt_attempt = 0.0
        self._mqtt_configured = False
        self._pcie_response_disabled = False
        self._last_power_command = float("-inf")
        self.alive = True
        self.fan_cfg = validate_fan_config(
            self.config.get('low_temp_threshold', 45),
            self.config.get('critical_temp_threshold', 65),
            self.config.get('base_fan_speed_percent', 20),
            self.config.get('high_temp_fan_speed_percent', 50),
        )

    def _log(self, level, message):
        print(f"[{level.upper()}] [{self.alias}] {message}", flush=True)

    def _on_mqtt_message(self, topic, payload):
        if payload != "PRESS" or not self.running or not running:
            return
        for action, description in POWER_ACTIONS.items():
            if topic != f"{self.mqtt.base_topic}/command/{action}":
                continue
            # A hidden entity is not an authorization boundary. Old discovery,
            # hand-published commands and malformed config must also be denied.
            if self.config.get(description["option"]) is not True:
                self._log("warning", f"Ignored disabled power action: {action}.")
                return
            now = time.monotonic()
            if now - self._last_power_command < 30:
                self._log("warning", "Ignored repeated power command within 30 seconds.")
                return
            self._last_power_command = now
            self._log("warning", f"Power command requested: {description['name']}.")
            result = getattr(self.ipmi, description["method"])()
            if result is None:
                self._log("error", f"Power command failed: {action}; no automatic retry.")
            else:
                self._log("info", f"iDRAC accepted power command: {action}.")
            return

    def _on_mqtt_connected(self):
        """Refresh discovery and expose only explicitly enabled power actions."""
        self._log("info", "MQTT connected; refreshing discovery and power permissions.")
        self.discovered_sensors.clear()
        self._reading_metadata.clear()
        if self.config.get("include_advanced_diagnostics") is not True:
            self.mqtt.watch_extra_discovery(self._reconcile_extra_discovery)
        for slug in self._known_readings:
            self.mqtt.publish(f"{self.mqtt.base_topic}/reading/{slug}/availability", "offline", retain=True)
        self.mqtt.publish(f"{self.mqtt.base_topic}/intrusion/availability", "offline", retain=True)
        for name in self._known_psus:
            self.mqtt.publish(f"{self.mqtt.base_topic}/psu/{name}/availability", "offline", retain=True)
        for action, description in POWER_ACTIONS.items():
            command_topic = f"{self.mqtt.base_topic}/command/{action}"
            # Remove any broker-stored command before subscribing on reconnect.
            self.mqtt.publish(command_topic, "", retain=True, qos=1)
            slug = f"{action}_button"
            if self.config.get(description["option"]) is not True:
                # Includes the legacy, always-on shutdown entity on upgrades.
                # This runs even if temperature reads fail or the host is off.
                self.mqtt.remove_discovery("button", slug)
                continue
            self.mqtt.subscribe(command_topic)
            self.mqtt.publish_discovery(
                component="button", slug=slug, name=description["name"],
                device_class=description.get("device_class"),
                icon=description["icon"], cmd_topic=command_topic,
                entity_category="config",
            )

    def _initialize(self):
        self._log("info", "Initializing server worker...")
        if self.config.get('fan_control_enabled', True) and self.fan_mode not in CUSTOM_FAN_MODES:
            # Hand back before potentially slow FRU, firmware or broker lookups.
            self._use_dell_auto()

        pid_config = self.config.get('pid_config', {})
        self.pid.setpoint = pid_config.get('target_temp', 55)
        self.pid.set_gains(pid_config.get('kp', 4.0), pid_config.get('ki', 0.2), pid_config.get('kd', 0.1))

        if os.path.exists(PID_STATE_FILE):
            with open(PID_STATE_FILE, 'r') as f:
                try:
                    all_states = json.load(f)
                    if self.alias in all_states:
                        self.pid.load_state(all_states[self.alias])
                        self._log("info", f"Loaded persistent PID state: integral={self.pid.integral}")
                except json.JSONDecodeError:
                    self._log("warning", "Could not decode PID state file.")

        model_data = self.ipmi.get_server_model_info()
        if model_data:
            self.server_info.update(model_data)
        firmware = self.ipmi.get_firmware_version()
        if firmware:
            self.server_info["firmware"] = firmware
            self._log("info", f"iDRAC firmware version: {firmware}")

        if not self.config.get('fan_control_enabled', True):
            self._log("info", "Cooling control: monitor only; iDRAC settings are left untouched.")
        elif self.fan_mode in CUSTOM_FAN_MODES:
            for warning in self.fan_cfg["warnings"]:
                self._log("warning", warning)
            self._log("info", f"Custom fan profile: base {self.fan_cfg['base']}%, high {self.fan_cfg['high']}% at "
                               f"{self.fan_cfg['low']} C, return to Dell auto at {self.fan_cfg['critical']} C")
        else:
            self._log("info", "Cooling mode: Dell automatic. Custom fan profile settings are ignored.")

        if (self.fan_mode in CUSTOM_FAN_MODES and self.config.get("disable_pcie_cooling_response")
                and self.config.get('fan_control_enabled', True)):
            if self.ipmi.set_third_party_pcie_cooling_response(enabled=False):
                self._pcie_response_disabled = True
            else:
                self._log("warning", "This server did not accept the third-party PCIe cooling-response "
                                     "command; leaving that setting unchanged.")

        self.mqtt.set_device_info(
            server_alias=self.alias,
            manufacturer=self.server_info.get("manufacturer"),
            model=self.server_info.get("model"),
            ip_address=self.config.get("idrac_ip"),
            serial_number=self.server_info.get("serial"),
            sw_version=self.server_info.get("firmware"),
        )
        self.mqtt.message_callback = self._on_mqtt_message
        self.mqtt.on_connect_callback = self._on_mqtt_connected

        # MQTT being down must not stop IPMI monitoring/control. Try once at
        # startup, then keep retrying in the background of the control loop.
        if self._try_mqtt_connect() and not self.mqtt.wait_connected(timeout=3):
            self._log("warning", "MQTT broker not reachable yet; will keep retrying in the background.")

        return True

    def _try_mqtt_connect(self):
        self._next_mqtt_attempt = time.time() + 15
        if not self._mqtt_configured:
            settings = resolve_mqtt_config(self.global_opts, self._log)
            if settings is None:
                return False
            try:
                self.mqtt.configure_broker(
                    settings["host"], settings["port"], settings["username"],
                    settings["password"], self.log_level, use_tls=settings["ssl"],
                )
            except (OSError, ValueError):
                self._log("warning", "Could not configure MQTT; monitoring continues and setup will retry.")
                return False
            self._mqtt_configured = True
        self.mqtt.connect()
        return True

    def run(self):
        try:
            self._initialize()
            self._run_loop()
        finally:
            # Runs on SIGTERM as well as on an unexpected exception, so the
            # fans do not stay in manual mode after the add-on stops.
            self.cleanup()
            self.alive = False

    def _run_loop(self):
        while self.running and running:
            start_time = time.time()

            if not self.mqtt.is_connected and time.time() >= self._next_mqtt_attempt:
                self._try_mqtt_connect()

            raw_temp_data = self.ipmi.retrieve_temperatures_raw()
            if raw_temp_data is None:
                self._log("warning", "Could not read temperature SDR data; manual cooling is unsafe.")
                target_fan_speed = "Monitor only"
                if self.config.get('fan_control_enabled', True):
                    target_fan_speed = self._use_dell_auto("temperature read failed")
                self._update_status({"cpu_temps": []}, [], None, [], target_fan_speed)
                if self.mqtt.is_connected:
                    self.mqtt.publish(self.mqtt.availability_topic, "offline", retain=True)
                self._sleep(60)
                continue

            if self.mqtt.is_connected:
                self.mqtt.publish(self.mqtt.availability_topic, "online", retain=True)

            temps = self.ipmi.parse_temperatures(raw_temp_data)
            raw_fan_data = self.ipmi.retrieve_fan_rpms_raw()
            fans = self.ipmi.parse_fan_rpms(raw_fan_data)
            power_sdr_data = self.ipmi.retrieve_power_sdr_raw()
            power = self.ipmi.parse_power_consumption(power_sdr_data)

            hottest_cpu = max(temps['cpu_temps']) if temps['cpu_temps'] else None
            target_fan_speed = "Dell Auto"

            if not self.config.get('fan_control_enabled', True):
                target_fan_speed = "Monitor only"
            elif self.fan_mode not in CUSTOM_FAN_MODES:
                target_fan_speed = self._use_dell_auto()
            elif self.ipmi.fan_control_refused:
                target_fan_speed = "Dell Auto (refused)"
            elif hottest_cpu is None:
                self._log("warning", "No CPU temperature sensor found; handing fans back to the iDRAC for safety.")
                target_fan_speed = self._use_dell_auto("no CPU sensor")
            else:
                crit_thresh = self.fan_cfg["critical"]
                if self.fan_state.should_use_auto(hottest_cpu, crit_thresh):
                    target_fan_speed = self._use_dell_auto()
                else:
                    fan_mode = self.fan_mode

                    if fan_mode == 'simple':
                        low_thresh = self.fan_cfg["low"]
                        target_fan_speed = self.fan_state.simple_speed(
                            hottest_cpu,
                            low_thresh,
                            self.fan_cfg["base"],
                            self.fan_cfg["high"],
                        )
                        if self.ipmi.apply_user_fan_control_profile(target_fan_speed) is None:
                            target_fan_speed = "Dell Auto (fan command failed)"

                    elif fan_mode == 'target':
                        base_fan = self.fan_cfg["base"]
                        speed = self.pid.update(hottest_cpu, base_fan)
                        if speed is not None:
                            target_fan_speed = clamp_speed(speed, minimum=15)
                            if self.ipmi.apply_user_fan_control_profile(target_fan_speed) is None:
                                target_fan_speed = "Dell Auto (fan command failed)"

                    elif fan_mode == 'curve':
                        try:
                            fan_curve = validate_fan_curve(self.config.get('fan_curve', []))
                        except ValueError as error:
                            self._log("warning", f"Invalid fan curve: {error} Handing fans back to Dell.")
                            target_fan_speed = self._use_dell_auto("invalid fan curve")
                        else:
                            lower, upper = fan_curve[0], fan_curve[-1]
                            for i in range(len(fan_curve) - 1):
                                if fan_curve[i]['temp'] <= hottest_cpu < fan_curve[i + 1]['temp']:
                                    lower, upper = fan_curve[i], fan_curve[i + 1]
                                    break

                            if hottest_cpu < lower['temp']:
                                speed = lower['speed']
                            elif hottest_cpu >= upper['temp']:
                                speed = upper['speed']
                            else:
                                temp_range = upper['temp'] - lower['temp']
                                speed_range = upper['speed'] - lower['speed']
                                speed = (lower['speed'] + ((hottest_cpu - lower['temp']) / temp_range * speed_range)
                                         if temp_range > 0 else lower['speed'])

                            target_fan_speed = clamp_speed(speed)
                            if self.ipmi.apply_user_fan_control_profile(target_fan_speed) is None:
                                target_fan_speed = "Dell Auto (fan command failed)"

            # Optional telemetry is read after applying cooling, at most once
            # a minute with a bounded timeout; never delay a fan hand-back.
            self._refresh_sensor_inventory()
            psu_statuses = self.ipmi.get_power_status(power_sdr_data)
            batteries = self.ipmi.parse_battery_status(power_sdr_data)
            intrusion = self.ipmi.parse_intrusion_status(power_sdr_data)
            self._refresh_power_details(power_sdr_data)
            if power is None:
                power = self._power_details.get("instantaneous_watts")
            readings = self._additional_readings(power_sdr_data, raw_temp_data, raw_fan_data)
            self._update_status(temps, fans, power, psu_statuses, target_fan_speed, batteries, intrusion, readings)
            self._sleep(max(0.1, self.global_opts["check_interval_seconds"] - (time.time() - start_time)))

    def _use_dell_auto(self, reason=None):
        """Never claim automatic cooling unless the iDRAC accepted hand-back."""
        if self.ipmi.apply_dell_fan_control_profile() is None:
            self._log("error", "Could not restore Dell automatic fan control; cooling mode is unknown. "
                               "Will retry on the next unsafe control cycle.")
            return "Unknown (Dell auto hand-back failed)"
        return f"Dell Auto ({reason})" if reason else "Dell Auto"

    def _refresh_sensor_inventory(self):
        # Spread optional type discovery over cycles; use current SDR values,
        # never the readings returned by a previous inventory query.
        now = time.monotonic()
        if now < self._next_sensor_inventory:
            return
        self._next_sensor_inventory = now + 60
        types = ("Battery", "Power Supply", "Physical Security")
        self.ipmi.refresh_sensor_type(types[self._sensor_inventory_index])
        self._sensor_inventory_index = (self._sensor_inventory_index + 1) % len(types)

    def _refresh_power_details(self, sdr_data):
        now = time.monotonic()
        if now < self._next_power_details:
            return
        self._next_power_details = now + 60
        sensor_name = self.ipmi.get_power_sensor_name(sdr_data)
        raw = self.ipmi.retrieve_power_details_raw(sensor_name)
        details = self.ipmi.parse_power_details(raw)
        # Threshold and OEM reads are independent. The normal SDR poll can
        # supply the power reading if the verbose sensor lookup failed.
        reading = self.ipmi.parse_power_consumption(sdr_data)
        if "reading_watts" not in details and reading is not None:
            details["reading_watts"] = reading
        self._power_details = details

    def _reconcile_extra_discovery(self, slug, metadata):
        """Remove only this device's optional extras when the switch is off."""
        if self.config.get("include_advanced_diagnostics") is True:
            return
        if not is_important_reading({"slug": slug, "name": metadata["name"],
                                     "device_class": metadata.get("device_class")}):
            self.mqtt.remove_discovery("sensor", slug)
            self.mqtt.publish(f"{self.mqtt.base_topic}/reading/{slug}/availability", "offline", retain=True)

    def _additional_readings(self, sdr_data, temperature_data=None, fan_data=None):
        # Full SDR is preferred when a record also appears in a typed query.
        data = "\n".join(part for part in (temperature_data, fan_data, sdr_data) if part)
        power_name = self.ipmi.get_power_sensor_name(sdr_data)
        current = parse_sdr_readings(sdr_data)
        self._threshold_alerts = [{key: item[key] for key in ("name", "status", "value", "unit")}
                                  for item in current if item["status"] != "ok"]
        critical = {"cr", "nr", "lcr", "lnr", "ucr", "unr"}
        self._threshold_status = ("Critical" if any(item["status"] in critical for item in self._threshold_alerts)
                                  else "Warning" if self._threshold_alerts else "No alerts") if current else None
        readings = []
        for reading in parse_sdr_readings(data):
            row = reading.pop("source_row")
            # Reuse the existing parsers to avoid duplicate numeric entities or
            # a second, divergent definition of CPU/inlet/exhaust/fan sensors.
            temperatures = self.ipmi.parse_temperatures(row) if reading["unit"] == "°C" else {}
            if (temperatures.get("cpu_temps") or temperatures.get("inlet_temp") is not None
                    or temperatures.get("exhaust_temp") is not None):
                continue
            if reading["unit"] == "RPM" and self.ipmi.parse_fan_rpms(row):
                continue
            if row.split("|", 1)[0].strip() == power_name and reading["unit"] == "W":
                continue
            readings.append(reading)
        # These decoded fields were already returned by the existing optional
        # Dell queries. Reserved bytes and sentinel values are never exposed.
        for slug, name, key, unit, device_class in (
            ("dell_instantaneous_power", "Instantaneous power", "instantaneous_watts", "W", "power"),
            ("dell_instantaneous_current", "Instantaneous current", "current_amps", "A", "current"),
            ("dell_instantaneous_headroom", "Instantaneous power headroom", "headroom_watts", "W", "power"),
            ("dell_peak_headroom", "Peak power headroom", "peak_headroom_watts", "W", "power"),
        ):
            value = self._power_details.get(key)
            if value is not None:
                readings.append({"slug": slug, "name": name, "value": value, "unit": unit,
                                 "device_class": device_class, "state_class": "measurement",
                                 "status": "ok", "source": "Dell OEM", "entity": None, "sensor_number": None})
        for reading in readings:
            reading["diagnostic_tier"] = "important" if is_important_reading(reading) else "advanced"
        if self.config.get("include_advanced_diagnostics") is not True:
            readings = [reading for reading in readings if reading["diagnostic_tier"] == "important"]
        return readings

    def _update_status(self, temps, fans, power, psu_statuses, target_fan_speed, batteries=None, intrusion=None, readings=None):
        # Keep an already discovered battery visible as unknown if a later
        # read fails or no longer contains it, rather than leaving stale health.
        self._known_batteries = {name: {"name": name, "health": "Unknown", "voltage": None}
                                 for name in self._known_batteries}
        self._known_batteries.update({battery["name"]: battery for battery in (batteries or [])})
        batteries = list(self._known_batteries.values())
        self._known_psus = {name: {"name": name, "ok": None} for name in self._known_psus}
        self._known_psus.update({psu["name"]: psu for psu in psu_statuses})
        psu_statuses = list(self._known_psus.values())
        has_current_readings = readings is not None
        self._known_readings = {slug: {**item, "value": None} for slug, item in self._known_readings.items()}
        self._known_readings.update({item["slug"]: item for item in (readings or [])})
        readings = list(self._known_readings.values())
        cpu_ids = temps.get("cpu_ids", list(range(1, len(temps.get("cpu_temps", [])) + 1)))
        self._intrusion_seen = self._intrusion_seen or intrusion is not None
        capacity = self._power_details.get("capacity_watts")
        capacity_percent = round(100 * power / capacity, 2) if capacity and power is not None else None
        hottest_cpu = max(temps['cpu_temps']) if temps['cpu_temps'] else None
        status_data = {
            "hottest_cpu_temp": hottest_cpu,
            "sensor_threshold_status": self._threshold_status if has_current_readings else None,
            "sensor_threshold_alerts": self._threshold_alerts if has_current_readings else [],
            "inlet_temp": temps.get('inlet_temp'),
            "exhaust_temp": temps.get('exhaust_temp'),
            "power": power,
            "power_capacity_percent": capacity_percent,
            "power_capacity_watts": capacity,
            "power_warning_watts": self._power_details.get("warning_watts"),
            "power_failure_watts": self._power_details.get("failure_watts"),
            "intrusion_seen": self._intrusion_seen,
            "intrusion_active": intrusion.get("active") if intrusion else None,
            "target_fan_speed": None if isinstance(target_fan_speed, str) else target_fan_speed,
            "cooling_mode": target_fan_speed if isinstance(target_fan_speed, str) else "Custom control",
            "cpus": temps.get('cpu_temps', []),
            "cpu_ids": cpu_ids,
            "batteries": batteries,
            "sensor_readings": readings,
            "include_advanced_diagnostics": self.config.get("include_advanced_diagnostics") is True,
            "fans": fans,
            "psus": psu_statuses,
        }
        with status_lock:
            ALL_SERVERS_STATUS[self.alias] = {
                "alias": self.alias,
                "ip": self.config['idrac_ip'],
                "last_updated": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "hottest_cpu_temp_c": hottest_cpu,
                "sensor_threshold_status": status_data["sensor_threshold_status"],
                "sensor_threshold_alerts": status_data["sensor_threshold_alerts"],
                "inlet_temp_c": temps.get('inlet_temp'),
                "exhaust_temp_c": temps.get('exhaust_temp'),
                "power_consumption_watts": power,
                "power_capacity_percent": capacity_percent,
                "power_capacity_watts": capacity,
                "power_warning_watts": self._power_details.get("warning_watts"),
                "power_failure_watts": self._power_details.get("failure_watts"),
                "intrusion_seen": self._intrusion_seen,
                "intrusion_active": status_data["intrusion_active"],
                "target_fan_speed_percent": target_fan_speed,
                "cpu_temps_c": temps.get('cpu_temps', []),
                "cpu_ids": cpu_ids,
                "batteries": batteries,
                "sensor_readings": readings,
                "include_advanced_diagnostics": self.config.get("include_advanced_diagnostics") is True,
                "actual_fan_rpms": fans,
                "psu_statuses": psu_statuses,
            }

        self._publish_mqtt_data(status_data)

    def _sleep(self, seconds):
        """Sleep in small steps so SIGTERM can run cleanup promptly."""
        deadline = time.time() + seconds
        while time.time() < deadline and self.running and running:
            time.sleep(0.2)

    def _publish_mqtt_data(self, status):
        if not self.mqtt.is_connected:
            return

        sensors = {
            "sensor_threshold_status": {"component": "sensor", "name": "Hardware threshold status", "entity_category": "diagnostic", "icon": "mdi:alert-circle-outline", "include_attributes": True},
            "cooling_mode": {"component": "sensor", "name": "Cooling status", "entity_category": "diagnostic"},
            "hottest_cpu_temp": {"component": "sensor", "device_class": "temperature", "unit": "°C", "state_class": "measurement", "suggested_display_precision": 1},
            "inlet_temp": {"component": "sensor", "device_class": "temperature", "unit": "°C", "state_class": "measurement", "suggested_display_precision": 1},
            "exhaust_temp": {"component": "sensor", "device_class": "temperature", "unit": "°C", "state_class": "measurement", "suggested_display_precision": 1},
            "power": {"component": "sensor", "device_class": "power", "unit": "W", "state_class": "measurement", "icon": "mdi:flash", "suggested_display_precision": 0},
            "target_fan_speed": {"component": "sensor", "unit": "%", "icon": "mdi:fan-chevron-up", "suggested_display_precision": 0},
        }
        for slug, name, unit, device_class in [
            ("power_capacity_percent", "Power Capacity Used", "%", None),
            ("power_capacity_watts", "Power Capacity Reference", "W", "power"),
            ("power_warning_watts", "Power Warning Threshold", "W", "power"),
            ("power_failure_watts", "Power Failure Threshold", "W", "power"),
        ]:
            sensors[slug] = {"component": "sensor", "name": name, "unit": unit,
                             "device_class": device_class, "entity_category": "diagnostic",
                             "state_class": "measurement", "suggested_display_precision": 2 if unit == "%" else 0}
        if status.get("intrusion_seen"):
            sensors["system_board_intrusion"] = {
                "component": "binary_sensor", "name": "System Board Intrusion",
                "device_class": "tamper", "entity_category": "diagnostic",
                "extra_availability_topic": f"{self.mqtt.base_topic}/intrusion/availability",
            }
        cpu_ids = status.get("cpu_ids", list(range(1, len(status.get("cpus", [])) + 1)))
        cpu_values = {number - 1: value for number, value in zip(cpu_ids, status.get("cpus", []))}
        # Preserve old unique IDs: cpu_0_temp is CPU1, cpu_1_temp is CPU2.
        # Socket numbers come from IPMI, so a missing CPU1 cannot relabel CPU2.
        cpu_indexes = set(cpu_values) | {
            int(slug.split("_")[1]) for slug in self.discovered_sensors
            if re.fullmatch(r"cpu_\d+_temp", slug)
        }
        for i in sorted(cpu_indexes):
            sensors[f"cpu_{i}_temp"] = {"component": "sensor", "name": f"CPU{i + 1} Temperature", "device_class": "temperature", "unit": "°C", "state_class": "measurement", "suggested_display_precision": 1}
        battery_values = {}
        for battery in status.get("batteries", []):
            slug = "battery_" + re.sub(r"[^a-zA-Z0-9_]+", "_", battery["name"]).strip("_").lower()
            health_slug = f"{slug}_health"
            sensors[health_slug] = {"component": "sensor", "name": f"{battery['name']} health", "entity_category": "diagnostic", "icon": "mdi:battery-heart-variant"}
            battery_values[health_slug] = battery["health"]
            voltage_slug = f"{slug}_voltage"
            if battery["voltage"] is not None or voltage_slug in self.discovered_sensors:
                sensors[voltage_slug] = {"component": "sensor", "name": f"{battery['name']} voltage", "device_class": "voltage", "unit": "V", "state_class": "measurement", "entity_category": "diagnostic"}
                battery_values[voltage_slug] = battery["voltage"]
        for fan in status.get('fans', []):
            sensors[f"fan_{re.sub(r'[^a-zA-Z0-9_]+', '', fan['name']).lower()}_rpm"] = {"component": "sensor", "name": f"{fan['name']} RPM", "unit": "RPM", "icon": "mdi:fan", "suggested_display_precision": 0}
        for psu in status.get('psus', []):
            sensors[f"psu_{re.sub(r'[^a-zA-Z0-9_]+', '', psu['name']).lower()}"] = {"component": "binary_sensor", "name": psu['name'], "device_class": "problem", "entity_category": "diagnostic", "extra_availability_topic": f"{self.mqtt.base_topic}/psu/{psu['name']}/availability"}

        if self.config.get("include_advanced_diagnostics") is not True:
            sensors = {slug: desc for slug, desc in sensors.items() if not is_advanced_core_slug(slug)}

        for slug, desc in sensors.items():
            if slug not in self.discovered_sensors:
                self.mqtt.publish_discovery(
                    component=desc['component'],
                    slug=slug,
                    name=desc.get('name', slug.replace("_", " ").title()),
                    device_class=desc.get('device_class'),
                    unit=desc.get('unit'),
                    icon=desc.get('icon'),
                    state_class=desc.get('state_class'),
                    entity_category=desc.get('entity_category'),
                    suggested_display_precision=desc.get('suggested_display_precision'),
                    extra_availability_topic=desc.get("extra_availability_topic"),
                    include_attributes=desc.get("include_attributes", False),
                )
                self.discovered_sensors.add(slug)

            if desc['component'] == 'sensor':
                value = None
                if slug in battery_values:
                    value = battery_values[slug]
                elif slug.startswith('fan_'):
                    value = next((f['rpm'] for f in status['fans']
                                  if f"fan_{re.sub(r'[^a-zA-Z0-9_]+', '', f['name']).lower()}_rpm" == slug), None)
                elif slug.startswith('cpu_'):
                    index = int(slug.split('_')[1])
                    value = cpu_values.get(index)
                else:
                    value = status.get(slug)
                if slug == "sensor_threshold_status":
                    self.mqtt.publish_state("sensor", slug, value, {"attributes": {"alerts": status.get("sensor_threshold_alerts", [])}})
                else:
                    self.mqtt.publish_state('sensor', slug, value)
            elif slug == "system_board_intrusion":
                active = status.get("intrusion_active")
                self.mqtt.publish(desc["extra_availability_topic"], "online" if active is not None else "offline", retain=True)
                if active is not None:
                    self.mqtt.publish_state("binary_sensor", slug, "ON" if active else "OFF")
            elif desc['component'] == 'binary_sensor' and slug.startswith('psu_'):
                psu_name = desc['name']
                psu_data = next((p for p in status['psus'] if p['name'] == psu_name), None)
                ok = psu_data["ok"] if psu_data else None
                self.mqtt.publish(desc["extra_availability_topic"], "online" if ok is not None else "offline", retain=True)
                if ok is not None:
                    self.mqtt.publish_state("binary_sensor", slug, "OFF" if ok else "ON")

        for reading in status.get("sensor_readings", []):
            slug = reading["slug"]
            availability = f"{self.mqtt.base_topic}/reading/{slug}/availability"
            metadata = tuple(reading[key] for key in ("name", "unit", "device_class", "state_class"))
            if self._reading_metadata.get(slug) != metadata:
                self.mqtt.publish_discovery(
                    component="sensor", slug=slug, name=reading["name"],
                    unit=reading["unit"], device_class=reading["device_class"],
                    state_class=reading["state_class"], entity_category="diagnostic",
                    extra_availability_topic=availability, include_attributes=True,
                    expire_after=3 * max(60, self.global_opts["check_interval_seconds"]),
                )
                self._reading_metadata[slug] = metadata
            available = reading["value"] is not None
            self.mqtt.publish(availability, "online" if available else "offline", retain=True)
            if available:
                attributes = {"ipmi_status": reading["status"], "entity": reading["entity"],
                              "sensor_number": reading["sensor_number"], "source": reading.get("source", "IPMI SDR")}
                if reading.get("raw_reading"):
                    attributes["raw_reading"] = reading["raw_reading"]
                attributes["diagnostic_tier"] = reading.get("diagnostic_tier", "advanced")
                value = reading["value"]
                if isinstance(value, str) and len(value) > 255:
                    attributes["full_reading"] = value
                    value = value[:254] + "…"
                self.mqtt.publish_state("sensor", slug, value, {"attributes": attributes})

    def cleanup(self):
        if self.config.get('fan_control_enabled', True):
            self._log("info", "Worker shutting down. Reverting to Dell auto fans.")
            self.ipmi.apply_dell_fan_control_profile()
        else:
            self._log("info", "Worker shutting down with fan control disabled; leaving iDRAC fan settings untouched.")

        if self._pcie_response_disabled:
            self._log("info", "Restoring Dell default third-party PCIe cooling response.")
            if not self.ipmi.set_third_party_pcie_cooling_response(enabled=True):
                self._log("warning", "Could not restore the third-party PCIe cooling response; it is still disabled.")

        if self.mqtt.is_connected:
            self.mqtt.disconnect()

        if os.path.exists(PID_STATE_FILE):
            try:
                with open(PID_STATE_FILE, 'r') as f:
                    all_states = json.load(f)
            except (IOError, json.JSONDecodeError):
                all_states = {}
        else:
            all_states = {}
        all_states[self.alias] = self.pid.get_state()
        with open(PID_STATE_FILE, 'w') as f:
            json.dump(all_states, f, indent=4)
        self._log("info", f"Saved persistent PID state: integral={self.pid.integral}")

    def stop(self):
        self.running = False


# --- Main Execution ---
if __name__ == "__main__":
    print("[MAIN] ===== HA iDRAC Multi-Server Controller Starting =====", flush=True)

    global_options = {
        "log_level": os.getenv("LOG_LEVEL", "info"),
        "check_interval_seconds": int(os.getenv("CHECK_INTERVAL_SECONDS", 60)),
        "mqtt_host": os.getenv("MQTT_HOST", ""),
        "mqtt_port": int(os.getenv("MQTT_PORT", 1883)),
        "mqtt_username": os.getenv("MQTT_USERNAME", ""),
        "mqtt_password": os.getenv("MQTT_PASSWORD", ""),
        "base_fan_speed_percent": int(os.getenv("BASE_FAN_SPEED_PERCENT", 20)),
        "low_temp_threshold": int(os.getenv("LOW_TEMP_THRESHOLD", 45)),
        "high_temp_fan_speed_percent": int(os.getenv("HIGH_TEMP_FAN_SPEED_PERCENT", 50)),
        "critical_temp_threshold": int(os.getenv("CRITICAL_TEMP_THRESHOLD", 65)),
    }

    servers_configs_list = []
    if not os.path.exists("/data/servers_config.json"):
        with open("/data/servers_config.json", 'w') as f:
            json.dump([], f)
    else:
        with open("/data/servers_config.json", 'r') as f:
            try:
                servers_configs_list = json.load(f)
            except json.JSONDecodeError:
                pass

    web_server.global_config = global_options
    web_server_port = int(os.getenv("INGRESS_PORT", 8099))
    web_thread = threading.Thread(target=web_server.run_web_server, args=(web_server_port, STATUS_FILE, status_lock), daemon=True)
    web_thread.start()

    worker_instances = []
    for server_conf in servers_configs_list:
        if server_conf.get("enabled", False):
            worker = ServerWorker(server_conf, global_options)
            worker_instances.append(worker)
            thread = threading.Thread(target=worker.run, daemon=True)
            threads.append(thread)
            thread.start()

    # Supervisor watchdog endpoint: 503 only if every configured worker has
    # died, not when the iDRAC/MQTT is merely unreachable (the worker stays
    # alive and keeps retrying in that case).
    web_server.set_health_check(lambda: not worker_instances or any(w.alive for w in worker_instances))

    try:
        while running:
            with status_lock:
                with open(STATUS_FILE, 'w') as f:
                    json.dump(list(ALL_SERVERS_STATUS.values()), f, indent=4)
            time.sleep(2)
    except KeyboardInterrupt:
        graceful_shutdown(None, None)

    print("[MAIN] Waiting up to 50s for fan hand-back and cleanup...", flush=True)
    for worker in worker_instances:
        worker.stop()
    for thread in threads:
        thread.join(timeout=50)
        if thread.is_alive():
            print("[WARNING] A server worker did not finish cleanup in time; "
                  "the container is stopping anyway.", flush=True)
    print("[MAIN] ===== HA iDRAC Controller Stopped =====", flush=True)
