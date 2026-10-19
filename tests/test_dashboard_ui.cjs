const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../ha-idrac-controller/app/static/dashboard.js"), "utf8");

// Minimal DOM nodes let CI verify identity preservation without a browser package.
class Node {
    constructor(name, attrs = {}, children = [], value = null) {
        this.nodeName = name;
        this.nodeType = name === "#text" ? 3 : 1;
        this.nodeValue = value;
        this.attrs = {...attrs};
        this.childNodes = [];
        this.parentNode = null;
        for (const child of children) this.appendChild(child);
    }
    get attributes() { return Object.entries(this.attrs).map(([name, value]) => ({name, value})); }
    get lastChild() { return this.childNodes.at(-1); }
    hasAttribute(name) { return Object.hasOwn(this.attrs, name); }
    getAttribute(name) { return this.hasAttribute(name) ? this.attrs[name] : null; }
    setAttribute(name, value) { this.attrs[name] = value; }
    removeAttribute(name) { delete this.attrs[name]; }
    appendChild(child) { return this.insertBefore(child, null); }
    insertBefore(child, before) {
        child.remove();
        const index = before ? this.childNodes.indexOf(before) : this.childNodes.length;
        assert.ok(index >= 0);
        this.childNodes.splice(index, 0, child);
        child.parentNode = this;
        return child;
    }
    remove() {
        if (this.parentNode) {
            this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1);
            this.parentNode = null;
        }
    }
    replaceWith(next) {
        const parent = this.parentNode;
        parent.insertBefore(next, this);
        this.remove();
    }
    cloneNode() { return new Node(this.nodeName, this.attrs, this.childNodes.map(node => node.cloneNode(true)), this.nodeValue); }
}
const text = value => new Node("#text", {}, [], value);
const card = (key, value) => new Node("ARTICLE", {"data-refresh-key": key}, [text(value)]);
const root = (...children) => new Node("DIV", {"data-dashboard-readings": ""}, children);
const {patchNode} = require("../ha-idrac-controller/app/static/dashboard.js");

test("reading updates keep focused links, open disclosures and existing nodes", () => {
    const link = new Node("A", {href: "servers/edit/A"}, [text("Edit")]);
    const details = new Node("DETAILS", {open: ""}, [text("35")]);
    const before = root(new Node("ARTICLE", {"data-refresh-key": "A"}, [link, details]));
    const incoming = root(new Node("ARTICLE", {"data-refresh-key": "A"}, [link.cloneNode(true), new Node("DETAILS", {}, [text("36")])]));
    patchNode(before, incoming);
    assert.equal(before.childNodes[0].childNodes[0], link);
    assert.equal(before.childNodes[0].childNodes[1], details);
    assert.equal(details.hasAttribute("open"), true);
    assert.equal(details.childNodes[0].nodeValue, "36");
    details.removeAttribute("open");
    incoming.childNodes[0].childNodes[1].setAttribute("open", "");
    patchNode(before, incoming);
    assert.equal(details.hasAttribute("open"), false);
});

test("server additions, reordering and removals retain the correct card identities", () => {
    const a = card("A", "35"), b = card("B", "40");
    const before = root(a, b);
    patchNode(before, root(card("B", "41"), card("A", "36"), card("C", "50")));
    assert.equal(before.childNodes[0], b);
    assert.equal(before.childNodes[1], a);
    assert.equal(b.childNodes[0].nodeValue, "41");
    patchNode(before, root(card("B", "42")));
    assert.deepEqual(before.childNodes, [b]);
    assert.equal(a.parentNode, null);
});

function harness(prefix = "") {
    const current = root(card("A", "35"));
    const classes = new Set();
    const indicator = {classList: {add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name)}};
    const message = {textContent: "Updates every 15s"};
    const docEvents = {}, winEvents = {}, timers = new Map(), replies = [], calls = [];
    let counter = 0, parsed = null;
    const doc = {hidden: false, getElementById: id => ({"dashboard-readings": current, "dashboard-updates": indicator, "update-message": message})[id], addEventListener: (name, fn) => docEvents[name] = fn};
    const win = {
        location: {href: `https://ha.example${prefix}/`},
        setTimeout: (fn, delay) => {timers.set(++counter, {fn, delay}); return counter;},
        clearTimeout: id => timers.delete(id),
        addEventListener: (name, fn) => winEvents[name] = fn,
        fetch: async (url, options) => {
            calls.push({url: url.href, ...options});
            const reply = replies.shift();
            assert.ok(reply, "Unexpected fetch");
            if (reply === "hang") return new Promise((resolve, reject) => options.signal.addEventListener("abort", () => reject(new Error("aborted"))));
            if (reply instanceof Error) throw reply;
            parsed = reply.invalid ? null : reply.fragment;
            return {ok: reply.ok !== false, text: async () => "fixture HTML"};
        },
    };
    const context = {module: {exports: {}}, URL, AbortController, DOMParser: class {parseFromString() {return {getElementById: () => parsed};}}};
    vm.runInNewContext(source, context);
    context.module.exports.startDashboard(doc, win);
    const fire = delay => {
        const entry = [...timers].find(([, timer]) => timer.delay === delay);
        assert.ok(entry, `Missing ${delay}ms timer`);
        timers.delete(entry[0]);
        return entry[1].fn();
    };
    return {current, message, classes, calls, replies, doc, docEvents, winEvents, timers, fire};
}

for (const prefix of ["", "/api/hassio_ingress/phone-session"]) {
    test(`polling stays in the same ingress and updates in place: ${prefix || "web"}`, async () => {
        const h = harness(prefix), original = h.current.childNodes[0];
        assert.equal(h.calls.length, 0);
        h.replies.push({fragment: root(card("A", "36"))});
        await h.fire(15000);
        assert.equal(h.calls[0].url, `https://ha.example${prefix}/dashboard/readings`);
        assert.equal(h.calls[0].credentials, "same-origin");
        assert.equal(h.calls[0].cache, "no-store");
        assert.equal(h.calls[0].redirect, "error");
        assert.equal(h.current.childNodes[0], original);
        assert.equal(original.childNodes[0].nodeValue, "36");
    });
}

for (const reply of [new Error("Network down"), {ok: false}, {invalid: true}]) {
    test(`failed/invalid updates retain readings and retry: ${String(reply)}`, async () => {
        const h = harness();
        h.replies.push(reply);
        await h.fire(15000);
        assert.equal(h.current.childNodes[0].childNodes[0].nodeValue, "35");
        assert.equal(h.classes.has("update-failed"), true);
        assert.match(h.message.textContent, /retrying/);
        h.replies.push({fragment: root(card("A", "37"))});
        await h.fire(15000);
        assert.equal(h.classes.has("update-failed"), false);
        assert.equal(h.message.textContent, "Updates every 15s");
    });
}

test("a hanging read times out and a hidden page pauses until visible", async () => {
    const h = harness();
    h.replies.push("hang");
    const pending = h.fire(15000);
    h.fire(10000);
    await pending;
    assert.equal(h.calls[0].signal.aborted, true);
    assert.equal(h.classes.has("update-failed"), true);
    h.doc.hidden = true;
    h.docEvents.visibilitychange();
    assert.equal(h.timers.size, 0);
    h.doc.hidden = false;
    h.docEvents.visibilitychange();
    h.replies.push({fragment: root(card("A", "38"))});
    await h.fire(0);
    assert.equal(h.current.childNodes[0].childNodes[0].nodeValue, "38");
});

test("leaving aborts in-flight requests; a back-cache restore resumes polling", async () => {
    const h = harness();
    h.replies.push("hang");
    const pending = h.fire(15000);
    h.winEvents.pagehide();
    await pending;
    assert.equal(h.timers.size, 0);
    assert.equal(h.classes.has("update-failed"), false);
    h.winEvents.pageshow({persisted: true});
    h.replies.push({fragment: root(card("A", "39"))});
    await h.fire(0);
    assert.equal(h.current.childNodes[0].childNodes[0].nodeValue, "39");
});
