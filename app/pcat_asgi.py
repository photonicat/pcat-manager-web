#!/usr/bin/env python3

"""granian entry point: `granian --interface asgi pcat_asgi:app`.

The websocket-capable sibling of pcat_wsgi. granian force-disables websockets
under WSGI, which is why the app moved to short-poll push (pcat_util/event_bus)
in the first place. Under ASGI granian speaks websockets natively, so this
module puts a real socket.io server back in front of the terminal:

  /socket.io/*  -> socketio.AsyncServer (websocket transport, engine.io)
  everything    -> the unchanged Flask app, via asgiref's WsgiToAsgi

Nothing about the Flask side changes: pages that poll the event bus keep
polling and keep working. The socket.io layer is a second doorway into the
same machinery -- client emits dispatch into the same @bus_event handlers,
and every event_bus.publish() is mirrored into the socket.io room of the same
name, so a websocket terminal gets its PTY bytes pushed instead of polled.

Importing pcat_wsgi (not pcat_manager_web directly) keeps startup identical:
cwd/sys.path anchoring, PCAT_* env flags, and start_subsystems() all happen
exactly once, there.
"""

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from pcat_wsgi import flask_app  # noqa: F401  (also performs all startup)

import pcat_config
import pcat_manager_web
import socketio
from asgiref.sync import sync_to_async
from asgiref.wsgi import WsgiToAsgi, WsgiToAsgiInstance
from pcat_util import event_bus
from werkzeug.test import EnvironBuilder

# ── Flask over ASGI ──────────────────────────────────────────────────────────
# asgiref's WsgiToAsgi runs every WSGI call through sync_to_async with its
# default thread_sensitive=True, which funnels ALL requests through one shared
# thread -- one slow uci call and the whole UI queues behind it, issue #113
# with different plumbing. Re-wrap the worker with an explicit pool instead.
#
# 16 is the --blocking-threads sizing from the init script, and its rationale
# carries over unchanged: any handler that blocks on I/O (call-audio
# rx_stream, a NAS zip download, firmware_update_check's 4.3s fetch) holds a
# pool thread for its whole duration, and with 4 threads a handful of those
# queued everything else for seconds. A blocked thread costs a stack, not a
# core. PCAT_WSGI_THREADS is the same knob BLOCKING_THREADS was.
_wsgi_pool = ThreadPoolExecutor(
    max_workers=int(os.environ.get("PCAT_WSGI_THREADS", "16") or 16),
    thread_name_prefix="wsgi",
)
WsgiToAsgiInstance.run_wsgi_app = sync_to_async(
    WsgiToAsgiInstance.__dict__["run_wsgi_app"].func,
    thread_sensitive=False,
    executor=_wsgi_pool,
)

_flask_asgi = WsgiToAsgi(flask_app)

# ── socket.io server ─────────────────────────────────────────────────────────
# Default CORS policy (Origin must match Host) is what we want: the terminal
# is same-origin. Websocket only costs an open TCP connection under ASGI, not
# a worker thread, so the #113 starvation mode cannot come back here.
sio = socketio.AsyncServer(async_mode="asgi")
_sio_asgi = socketio.ASGIApp(sio, socketio_path="/socket.io")

# The asyncio loop granian runs us on; needed to emit from publisher threads
# (PTY readers, background workers). Set at lifespan startup and again on
# every connect, whichever comes first.
_loop = None
_loop_lock = threading.Lock()


def _remember_loop():
    global _loop
    with _loop_lock:
        _loop = asyncio.get_running_loop()


# ── event-bus mirror ─────────────────────────────────────────────────────────
# Every publish lands in the socket.io room of the same name (room=None is a
# broadcast, same as the bus). Emitted via the loop because publishers are
# plain threads; fire-and-forget because a publisher must never block on a
# browser, which is the bus's own contract too.
def _mirror_publish(event, data, room):
    loop = _loop
    if loop is None or loop.is_closed():
        return
    coro = sio.emit(event, {} if data is None else data, room=room)
    try:
        asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError:
        pass  # loop shutting down


event_bus.bus.add_listener(_mirror_publish)


# ── session auth ─────────────────────────────────────────────────────────────
# The @bus_event handlers read flask.session, which only exists inside a
# request context. Websocket events have no Flask request, so each handler
# runs inside a synthetic context built from the cookie the browser sent on
# the socket.io handshake -- same cookie, same session, same auth outcome.
def _request_ctx(cookie):
    builder = EnvironBuilder(path="/socket.io/", headers={"Cookie": cookie or ""})
    try:
        return flask_app.request_context(builder.get_environ())
    finally:
        builder.close()


def _session_authed(cookie):
    from flask import session
    with _request_ctx(cookie):
        return pcat_config.skip_auth or ("username" in session)


@sio.event
async def connect(sid, environ, auth=None):
    _remember_loop()
    cookie = environ.get("HTTP_COOKIE", "")
    authed = await asyncio.get_running_loop().run_in_executor(
        _wsgi_pool, _session_authed, cookie)
    if not authed:
        return False  # refuse the connection outright, like the login gate
    await sio.save_session(sid, {"cookie": cookie})


def _run_handler(handler, cookie, message):
    """Dispatch one client command exactly like events_emit_action does:
    same thread-local reply/join scratch, same request context semantics."""
    ctx = pcat_manager_web._dispatch_ctx
    with _request_ctx(cookie):
        ctx.replies = []
        ctx.join = []
        try:
            handler(message or {})
            return list(ctx.replies), list(ctx.join)
        finally:
            ctx.replies = None
            ctx.join = None


async def _enter_room(sid, room):
    # enter_room is sync in some python-socketio 5.x releases and a coroutine
    # in others; the dev machine and the device do not run the same one.
    result = sio.enter_room(sid, room)
    if asyncio.iscoroutine(result):
        await result


@sio.on("*")
async def _dispatch(event, sid, data=None):
    handler = pcat_manager_web._EVENT_HANDLERS.get(event)
    if handler is None:
        return
    sess = await sio.get_session(sid)
    cookie = sess.get("cookie", "")

    # terminal_open starts a PTY whose reader publishes the shell prompt
    # within milliseconds. Join the room before the handler runs, or that
    # first burst is emitted to a room this socket has not entered yet.
    # (The polling path solves the same race with its join_from rewind.)
    if event == "terminal_open":
        session_id = (data or {}).get("session_id", "default")
        await _enter_room(sid, "terminal:" + str(session_id))

    replies, joined = await asyncio.get_running_loop().run_in_executor(
        _wsgi_pool, _run_handler, handler, cookie, data)

    for room in joined:
        await _enter_room(sid, room)
    for r in replies:
        await sio.emit(r["event"], r["data"], to=sid)


# ── top-level router ─────────────────────────────────────────────────────────
async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                _remember_loop()
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    elif scope.get("path", "").startswith("/socket.io"):
        await _sio_asgi(scope, receive, send)
    else:
        await _flask_asgi(scope, receive, send)


__all__ = ["app"]
