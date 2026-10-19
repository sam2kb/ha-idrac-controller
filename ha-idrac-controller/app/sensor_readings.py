"""Available SDR values for discovery; record identity does not depend on its label."""
import math
import re


# Common IPMI units with HA-native spellings. Other units remain as reported,
# without assigning a device class that would imply unverified semantics.
UNITS = {
    "degrees c": ("°C", "temperature"), "degrees f": ("°F", "temperature"),
    "degrees k": ("K", "temperature"), "volts": ("V", "voltage"),
    "amps": ("A", "current"), "watts": ("W", "power"),
    "rpm": ("RPM", None), "percent": ("%", None), "%": ("%", None),
    "hz": ("Hz", "frequency"), "seconds": ("s", "duration"),
}
UNAVAILABLE = {"", "na", "n/a", "none", "null", "unset", "unknown", "no reading",
               "not readable", "disabled", "not available", "not specified"}
VALID_STATUS = {"ok", "nc", "cr", "nr", "lnc", "lcr", "lnr", "unc", "ucr", "unr"}
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def parse_sdr_readings(data):
    """Keep numeric zero, negative temperatures, and asserted discrete states.

    Opaque hexadecimal codes and absent values cannot establish a reading.
    Only extended SDR rows have the sensor number/entity needed for identity.
    """
    readings = {}
    for row in (data or "").splitlines():
        fields = [part.strip() for part in row.split("|")]
        if len(fields) != 5:
            continue
        name, number, status, entity, raw = fields
        if (not name or not re.fullmatch(r"[0-9a-fA-F]{1,2}h", number)
                or not re.fullmatch(r"\d+\.\d+", entity)):
            continue
        sensor_number = f"{int(number[:-1], 16):02x}"
        entity = ".".join(str(int(part)) for part in entity.split("."))
        slug = f"ipmi_{sensor_number}_{entity.replace('.', '_')}"
        readings.pop(slug, None)  # A later unreadable row supersedes an earlier read.
        if (status.lower() not in VALID_STATUS or raw.lower() in UNAVAILABLE
                or re.fullmatch(r"(?:0x[0-9a-f]+|[0-9a-f]+h)", raw, re.IGNORECASE)):
            continue
        value, unit, device_class, state_class = raw, None, None, None
        numeric = re.fullmatch(rf"({NUMBER})(?:\s+(.+))?", raw)
        if numeric:
            value = float(numeric.group(1))
            if not math.isfinite(value):
                continue
            if value.is_integer():
                value = int(value)
            raw_unit = numeric.group(2)
            if raw_unit:
                unit, device_class = UNITS.get(raw_unit.lower(), (raw_unit, None))
            state_class = "measurement"
        elif re.fullmatch(r"(?:nan|[-+]?inf(?:inity)?)(?:\s+.*)?", raw, re.IGNORECASE):
            continue
        readings[slug] = {
            "slug": slug, "name": name, "sensor_number": f"0x{sensor_number}",
            "entity": entity, "status": status.lower(), "value": value,
            "unit": unit, "device_class": device_class, "state_class": state_class,
            "source_row": row,
        }
    names = [item["name"] for item in readings.values()]
    for item in readings.values():
        if names.count(item["name"]) > 1:
            item["name"] += f" ({item['entity']}, {item['sensor_number']})"
    return list(readings.values())
