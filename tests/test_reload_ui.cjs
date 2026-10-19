const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../ha-idrac-controller/app/static/reload.js"), "utf8");

async function runPage({prefix = "", replies, expire = false}) {
    const calls = [];
    const message = {textContent: ""};
    let destination = null;
    let clock = 0;
    const context = {
        URL, AbortController,
        clearTimeout: () => {},
        document: {getElementById: id => id === "reload-app" ? {dataset: {bootId: "old", reloadToken: "one-use-ticket"}} : message},
        window: {location: {href: `https://ha.example${prefix}/app/reload`, replace: url => {destination = url;}}},
        Date: {now: () => expire ? (clock++ ? 120001 : 0) : 0},
        setTimeout: (callback, timeout) => { if (timeout === 1500) callback(); return 1; },
        fetch: async (url, options) => {
            calls.push({url: url.href, ...options});
            assert.ok(replies.length, "Unexpected extra network request");
            const reply = replies.shift();
            if (reply instanceof Error) throw reply;
            return {ok: reply.ok !== false, json: async () => reply};
        },
    };
    await vm.runInNewContext(source, context);
    return {calls, message: message.textContent, destination};
}

for (const prefix of ["", "/api/hassio_ingress/phone-session"]) {
    test(`restart stays in the same page context: ${prefix || "web"}`, async () => {
        const result = await runPage({prefix, replies: [
            {state: "restarting"}, new Error("Ingress temporarily down"),
            {boot_id: "old", state: "restarting"}, {boot_id: "new", state: "idle"},
        ]});
        assert.equal(result.destination, `https://ha.example${prefix}/servers`);
        assert.equal(result.calls[0].url, `https://ha.example${prefix}/app/reload`);
        assert.equal(result.calls[0].method, "POST");
        assert.equal(result.calls[0].credentials, "same-origin");
        assert.equal(result.calls[0].headers["X-Reload-Token"], "one-use-ticket");
        assert.equal(result.calls.filter(call => call.method === "POST").length, 1);
        assert.equal(result.calls[1].cache, "no-store");
        assert.match(result.message, /App reloaded/);
    });
}

test("Supervisor rejection remains visible without navigating away", async () => {
    const result = await runPage({replies: [{ok: false, error: "Reload request expired. Save your changes again."}]});
    assert.equal(result.destination, null);
    assert.equal(result.calls.length, 1);
    assert.match(result.message, /expired/);
});

test("restart closing the POST connection can still succeed", async () => {
    const result = await runPage({replies: [new Error("Closed during restart"), {boot_id: "new", state: "idle"}]});
    assert.equal(result.destination, "https://ha.example/servers");
});

test("failed self restart is shown without automatic retry", async () => {
    const result = await runPage({replies: [{state: "restarting"}, {boot_id: "old", state: "failed"}]});
    assert.equal(result.destination, null);
    assert.equal(result.calls.filter(call => call.method === "POST").length, 1);
    assert.match(result.message, /could not reload/);
});

test("accepting the request alone does not claim successful reload", async () => {
    const result = await runPage({expire: true, replies: [{state: "restarting"}]});
    assert.equal(result.destination, null);
    assert.match(result.message, /has not confirmed/);
});
