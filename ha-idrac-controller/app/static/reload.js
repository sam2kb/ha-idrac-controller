"use strict";

async function fetchBounded(url, options, timeout) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
        return await fetch(url, {...options, signal: controller.signal});
    } finally {
        clearTimeout(timer);
    }
}

(async function reloadApp() {
    const panel = document.getElementById("reload-app");
    const message = document.getElementById("reload-message");
    const statusUrl = new URL("./status", window.location.href);
    const serverUrl = new URL("../servers", window.location.href);
    try {
        const response = await fetchBounded(new URL("./reload", window.location.href), {
            method: "POST", credentials: "same-origin",
            headers: {"X-Reload-Token": panel.dataset.reloadToken},
        }, 10000);
        if (!response.ok) {
            const result = await response.json();
            message.textContent = result.error || "Configuration saved, but the app could not reload. Restart it from Home Assistant Settings.";
            return;
        }
    } catch (_) {
        // A successful restart can close the connection before the response arrives.
    }
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        try {
            const response = await fetchBounded(statusUrl, {cache: "no-store", credentials: "same-origin"}, 5000);
            if (!response.ok) continue;
            const status = await response.json();
            if (status.boot_id && status.boot_id !== panel.dataset.bootId) {
                message.textContent = "App reloaded. Returning to Manage Servers…";
                window.location.replace(serverUrl.href);
                return;
            }
            if (status.state === "failed") {
                message.textContent = "Configuration saved, but the app could not reload. Restart it from Home Assistant Settings.";
                return;
            }
        } catch (_) {
            // Ingress is briefly unavailable while this app restarts.
        }
    }
    message.textContent = "Configuration saved. The app has not confirmed its reload. Reopen this panel or check the app in Home Assistant Settings.";
})();
