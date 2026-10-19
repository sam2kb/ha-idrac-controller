"""MQTT discovery payload tests without a broker connection."""
import json

from conftest import load_module


mqtt_mod = load_module("app_mqtt_client", "ha-idrac-controller/app/mqtt_client.py")


def make_client():
    client = mqtt_mod.MqttClient(client_id="test")
    captured = []
    client.publish = lambda topic, payload, retain=False, qos=0: captured.append((topic, payload, retain))
    client.set_device_info(
        server_alias="T430",
        manufacturer="DELL",
        model="PowerEdge T430",
        ip_address="10.0.0.5",
        serial_number="ABC1234",
        sw_version="2.86.45.45",
    )
    return client, captured


def test_sensor_discovery_has_origin_availability_and_precision():
    client, captured = make_client()
    client.publish_discovery(
        component="sensor",
        slug="hottest_cpu_temp",
        name="Hottest CPU Temperature",
        device_class="temperature",
        unit="°C",
        state_class="measurement",
        suggested_display_precision=1,
    )

    topic, payload, retain = captured[-1]
    data = json.loads(payload)

    assert topic.startswith("homeassistant/sensor/")
    assert retain is True
    assert data["origin"]["name"] == "HA iDRAC Controller"
    assert data["availability"][0]["topic"].endswith("/status")
    assert data["availability"][0]["payload_available"] == "online"
    assert data["suggested_display_precision"] == 1
    assert data["state_class"] == "measurement"
    assert "json_attributes_topic" not in data
    assert data["device"]["serial_number"] == "ABC1234"
    assert data["device"]["sw_version"] == "2.86.45.45"


def test_binary_sensor_can_be_diagnostic():
    client, captured = make_client()
    client.publish_discovery(
        component="binary_sensor",
        slug="psu_psu1",
        name="PSU1",
        device_class="problem",
        entity_category="diagnostic",
    )

    data = json.loads(captured[-1][1])
    assert data["entity_category"] == "diagnostic"
    assert data["payload_on"] == "ON"
    assert data["payload_off"] == "OFF"


def test_removing_legacy_button_clears_retained_config():
    client, captured = make_client()
    client.remove_discovery("button", "shutdown_button")
    assert captured[-1] == ("homeassistant/button/idrac_controller_T430_shutdown_button/config", "", True)


def test_power_button_commands_are_not_retained_and_can_be_configuration_entities():
    client, captured = make_client()
    client.publish_discovery("button", "reboot_button", "Reboot server (hard reset)",
                             cmd_topic="fixture/command/reboot", device_class="restart",
                             entity_category="config")
    payload = json.loads(captured[-1][1])
    assert payload["retain"] is False
    assert payload["command_topic"] == "fixture/command/reboot"
    assert payload["payload_press"] == "PRESS"
    assert payload["entity_category"] == "config"
    assert captured[-1][2] is True  # Retain discovery, never the action.



def test_power_availability_does_not_depend_on_cpu_temperatures():
    client, captured = make_client()
    client.publish_discovery("button", "power_on_button", "Power on", cmd_topic="fixture/command/power_on")
    button = json.loads(captured[-1][1])
    assert [item["topic"] for item in button["availability"]] == [client.connection_topic]
    client.publish_discovery("sensor", "cpu_0_temp", "CPU1 Temperature")
    sensor = json.loads(captured[-1][1])
    assert [item["topic"] for item in sensor["availability"]] == [client.availability_topic, client.connection_topic]
    assert sensor["availability_mode"] == "all"
