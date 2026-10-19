/* Update existing readings without replacing the page, focus or open details. */
(function () {
    "use strict";

    function patchNode(current, incoming) {
        if (current.nodeType !== incoming.nodeType || current.nodeName !== incoming.nodeName) {
            current.replaceWith(incoming.cloneNode(true));
            return;
        }
        if (current.nodeType !== 1) {
            if (current.nodeValue !== incoming.nodeValue) current.nodeValue = incoming.nodeValue;
            return;
        }
        const preserveOpen = current.nodeName === "DETAILS";
        for (const attr of Array.from(current.attributes)) {
            if (preserveOpen && attr.name === "open") continue;
            if (!incoming.hasAttribute(attr.name)) current.removeAttribute(attr.name);
        }
        for (const attr of Array.from(incoming.attributes)) {
            if (preserveOpen && attr.name === "open") continue;
            if (current.getAttribute(attr.name) !== attr.value) current.setAttribute(attr.name, attr.value);
        }
        const nextChildren = Array.from(incoming.childNodes);
        for (let index = 0; index < nextChildren.length; index++) {
            const next = nextChildren[index];
            const key = next.nodeType === 1 ? next.getAttribute("data-refresh-key") : null;
            let existing = current.childNodes[index];
            if (key !== null) {
                const matching = Array.from(current.childNodes).find(node =>
                    node.nodeType === 1 && node.getAttribute("data-refresh-key") === key);
                if (matching && matching !== existing) {
                    current.insertBefore(matching, existing || null);
                    existing = matching;
                } else if (!matching) {
                    current.insertBefore(next.cloneNode(true), existing || null);
                    continue;
                }
            }
            if (existing) patchNode(existing, next);
            else current.appendChild(next.cloneNode(true));
        }
        while (current.childNodes.length > nextChildren.length) current.lastChild.remove();
    }

    function startDashboard(doc, win) {
        const readings = doc.getElementById("dashboard-readings");
        if (!readings) return;
        const indicator = doc.getElementById("dashboard-updates");
        const message = doc.getElementById("update-message");
        const endpoint = new URL("dashboard/readings", win.location.href);
        let timer = null;
        let active = null;
        let stopped = false;
        let resumeRequested = false;

        function schedule(delay = 15000) {
            win.clearTimeout(timer);
            if (!stopped && !doc.hidden) timer = win.setTimeout(refresh, delay);
        }

        async function refresh() {
            if (stopped || doc.hidden || active) return;
            const controller = new AbortController();
            active = controller;
            const timeout = win.setTimeout(() => controller.abort("timeout"), 10000);
            try {
                const response = await win.fetch(endpoint, {
                    credentials: "same-origin", cache: "no-store", redirect: "error",
                    signal: controller.signal,
                });
                if (!response.ok) throw new Error("Readings unavailable");
                const html = await response.text();
                const fragment = new DOMParser().parseFromString(html, "text/html");
                const incoming = fragment.getElementById("dashboard-readings");
                if (!incoming || !incoming.hasAttribute("data-dashboard-readings")) throw new Error("Invalid readings response");
                if (stopped || doc.hidden || controller.signal.aborted) return;
                patchNode(readings, incoming);
                if (indicator.classList.contains("update-failed")) indicator.classList.remove("update-failed");
                if (message.textContent !== "Updates every 15s") message.textContent = "Updates every 15s";
            } catch (_) {
                if (!stopped && !doc.hidden && controller.signal.reason !== "hidden") {
                    if (!indicator.classList.contains("update-failed")) indicator.classList.add("update-failed");
                    if (message.textContent !== "Update failed · retrying") message.textContent = "Update failed · retrying";
                }
            } finally {
                win.clearTimeout(timeout);
                active = null;
                schedule(resumeRequested ? 0 : 15000);
                resumeRequested = false;
            }
        }

        doc.addEventListener("visibilitychange", () => {
            win.clearTimeout(timer);
            if (doc.hidden) {
                if (active) active.abort("hidden");
            } else if (active) resumeRequested = true;
            else schedule(0);
        });
        win.addEventListener("pagehide", () => {
            stopped = true;
            win.clearTimeout(timer);
            if (active) active.abort("leaving");
        });
        win.addEventListener("pageshow", event => {
            if (!event.persisted) return;
            stopped = false;
            if (active) resumeRequested = true;
            else schedule(0);
        });
        schedule();
    }

    if (typeof module !== "undefined" && module.exports) module.exports = {patchNode, startDashboard};
    if (typeof document !== "undefined") startDashboard(document, window);
}());
