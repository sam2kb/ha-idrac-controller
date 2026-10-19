# HA-iDRAC/ha-idrac-controller/app/ipmi_manager.py
"""IPMI access for the iDRAC add-on.

The parsers below are written for both 12th and 13th generation PowerEdge
servers. 13th generation machines (R630/R730/T430/...) name their CPU
temperature sensors "CPU1 Temp"/"CPU2 Temp" and expose them as processor
entities (3.x), while 12th generation machines use a sensor literally named
"Temp" (also on entity 3.x). Entity IDs are therefore used first and names
only as a fallback.
"""
import os
import re
import subprocess
import time

MIN_FAN_PERCENT = 5
MAX_FAN_PERCENT = 100

# "CPU1 Temp        | 01h | ok  |  3.1 | 45 degrees C"
_TEMP_LINE_RE = re.compile(
    r"^(?P<name>.+?)\s*\|\s*[0-9a-fA-F]+h\s*\|\s*(?P<status>[a-z]+)\s*\|"
    r"\s*(?P<entity>\d+\.\d+)\s*\|\s*(?P<value>[-+]?\d*\.?\d+)\s*degrees C",
    re.IGNORECASE,
)
# "Fan1A Tach       | 30h | ok  |  7.1 | 2040 RPM"
_FAN_LINE_RE = re.compile(
    r"^(?P<name>.+?)\s*\|\s*[0-9a-fA-F]+h\s*\|\s*(?P<status>[a-z]+)\s*\|"
    r"\s*(?P<entity>\d+\.\d+)\s*\|\s*(?P<value>[\d.]+)\s*RPM",
    re.IGNORECASE,
)
_CPU_NAME_RE = re.compile(r"(?:^|\b)CPU\s?\d", re.IGNORECASE)


class IPMIManager:
    def __init__(self, ip, user, password, conn_type="lanplus", log_level="info"):
        self.ip = ip
        self.user = user
        self.password = password
        self.log_level = log_level.lower()
        self.base_args = self._build_base_args(conn_type)
        self.last_stderr = ""
        self._sensor_types = {}
        self._battery_primary = {}
        # Set when the iDRAC answers "I do not have that command" or "not for
        # this account". Only ever set while no fan command has succeeded, so
        # a later failure can never leave the fans pinned at a user speed.
        self.fan_control_refused = False
        self.fan_control_ever_accepted = False
        self._log("info", f"IPMI Manager initialized for host: {self.ip}")

    def _build_base_args(self, conn_type):
        if conn_type.lower() in ["local", "open"]:
            return ["-I", "open"]
        # -E makes ipmitool read IPMI_PASSWORD from the environment instead of
        # exposing the password in the process list via -P.
        return ["-I", "lanplus", "-H", self.ip, "-U", self.user, "-E"]

    def _log(self, level, message):
        levels = {"trace": -1, "debug": 0, "info": 1, "warning": 2, "error": 3, "fatal": 4}
        if levels.get(self.log_level, levels["info"]) <= levels.get(level.lower(), levels["info"]):
            print(f"[{level.upper()}] IPMI ({self.ip}): {message}", flush=True)

    def _run_ipmi_command(self, args_list, is_raw_command=True, timeout=15):
        if not self.base_args:
            self._log("error", "IPMI not configured.")
            return None

        base_command = ["ipmitool"] + self.base_args
        command_to_run = base_command + (["raw"] + args_list if is_raw_command else args_list)

        self._log("debug", f"Executing command: {' '.join(command_to_run)}")

        env = os.environ.copy()
        if self.password:
            env["IPMI_PASSWORD"] = self.password

        try:
            result = subprocess.run(
                command_to_run, capture_output=True, text=True, check=False,
                timeout=timeout, env=env,
            )

            if result.returncode != 0:
                self.last_stderr = result.stderr.strip()
                self._log("error", f"Command failed: {' '.join(command_to_run)}")
                self._log("error", f"STDOUT: {result.stdout.strip()}")
                self._log("error", f"STDERR: {self.last_stderr}")
                return None

            self.last_stderr = ""

            self._log("debug", f"Command STDOUT: {result.stdout.strip()}")
            return result.stdout.strip()

        except FileNotFoundError:
            self._log("error", "ipmitool command not found. Is it installed and in the system PATH?")
        except subprocess.TimeoutExpired:
            self._log("error", f"Command timed out: {' '.join(command_to_run)}")
        except Exception as e:
            self._log("error", f"An unexpected error occurred with command: {e}")
        return None

    def _clamp_fan_speed(self, decimal_value):
        """Clamp a requested fan speed into the safe 5-100% range."""
        try:
            val = int(decimal_value)
        except (TypeError, ValueError):
            self._log("warning", f"Invalid decimal value '{decimal_value}'. Using {MIN_FAN_PERCENT}%.")
            val = MIN_FAN_PERCENT
        if val < MIN_FAN_PERCENT:
            self._log("warning", f"Fan speed {val}% is below the safe minimum. Clamping to {MIN_FAN_PERCENT}%.")
            val = MIN_FAN_PERCENT
        if val > MAX_FAN_PERCENT:
            self._log("warning", f"Fan speed {val}% is above 100%. Clamping to 100%.")
            val = MAX_FAN_PERCENT
        return val

    def _decimal_to_hex_for_ipmi(self, decimal_value):
        val = self._clamp_fan_speed(decimal_value)
        return f"0x{val:02x}"

    # --- Fan control refusal handling -------------------------------------
    # iDRAC 9/10 and accounts below Administrator answer the Dell OEM fan
    # commands with 0xc1/0xd5 (no such command) or 0xd4 (privilege). Those are
    # permanent for the life of the add-on, so retrying them every cycle only
    # produces noise. A 0xcc ("invalid data field") is not a refusal of fan
    # control itself and is handled as a failed write.
    _REFUSAL_SIGNATURES = ("rsp=0xc1", "rsp=0xd5", "rsp=0xd4")

    def _is_fan_control_refusal(self):
        if self.fan_control_ever_accepted:
            return False
        stderr = (self.last_stderr or "").lower()
        return any(sig in stderr for sig in self._REFUSAL_SIGNATURES)

    def _note_fan_control_refusal(self):
        if self.fan_control_refused or not self._is_fan_control_refusal():
            return
        self.fan_control_refused = True
        self._log(
            "warning",
            "This iDRAC refused the Dell fan-control command (0xc1/0xd4/0xd5). "
            "It is either a 14G+ iDRAC, or the configured account is not an "
            "Administrator. Fan control is disabled and temperatures will keep "
            "being monitored.",
        )

    def apply_dell_fan_control_profile(self, force=False):
        if self.fan_control_refused and not force:
            self._log("debug", "Skipping Dell fan profile: this server refused fan control.")
            return None
        self._log("info", "Applying Dell default dynamic fan control.")
        result = self._run_ipmi_command(["0x30", "0x30", "0x01", "0x01"])
        if result is None and not self._is_fan_control_refusal():
            # This is the safety-critical hand-back; one retry for a transient
            # BMC/session error before reporting failure.
            time.sleep(0.5)
            self._log("warning", "Retrying Dell default fan control once.")
            result = self._run_ipmi_command(["0x30", "0x30", "0x01", "0x01"])
        if result is None:
            self._note_fan_control_refusal()
        return result

    def apply_user_fan_control_profile(self, decimal_fan_speed):
        if self.fan_control_refused:
            self._log("debug", "Skipping user fan profile: this server refused fan control.")
            return None

        speed = self._clamp_fan_speed(decimal_fan_speed)
        hex_fan_speed = f"0x{speed:02x}"
        self._log("info", f"Applying user static fan control: {speed}% ({hex_fan_speed})")

        if self._run_ipmi_command(["0x30", "0x30", "0x01", "0x00"]) is None:
            self._log("error", "Failed to enable manual fan control mode.")
            self._note_fan_control_refusal()
            return None
        time.sleep(0.5)

        result = self._run_ipmi_command(["0x30", "0x30", "0x02", "0xff", hex_fan_speed])
        if result is None:
            # The manual command landed but the speed did not. Leaving the fans
            # in manual mode would pin them at whatever the previous cycle set,
            # so hand them straight back to Dell's dynamic profile.
            self._log("error", f"Failed to set fan speed to {hex_fan_speed}; handing fans back to Dell auto.")
            self.apply_dell_fan_control_profile(force=True)
            return None

        self.fan_control_ever_accepted = True
        self._log("info", f"Successfully applied user fan control: {speed}%")
        return result

    # --- Third-party PCIe card cooling response ---------------------------
    # Dell OEM command from the 12G/13G IPMI set (same bytes used by
    # tigerblue77's controller): the last-but-two byte is 0x00 to leave Dell's
    # default cooling response enabled and 0x01 to disable it.
    _PCIE_COOLING_RESPONSE_CMD = [
        "0x30", "0xce", "0x00", "0x16", "0x05", "0x00", "0x00", "0x00", "0x05", "0x00",
    ]

    def set_third_party_pcie_cooling_response(self, enabled):
        payload = self._PCIE_COOLING_RESPONSE_CMD + ["0x00" if enabled else "0x01", "0x00", "0x00"]
        self._log("info", f"Setting third-party PCIe cooling response to {'Dell default' if enabled else 'disabled'}.")
        return self._run_ipmi_command(payload) is not None

    def get_firmware_version(self):
        output = self._run_ipmi_command(["mc", "info"], is_raw_command=False, timeout=20)
        if not output:
            return None
        match = re.search(r"Firmware Revision\s*:\s*(\S+)", output, re.IGNORECASE)
        return match.group(1) if match else None

    def get_server_model_info(self):
        self._log("info", "Retrieving server model information...")
        fru_data = self._run_ipmi_command(["fru"], is_raw_command=False, timeout=20)
        if not fru_data:
            self._log("warning", "Could not retrieve FRU data.")
            return None

        model_info = {"manufacturer": "Unknown", "model": "Unknown", "serial": None}
        patterns = {
            "manufacturer": r"Product Manufacturer\s*:\s*(.*)",
            "model": r"Product Name\s*:\s*(.*)",
            "board_mfg": r"Board Mfg\s*:\s*(.*)",
            "board_product": r"Board Product\s*:\s*(.*)",
        }

        m_manuf = re.search(patterns["manufacturer"], fru_data, re.IGNORECASE)
        m_model = re.search(patterns["model"], fru_data, re.IGNORECASE)

        model_info["manufacturer"] = m_manuf.group(1).strip() if m_manuf else "Unknown"
        model_info["model"] = m_model.group(1).strip() if m_model else "Unknown"

        if model_info["manufacturer"] == "Unknown":
            m_board_mfg = re.search(patterns["board_mfg"], fru_data, re.IGNORECASE)
            if m_board_mfg:
                model_info["manufacturer"] = m_board_mfg.group(1).strip()

        if model_info["model"] == "Unknown":
            m_board_prod = re.search(patterns["board_product"], fru_data, re.IGNORECASE)
            if m_board_prod:
                model_info["model"] = m_board_prod.group(1).strip()

        for serial_field in (r"Product Serial\s*:\s*(.*)", r"Chassis Serial\s*:\s*(.*)",
                             r"Board Serial\s*:\s*(.*)"):
            m_serial = re.search(serial_field, fru_data, re.IGNORECASE)
            if m_serial and m_serial.group(1).strip():
                model_info["serial"] = m_serial.group(1).strip()
                break

        if "dell" in model_info["manufacturer"].lower():
            model_info["manufacturer"] = "DELL"

        self._log("info", f"Server Info: Manufacturer='{model_info['manufacturer']}', "
                           f"Model='{model_info['model']}', Serial={model_info['serial']}")
        return model_info

    def get_chassis_power_state(self):
        """Read standard IPMI chassis state; a failed read is never 'off'."""
        raw = self._run_ipmi_command(["chassis", "power", "status"], is_raw_command=False, timeout=3)
        match = re.fullmatch(r"Chassis Power is (on|off)", (raw or "").strip(), re.IGNORECASE)
        return match.group(1).lower() == "on" if match else None

    def retrieve_temperatures_raw(self):
        self._log("debug", "Retrieving raw temperature SDR data...")
        return self._run_ipmi_command(["sdr", "type", "temperature"], is_raw_command=False)

    def parse_temperatures(self, sdr_data, cpu_pattern_str=None, inlet_pattern_str=r"^Inlet Temp\b",
                           exhaust_pattern_str=r"^Exhaust Temp\b"):
        """Parse `ipmitool sdr type temperature` output.

        CPU sensors are identified by processor entity ID (3.x), which covers
        both "CPU1 Temp" (13G) and "Temp" (12G). `cpu_pattern_str` remains as
        an optional extra name filter for unusual firmware.
        """
        temps = {"cpu_temps": [], "cpu_ids": [], "inlet_temp": None, "exhaust_temp": None}
        if not sdr_data:
            return temps

        for line in sdr_data.splitlines():
            match = _TEMP_LINE_RE.match(line.strip())
            if not match:
                continue
            if match.group("status").lower() in ("ns", "na", "u"):
                continue
            try:
                temp_value = int(round(float(match.group("value"))))
            except ValueError:
                continue
            if not -20 <= temp_value <= 125:
                self._log("debug", f"Ignoring implausible temperature {temp_value} C from {line.strip()}")
                continue

            sensor_name = match.group("name").strip()
            entity = match.group("entity")

            if entity.split(".")[0] in ("55", "64") or (inlet_pattern_str and re.search(inlet_pattern_str, sensor_name, re.IGNORECASE)):
                if temps["inlet_temp"] is None:
                    temps["inlet_temp"] = temp_value
                continue
            if exhaust_pattern_str and re.search(exhaust_pattern_str, sensor_name, re.IGNORECASE):
                if temps["exhaust_temp"] is None:
                    temps["exhaust_temp"] = temp_value
                continue

            is_cpu = entity.split(".")[0] in ("3", "65") or bool(_CPU_NAME_RE.search(sensor_name))
            if cpu_pattern_str:
                is_cpu = is_cpu or bool(re.search(cpu_pattern_str, sensor_name, re.IGNORECASE))
            if is_cpu:
                cpu_name = re.search(r"\bCPU\s*(\d+)\b", sensor_name, re.IGNORECASE)
                cpu_number = (int(cpu_name.group(1)) if cpu_name else
                              int(entity.split(".")[1]) if entity.split(".")[0] in ("3", "65") else
                              len(temps["cpu_temps"]) + 1)
                temps["cpu_ids"].append(max(1, cpu_number))
                temps["cpu_temps"].append(temp_value)

        return temps

    def retrieve_fan_rpms_raw(self):
        self._log("debug", "Retrieving raw fan SDR data...")
        return self._run_ipmi_command(["sdr", "type", "fan"], is_raw_command=False, timeout=10)

    def parse_fan_rpms(self, sdr_data):
        fans = []
        if not sdr_data:
            return fans

        for line in sdr_data.splitlines():
            match = _FAN_LINE_RE.match(line.strip())
            if not match or match.group("status").lower() in ("ns", "na", "u"):
                continue
            try:
                fans.append({"name": match.group("name").strip(),
                             "rpm": int(float(match.group("value")))})
            except ValueError:
                continue
        return fans

    def retrieve_power_sdr_raw(self):
        self._log("debug", "Retrieving raw power SDR data...")
        return self._run_ipmi_command(["sdr", "elist"], is_raw_command=False, timeout=20)

    def parse_power_consumption(self, sdr_data):
        if not sdr_data:
            return None

        sensor_name = self.get_power_sensor_name(sdr_data)
        for line in sdr_data.splitlines():
            fields = [part.strip() for part in line.split("|")]
            if len(fields) == 5 and fields[0] == sensor_name:
                return int(float(fields[4].split()[0]))
        return None

    def refresh_sensor_type(self, sensor_type):
        """Discover identities by standard IPMI type, independent of Dell labels.

        Only identities are cached. Every health value still comes from the
        current SDR poll, so a failed poll cannot reuse old healthy readings.
        """
        if sensor_type not in ("Battery", "Power Supply", "Physical Security"):
            return
        raw = self._run_ipmi_command(["sdr", "type", sensor_type], is_raw_command=False, timeout=5)
        if raw is not None:
            self._sensor_types[sensor_type] = {
                (fields[0], fields[1].lower(), fields[3])
                for line in raw.splitlines()
                if len(fields := [part.strip() for part in line.split("|")]) == 5
            }

    def _has_sensor_type(self, fields, sensor_type):
        return (fields[0], fields[1].lower(), fields[3]) in self._sensor_types.get(sensor_type, set())

    def get_power_status(self, sdr_data):
        """Decode PSU presence and asserted faults; unreadable stays unknown."""
        psus = {}
        for line in (sdr_data or "").splitlines():
            fields = [part.strip() for part in line.split("|")]
            if len(fields) != 5:
                continue
            name, _, status, entity, reading = fields
            named = re.fullmatch(r"PS(?:U)?(\d+)\s+(Status|PG Fail)", name, re.IGNORECASE)
            entity_match = re.fullmatch(r"10\.(\d+)", entity)
            typed = self._has_sensor_type(fields, "Power Supply")
            if not named and not (entity_match and (typed or name.lower() in ("status", "pg fail"))):
                continue
            # A typed Power Supply can also expose voltage/current sensors.
            if re.search(r"\b(?:Volts?|Amps?|Watts?)\b", reading, re.IGNORECASE):
                continue
            index = int(entity_match.group(1)) if entity_match else 0
            if index == 0:
                if not named:
                    continue
                index = int(named.group(1))
            data = psus.setdefault(index, {"present": False, "fault": False, "unknown": False})
            status = status.lower()
            if status in ("ns", "na", "u") or reading.lower() in ("no reading", "disabled", "not readable"):
                data["unknown"] = True
                continue
            text = re.sub(r"[-_]", " ", reading.lower())
            pg_fail = (named and named.group(2).lower() == "pg fail") or name.lower() == "pg fail"
            critical = status in ("cr", "nr", "lcr", "ucr", "lnr", "unr", "nc", "lnc", "unc")
            fault = bool(re.search(r"\b(?:failure|failed|predictive failure|ac lost|input lost|out of range|configuration error)\b", text))
            fault = fault or critical or bool(pg_fail and text in ("state asserted", "asserted"))
            data["fault"] |= fault
            data["present"] |= "presence detected" in text or text == "present"
            if not fault and "presence detected" not in text and text != "present" and text not in ("", "0x00", "0x0000", "state deasserted", "deasserted"):
                data["unknown"] = True
        results = []
        for index, data in sorted(psus.items()):
            if data["fault"]:
                ok = False  # An asserted input fault is meaningful without presence text.
            elif data["unknown"]:
                ok = None
            elif data["present"]:
                ok = True
            else:
                continue  # An empty, readable PSU bay is not a failed supply.
            results.append({"name": f"PSU{index}", "ok": ok})
        return results

    def parse_battery_status(self, sdr_data):
        """Read battery health/voltage from the existing SDR list, when exposed.

        IPMI battery events are Low, Failed and Presence Detected. These are
        health states, not charge percentages. Unavailable readings stay unknown.
        """
        batteries = []
        candidates = {}
        for line in (sdr_data or "").splitlines():
            fields = [part.strip() for part in line.split("|")]
            if len(fields) != 5:
                continue
            if fields[3].split(".")[0] != "40" and not self._has_sensor_type(fields, "Battery") and not re.search(r"\b(?:battery|batt|vbatt|cmos)\b", fields[0], re.IGNORECASE):
                continue
            identity = (fields[0], fields[1].lower(), fields[3])
            candidates[identity] = fields
        # The same label can describe multiple battery slots. Give the lowest
        # entity/sensor identity the legacy name; keep that choice if a later
        # poll omits it. Disabled spare slots must not replace an active battery.
        def identity_order(identity):
            _, sensor, entity = identity
            entity_parts = tuple(int(part) for part in entity.split(".")) if re.fullmatch(r"\d+\.\d+", entity) else (999, 999)
            number = int(sensor[:-1], 16) if re.fullmatch(r"[0-9a-f]+h", sensor) else 999
            return identity[0], entity_parts, number, sensor, entity

        for identity in sorted(candidates, key=identity_order):
            name, sensor, status, entity, reading = candidates[identity]
            primary = self._battery_primary.setdefault(name, identity)
            if status.lower() in ("ns", "na", "u") and reading.lower() == "disabled":
                continue
            if identity != primary:
                name = f"{name} ({entity}, {identity[1]})"
            reading_lower = reading.lower()
            health = "Unknown"
            voltage = None
            voltage_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:Volts?|V)", reading, re.IGNORECASE)
            if status.lower() not in ("ns", "na", "u"):
                if re.search(r"\b(?:failed|failure)\b", reading_lower) or status.lower() in ("cr", "nr", "lcr", "ucr", "lnr", "unr"):
                    health = "Failed"
                elif status.lower() in ("nc", "lnc", "unc"):
                    health = "Warning"
                elif re.search(r"\blow\b", reading_lower):
                    health = "Low"
                elif re.search(r"\b(?:absent|missing|not present)\b", reading_lower):
                    health = "Missing"
                elif status.lower() == "ok" and ("presence detected" in reading_lower or reading_lower in ("ok", "present") or voltage_match):
                    health = "OK"
                elif status.lower() == "ok" and reading_lower in ("", "0x00", "0x0000", "state deasserted", "deasserted"):
                    # Compact discrete sensors print nothing when no event is
                    # asserted. This does not prove a battery is installed.
                    health = "OK"
                elif status.lower() == "ok" and reading_lower in ("state asserted", "asserted"):
                    health = "Warning"
                if voltage_match:
                    try:
                        voltage = float(voltage_match.group(1))
                    except ValueError:
                        pass
            batteries.append({"name": name, "health": health, "voltage": voltage})
        return batteries

    def get_power_sensor_name(self, sdr_data):
        """Choose an unambiguous system watt sensor, never a PSU input sensor."""
        named, names = [], []
        for line in (sdr_data or "").splitlines():
            fields = [part.strip() for part in line.split("|")]
            if len(fields) != 5:
                continue
            names.append(fields[0])
            if fields[2].lower() in ("ns", "na", "u"):
                continue
            if not re.fullmatch(r"\d+(?:\.\d+)?\s+Watts", fields[4], re.IGNORECASE):
                continue
            if fields[0].lower() in ("pwr consumption", "system level"):
                named.append(fields[0])
        # An arbitrary board-level watt sensor could measure one component.
        # The independent Dell power query supplies total watts on other labels.
        return named[0] if len(named) == 1 and names.count(named[0]) == 1 else None

    def retrieve_power_details_raw(self, sensor_name):
        """Read thresholds and headroom independently, preserving partial data.

        Dell's powermonitor powerconsumption CLI hardcodes System Level and
        aborts on machines exposing Pwr Consumption. These queries use the
        discovered sensor and the same read-only Dell headroom command (0xBB).
        """
        if sensor_name is not None and (not isinstance(sensor_name, str) or not sensor_name or sensor_name.startswith("-") or any(c in sensor_name for c in "\r\n\x00")):
            return None
        sensor = self._run_ipmi_command(
            ["sdr", "get", sensor_name], is_raw_command=False, timeout=5,
        ) if sensor_name else None
        # These are the read-only commands used by ipmitool's Dell power monitor.
        # SDR watts are quantized; do not combine them with precise OEM headroom.
        raw_power = self._run_ipmi_command(["0x30", "0xb3", "0x0a", "0x00"], timeout=3)
        raw_headroom = self._run_ipmi_command(["0x30", "0xbb"], timeout=3)
        parts = [sensor] if sensor else []
        for raw, length, label in ((raw_power, 7, "Instantaneous power"),
                                   (raw_headroom, 4, "System Instantaneous Headroom")):
            try:
                data = bytes.fromhex(raw) if raw is not None else b""
            except ValueError:
                data = b""
            if len(data) == length:
                value = int.from_bytes(data[:2], "little")
                if value != 0xffff:  # Do not publish a sentinel as a measurement.
                    parts.append(f"{label}: {value} W")
                secondary = int.from_bytes(data[2:4], "little")
                if secondary != 0xffff:
                    if length == 7:
                        parts.append(f"Instantaneous current: {secondary / 10:g} A")
                    else:
                        parts.append(f"System Peak Headroom: {secondary} W")
        return "\n".join(parts) if parts else None

    def parse_power_details(self, data):
        """Use OEM instantaneous power plus headroom as the capacity reference.

        Warning/failure thresholds and PSU nameplate ratings are not capacity.
        Keep unsupported/missing values unknown rather than estimating them.
        """
        values = {}
        labels = {
            "reading": "reading_watts", "instantaneous power": "instantaneous_watts",
            "warning threshold": "warning_watts",
            "failure threshold": "failure_watts",
            "system instantaneous headroom": "headroom_watts",
            "system peak headroom": "peak_headroom_watts",
        }
        for line in (data or "").splitlines():
            match = re.fullmatch(r"\s*([^:]+):\s*(\d+(?:\.\d+)?)\s*W(?:att)?s?\s*", line, re.IGNORECASE)
            if match and (key := labels.get(match.group(1).strip().lower())):
                values[key] = float(match.group(2))
            amperage = re.fullmatch(r"\s*Instantaneous current\s*:\s*(\d+(?:\.\d+)?)\s*A\s*", line, re.IGNORECASE)
            if amperage:
                values["current_amps"] = float(amperage.group(1))
            sensor_reading = re.fullmatch(
                r"\s*Sensor Reading\s*:\s*(\d+(?:\.\d+)?)(?:\s*\([^)]*\))?\s*W(?:att)?s?\s*",
                line, re.IGNORECASE,
            )
            if sensor_reading:
                values["reading_watts"] = float(sensor_reading.group(1))
            threshold = re.fullmatch(
                r"\s*Upper (non-critical|critical)\s*:\s*(\d+(?:\.\d+)?)\s*",
                line, re.IGNORECASE,
            )
            if threshold:
                key = "warning_watts" if threshold.group(1).lower() == "non-critical" else "failure_watts"
                values[key] = float(threshold.group(2))
        if "instantaneous_watts" in values and "headroom_watts" in values:
            capacity = values["instantaneous_watts"] + values["headroom_watts"]
            if capacity > 0:
                values["capacity_watts"] = capacity
        return values

    def parse_intrusion_status(self, sdr_data):
        """Read the chassis tamper state, not the discrete sensor's generic 'ok'."""
        for line in (sdr_data or "").splitlines():
            fields = [part.strip() for part in line.split("|")]
            if len(fields) != 5:
                continue
            if "intrusion" not in fields[0].lower() and not (
                self._has_sensor_type(fields, "Physical Security") and "chassis intrusion" in fields[4].lower()
            ):
                continue
            reading = fields[4].lower()
            active = None
            if fields[2].lower() not in ("ns", "na", "u"):
                if reading in ("", "0x00", "0x0000", "clear", "normal", "ok") or re.search(r"\b(?:not asserted|deasserted|no intrusion)\b", reading):
                    active = False
                elif "intrusion" in reading or reading == "asserted":
                    active = True
            return {"name": "System Board Intrusion", "active": active}
        return None

    def chassis_power_off(self):
        """Immediately force power off; only called through an explicit opt-in."""
        return self._run_ipmi_command(["chassis", "power", "off"], is_raw_command=False)

    def chassis_power_cycle(self):
        """Cold boot by cycling chassis power without an OS shutdown."""
        return self._run_ipmi_command(["chassis", "power", "cycle"], is_raw_command=False)

    def chassis_power_on(self):
        """Power on the chassis; never toggle, cycle or force power off."""
        return self._run_ipmi_command(["chassis", "power", "on"], is_raw_command=False)

    def chassis_hard_reset(self):
        """Immediately reset the chassis without a graceful OS shutdown."""
        return self._run_ipmi_command(["chassis", "power", "reset"], is_raw_command=False)

    def chassis_shutdown(self):
        """Sends a graceful ACPI shutdown command to the server."""
        self._log("info", "Sending graceful shutdown command to server...")
        return self._run_ipmi_command(["chassis", "power", "soft"], is_raw_command=False)
