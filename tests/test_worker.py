"""Worker integration tests; all IPMI and MQTT I/O is mocked."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

with patch("signal.signal"):
    from app import main


CPU_COOL = "CPU1 Temp | 01h | ok | 3.1 | 30 degrees C"
CPU_WARM = "CPU1 Temp | 01h | ok | 3.1 | 50 degrees C"
CPU_HOT = "CPU1 Temp | 01h | ok | 3.1 | 65 degrees C"


@pytest.fixture
def make_worker(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(main, "running", True)
    monkeypatch.setattr(main, "PID_STATE_FILE", str(tmp_path / "pid.json"))
    monkeypatch.setattr(main, "ALL_SERVERS_STATUS", {})

    def create(**overrides):
        config = {
            "alias": "fixture", "idrac_ip": "127.0.0.1",
            "idrac_username": "fixture", "idrac_password": "fixture",
            "fan_control_enabled": True, "fan_mode": "simple",
        }
        config.update(overrides)
        opts = {
            "log_level": "fatal", "mqtt_host": "", "mqtt_port": 1883,
            "mqtt_username": "", "mqtt_password": "",
            "check_interval_seconds": 30,
        }
        item = main.ServerWorker(config, opts)
        item._log = Mock()
        item.ipmi.retrieve_temperatures_raw = Mock(return_value=CPU_COOL)
        item.ipmi.retrieve_fan_rpms_raw = Mock(return_value="")
        item.ipmi.retrieve_power_sdr_raw = Mock(return_value="")
        item.ipmi.retrieve_power_details_raw = Mock(return_value=None)
        item.ipmi.refresh_sensor_type = Mock()
        item.ipmi.apply_user_fan_control_profile = Mock(return_value="")
        item.ipmi.apply_dell_fan_control_profile = Mock(return_value="")
        item.ipmi.get_server_model_info = Mock(return_value={})
        item.ipmi.get_firmware_version = Mock(return_value=None)
        item.ipmi.chassis_shutdown = Mock(return_value="accepted")
        item.ipmi.chassis_power_on = Mock(return_value="accepted")
        item.ipmi.chassis_hard_reset = Mock(return_value="accepted")
        item.ipmi.chassis_power_off = Mock(return_value="accepted")
        item.ipmi.chassis_power_cycle = Mock(return_value="accepted")
        item.mqtt.publish = Mock()
        item.mqtt.connect = Mock()
        item.mqtt.subscribe = Mock()
        item._publish_mqtt_data = Mock()
        return item
    return create


def run_cycles(item, count=1):
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= count:
            item.stop()
    item._sleep = sleep
    item._run_loop()
    return sleeps


def test_temperature_poll_failure_restores_auto_and_clears_stale_status(make_worker):
    item = make_worker()
    item.ipmi.retrieve_temperatures_raw.side_effect = [CPU_COOL, None]
    run_cycles(item, 2)
    item.ipmi.apply_user_fan_control_profile.assert_called_once_with(20)
    item.ipmi.apply_dell_fan_control_profile.assert_called_once()
    status = main.ALL_SERVERS_STATUS[item.alias]
    assert status["hottest_cpu_temp_c"] is None
    assert status["cpu_temps_c"] == []
    assert status["actual_fan_rpms"] == []
    assert status["target_fan_speed_percent"] == "Dell Auto (temperature read failed)"


def test_failed_handback_is_reported_and_retried_on_next_failed_poll(make_worker):
    item = make_worker()
    item.ipmi.retrieve_temperatures_raw.return_value = None
    item.ipmi.apply_dell_fan_control_profile.return_value = None
    item.mqtt.is_connected = True
    run_cycles(item, 2)
    assert item.ipmi.apply_dell_fan_control_profile.call_count == 2
    status = main.ALL_SERVERS_STATUS[item.alias]["target_fan_speed_percent"]
    assert "Unknown" in status
    assert "failed" in status
    assert any(call.args[0] == "error" for call in item._log.call_args_list)
    item.mqtt.publish.assert_called_with(item.mqtt.availability_topic, "offline", retain=True)


@pytest.mark.parametrize("temperature_data", [None, "", CPU_COOL])
def test_monitor_only_never_writes_fan_settings(make_worker, temperature_data):
    item = make_worker(fan_control_enabled=False, fan_mode="curve", fan_curve=[])
    item.ipmi.retrieve_temperatures_raw.return_value = temperature_data
    run_cycles(item)
    item.ipmi.apply_user_fan_control_profile.assert_not_called()
    item.ipmi.apply_dell_fan_control_profile.assert_not_called()
    assert main.ALL_SERVERS_STATUS[item.alias]["target_fan_speed_percent"] == "Monitor only"


@pytest.mark.parametrize("curve", [
    [],
    [{"temp": 40, "speed": 20}],
    [{"temp": 40, "speed": 20}, {"temp": 40, "speed": 50}],
    [{"temp": 40, "speed": 0}, {"temp": 60, "speed": 50}],
    [{"temp": "bad", "speed": 20}, {"temp": 60, "speed": 50}],
])
def test_invalid_persisted_curve_explicitly_restores_auto(make_worker, curve):
    item = make_worker(fan_mode="curve", fan_curve=curve)
    run_cycles(item)
    item.ipmi.apply_user_fan_control_profile.assert_not_called()
    item.ipmi.apply_dell_fan_control_profile.assert_called_once()
    assert main.ALL_SERVERS_STATUS[item.alias]["target_fan_speed_percent"] == "Dell Auto (invalid fan curve)"


def test_invalid_curve_handback_failure_is_not_reported_as_auto(make_worker):
    item = make_worker(fan_mode="curve", fan_curve=[])
    item.ipmi.apply_dell_fan_control_profile.return_value = None
    run_cycles(item)
    assert "Unknown" in main.ALL_SERVERS_STATUS[item.alias]["target_fan_speed_percent"]


def test_valid_curve_still_interpolates(make_worker):
    item = make_worker(fan_mode="curve", fan_curve=[
        {"temp": 60, "speed": 70}, {"temp": 45, "speed": 40},
    ])
    item.ipmi.retrieve_temperatures_raw.return_value = CPU_WARM
    run_cycles(item)
    item.ipmi.apply_user_fan_control_profile.assert_called_once_with(50)
    item.ipmi.apply_dell_fan_control_profile.assert_not_called()


@pytest.mark.parametrize("temperature_data", ["", CPU_HOT])
def test_missing_cpu_and_critical_handback_failures_are_not_reported_as_auto(make_worker, temperature_data):
    item = make_worker()
    item.ipmi.retrieve_temperatures_raw.return_value = temperature_data
    item.ipmi.apply_dell_fan_control_profile.return_value = None
    run_cycles(item)
    assert "Unknown" in main.ALL_SERVERS_STATUS[item.alias]["target_fan_speed_percent"]


def test_shutdown_handler_is_ready_before_connect_and_dispatches_only_press(make_worker):
    item = make_worker(allow_shutdown=True)
    item.global_opts["mqtt_host"] = "fixture"
    item.mqtt.wait_connected = Mock(return_value=True)
    item.discovered_sensors.add("old")

    def connect():
        assert item.mqtt.message_callback is not None
        assert item.mqtt.on_connect_callback is not None
        item.mqtt.on_connect_callback()
    item.mqtt.connect.side_effect = connect
    item._initialize()
    topic = f"{item.mqtt.base_topic}/command/shutdown"
    assert [call.args[0] for call in item.mqtt.subscribe.call_args_list
            if "/command/" in call.args[0]] == [topic]
    assert not item.discovered_sensors
    item.mqtt._on_message(None, None, SimpleNamespace(topic=topic + "/other", payload=b"PRESS"))
    item.mqtt._on_message(None, None, SimpleNamespace(topic=topic, payload=b"other"))
    item.ipmi.chassis_shutdown.assert_not_called()
    item.mqtt._on_message(None, None, SimpleNamespace(topic=topic, payload=b"PRESS"))
    item.ipmi.chassis_shutdown.assert_called_once_with()


def test_late_mqtt_service_discovery_does_not_stop_ipmi(make_worker, monkeypatch):
    item = make_worker()
    settings = {"host": "core-mosquitto", "port": 1883,
                "username": "fixture-service", "password": "fixture-password", "ssl": False}
    resolver = Mock(side_effect=[None, settings])
    monkeypatch.setattr(main, "resolve_mqtt_config", resolver)
    cycles = []

    def sleep(_):
        cycles.append(True)
        item._next_mqtt_attempt = 0
        if len(cycles) == 2:
            item.stop()
    item._sleep = sleep
    item._run_loop()
    assert item.ipmi.retrieve_temperatures_raw.call_count == 2
    assert item.ipmi.apply_user_fan_control_profile.call_count == 2
    item.mqtt.connect.assert_called_once()
    assert item.mqtt.username == "fixture-service"
    assert item.global_opts["mqtt_password"] == ""


def test_successful_settings_are_cached_and_tls_is_only_configured_once(make_worker, monkeypatch):
    item = make_worker()
    settings = {"host": "core-mosquitto", "port": 8883,
                "username": "fixture-service", "password": "fixture-password", "ssl": True}
    resolver = Mock(return_value=settings)
    monkeypatch.setattr(main, "resolve_mqtt_config", resolver)
    item.mqtt.client.tls_set = Mock()
    item._try_mqtt_connect()
    item._try_mqtt_connect()
    resolver.assert_called_once()
    item.mqtt.client.tls_set.assert_called_once()
    assert item.mqtt.connect.call_count == 2


def test_tls_configuration_failure_does_not_stop_ipmi(make_worker, monkeypatch):
    item = make_worker()
    monkeypatch.setattr(main, "resolve_mqtt_config", Mock(return_value={
        "host": "core-mosquitto", "port": 8883,
        "username": "", "password": "", "ssl": True,
    }))
    item.mqtt.client.tls_set = Mock(side_effect=OSError("fixture-private-detail"))
    run_cycles(item)
    item.ipmi.retrieve_temperatures_raw.assert_called_once()
    item.mqtt.connect.assert_not_called()
    assert "fixture-private-detail" not in str(item._log.call_args_list)


@pytest.mark.parametrize("mode", ["auto", None, "unsupported"])
@pytest.mark.parametrize("temperature_data", [CPU_COOL, CPU_HOT, "", None])
def test_dell_auto_never_applies_custom_speeds_or_thresholds(make_worker, mode, temperature_data):
    item = make_worker(fan_mode=mode, base_fan_speed_percent=100,
                       low_temp_threshold=0, high_temp_fan_speed_percent=100,
                       critical_temp_threshold=0)
    item.ipmi.retrieve_temperatures_raw.return_value = temperature_data
    run_cycles(item)
    item.ipmi.apply_user_fan_control_profile.assert_not_called()
    item.ipmi.apply_dell_fan_control_profile.assert_called_once()
    assert main.ALL_SERVERS_STATUS[item.alias]["target_fan_speed_percent"].startswith("Dell Auto")


def test_missing_cooling_mode_defaults_to_auto(make_worker):
    item = make_worker()
    del item.config["fan_mode"]
    item = main.ServerWorker(item.config, item.global_opts)
    item.ipmi.retrieve_temperatures_raw = Mock(return_value=CPU_COOL)
    item.ipmi.retrieve_fan_rpms_raw = Mock(return_value="")
    item.ipmi.retrieve_power_sdr_raw = Mock(return_value="")
    item.ipmi.apply_dell_fan_control_profile = Mock(return_value="")
    item.ipmi.apply_user_fan_control_profile = Mock()
    item._publish_mqtt_data = Mock()
    run_cycles(item)
    item.ipmi.apply_dell_fan_control_profile.assert_called_once()
    item.ipmi.apply_user_fan_control_profile.assert_not_called()


def test_auto_handback_happens_before_slow_initialization_and_leaves_pcie_setting(make_worker):
    item = make_worker(fan_mode="auto", disable_pcie_cooling_response=True)
    events = []
    item.ipmi.apply_dell_fan_control_profile.side_effect = lambda: events.append("auto") or ""
    item.ipmi.get_server_model_info.side_effect = lambda: events.append("fru") or {}
    item.ipmi.get_firmware_version.side_effect = lambda: events.append("firmware")
    item.ipmi.set_third_party_pcie_cooling_response = Mock()
    item._initialize()
    assert events == ["auto", "fru", "firmware"]
    item.ipmi.set_third_party_pcie_cooling_response.assert_not_called()
    item.ipmi.apply_user_fan_control_profile.assert_not_called()


def test_monitor_only_auto_mode_is_read_only_including_startup_and_cleanup(make_worker):
    item = make_worker(fan_mode="auto", fan_control_enabled=False,
                       disable_pcie_cooling_response=True)
    item.ipmi.set_third_party_pcie_cooling_response = Mock()
    item._sleep = lambda _: item.stop()
    item.run()
    item.ipmi.apply_dell_fan_control_profile.assert_not_called()
    item.ipmi.apply_user_fan_control_profile.assert_not_called()
    item.ipmi.set_third_party_pcie_cooling_response.assert_not_called()


def test_auto_handback_failure_reports_unknown_and_retries(make_worker):
    item = make_worker(fan_mode="auto")
    item.ipmi.apply_dell_fan_control_profile.side_effect = [None, ""]
    states = []

    def sleep(_):
        states.append(main.ALL_SERVERS_STATUS[item.alias]["target_fan_speed_percent"])
        if len(states) == 2:
            item.stop()
    item._sleep = sleep
    item._run_loop()
    assert states == ["Unknown (Dell auto hand-back failed)", "Dell Auto"]
    item.ipmi.apply_user_fan_control_profile.assert_not_called()


@pytest.mark.parametrize("mode", ["simple", "curve", "target"])
def test_existing_explicit_custom_modes_remain_active(make_worker, mode):
    item = make_worker(fan_mode=mode, fan_curve=[
        {"temp": 20, "speed": 20}, {"temp": 60, "speed": 60},
    ])
    item._initialize()
    # Ensure elapsed time even on Windows clocks with coarse resolution.
    item.pid.last_time -= 1
    run_cycles(item)
    item.ipmi.apply_user_fan_control_profile.assert_called_once()
    item.ipmi.apply_dell_fan_control_profile.assert_not_called()
    assert item.config["fan_mode"] == mode


@pytest.mark.parametrize("value", [None, False, "false", "true", 1, [], {}])
def test_power_actions_require_boolean_true_not_truthy_config(make_worker, value):
    item = make_worker(**{action["option"]: value for action in main.POWER_ACTIONS.values()})
    item._initialize()
    item._on_mqtt_connected()
    assert not any("/command/" in call.args[0] for call in item.mqtt.subscribe.call_args_list)
    for action in main.POWER_ACTIONS:
        item._on_mqtt_message(f"{item.mqtt.base_topic}/command/{action}", "PRESS")
    item.ipmi.chassis_shutdown.assert_not_called()
    item.ipmi.chassis_hard_reset.assert_not_called()
    item.ipmi.chassis_power_on.assert_not_called()
    configs = [(call.args[0], call.args[1]) for call in item.mqtt.publish.call_args_list
               if call.args[0].startswith("homeassistant/button/")]
    assert configs == [
        (f"homeassistant/button/idrac_controller_fixture_{action}_button/config", "")
        for action in main.POWER_ACTIONS
    ]


@pytest.mark.parametrize("action", list(main.POWER_ACTIONS))
def test_only_allowed_power_action_is_discovered_subscribed_and_dispatched(make_worker, action):
    import json
    description = main.POWER_ACTIONS[action]
    item = make_worker(**{description["option"]: True})
    item._initialize()
    item._on_mqtt_connected()
    topic = f"{item.mqtt.base_topic}/command/{action}"
    assert [call.args[0] for call in item.mqtt.subscribe.call_args_list
            if "/command/" in call.args[0]] == [topic]
    configs = [json.loads(call.args[1]) for call in item.mqtt.publish.call_args_list
               if call.args[0].startswith("homeassistant/button/") and call.args[1]]
    assert len(configs) == 1
    assert configs[0]["name"] == description["name"]
    assert configs[0]["entity_category"] == "config"
    assert configs[0]["retain"] is False
    assert configs[0].get("device_class") == ("restart" if action in ("reboot", "power_cycle") else None)
    for other in main.POWER_ACTIONS:
        if other != action:
            item._on_mqtt_message(f"{item.mqtt.base_topic}/command/{other}", "PRESS")
    for info in main.POWER_ACTIONS.values():
        getattr(item.ipmi, info["method"]).assert_not_called()
    item._on_mqtt_message(topic, "PRESS")
    getattr(item.ipmi, description["method"]).assert_called_once_with()
    # Duplicate clicks/messages must not restart or shut down the server again.
    item._on_mqtt_message(topic, "PRESS")
    getattr(item.ipmi, description["method"]).assert_called_once_with()


def test_reconnect_removes_legacy_button_even_when_telemetry_fails(make_worker):
    item = make_worker()
    item._initialize()
    item.mqtt.publish.reset_mock()
    item._on_mqtt_connected()
    item._on_mqtt_connected()
    legacy = "homeassistant/button/idrac_controller_fixture_shutdown_button/config"
    calls = [call for call in item.mqtt.publish.call_args_list if call.args[0] == legacy]
    assert len(calls) == 2
    assert all(call.args[1] == "" and call.kwargs["retain"] is True for call in calls)
    assert not any("/command/" in call.args[0] for call in item.mqtt.subscribe.call_args_list)
    item.ipmi.chassis_shutdown.assert_not_called()


def test_stale_retained_and_invalid_commands_do_not_reach_power_backend(make_worker):
    item = make_worker(allow_shutdown=True)
    item._initialize()
    topic = f"{item.mqtt.base_topic}/command/shutdown"
    item.mqtt._on_message(None, None, SimpleNamespace(topic=topic, payload=b"PRESS", retain=True))
    item.mqtt._on_message(None, None, SimpleNamespace(topic=topic, payload=bytes([255]), retain=False))
    item.ipmi.chassis_shutdown.assert_not_called()
    item.mqtt._on_message(None, None, SimpleNamespace(topic=topic, payload=b"PRESS", retain=False))
    item.ipmi.chassis_shutdown.assert_called_once_with()


def test_retained_command_cleared_before_subscribing_and_not_executed(make_worker):
    item = make_worker(allow_shutdown=True)
    item._initialize()
    events = []
    item.mqtt.publish.side_effect = lambda topic, payload, **kw: events.append(("publish", topic, payload))
    item.mqtt.subscribe.side_effect = lambda topic: events.append(("subscribe", topic))
    item._on_mqtt_connected()
    topic = f"{item.mqtt.base_topic}/command/shutdown"
    assert events.index(("publish", topic, "")) < events.index(("subscribe", topic))
    item.ipmi.chassis_shutdown.assert_not_called()


def test_stopping_worker_rejects_power_commands(make_worker):
    item = make_worker(allow_shutdown=True)
    item.stop()
    item._on_mqtt_message(f"{item.mqtt.base_topic}/command/shutdown", "PRESS")
    item.ipmi.chassis_shutdown.assert_not_called()


def test_failed_power_command_is_not_automatically_retried(make_worker):
    item = make_worker(allow_reboot=True)
    item.ipmi.chassis_hard_reset.return_value = None
    topic = f"{item.mqtt.base_topic}/command/reboot"
    item._on_mqtt_message(topic, "PRESS")
    item._on_mqtt_message(topic, "PRESS")
    item.ipmi.chassis_hard_reset.assert_called_once_with()
    assert any(call.args[0] == "error" for call in item._log.call_args_list)


def test_auto_cooling_status_publishes_text_without_inventing_fan_percentage(make_worker):
    import json
    item = make_worker(fan_mode="auto")
    del item._publish_mqtt_data  # Exercise real discovery and state publishing.
    item.mqtt.is_connected = True
    item._update_status({"cpu_temps": [35, 32], "inlet_temp": 24}, [], 70, [], "Dell Auto")
    payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
    assert json.loads(payloads[f"{item.mqtt.base_topic}/sensor/cooling_mode"])["state"] == "Dell Auto"
    assert json.loads(payloads[f"{item.mqtt.base_topic}/sensor/target_fan_speed"])["state"] is None
    assert not any(topic.startswith("homeassistant/button/") for topic in payloads)



def test_cpu_labels_change_without_changing_unique_ids_or_socket_assignment(make_worker):
    import json
    item = make_worker()
    item._initialize()
    del item._publish_mqtt_data
    item.mqtt.is_connected = True
    # Reverse SDR order must still publish each value to its original socket ID.
    item._update_status({"cpu_temps": [32, 35], "cpu_ids": [2, 1]}, [], 70, [], "Dell Auto")
    payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
    for index, expected in [(0, 35), (1, 32)]:
        discovery = json.loads(payloads[f"homeassistant/sensor/idrac_controller_fixture_cpu_{index}_temp/config"])
        assert discovery["name"] == f"CPU{index + 1} Temperature"
        assert discovery["unique_id"] == f"idrac_controller_fixture_cpu_{index}_temp"
        assert json.loads(payloads[f"{item.mqtt.base_topic}/sensor/cpu_{index}_temp"])["state"] == expected
    item.mqtt.publish.reset_mock()
    item._update_status({"cpu_temps": [32], "cpu_ids": [2]}, [], 70, [], "Dell Auto")
    payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
    assert json.loads(payloads[f"{item.mqtt.base_topic}/sensor/cpu_0_temp"])["state"] is None
    assert json.loads(payloads[f"{item.mqtt.base_topic}/sensor/cpu_1_temp"])["state"] == 32


def test_battery_discovery_is_optional_and_failed_reads_clear_stale_health(make_worker):
    import json
    item = make_worker(include_advanced_diagnostics=True)
    item._initialize()
    del item._publish_mqtt_data
    item.mqtt.is_connected = True
    temperatures = {"cpu_temps": [35]}
    item._update_status(temperatures, [], 70, [], "Dell Auto")
    assert not any("/idrac_controller_fixture_battery_" in call.args[0] for call in item.mqtt.publish.call_args_list)
    battery = {"name": "CMOS Battery", "health": "Healthy", "voltage": 3.1}
    item._update_status(temperatures, [], 70, [], "Dell Auto", [battery])
    payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
    slug = "battery_cmos_battery"
    health = json.loads(payloads[f"homeassistant/sensor/idrac_controller_fixture_{slug}_health/config"])
    voltage = json.loads(payloads[f"homeassistant/sensor/idrac_controller_fixture_{slug}_voltage/config"])
    assert health["entity_category"] == "diagnostic"
    assert "unit_of_measurement" not in health
    assert "device_class" not in health  # HA battery class means charge percentage.
    assert voltage["device_class"] == "voltage" and voltage["unit_of_measurement"] == "V"
    assert main.ALL_SERVERS_STATUS[item.alias]["batteries"] == [battery]
    item.mqtt.publish.reset_mock()
    item._update_status(temperatures, [], 70, [], "Dell Auto", [])
    payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
    assert json.loads(payloads[f"{item.mqtt.base_topic}/sensor/{slug}_health"])["state"] == "Unknown"
    assert json.loads(payloads[f"{item.mqtt.base_topic}/sensor/{slug}_voltage"])["state"] is None


def test_discrete_battery_health_does_not_invent_voltage(make_worker):
    item = make_worker()
    item._initialize()
    del item._publish_mqtt_data
    item.mqtt.is_connected = True
    item._update_status({"cpu_temps": [35]}, [], 70, [], "Dell Auto", [
        {"name": "CMOS Battery", "health": "Healthy", "voltage": None},
    ])
    topics = [call.args[0] for call in item.mqtt.publish.call_args_list]
    assert any("battery_cmos_battery_health" in topic for topic in topics)
    assert not any("battery_cmos_battery_voltage" in topic for topic in topics)


POWER_DETAILS = """System Board System Level
Reading : 70 W
Warning threshold : 896 W
Failure threshold : 980 W
Amperage value: 0.6 A
Headroom
Instantaneous power : 83 W
System Instantaneous Headroom : 817 W
System Peak Headroom : 800 W
"""


def test_power_capacity_uses_headroom_not_thresholds_and_publishes_separate_sensors(make_worker):
    import json
    item = make_worker(include_advanced_diagnostics=True)
    item._initialize()
    del item._publish_mqtt_data
    item.mqtt.is_connected = True
    item._power_details = item.ipmi.parse_power_details(POWER_DETAILS)
    item._update_status({"cpu_temps": [35]}, [], 70, [], "Dell Auto")
    payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
    for slug, value in [("power_capacity_percent", 7.78), ("power_capacity_watts", 900),
                        ("power_warning_watts", 896), ("power_failure_watts", 980)]:
        assert json.loads(payloads[f"{item.mqtt.base_topic}/sensor/{slug}"])["state"] == value
    assert main.ALL_SERVERS_STATUS[item.alias]["power_capacity_percent"] == 7.78
    item._power_details = {}  # Failed/unsupported refresh must clear previous values.
    item.mqtt.publish.reset_mock()
    item._update_status({"cpu_temps": [35]}, [], 70, [], "Dell Auto")
    payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
    assert json.loads(payloads[f"{item.mqtt.base_topic}/sensor/power_capacity_percent"])["state"] is None
    assert json.loads(payloads[f"{item.mqtt.base_topic}/sensor/power_warning_watts"])["state"] is None


def test_optional_power_poll_is_throttled_and_runs_after_cooling(make_worker, monkeypatch):
    item = make_worker(fan_mode="auto")
    events = []
    item.ipmi.apply_dell_fan_control_profile.side_effect = lambda: events.append("cooling") or ""
    sdr_data = "Pwr Consumption | 77h | ok | 7.1 | 70 Watts"
    item.ipmi.retrieve_power_sdr_raw.return_value = sdr_data
    item.ipmi.retrieve_power_details_raw.side_effect = lambda name: events.append("power-read") or POWER_DETAILS
    clock = Mock(return_value=1000)
    monkeypatch.setattr(main.time, "monotonic", clock)
    run_cycles(item)
    assert events == ["cooling", "power-read"]
    assert item._power_details["capacity_watts"] == 900
    item._refresh_power_details(sdr_data)
    assert item.ipmi.retrieve_power_details_raw.call_count == 1
    clock.return_value = 1061
    item.ipmi.retrieve_power_details_raw.side_effect = None
    item.ipmi.retrieve_power_details_raw.return_value = None
    item._refresh_power_details(sdr_data)
    assert item.ipmi.retrieve_power_details_raw.call_count == 2
    assert item._power_details == {"reading_watts": 70}


def test_intrusion_publishes_tamper_state_and_unavailable_when_reading_missing(make_worker):
    import json
    item = make_worker()
    item._initialize()
    del item._publish_mqtt_data
    item.mqtt.is_connected = True
    temperatures = {"cpu_temps": [35]}
    item._update_status(temperatures, [], 70, [], "Dell Auto")
    assert not any("system_board_intrusion/config" in call.args[0] for call in item.mqtt.publish.call_args_list)
    for active in (False, True):
        item._update_status(temperatures, [], 70, [], "Dell Auto", intrusion={"active": active})
        payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
        assert payloads[f"{item.mqtt.base_topic}/binary_sensor/system_board_intrusion"] == ("ON" if active else "OFF")
        assert payloads[f"{item.mqtt.base_topic}/intrusion/availability"] == "online"
    discovery = json.loads(payloads["homeassistant/binary_sensor/idrac_controller_fixture_system_board_intrusion/config"])
    assert discovery["device_class"] == "tamper"
    assert discovery["availability"][-1]["topic"] == f"{item.mqtt.base_topic}/intrusion/availability"
    assert discovery["availability_mode"] == "all"
    item.mqtt.publish.reset_mock()
    item._update_status(temperatures, [], 70, [], "Dell Auto", intrusion=None)
    payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
    assert payloads[f"{item.mqtt.base_topic}/intrusion/availability"] == "offline"
    assert f"{item.mqtt.base_topic}/binary_sensor/system_board_intrusion" not in payloads
    assert main.ALL_SERVERS_STATUS[item.alias]["intrusion_active"] is None



def test_headroom_does_not_use_quantized_sdr_reading_as_capacity_input(make_worker):
    item = make_worker()
    item.ipmi.retrieve_power_details_raw.return_value = "System Instantaneous Headroom: 830 W"
    item._refresh_power_details("Pwr Consumption | 77h | ok | 7.1 | 70 Watts")
    item.ipmi.retrieve_power_details_raw.assert_called_once_with("Pwr Consumption")
    assert item._power_details == {"headroom_watts": 830, "reading_watts": 70}


def test_missing_power_sensor_still_reads_oem_data_without_guessing_a_name(make_worker):
    item = make_worker()
    item._refresh_power_details("CPU1 Temp | 01h | ok | 3.1 | 35 degrees C")
    item.ipmi.retrieve_power_details_raw.assert_called_once_with(None)
    assert item._power_details == {}


def test_psu_fault_discovery_and_unavailable_on_missing_reading(make_worker):
    import json
    item = make_worker()
    item._initialize()
    del item._publish_mqtt_data
    item.mqtt.is_connected = True
    temperatures = {"cpu_temps": [35]}
    for ok in (False, True):
        item._update_status(temperatures, [], 70, [{"name": "PSU1", "ok": ok}], "Dell Auto")
        payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
        assert payloads[f"{item.mqtt.base_topic}/binary_sensor/psu_psu1"] == ("OFF" if ok else "ON")
        assert payloads[f"{item.mqtt.base_topic}/psu/PSU1/availability"] == "online"
    discovery = json.loads(payloads["homeassistant/binary_sensor/idrac_controller_fixture_psu_psu1/config"])
    assert discovery["device_class"] == "problem"
    assert discovery["availability"][-1]["topic"] == f"{item.mqtt.base_topic}/psu/PSU1/availability"
    for psus in ([{"name": "PSU1", "ok": None}], []):
        item.mqtt.publish.reset_mock()
        item._update_status(temperatures, [], 70, psus, "Dell Auto")
        payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
        assert payloads[f"{item.mqtt.base_topic}/psu/PSU1/availability"] == "offline"
        assert f"{item.mqtt.base_topic}/binary_sensor/psu_psu1" not in payloads
        assert main.ALL_SERVERS_STATUS[item.alias]["psu_statuses"][0]["ok"] is None
    item.mqtt.publish.reset_mock()
    item._on_mqtt_connected()
    item.mqtt.publish.assert_any_call(f"{item.mqtt.base_topic}/psu/PSU1/availability", "offline", retain=True)


def test_type_discovery_is_throttled_and_rotates_standard_classes_after_cooling(make_worker, monkeypatch):
    item = make_worker(fan_mode="auto")
    events = []
    item.ipmi.apply_dell_fan_control_profile.side_effect = lambda: events.append("cooling") or ""
    item.ipmi.refresh_sensor_type.side_effect = lambda kind: events.append(kind)
    clock = Mock(return_value=1000)
    monkeypatch.setattr(main.time, "monotonic", clock)
    run_cycles(item)
    assert events == ["cooling", "Battery"]
    item._refresh_sensor_inventory()
    assert events == ["cooling", "Battery"]
    for now in (1060, 1120, 1180):
        clock.return_value = now
        item._refresh_sensor_inventory()
    assert events == ["cooling", "Battery", "Power Supply", "Physical Security", "Battery"]


def test_worker_reads_total_power_with_unfamiliar_sdr_labels(make_worker):
    item = make_worker(fan_control_enabled=False)
    item.ipmi.retrieve_power_sdr_raw.return_value = "Component Power | 77h | ok | 7.1 | 126 Watts"
    item.ipmi.retrieve_power_details_raw.return_value = "Instantaneous power: 83 W\nSystem Instantaneous Headroom: 817 W"
    run_cycles(item)
    item.ipmi.retrieve_power_details_raw.assert_called_once_with(None)
    assert main.ALL_SERVERS_STATUS[item.alias]["power_consumption_watts"] == 83
    assert main.ALL_SERVERS_STATUS[item.alias]["power_capacity_watts"] == 900
    item.ipmi.apply_dell_fan_control_profile.assert_not_called()
    item.ipmi.apply_user_fan_control_profile.assert_not_called()


def test_all_available_extra_readings_publish_and_failures_clear_availability(make_worker):
    import json
    item = make_worker(fan_control_enabled=False, include_advanced_diagnostics=True)
    rows = "DIMM Temp | 20h | ok | 32.1 | 40 degrees C\nPS1 Voltage | 21h | ok | 10.1 | 0 Volts\nPS1 Current | 22h | ok | 10.1 | 0.1 Amps\nPS1 Status | 23h | ok | 10.1 | Power Supply AC lost\nUnused | 24h | ns | 7.1 | No Reading"
    item._initialize()
    del item._publish_mqtt_data
    item.mqtt.is_connected = True
    readings = item._additional_readings(rows)
    item._update_status({"cpu_temps": [35]}, [], 70, [], "Monitor only", readings=readings)
    payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
    topic = "homeassistant/sensor/idrac_controller_fixture_ipmi_21_10_1/config"
    config = json.loads(payloads[topic])
    assert config["device_class"] == "voltage"
    assert config["expire_after"] == 180
    assert config["unit_of_measurement"] == "V"
    assert config["json_attributes_topic"] == f"{item.mqtt.base_topic}/sensor/ipmi_21_10_1"
    state = json.loads(payloads[config["state_topic"]])
    assert state["state"] == 0
    assert state["attributes"] == {"ipmi_status": "ok", "entity": "10.1", "sensor_number": "0x21", "source": "IPMI SDR", "diagnostic_tier": "advanced"}
    assert not any("ipmi_24_" in topic for topic in payloads)
    item.mqtt.publish.reset_mock()
    item._update_status({"cpu_temps": [35]}, [], 70, [], "Monitor only")
    payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
    for reading in readings:
        assert payloads[f"{item.mqtt.base_topic}/reading/{reading['slug']}/availability"] == "offline"
        assert f"{item.mqtt.base_topic}/sensor/{reading['slug']}" not in payloads
    item.mqtt.publish.reset_mock()
    item._on_mqtt_connected()
    item.mqtt.publish.assert_any_call(f"{item.mqtt.base_topic}/reading/ipmi_21_10_1/availability", "offline", retain=True)


def test_additional_discovery_reuses_core_readings_and_only_exposes_decoded_oem_fields(make_worker):
    item = make_worker(include_advanced_diagnostics=True)
    data = "CPU1 Temp | 01h | ok | 3.1 | 35 degrees C\nInlet Temp | 02h | ok | 7.1 | 24 degrees C\nFan1 | 03h | ok | 7.1 | 1440 RPM\nPwr Consumption | 77h | ok | 7.1 | 70 Watts\nDIMM Temp | 20h | ok | 32.1 | 40 degrees C"
    assert [reading["name"] for reading in item._additional_readings(data)] == ["DIMM Temp"]
    item._power_details = {"instantaneous_watts": 83, "current_amps": 0.7, "headroom_watts": 817, "peak_headroom_watts": 800}
    assert len(item._additional_readings(data)) == 5
    item._power_details = {}
    assert len(item._additional_readings(data)) == 1


def test_long_discrete_readings_preserve_full_text_in_attributes(make_worker):
    import json
    item = make_worker(include_advanced_diagnostics=True)
    item._initialize()
    del item._publish_mqtt_data
    item.mqtt.is_connected = True
    text = "Asserted condition, " * 20
    readings = item._additional_readings(f"Status | 21h | ok | 7.1 | {text}")
    item._update_status({"cpu_temps": [35]}, [], 70, [], "Monitor only", readings=readings)
    payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
    state = json.loads(payloads[f"{item.mqtt.base_topic}/sensor/ipmi_21_7_1"])
    assert len(state["state"]) == 255
    assert state["attributes"]["full_reading"] == text.strip()


CURATED_ROWS = """DIMM Temp | 20h | ok | 32.1 | 40 degrees C
PS Redundancy | 21h | ok | 7.1 | Fully Redundant
CPU1 IERR | 22h | ok | 3.1 | State Deasserted
CPU1 VCORE PG | 23h | ok | 3.1 | State Deasserted
PS1 Voltage | 24h | ok | 10.1 | 120 Volts
PS1 Status | 25h | ok | 10.1 | Presence detected
"""


@pytest.mark.parametrize("setting", [None, False, "true", "false", 1])
def test_important_readings_default_on_and_advanced_requires_explicit_boolean(make_worker, setting):
    item = make_worker(include_advanced_diagnostics=setting)
    readings = item._additional_readings(CURATED_ROWS)
    assert [reading["name"] for reading in readings] == ["DIMM Temp", "PS Redundancy", "CPU1 IERR"]
    assert readings[-1]["value"] == "Inactive"
    assert readings[-1]["raw_reading"] == "State Deasserted"
    assert all(reading["diagnostic_tier"] == "important" for reading in readings)
    active = item._additional_readings(CURATED_ROWS.replace("State Deasserted", "State Asserted"))
    assert [reading["slug"] for reading in active] == [reading["slug"] for reading in readings]
    assert active[-1]["value"] == "Active"


def test_advanced_opt_in_preserves_important_readings_and_restores_stable_ids(make_worker):
    basic = make_worker()
    full = make_worker(include_advanced_diagnostics=True)
    basic_readings = basic._additional_readings(CURATED_ROWS)
    full_readings = full._additional_readings(CURATED_ROWS)
    assert len(full_readings) == 6
    assert {reading["slug"] for reading in basic_readings} < {reading["slug"] for reading in full_readings}
    assert next(reading for reading in full_readings if reading["name"] == "CPU1 VCORE PG")["value"] == "Inactive"


def test_reconcile_off_removes_only_advanced_extra_discovery_even_without_current_reading(make_worker):
    item = make_worker()
    item._initialize()
    item.mqtt.publish.reset_mock()
    # A retained record can outlive its hardware or current SDR reading.
    item._reconcile_extra_discovery("ipmi_24_10_1", {"name": "PS1 Voltage", "device_class": "voltage"})
    item._reconcile_extra_discovery("dell_peak_headroom", {"name": "Peak power headroom", "device_class": "power"})
    item._reconcile_extra_discovery("ipmi_20_32_1", {"name": "DIMM Temp", "device_class": "temperature"})
    item._reconcile_extra_discovery("ipmi_22_3_1", {"name": "CPU1 IERR"})
    removed = [call.args[0] for call in item.mqtt.publish.call_args_list if call.args[1] == ""]
    assert removed == ["homeassistant/sensor/idrac_controller_fixture_ipmi_24_10_1/config",
                       "homeassistant/sensor/idrac_controller_fixture_dell_peak_headroom/config"]
    item.config["include_advanced_diagnostics"] = True
    item.mqtt.publish.reset_mock()
    item._reconcile_extra_discovery("ipmi_24_10_1", {"name": "PS1 Voltage", "device_class": "voltage"})
    item.mqtt.publish.assert_not_called()


def test_advanced_setting_does_not_enable_server_power_or_control_fans(make_worker):
    for setting in (False, True):
        item = make_worker(fan_control_enabled=False, include_advanced_diagnostics=setting)
        item.ipmi.retrieve_power_sdr_raw.return_value = CURATED_ROWS
        run_cycles(item)
        item.ipmi.apply_user_fan_control_profile.assert_not_called()
        item.ipmi.apply_dell_fan_control_profile.assert_not_called()
        item.ipmi.chassis_shutdown.assert_not_called()
        item.ipmi.chassis_power_on.assert_not_called()


def test_default_threshold_summary_does_not_hide_abnormal_advanced_measurements(make_worker):
    import json
    item = make_worker()
    item._initialize()
    del item._publish_mqtt_data
    item.mqtt.is_connected = True
    for status, expected in (("lcr", "Critical"), ("lnc", "Warning"), ("ok", "No alerts")):
        readings = item._additional_readings(f"CPU VCORE | 24h | {status} | 3.1 | 0 Volts")
        assert readings == []  # The raw voltage is optional, not the alert summary.
        item._update_status({"cpu_temps": [35]}, [], 70, [], "Monitor only", readings=readings)
        payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
        summary = json.loads(payloads[f"{item.mqtt.base_topic}/sensor/sensor_threshold_status"])
        assert summary["state"] == expected
        assert len(summary["attributes"]["alerts"]) == (0 if status == "ok" else 1)
    item._update_status({"cpu_temps": []}, [], None, [], "Monitor only")
    payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
    assert json.loads(payloads[f"{item.mqtt.base_topic}/sensor/sensor_threshold_status"])["state"] is None


def test_advanced_off_keeps_core_health_and_load_but_not_auxiliary_metadata(make_worker):
    import json
    item = make_worker()
    item._initialize()
    del item._publish_mqtt_data
    item.mqtt.is_connected = True
    item._power_details = item.ipmi.parse_power_details(POWER_DETAILS)
    item._update_status({"cpu_temps": [35]}, [], 70, [{"name": "PSU1", "ok": True}], "Monitor only",
                        batteries=[{"name": "CMOS Battery", "health": "Healthy", "voltage": 3.1}], intrusion={"active": False})
    payloads = {call.args[0]: call.args[1] for call in item.mqtt.publish.call_args_list}
    assert json.loads(payloads[f"{item.mqtt.base_topic}/sensor/power_capacity_percent"])["state"] == 7.78
    assert payloads[f"{item.mqtt.base_topic}/binary_sensor/psu_psu1"] == "OFF"
    assert payloads[f"{item.mqtt.base_topic}/binary_sensor/system_board_intrusion"] == "OFF"
    assert json.loads(payloads[f"{item.mqtt.base_topic}/sensor/battery_cmos_battery_health"])["state"] == "Healthy"
    for slug in ("power_capacity_watts", "power_warning_watts", "power_failure_watts", "battery_cmos_battery_voltage"):
        assert f"homeassistant/sensor/idrac_controller_fixture_{slug}/config" not in payloads
        item._reconcile_extra_discovery(slug, {"name": slug, "device_class": "power"})
    tombstones = [call.args[0] for call in item.mqtt.publish.call_args_list if call.args[1] == ""]
    assert len(tombstones) == 4
