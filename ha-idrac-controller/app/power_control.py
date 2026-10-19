"""Explicit, per-server opt-ins for chassis power actions."""

POWER_ACTIONS = {
    "power_on": {
        "option": "allow_power_on", "name": "Power on server",
        "icon": "mdi:power", "method": "chassis_power_on",
        "description": "Starts the server if it is powered off.",
    },
    "shutdown": {
        "option": "allow_shutdown", "name": "Shut down server (graceful)",
        "icon": "mdi:server-off", "method": "chassis_shutdown",
        "description": "Asks the operating system to shut down and power off via ACPI. Requires OS support.",
    },
    "reboot": {
        "option": "allow_reboot", "name": "Reboot server (hard reset)",
        "icon": "mdi:restart-alert", "method": "chassis_hard_reset",
        "device_class": "restart",
        "description": "Immediately resets the server without an OS shutdown. Unsaved data can be lost. Use the OS for a graceful reboot.",
    },
}
