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
        item.ipmi.apply_user_fan_control_profile = Mock(return_value="")
        item.ipmi.apply_dell_fan_control_profile = Mock(return_value="")
        item.ipmi.get_server_model_info = Mock(return_value={})
        item.ipmi.get_firmware_version = Mock(return_value=None)
        item.ipmi.chassis_shutdown = Mock()
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
    item = make_worker()
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
    item.mqtt.subscribe.assert_called_once_with(topic)
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
