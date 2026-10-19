"""MQTT discovery payload tests, with paho replaced by a tiny stub."""
import json
import sys
import types

from conftest import load_module


class _FakePahoClient:
    def __init__(self, *args, **kwargs):
        pass

    def reconnect_delay_set(self, *args, **kwargs):
        pass


paho = types.ModuleType("paho")
paho_mqtt = types.ModuleType("paho.mqtt")
paho_client = types.ModuleType("paho.mqtt.client")
paho_client.Client = _FakePahoClient
paho_client.MQTTv311 = 4
paho.mqtt = paho_mqtt
paho_mqtt.client = paho_client
sys.modules.setdefault("paho", paho)
sys.modules.setdefault("paho.mqtt", paho_mqtt)
sys.modules.setdefault("paho.mqtt.client", paho_client)

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
