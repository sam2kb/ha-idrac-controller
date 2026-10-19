"""Generic SDR discovery uses available values and stable record identities."""
import pytest

from app.sensor_readings import parse_sdr_readings


@pytest.mark.parametrize("raw,unit,device_class,value", [
    ("0 Volts", "V", "voltage", 0), ("0.1 Amps", "A", "current", 0.1),
    ("-5 degrees C", "°C", "temperature", -5), ("196 Watts", "W", "power", 196),
    ("1440 RPM", "RPM", None, 1440), ("20 percent", "%", None, 20),
    ("60 Hz", "Hz", "frequency", 60), ("1.5 seconds", "s", "duration", 1.5),
    ("4 Widgets", "Widgets", None, 4), ("0", None, None, 0),
    ("Presence detected", None, None, "Presence detected"),
    ("Power Supply AC lost", None, None, "Power Supply AC lost"),
    ("Not Present", None, None, "Not Present"),
])
def test_available_numeric_and_discrete_values(raw, unit, device_class, value):
    item = parse_sdr_readings(f"Reading | 0Ah | ok | 10.1 | {raw}")[0]
    assert item["slug"] == "ipmi_0a_10_1"
    assert item["value"] == value
    assert item["unit"] == unit
    assert item["device_class"] == device_class
    assert item["state_class"] == ("measurement" if isinstance(value, (int, float)) else None)


@pytest.mark.parametrize("raw", ["", "Unset", "N/A", "No Reading", "Unknown", "Disabled", "Not Readable", "0x00", "0xff", "00h", "nan", "inf Watts", "1e309 Watts"])
def test_missing_opaque_and_nonfinite_values_do_not_create_entities(raw):
    assert parse_sdr_readings(f"Reading | 0Ah | ok | 10.1 | {raw}") == []


@pytest.mark.parametrize("status", ["ns", "na", "u", "unsupported"])
def test_unavailable_sensor_cannot_publish_numeric_or_assertion_as_current(status):
    assert parse_sdr_readings(f"Reading | 0Ah | {status} | 10.1 | 120 Volts") == []
    assert parse_sdr_readings(f"Reading | 0Ah | {status} | 10.1 | Presence detected") == []


def test_critical_values_are_readings_and_renames_preserve_identity():
    before = parse_sdr_readings("Voltage | 0Ah | lcr | 10.1 | 0 Volts")[0]
    after = parse_sdr_readings("PSU Voltage | 0ah | ok | 10.1 | 120 Volts")[0]
    assert before["slug"] == after["slug"]
    assert before["status"] == "lcr"
    assert before["value"] == 0


def test_duplicate_labels_are_distinct_and_latest_unavailable_read_wins():
    rows = "Voltage | 0Ah | ok | 10.1 | 120 Volts\nVoltage | 0Bh | ok | 10.2 | 121 Volts"
    readings = parse_sdr_readings(rows)
    assert len({item["slug"] for item in readings}) == 2
    assert readings[0]["name"] != readings[1]["name"]
    assert len(parse_sdr_readings(rows + "\nVoltage | 0Ah | ns | 10.1 | No Reading")) == 1
    assert parse_sdr_readings(None) == []
    assert parse_sdr_readings("Voltage | 120 Volts | ok") == []
