#!/usr/bin/env python3

"""granian entry point: `granian --interface wsgi pcat_wsgi:flask_app`.

Importing this module *is* the startup. granian's worker is a real process
(server/__init__.py picks MPServer whenever BUILD_GIL is true, which it is on
our CPython 3.13) and it calls the target loader inside that child, so the
modem/SMS/socket clients come up here, in the same process that serves
requests. There is still exactly one of them, hence one owner of ttyUSB3 (AT)
and ttyUSB1 (NMEA) -- which is why this must stay at --workers 1. Two workers
would be two processes racing the same exclusive-open ttys, and every
threading.Lock in pc_modem_client/pc_gps_client is in-process only, so it
would not arbitrate anything.

Why granian at all: the werkzeug dev server behind socketio.run() was serving
25 req/s for a 24KB font at concurrency 10, and got *slower* than at
concurrency 1 (49 req/s) -- thread-per-connection all serialized on the GIL,
with HTTP parsing in pure Python. granian parses HTTP in Rust off the GIL and,
with --static-path-mount, serves the static tree without entering Python.

granian owns argv, so the options pcat-manager-web-main.py takes as flags are
read from the environment here instead.
"""

import os
import pathlib
import sys

# Templates and the static tree are all resolved relative to the app dir, and
# granian is not started from it in every context (an operator running the
# command by hand, say). Anchor both cwd and the import path on this file so
# the module is importable as a bare `pcat_wsgi:flask_app` target regardless.
_APP_DIR = pathlib.Path(__file__).parent.resolve()
os.chdir(str(_APP_DIR))
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import pcat_config


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


if os.environ.get("PCAT_WEB_PORT", "").strip():
    pcat_config.web_port = int(os.environ["PCAT_WEB_PORT"])
if _env_flag("PCAT_SKIP_AUTH"):
    print("skip-auth flag enabled.")
    pcat_config.skip_auth = True
if _env_flag("PCAT_SKIP_PIN"):
    print("skip-pin flag enabled.")
    pcat_config.skip_pin = True
if _env_flag("PCAT_DEV_MODE"):
    print("dev-mode flag enabled (dev/test endpoints reachable).")
    pcat_config.dev_mode = True
if os.environ.get("PCAT_DEV_SERVER_BASE", "").strip():
    pcat_config.dev_server_base = os.environ["PCAT_DEV_SERVER_BASE"].rstrip("/")

import pcat_bootstrap

pcat_bootstrap.start_subsystems(serve_http=False)

# Exported last, and deliberately after start_subsystems(), so the app module
# is fully initialised before granian is handed the object it will call.
#
# There is no websocket here and no socket.io either. granian force-disables
# websockets under WSGI (server/common.py:673, "Websockets are not supported on
# WSGI, ignoring"), which left socket.io on its XHR long-polling fallback --
# and engine.io holds each poll open for a full ping_interval, so every open
# tab permanently owned one of the --blocking-threads slots and starved
# everything else (issue #113). Push now goes over pcat_util/event_bus, which
# answers each poll immediately. Real websockets exist now, but not here: they
# are pcat_asgi (granian --interface asgi), which imports this module for all
# of the startup above and serves this same flask_app through WsgiToAsgi.
from pcat_manager_web import flask_app  # noqa: E402

__all__ = ["flask_app"]
