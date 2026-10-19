"""Explicit, per-server opt-ins for chassis power actions."""

POWER_ACTIONS = {
    "power_on": {
        "option": "allow_power_on", "name": "Power On System",
        "icon": "mdi:power", "method": "chassis_power_on",
        "description": "Starts the server if it is powered off.",
    },
    "shutdown": {
        "option": "allow_shutdown", "name": "Graceful Shutdown",
        "icon": "mdi:server-off", "method": "chassis_shutdown",
        "description": "Asks the operating system to shut down and power off via ACPI. Requires OS support.",
    },
    "power_off": {
        "option": "allow_power_off", "name": "Power Off System (forced)",
        "icon": "mdi:power-plug-off", "method": "chassis_power_off",
        "description": "Immediately cuts server power without an OS shutdown. Unsaved data can be lost.",
    },
    "power_cycle": {
        "option": "allow_power_cycle", "name": "Power Cycle System (cold boot)",
        "icon": "mdi:restart-alert", "method": "chassis_power_cycle",
        "device_class": "restart",
        "description": "Cuts power, then starts the server again without an OS shutdown. Unsaved data can be lost.",
    },
    "reboot": {
        "option": "allow_reboot", "name": "Reset System (warm boot)",
        "icon": "mdi:restart-alert", "method": "chassis_hard_reset",
        "device_class": "restart",
        "description": "Immediately resets the server without an OS shutdown. Unsaved data can be lost. Use the OS for a graceful reboot.",
    },
}
