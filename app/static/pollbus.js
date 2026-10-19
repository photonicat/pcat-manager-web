/*
 * pollbus.js — drop-in replacement for the socket.io client.
 *
 * Why this exists: the app is served by granian under WSGI, where websockets
 * are force-disabled, so socket.io fell back to XHR long-polling. engine.io
 * holds each poll open until the next PING — a full ping_interval, 25s — and
 * the client re-polls immediately, so every open tab permanently occupied one
 * of granian's four --blocking-threads request slots. Four tabs starved the
 * server outright and every other request waited out a ping cycle (issue
 * #113: 18–24s freezes, every ~25s).
 *
 * So this speaks the same API — io(), .on(), .emit(), .connected — but over
 * short polls that return immediately: GET /api/v1/events.json?since=<seq>
 * for server→client, POST /api/v1/events/emit.json for client→server. No
 * request is ever held open, so a watching page costs one brief request per
 * second instead of a permanently blocked thread.
 *
 * Exposed as window.io so pages keep working with `io({...})` unchanged; the
 * transport options socket.io took are accepted and ignored.
 */
(function () {
  "use strict";

  var POLL_IDLE_MS = 1000; // steady state: one cheap request per second
  var POLL_BUSY_MS = 150; // a burst arrived (log lines, PTY output) — keep up
  var POLL_ERROR_MAX_MS = 5000; // backoff ceiling while the server is unreachable
  var ERRORS_BEFORE_DISCONNECT = 3; // ride out a single dropped poll silently
  var SEEN_MAX = 256; // sequence numbers remembered for de-duplication

  function PollBus() {
    this.connected = false;
    this._handlers = Object.create(null);
    this._rooms = [];
    this._cursor = null; // null = "start me at the current end of the buffer"
    this._errors = 0;
    this._seen = [];
    this._timer = null;
    this._stopped = false;
    this._polling = false;
    this._loop(0);
  }

  PollBus.prototype.on = function (event, cb) {
    (this._handlers[event] || (this._handlers[event] = [])).push(cb);
    return this;
  };

  PollBus.prototype.off = function (event, cb) {
    var list = this._handlers[event];
    if (!list) return this;
    if (!cb) delete this._handlers[event];
    else this._handlers[event] = list.filter(function (h) { return h !== cb; });
    return this;
  };

  /* socket.io hands listeners an ack callback as the second argument. Nothing
   * here uses it, but pages pass `(msg, cb) => ...`, so supply a no-op rather
   * than leaving cb undefined and risking a TypeError in page code. */
  PollBus.prototype._dispatch = function (event, data) {
    var list = this._handlers[event];
    if (!list) return;
    for (var i = 0; i < list.length; i++) {
      try {
        list[i](data, function () {});
      } catch (e) {
        console.error("pollbus handler for " + event + " threw", e);
      }
    }
  };

  /* True if this sequence number has already been handed to the page. Only a
   * rewind after joining a room can replay anything, so the window that needs
   * remembering is small; SEEN_MAX bounds it either way. */
  PollBus.prototype._seen_before = function (seq) {
    if (typeof seq !== "number") return false;
    if (this._seen.indexOf(seq) !== -1) return true;
    this._seen.push(seq);
    if (this._seen.length > SEEN_MAX) this._seen.splice(0, this._seen.length - SEEN_MAX);
    return false;
  };

  PollBus.prototype.emit = function (event, data) {
    var self = this;
    fetch("/api/v1/events/emit.json", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event: event, data: data || {}, rooms: this._rooms }),
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (body) {
        if (!body) return;
        /* Rooms the handler joined on our behalf (the terminal's per-session
         * PTY stream). Sent back on every later poll so the server knows what
         * to deliver — join_room() used to be server-side state on the socket,
         * and with no socket there is nowhere else to keep it. */
        var joined = false;
        if (body.join && body.join.length) {
          for (var i = 0; i < body.join.length; i++) {
            if (self._rooms.indexOf(body.join[i]) === -1) {
              self._rooms.push(body.join[i]);
              joined = true;
            }
          }
        }
        /* Just joined: rewind to where the buffer stood before the handler
         * ran, so output it produced for the new room while a poll was already
         * in flight is not skipped — that is the terminal's first shell prompt.
         * _seen suppresses the broadcasts this replays. */
        if (joined && typeof body.join_from === "number") {
          if (self._cursor === null || body.join_from < self._cursor) {
            self._cursor = body.join_from;
          }
        }
        /* Replies the handler addressed to this caller alone, which used to be
         * flask_socketio's bare emit(). Dispatched locally: they were never in
         * the broadcast buffer, so no other client should see them. */
        if (body.replies) {
          for (var j = 0; j < body.replies.length; j++) {
            self._dispatch(body.replies[j].event, body.replies[j].data);
          }
        }
        self._poll_soon(0); // a command usually produces output; go look now
      })
      .catch(function () {});
    return this;
  };

  PollBus.prototype.disconnect = function () {
    this._stopped = true;
    if (this._timer) clearTimeout(this._timer);
    if (this.connected) {
      this.connected = false;
      this._dispatch("disconnect", {});
    }
    return this;
  };

  PollBus.prototype._poll_soon = function (ms) {
    if (this._stopped) return;
    if (this._timer) clearTimeout(this._timer);
    var self = this;
    this._timer = setTimeout(function () { self._loop(); }, ms);
  };

  PollBus.prototype._loop = function () {
    if (this._stopped || this._polling) return;
    this._polling = true;
    var self = this;
    var url = "/api/v1/events.json";
    var query = [];
    // Remembered so the response can tell whether a room join rewound the
    // cursor while this poll was in flight; if it did, this response must not
    // fast-forward back over the events that rewind exists to collect.
    var issued_with = this._cursor;
    if (this._cursor !== null) query.push("since=" + encodeURIComponent(this._cursor));
    if (this._rooms.length) query.push("rooms=" + encodeURIComponent(this._rooms.join(",")));
    if (query.length) url += "?" + query.join("&");

    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) {
        if (r.status === 401 || r.status === 403) {
          self._dispatch("connect_error", new Error("not authenticated"));
          throw new Error("auth");
        }
        if (!r.ok) throw new Error("http " + r.status);
        return r.json();
      })
      .then(function (body) {
        self._errors = 0;
        if (!self.connected) {
          self.connected = true;
          self._dispatch("connect", {});
        }
        if (self._cursor === issued_with) self._cursor = body.cursor;
        var events = body.events || [];
        for (var i = 0; i < events.length; i++) {
          if (self._seen_before(events[i].seq)) continue;
          self._dispatch(events[i].event, events[i].data);
        }
        /* We fell behind far enough that the server dropped events we never
         * saw. Say so rather than let a page believe it has the whole stream;
         * every page here also refreshes its own state on a timer, so this is
         * a diagnostic, not a failure. */
        if (body.resync) console.warn("pollbus: missed events, state may be stale");
        self._polling = false;
        self._poll_soon(events.length ? POLL_BUSY_MS : POLL_IDLE_MS);
      })
      .catch(function () {
        self._polling = false;
        self._errors++;
        if (self.connected && self._errors >= ERRORS_BEFORE_DISCONNECT) {
          self.connected = false;
          self._dispatch("disconnect", {});
        }
        self._poll_soon(Math.min(POLL_ERROR_MAX_MS, POLL_IDLE_MS * self._errors));
      });
  };

  /* One bus per page: socket.io's io() returned a shared manager connection,
   * and pages call it expecting that. Two independent poll loops would double
   * the request rate and split events between them at random. */
  var singleton = null;
  window.io = function () {
    if (!singleton) singleton = new PollBus();
    return singleton;
  };
  window.PollBus = PollBus;
})();
