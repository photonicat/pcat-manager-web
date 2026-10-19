/*
 * Tests for app/static/pollbus.js — the client half of the socket.io
 * replacement (issue #113). Plain node, no test framework: the repo has no JS
 * test tooling and this is not worth adding one for.
 *
 * Driven by tests/test_pollbus_js.py so it runs with the rest of the suite.
 * Run directly with: node tests/pollbus_test.js
 *
 * fetch and the timers are stubbed, so the poll loop is stepped deliberately
 * rather than waited on — the logic under test is which cursor goes out on the
 * next request, not how long the shim sleeps between them.
 */
"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SOURCE = fs.readFileSync(path.join(__dirname, "..", "app", "static", "pollbus.js"), "utf8");

/* A sandbox with controllable time and a scripted server. `respond` receives
 * (url, init) and returns the body to answer with, or a status to fail with. */
function makeBus(respond) {
  const timers = [];
  let nextId = 1;
  const requests = [];

  const sandbox = {
    console: {
      warn() {},
      error(...a) {
        sandbox._errors.push(a);
      },
    },
    _errors: [],
    setTimeout(fn, ms) {
      const id = nextId++;
      timers.push({ id, fn, at: ms || 0 });
      return id;
    },
    clearTimeout(id) {
      const i = timers.findIndex((t) => t.id === id);
      if (i >= 0) timers.splice(i, 1);
    },
    fetch(url, init) {
      requests.push({ url, init });
      const answer = respond(url, init);
      if (answer && answer.status && answer.status >= 400) {
        return Promise.resolve({ ok: false, status: answer.status, json: () => Promise.resolve({}) });
      }
      if (answer === null) return Promise.reject(new Error("network"));
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(answer) });
    },
    window: {},
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox);

  // Let queued promise callbacks run, then fire the earliest pending timer.
  const settle = () => new Promise((r) => setImmediate(r));
  async function step(times = 1) {
    for (let i = 0; i < times; i++) {
      await settle();
      await settle();
      if (!timers.length) continue;
      timers.sort((a, b) => a.at - b.at);
      const t = timers.shift();
      t.fn();
      await settle();
      await settle();
    }
  }

  return { sandbox, requests, step, io: sandbox.window.io };
}

function query(url) {
  const i = url.indexOf("?");
  return i === -1 ? "" : url.slice(i + 1);
}

const tests = {};

tests["first poll asks for no cursor, then tracks the server's"] = async () => {
  let cursor = 7;
  const h = makeBus(() => ({ cursor, events: [], resync: false }));
  h.io();
  await h.step(2);

  assert.strictEqual(query(h.requests[0].url), "", "a fresh client must not ask for history");
  assert.ok(h.requests.length > 1, "should keep polling");
  assert.strictEqual(query(h.requests[1].url), "since=7");
};

tests["connect fires once, on the first successful poll"] = async () => {
  const h = makeBus(() => ({ cursor: 1, events: [], resync: false }));
  const bus = h.io();
  let connects = 0;
  bus.on("connect", () => connects++);
  await h.step(3);

  assert.strictEqual(connects, 1);
  assert.strictEqual(bus.connected, true);
};

tests["events reach their handler with the payload"] = async () => {
  let sent = false;
  const h = makeBus(() => {
    if (sent) return { cursor: 5, events: [], resync: false };
    sent = true;
    return { cursor: 5, events: [{ seq: 5, event: "nas_job_status", data: { id: "j1", state: "running" } }], resync: false };
  });
  const bus = h.io();
  const got = [];
  bus.on("nas_job_status", (j) => got.push(j));
  await h.step(2);

  assert.deepStrictEqual(got, [{ id: "j1", state: "running" }]);
};

tests["a replayed sequence number is not dispatched twice"] = async () => {
  const event = { seq: 9, event: "log_response", data: { data: "line\n" } };
  const h = makeBus(() => ({ cursor: 9, events: [event], resync: false }));
  const bus = h.io();
  let calls = 0;
  bus.on("log_response", () => calls++);
  await h.step(4);

  assert.strictEqual(calls, 1, "duplicate seq must be suppressed");
};

tests["joining a room rewinds the cursor and repeats the room"] = async () => {
  let cursor = 100;
  const h = makeBus((url, init) => {
    if (url.indexOf("/emit.json") !== -1) {
      assert.strictEqual(init.method, "POST");
      return { status_: "ok", replies: [], join: ["terminal:a"], join_from: 42 };
    }
    return { cursor, events: [], resync: false };
  });
  const bus = h.io();
  await h.step(2); // first poll settles the cursor at 100
  bus.emit("terminal_open", { session_id: "a" });
  await h.step(3);

  const polls = h.requests.filter((r) => r.url.indexOf("/events.json") !== -1);
  // The poll issued straight after the join must go back to join_from, or the
  // shell prompt published between the join and this request is lost. Later
  // polls legitimately move forward again from whatever cursor came back.
  assert.ok(
    polls.some((p) => p.url.indexOf("since=42") !== -1),
    "must rewind to join_from once joined: " + polls.map((p) => p.url).join(" ")
  );
  const last = polls[polls.length - 1].url;
  assert.ok(last.indexOf("rooms=terminal%3Aa") !== -1, "must repeat the joined room on every poll: " + last);
  assert.ok(last.indexOf("since=100") !== -1, "must resume from the server's cursor once caught up: " + last);
};

tests["caller-only replies are dispatched locally"] = async () => {
  const h = makeBus((url) => {
    if (url.indexOf("/emit.json") !== -1) {
      return { replies: [{ event: "connected_response", data: { data: "Welcome\n" } }], join: [], join_from: 3 };
    }
    return { cursor: 3, events: [], resync: false };
  });
  const bus = h.io();
  const got = [];
  bus.on("connected_response", (m) => got.push(m.data));
  await h.step(1);
  bus.emit("client_connected", {});
  await h.step(2);

  assert.deepStrictEqual(got, ["Welcome\n"]);
};

tests["a single failed poll does not report a disconnect"] = async () => {
  let n = 0;
  const h = makeBus(() => (++n === 2 ? null : { cursor: 1, events: [], resync: false }));
  const bus = h.io();
  let disconnects = 0;
  bus.on("disconnect", () => disconnects++);
  await h.step(4);

  assert.strictEqual(disconnects, 0, "one dropped poll must be ridden out");
};

tests["repeated failures report a disconnect"] = async () => {
  let n = 0;
  const h = makeBus(() => (n++ === 0 ? { cursor: 1, events: [], resync: false } : null));
  const bus = h.io();
  let disconnects = 0;
  bus.on("disconnect", () => disconnects++);
  await h.step(6);

  assert.strictEqual(disconnects, 1);
  assert.strictEqual(bus.connected, false);
};

tests["a rejected session raises connect_error"] = async () => {
  const h = makeBus(() => ({ status: 403 }));
  const bus = h.io();
  let errors = 0;
  bus.on("connect_error", () => errors++);
  await h.step(2);

  assert.ok(errors >= 1);
  assert.strictEqual(bus.connected, false);
};

tests["a throwing handler does not stop the loop"] = async () => {
  let first = true;
  const h = makeBus(() => {
    if (!first) return { cursor: 2, events: [], resync: false };
    first = false;
    return { cursor: 2, events: [{ seq: 2, event: "boom", data: {} }], resync: false };
  });
  const bus = h.io();
  bus.on("boom", () => {
    throw new Error("handler bug");
  });
  await h.step(4);

  assert.ok(h.requests.length >= 3, "polling must continue past a handler throw");
};

(async () => {
  let failed = 0;
  for (const name of Object.keys(tests)) {
    try {
      await tests[name]();
      console.log("ok   - " + name);
    } catch (e) {
      failed++;
      console.log("FAIL - " + name + "\n       " + (e && e.message));
    }
  }
  console.log("\n" + (Object.keys(tests).length - failed) + "/" + Object.keys(tests).length + " passed");
  process.exit(failed ? 1 : 0);
})();
