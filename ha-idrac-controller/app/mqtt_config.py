"""Resolve MQTT settings from explicit options or HA Supervisor services."""
from http.client import HTTPConnection, HTTPException
import json
import os


SUPERVISOR_TIMEOUT = 3
MAX_RESPONSE_BYTES = 65536


def resolve_mqtt_config(options, log):
    """Return connection settings, or None so monitoring can continue."""
    host = (options.get("mqtt_host") or "").strip()
    username = options.get("mqtt_username") or ""
    password = options.get("mqtt_password") or ""
    try:
        port = int(options.get("mqtt_port", 1883))
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        log("warning", "Invalid MQTT port; MQTT is unavailable.")
        return None

    # Old installations saved this placeholder even without credentials.
    legacy_default = host == "core-mosquitto" and port == 1883 and not username and not password
    if host and not legacy_default:
        return {"host": host, "port": port, "username": username,
                "password": password, "ssl": False}
    if username or password:
        log("warning", "Set mqtt_host for a manual broker, or clear MQTT credentials for automatic setup.")
        return None

    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        log("warning", "No Supervisor token; set a manual MQTT broker or run as a Home Assistant app.")
        return None

    connection = HTTPConnection("supervisor", timeout=SUPERVISOR_TIMEOUT)
    try:
        # Fixed host/path, no redirects or environment HTTP proxies. Never log
        # response bodies, Authorization headers, or exception text.
        connection.request("GET", "/services/mqtt",
                           headers={"Authorization": f"Bearer {token}"})
        response = connection.getresponse()
        if response.status != 200:
            log("warning", "Supervisor MQTT service is unavailable; will retry.")
            return None
        payload = json.loads(response.read(MAX_RESPONSE_BYTES))
        if not isinstance(payload, dict) or payload.get("result") != "ok":
            raise ValueError
        data = payload["data"]
        if not isinstance(data, dict):
            raise ValueError
        service_host = data["host"]
        service_port = data["port"]
        service_user = data.get("username", "")
        service_password = data.get("password", "")
        use_tls = data.get("ssl", False)
        if (not isinstance(service_host, str) or not service_host.strip()
                or isinstance(service_port, bool)
                or not isinstance(service_user, str)
                or not isinstance(service_password, str)
                or not isinstance(use_tls, bool)
                or data.get("protocol", "3.1.1") != "3.1.1"):
            raise ValueError
        service_port = int(service_port)
        if not 1 <= service_port <= 65535:
            raise ValueError
        log("info", "MQTT configured automatically from Home Assistant Supervisor.")
        return {"host": service_host.strip(), "port": service_port,
                "username": service_user, "password": service_password, "ssl": use_tls}
    except (OSError, HTTPException, ValueError, KeyError, TypeError, OverflowError):
        log("warning", "Could not read valid Supervisor MQTT service details; will retry.")
        return None
    finally:
        connection.close()
