"""Parser tests against output shapes from 12G (iDRAC7) and 13G (iDRAC8) servers."""
from conftest import load_module

ipmi_mod = load_module("main_ipmi_manager", "ha-idrac-controller/app/ipmi_manager.py")

# Dell PowerEdge R730/T430 (13th gen, iDRAC8): CPU sensors carry the socket number.
SDR_13G = """\
CPU1 Temp        | 01h | ok  |  3.1 | 45 degrees C
CPU2 Temp        | 02h | ok  |  3.2 | 47 degrees C
Inlet Temp       | 04h | ok  |  7.1 | 22 degrees C
Exhaust Temp     | 05h | ok  |  7.2 | 30 degrees C
DIMM A1 Temp     | 20h | ok  | 32.1 | 40 degrees C
"""

# Dell PowerEdge R720 (12th gen, iDRAC7): sensors are literally named "Temp".
SDR_12G = """\
Temp             | 01h | ok  |  3.1 | 40 degrees C
Temp             | 02h | ok  |  3.2 | 42 degrees C
Inlet Temp       | 04h | ok  |  7.1 | 20 degrees C
Exhaust Temp     | 05h | ok  |  7.2 | 28 degrees C
"""

FAN_SDR = """\
Fan1A Tach       | 30h | ok  |  7.1 | 2040 RPM
Fan1B Tach       | 31h | ok  |  7.2 | 2160 RPM
Fan Redundancy   | 32h | ok  |  7.3 | 0x00
"""

ELIST = """\
PS1 Status       | 0Ah | ok  | 10.1 | Presence detected
PS2 Status       | 0Bh | ok  | 10.2 | Presence detected
PS1 PG Fail      | 0Ch | ok  | 10.1 |
PS2 PG Fail      | 0Dh | ok  | 10.2 |
Pwr Consumption  | 77h | ok  | 10.1 | 196 Watts
"""


def make_manager():
    return ipmi_mod.IPMIManager("10.0.0.10", "root", "secret", log_level="fatal")


def test_13g_cpu_sensors_are_detected_by_entity_id():
    temps = make_manager().parse_temperatures(SDR_13G)
    assert temps["cpu_temps"] == [45, 47]
    assert temps["inlet_temp"] == 22
    assert temps["exhaust_temp"] == 30
    assert 40 not in temps["cpu_temps"]  # DIMM sensor must not drive fan control


def test_12g_sensors_named_temp_are_detected():
    temps = make_manager().parse_temperatures(SDR_12G)
    assert temps["cpu_temps"] == [40, 42]


def test_fan_rpms_are_parsed_and_non_tach_rows_ignored():
    fans = make_manager().parse_fan_rpms(FAN_SDR)
    assert fans == [{"name": "Fan1A Tach", "rpm": 2040}, {"name": "Fan1B Tach", "rpm": 2160}]


def test_power_consumption_from_sdr_elist():
    assert make_manager().parse_power_consumption(ELIST) == 196


def test_psu_status_uses_presence_and_pg_fail_not_voltage():
    psus = make_manager().get_power_status(ELIST)
    assert psus == [{"name": "PSU1", "ok": True}, {"name": "PSU2", "ok": True}]


def test_psu_fault_detected():
    data = "PS1 Status       | 0Ah | ok  | 10.1 | Presence detected\n" \
           "PS1 PG Fail      | 0Ch | cr  | 10.1 |\n"
    assert make_manager().get_power_status(data) == [{"name": "PSU1", "ok": False}]


def test_fan_speed_floor_prevents_zero_percent():
    manager = make_manager()
    assert manager._decimal_to_hex_for_ipmi(0) == "0x05"
    assert manager._decimal_to_hex_for_ipmi(3) == "0x05"
    assert manager._decimal_to_hex_for_ipmi(100) == "0x64"
    assert manager._decimal_to_hex_for_ipmi(250) == "0x64"


def test_ipmi_password_is_not_in_the_process_arguments():
    manager = make_manager()
    assert "-E" in manager.base_args
    assert "secret" not in manager.base_args


def test_empty_psu_bay_is_not_reported_as_failed():
    data = "PS1 Status       | 0Ah | ok  | 10.1 | Presence detected\n" \
           "PS2 Status       | 0Bh | ok  | 10.2 | 0x00\n"
    assert make_manager().get_power_status(data) == [{"name": "PSU1", "ok": True}]


SDR_WITH_PSU_INLET = """\
PSU1 Inlet Temp   | 0Dh | ok  | 10.1 | 34 degrees C
Inlet Temp       | 04h | ok  |  7.1 | 22 degrees C
CPU1 Temp        | 01h | ok  |  3.1 | 45 degrees C
"""


def test_psu_inlet_sensor_does_not_override_chassis_inlet():
    temps = make_manager().parse_temperatures(SDR_WITH_PSU_INLET)
    assert temps["inlet_temp"] == 22
    assert temps["cpu_temps"] == [45]


def test_third_party_cooling_response_uses_dell_oem_bytes(monkeypatch):
    manager = make_manager()
    calls = []
    monkeypatch.setattr(manager, "_run_ipmi_command", lambda args, **kwargs: calls.append(args) or "ok")

    assert manager.set_third_party_pcie_cooling_response(False) is True
    assert calls[-1] == ["0x30", "0xce", "0x00", "0x16", "0x05", "0x00", "0x00", "0x00",
                         "0x05", "0x00", "0x01", "0x00", "0x00"]

    assert manager.set_third_party_pcie_cooling_response(True) is True
    assert calls[-1][-3:] == ["0x00", "0x00", "0x00"]


def test_refusal_codes_disable_fan_control_until_something_was_accepted():
    manager = make_manager()
    manager.last_stderr = "Unable to send RAW command (channel=0x0 netfn=0x30 lun=0x0 cmd=0x30 rsp=0xd4): Insufficient privilege level"
    assert manager._is_fan_control_refusal() is True

    manager.fan_control_ever_accepted = True
    assert manager._is_fan_control_refusal() is False


def test_failed_speed_command_hands_fans_back_to_dell(monkeypatch):
    manager = make_manager()
    manager._log = lambda *args, **kwargs: None
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = []
    replies = iter(["ok", None, "ok"])

    def fake_run(args, **kwargs):
        calls.append(args)
        return next(replies)

    monkeypatch.setattr(manager, "_run_ipmi_command", fake_run)
    assert manager.apply_user_fan_control_profile(30) is None
    assert calls[0] == ["0x30", "0x30", "0x01", "0x00"]
    assert calls[1] == ["0x30", "0x30", "0x02", "0xff", "0x1e"]
    assert calls[2] == ["0x30", "0x30", "0x01", "0x01"]  # Dell auto hand-back


def test_firmware_version_parsed(monkeypatch):
    manager = make_manager()
    monkeypatch.setattr(manager, "_run_ipmi_command",
                        lambda *args, **kwargs: "Firmware Revision : 2.86.45.45\n")
    assert manager.get_firmware_version() == "2.86.45.45"


def test_serial_number_parsed_from_fru(monkeypatch):
    manager = make_manager()
    fru = "Product Manufacturer : DELL\nProduct Name : PowerEdge T430\nProduct Serial : ABC1234\n"
    monkeypatch.setattr(manager, "_run_ipmi_command", lambda *args, **kwargs: fru)
    info = manager.get_server_model_info()
    assert info["model"] == "PowerEdge T430"
    assert info["serial"] == "ABC1234"


def test_implausible_temperature_is_ignored():
    sdr = "CPU1 Temp | 01h | ok | 3.1 | 255 degrees C\n" \
          "CPU2 Temp | 02h | ok | 3.2 | 47 degrees C\n"
    assert make_manager().parse_temperatures(sdr)["cpu_temps"] == [47]



def test_power_methods_use_exact_named_ipmi_semantics(monkeypatch):
    from unittest.mock import Mock, call
    manager = make_manager()
    command = Mock(return_value="accepted")
    monkeypatch.setattr(manager, "_run_ipmi_command", command)
    assert manager.chassis_shutdown() == "accepted"
    assert manager.chassis_power_on() == "accepted"
    assert manager.chassis_hard_reset() == "accepted"
    assert manager.chassis_power_off() == "accepted"
    assert manager.chassis_power_cycle() == "accepted"
    assert command.call_args_list == [
        call(["chassis", "power", "soft"], is_raw_command=False),
        call(["chassis", "power", "on"], is_raw_command=False),
        call(["chassis", "power", "reset"], is_raw_command=False),
        call(["chassis", "power", "off"], is_raw_command=False),
        call(["chassis", "power", "cycle"], is_raw_command=False),
    ]



def test_cpu_socket_ids_follow_ipmi_names_and_12g_entity_instances():
    manager = make_manager()
    assert manager.parse_temperatures(SDR_13G)["cpu_ids"] == [1, 2]
    assert manager.parse_temperatures(SDR_12G)["cpu_ids"] == [1, 2]
    only_cpu2 = manager.parse_temperatures("CPU2 Temp | 02h | ok | 3.2 | 47 degrees C")
    assert only_cpu2["cpu_ids"] == [2]
    assert only_cpu2["cpu_temps"] == [47]


def test_battery_health_uses_reported_discrete_states_and_optional_voltage():
    manager = make_manager()
    rows = [
        ("ok", "Presence Detected", "Healthy", None),
        ("ok", "Presence Detected, Low", "Low", None),
        ("ok", "Failed, Presence Detected", "Failed", None),
        ("ok", "Not Present", "Missing", None),
        ("ok", "3.12 Volts", "Healthy", 3.12),
        ("lcr", "2.0 Volts", "Failed", 2.0),
        ("nr", "2.0 Volts", "Failed", 2.0),
        ("lnc", "2.7 Volts", "Warning", 2.7),
        ("ns", "No Reading", "Unknown", None),
        ("ns", "Presence Detected", "Unknown", None),
        ("ok", "Unrecognized state", "Unknown", None),
    ]
    for status, reading, health, voltage in rows:
        result = manager.parse_battery_status(f"CMOS Battery | 0Fh | {status} | 23.1 | {reading}")
        assert result == [{"name": "CMOS Battery", "health": health, "voltage": voltage}]
    assert manager.parse_battery_status(None) == []
    assert manager.parse_battery_status(ELIST) == []
    assert manager.parse_battery_status("CMOS Battery | invalid") == []


def test_multiple_batteries_are_independent_and_do_not_create_charge_percentages():
    result = make_manager().parse_battery_status(
        "CMOS Battery | 0Fh | ok | 23.1 | Presence Detected\n"
        "RAID Battery | 10h | ok | 23.2 | Low\n")
    assert [battery["health"] for battery in result] == ["Healthy", "Low"]
    assert all(set(battery) == {"name", "health", "voltage"} for battery in result)



def test_power_details_parse_only_reported_capacity_inputs_and_thresholds():
    manager = make_manager()
    data = "Reading: 70 W\nWarning threshold: 896 W\nFailure threshold: 980 W\nInstantaneous power: 83 W\nSystem Instantaneous Headroom: 817 W\n"
    result = manager.parse_power_details(data)
    assert result == {"reading_watts": 70, "warning_watts": 896, "failure_watts": 980,
                      "instantaneous_watts": 83, "headroom_watts": 817, "capacity_watts": 900}
    assert "capacity_watts" not in manager.parse_power_details("Warning threshold: 896 W\nFailure threshold: 980 W")
    assert manager.parse_power_details(None) == {}
    assert "capacity_watts" not in manager.parse_power_details("Reading: 0 W\nSystem Instantaneous Headroom: 0 W")
    assert "capacity_watts" not in manager.parse_power_details("Reading: 70 BTU/hr\nSystem Instantaneous Headroom: 830 BTU/hr")


def test_optional_power_queries_use_discovered_sensor_and_read_only_headroom(monkeypatch):
    from unittest.mock import Mock, call
    manager = make_manager()
    output = "Sensor Reading : 70 (+/- 7) Watts\nUpper non-critical : 896.000\nUpper critical : 980.000"
    run = Mock(side_effect=[output, "53 00 07 00 00 00 00", "31 03 20 03"])
    monkeypatch.setattr(manager, "_run_ipmi_command", run)
    raw = manager.retrieve_power_details_raw("Pwr Consumption")
    assert run.call_args_list == [
        call(["sdr", "get", "Pwr Consumption"], is_raw_command=False, timeout=5),
        call(["0x30", "0xb3", "0x0a", "0x00"], timeout=3),
        call(["0x30", "0xbb"], timeout=3),
    ]
    assert manager.parse_power_details(raw) == {
        "reading_watts": 70, "warning_watts": 896, "failure_watts": 980,
        "instantaneous_watts": 83, "headroom_watts": 817, "capacity_watts": 900,
    }


def test_power_sensor_name_comes_from_real_sdr_inventory():
    manager = make_manager()
    assert manager.get_power_sensor_name(ELIST) == "Pwr Consumption"
    assert manager.get_power_sensor_name("System Level | 77h | ok | 7.1 | 70 Watts") == "System Level"
    assert manager.get_power_sensor_name("PS1 Input | 10h | ok | 10.1 | 70 Watts") is None
    assert manager.get_power_sensor_name(None) is None


def test_partial_power_results_survive_independent_query_failure(monkeypatch):
    from unittest.mock import Mock
    manager = make_manager()
    run = Mock(side_effect=["Upper non-critical : 896.000\nUpper critical : 980.000", None, None])
    monkeypatch.setattr(manager, "_run_ipmi_command", run)
    values = manager.parse_power_details(manager.retrieve_power_details_raw("Pwr Consumption"))
    assert values == {"warning_watts": 896, "failure_watts": 980}
    run.side_effect = [None, None, "3e 03 20 03"]
    assert manager.parse_power_details(manager.retrieve_power_details_raw("System Level")) == {"headroom_watts": 830}
    run.reset_mock()
    assert manager.retrieve_power_details_raw("invalid\nname") is None
    run.assert_not_called()


def test_malformed_headroom_does_not_invent_capacity(monkeypatch):
    from unittest.mock import Mock
    manager = make_manager()
    for raw in ("", "bad data", "3e 03", "3e 03 20 03 00"):
        monkeypatch.setattr(manager, "_run_ipmi_command", Mock(side_effect=["Upper critical : 980.000", None, raw]))
        assert manager.parse_power_details(manager.retrieve_power_details_raw("Pwr Consumption")) == {"failure_watts": 980}


def test_readable_battery_with_no_asserted_events_does_not_require_presence_text():
    manager = make_manager()
    for reading in ("", "0x00", "0x0000", "State Deasserted"):
        result = manager.parse_battery_status(f"CMOS Battery | 0Fh | ok | 23.1 | {reading}")
        assert result[0]["health"] == "No fault reported"
    assert manager.parse_battery_status("ROMB Battery | 10h | ns | 23.2 | No Reading")[0]["health"] == "Unknown"


def test_intrusion_is_decoded_from_assertions_not_generic_ok_status():
    manager = make_manager()
    for status, reading, expected in [
        ("ok", "", False), ("ok", "0x00", False), ("ok", "No Intrusion", False),
        ("ok", "Deasserted", False), ("ok", "General Chassis intrusion", True),
        ("ok", "Asserted", True), ("ns", "", None),
        ("ns", "General Chassis intrusion", None), ("ok", "Unknown event", None),
    ]:
        assert manager.parse_intrusion_status(f"Intrusion | 73h | {status} | 23.1 | {reading}") == {
            "name": "System Board Intrusion", "active": expected,
        }
    assert manager.parse_intrusion_status(ELIST) is None
    assert manager.parse_intrusion_status(None) is None


def test_psu_standard_assertions_and_unknown_readings_across_label_styles():
    for name in ("PS1 Status", "PSU1 Status", "Status"):
        for reading in ("Presence detected, Power Supply AC lost", "AC-Lost-Out-Range",
                        "Power Supply input lost (AC/DC)", "Predictive failure", "Configuration error"):
            row = f"{name} | 0Ah | ok | 10.1 | {reading}"
            assert make_manager().get_power_status(row) == [{"name": "PSU1", "ok": False}]
        assert make_manager().get_power_status(f"{name} | 0Ah | ns | 10.1 | No Reading") == [{"name": "PSU1", "ok": None}]
    data = "Status | 0Ah | ok | 10.1 | Presence detected\nPG Fail | 0Ch | nr | 10.1 |"
    assert make_manager().get_power_status(data) == [{"name": "PSU1", "ok": False}]
    # An unavailable PG sensor cannot establish that the PSU is healthy.
    assert make_manager().get_power_status(data.replace("nr", "ns")) == [{"name": "PSU1", "ok": None}]


def test_typed_inventory_classifies_unfamiliar_names_and_keeps_only_identities(monkeypatch):
    from unittest.mock import Mock, call
    manager = make_manager()
    rows = {
        "Battery": "Backup Cell | 0Fh | ok | 23.1 | Presence detected",
        "Power Supply": "Supply State | 0Ah | ok | 10.2 | Presence detected",
        "Physical Security": "Security | 73h | ok | 23.1 | General Chassis intrusion",
    }
    run = Mock(side_effect=lambda args, **kwargs: rows[args[2]])
    monkeypatch.setattr(manager, "_run_ipmi_command", run)
    for sensor_type in rows:
        manager.refresh_sensor_type(sensor_type)
    assert run.call_args_list == [call(["sdr", "type", kind], is_raw_command=False, timeout=5) for kind in rows]
    assert manager.parse_battery_status(rows["Battery"].replace("Presence detected", "Low"))[0]["health"] == "Low"
    assert manager.get_power_status(rows["Power Supply"].replace("Presence detected", "Power Supply AC lost")) == [{"name": "PSU2", "ok": False}]
    assert manager.parse_intrusion_status(rows["Physical Security"])["active"] is True
    assert manager.parse_battery_status(None) == []
    assert manager.get_power_status(None) == []
    assert manager.parse_intrusion_status(None) is None
    # Discovery values must never become cached healthy telemetry.
    unreadable = rows["Power Supply"].replace("ok", "ns").replace("Presence detected", "No Reading")
    assert manager.get_power_status(unreadable) == [{"name": "PSU2", "ok": None}]
    run.side_effect = None
    run.return_value = None
    manager.refresh_sensor_type("Battery")
    assert manager.parse_battery_status(rows["Battery"])[0]["health"] == "Healthy"
    run.return_value = ""
    manager.refresh_sensor_type("Battery")
    assert manager.parse_battery_status(rows["Battery"]) == []


def test_standard_battery_entity_and_generic_states():
    manager = make_manager()
    for reading, expected in (("Present", "Healthy"), ("State Asserted", "Warning"),
                              ("State Deasserted", "No fault reported")):
        assert manager.parse_battery_status(f"Cell | 0Fh | ok | 40.1 | {reading}")[0]["health"] == expected
    assert manager.parse_battery_status("Cell | 0Fh | ns | 40.1 | Present")[0]["health"] == "Unknown"


def test_system_power_selection_excludes_psu_input_and_ambiguous_board_sensors():
    manager = make_manager()
    psu = "PS1 Input | 10h | ok | 10.1 | 180 Watts"
    system = "Total Draw | 77h | ok | 7.1 | 126 Watts"
    assert manager.get_power_sensor_name(psu + "\n" + system) is None
    assert manager.parse_power_consumption(psu + "\n" + system) is None
    assert manager.get_power_sensor_name(psu) is None
    assert manager.get_power_sensor_name(system + "\n" + system.replace("Total Draw", "Other Draw")) is None
    assert manager.get_power_sensor_name(system.replace("ok", "ns")) is None
    for alias in ("System Level", "Pwr Consumption"):
        row = f"{alias} | 77h | ok | 7.1 | 70 Watts"
        assert manager.parse_power_consumption(row) == 70
        assert manager.get_power_sensor_name(psu + "\n" + row) == alias


def test_oem_power_lengths_and_partial_support_are_independent(monkeypatch):
    from unittest.mock import Mock
    manager = make_manager()
    for raw in (None, "", "bad", "53 00", "53 00 00 00 00 00", "ff ff 00 00 00 00 00"):
        monkeypatch.setattr(manager, "_run_ipmi_command", Mock(side_effect=[None, raw, "31 03 20 03"]))
        values = manager.parse_power_details(manager.retrieve_power_details_raw("Total Draw"))
        assert values == {"headroom_watts": 817}
    monkeypatch.setattr(manager, "_run_ipmi_command", Mock(side_effect=[None, "53 00 07 00 00 00 00", None]))
    assert manager.parse_power_details(manager.retrieve_power_details_raw("System Level")) == {"instantaneous_watts": 83}
    assert "capacity_watts" not in manager.parse_power_details("Reading: 70 W\nSystem Instantaneous Headroom: 817 W")


def test_standard_processor_and_air_inlet_entities_do_not_require_dell_labels():
    data = "Socket | 01h | ucr | 3.2 | 95 degrees C\nAmbient | 02h | ok | 55.1 | 24 degrees C\nDIMM | 03h | ok | 32.1 | 50 degrees C"
    result = make_manager().parse_temperatures(data)
    assert result["cpu_ids"] == [2]
    assert result["cpu_temps"] == [95]
    assert result["inlet_temp"] == 24
    assert make_manager().parse_temperatures(data.replace("ucr", "ns"))["cpu_temps"] == []


def test_physical_security_type_does_not_turn_unrelated_security_alarm_into_intrusion(monkeypatch):
    from unittest.mock import Mock
    manager = make_manager()
    row = "Security | 73h | ok | 23.1 | LAN Leash lost"
    monkeypatch.setattr(manager, "_run_ipmi_command", Mock(return_value=row))
    manager.refresh_sensor_type("Physical Security")
    assert manager.parse_intrusion_status(row) is None


def test_duplicate_sensor_names_do_not_select_psu_input_as_system_power():
    data = "Total Draw | 10h | ok | 10.1 | 180 Watts\nTotal Draw | 77h | ok | 7.1 | 126 Watts"
    manager = make_manager()
    assert manager.get_power_sensor_name(data) is None
    assert manager.parse_power_consumption(data) is None


def test_unavailable_fan_reading_is_not_a_valid_speed():
    assert make_manager().parse_fan_rpms("Fan1 | 30h | ns | 7.1 | 0 RPM") == []
    assert make_manager().parse_fan_rpms("Fan1 | 30h | lcr | 7.1 | 0 RPM") == [{"name": "Fan1", "rpm": 0}]


def test_dell_total_power_and_headroom_do_not_depend_on_sensor_labels(monkeypatch):
    from unittest.mock import Mock, call
    manager = make_manager()
    run = Mock(side_effect=["53 00 07 00 00 00 00", "31 03 20 03"])
    monkeypatch.setattr(manager, "_run_ipmi_command", run)
    values = manager.parse_power_details(manager.retrieve_power_details_raw(None))
    assert values == {"instantaneous_watts": 83, "headroom_watts": 817, "capacity_watts": 900}
    assert run.call_args_list == [call(["0x30", "0xb3", "0x0a", "0x00"], timeout=3),
                                 call(["0x30", "0xbb"], timeout=3)]
