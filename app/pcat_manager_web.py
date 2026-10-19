#@Author    : qichunren
# Security enhancements implemented:
# - Secure random SECRET_KEY generation
# - Session cookie security (HttpOnly, SameSite, Secure for HTTPS)
# - Rate limiting for login attempts (5 attempts per 5 minutes)
# - CSRF protection for forms
# - Security headers (X-Frame-Options, CSP, etc.)
# - Reasonable session lifetimes (30 days default, 180 days remember)

from flask import Flask, jsonify, render_template, request, redirect, session, make_response, url_for, g
from flask import Response, stream_with_context
from pc_call_audio import call_audio_bridge
from werkzeug.exceptions import HTTPException
from datetime import datetime, date, timedelta
import secrets
import time
import urllib.parse
from pcat_util import user_auth, pcat_logger, command_parser, hardware_util, isp_dict, wifi_wan, wifi_radio, wan_manager, captive
from pcat_util import nas_manager, nas_jobs, nas_files
from pcat_util import bluetooth as bt_util
from pcat_util import osm_tiles
from pcat_util import pkg_format
from pcat_util import event_bus
from pcat_util.openwrt import ubus_call as _openwrt_ubus_call, uci_get as _openwrt_uci_get, uci_set as _openwrt_uci_set, uci_commit as _openwrt_uci_commit, uci_show_dict as _openwrt_uci_show_dict, debug_status as _openwrt_debug_status
import urllib.error
import subprocess
import threading
# import pyprctl # patch:set real thread name
import pathlib
import json
import base64
import io
import re
import os
import glob
import math
import urllib.request
import urllib.error

import pcat_config
import oem_config
import remember_token
import power_event_log
import battery_history
import gauge_learning_log
import locales as _locales
import nt
import pc_modem_client
import pc_gps_client
import pc_gps_logger
import app as pc_app
from cmd_tool import CmdTool
from pc_terminal import terminal_manager

install_thread = None
thread_lock = threading.Lock()

# Server-side store for the password used to SSO into LuCI. We deliberately do
# NOT put the password in the Flask session cookie (signed, not encrypted) or
# embed it in every page's HTML. A random token in the session indexes this
# process-local map; cleared on logout / process restart.
_DEFAULT_LUCI_PASSWORD = "photonicat"
_luci_pw_lock = threading.Lock()
_luci_pw_by_token = {}  # token -> plaintext password


def _store_luci_password(password):
    """Remember the login password for OpenWrt SSO (server memory only)."""
    if not password:
        return
    old = session.pop("luci_pw_token", None)
    token = secrets.token_hex(16)
    with _luci_pw_lock:
        if old:
            _luci_pw_by_token.pop(old, None)
        _luci_pw_by_token[token] = password
    session["luci_pw_token"] = token


def _clear_luci_password():
    """Drop any stored LuCI SSO password for this session."""
    token = session.pop("luci_pw_token", None)
    session.pop("remembered_password", None)  # legacy key cleanup
    if token:
        with _luci_pw_lock:
            _luci_pw_by_token.pop(token, None)


def _get_luci_password():
    """Password to POST to LuCI: the one used at login, else factory default."""
    token = session.get("luci_pw_token")
    if token:
        with _luci_pw_lock:
            pw = _luci_pw_by_token.get(token)
        if pw:
            return pw
    return getattr(pcat_config, "default_luci_password", None) or _DEFAULT_LUCI_PASSWORD


# SMS list caching for better performance
sms_cache = {
    'data': None,
    'timestamp': 0,
    'lock': threading.Lock(),
    'fetch_in_progress': False,
    'last_fetch_time': 0
}
SMS_CACHE_DURATION = 15  # seconds
SMS_FETCH_THROTTLE = 10  # seconds - minimum interval between get_sms() calls

# Device control caching for better performance
device_cache = {
    'base_locales': None,
    'base_locales_timestamp': 0,
    'stm32_firmware_exist': None,
    'stm32_firmware_exist_timestamp': 0,
    'is_em05_modem': None,
    'is_em05_modem_timestamp': 0,
    'lock': threading.Lock()
}
DEVICE_CACHE_DURATION = 300  # seconds (5 minutes for hardware checks)
BASE_LOCALES_CACHE_DURATION = 3600  # seconds (1 hour for static config)


# Country calling codes ordered longest-first for correct matching
_COUNTRY_CODES = [
    '886','855','856','853','852','673','670','675','676','677','678','679',
    '880','960','961','962','963','964','965','966','967','968','971','972','973','974','975','976','977',
    '992','993','994','995','996','998',
    '212','213','216','218','220','221','222','223','224','225','226','227','228','229',
    '230','231','232','233','234','235','236','237','238','239','240','241','242','243','244','245','246','247','248','249',
    '250','251','252','253','254','255','256','257','258','260','261','262','263','264','265','266','267','268','269',
    '290','291','297','298','299',
    '350','351','352','353','354','355','356','357','358','359',
    '370','371','372','373','374','375','376','377','378','380','381','382','383','385','386','387','389',
    '420','421','423',
    '500','501','502','503','504','505','506','507','508','509','590','591','592','593','594','595','596','597','598','599',
    '850','870',
    '90','91','92','93','94','95','98',
    '84','82','81','86','62','60','63','64','65','66',
    '20','27',
    '30','31','32','33','34','36','39',
    '40','41','43','44','45','46','47','48','49',
    '51','52','53','54','55','56','57','58','61',
    '7','1',
]

# cell_tech values that carry no usable radio generation. handle_qnwinfo() falls
# back to "Other" whenever AT+QNWINFO's network-mode field matches none of its
# NR/LTE/WCDMA patterns -- which the RM500Q-GL triggers routinely by answering
# "CDMA","46001","CDMA BC0" while actually camped on 5G. Treating those as empty
# lets the PMU-reported modem_mode supply the generation instead, so the
# dashboard shows "5G China Unicom" rather than "Other China Unicom".
CELL_TECH_UNKNOWN = ("", "other", "none", "unknown")

def usable_cell_tech(cell_tech):
    """Return cell_tech when it names a real radio generation, else ""."""
    if not cell_tech or cell_tech.strip().lower() in CELL_TECH_UNKNOWN:
        return ""
    return cell_tech

def format_phone_number(number):
    """Prepend + to phone number if it matches a known country calling code."""
    if not number:
        return number
    number = number.strip()
    if number.startswith('+'):
        return number
    for code in _COUNTRY_CODES:
        if number.startswith(code) and len(number) > len(code) + 4:
            return '+' + number
    return number


def _home_dial_code():
    """Country calling code of the SIM, read back off its own MSISDN.

    Caller ID for a domestic call arrives in national format -- "13800138000",
    no country code -- so there is nothing in the number itself to key on.
    The SIM's own number is in the same numbering plan, so its country code is
    the right one to borrow, and it needs no MCC table to derive. Empty when
    the SIM does not publish an MSISDN, which is common; the number is then
    shown exactly as the network gave it."""
    try:
        own = pcat_config.get_user_config_key("custom_sim_number", None) \
            or pc_app.modem_client.basic.get("sim_number", "")
    except Exception:
        return ""
    own = format_phone_number((own or "").strip())
    if not own.startswith('+'):
        return ""
    for code in sorted(_COUNTRY_CODES, key=len, reverse=True):
        if own[1:].startswith(code):
            return code
    return ""


def intl_phone_number(number):
    """Caller ID in full international form, for display. Falls back to the
    number as received rather than guessing when the country is unknowable.

    The SIM's own country wins over format_phone_number()'s prefix matching,
    which cannot tell a national number from an international one: "1" is a
    country code, so it reads the Chinese mobile 13800138000 as +1 3800138000.
    Same numbering plan as the SIM is the far better assumption for a caller
    ID that arrived without a country code."""
    if not number:
        return number
    number = number.strip()
    if number.startswith('+'):
        return number
    code = _home_dial_code()
    # Only prefix something long enough to be a subscriber number -- never a
    # short code or a service number.
    if code and number.isdigit() and len(number) >= 7:
        if number.startswith(code) and len(number) > len(code) + 4:
            return '+' + number          # already international, just unsigned
        return '+' + code + number.lstrip('0')
    return format_phone_number(number)


def invalidate_sms_cache():
    """Invalidate SMS cache when messages are modified"""
    with sms_cache['lock']:
        sms_cache['data'] = None
        sms_cache['timestamp'] = 0
        sms_cache['fetch_in_progress'] = False
        sms_cache['last_fetch_time'] = 0
        print("SMS cache invalidated")

# Helper function for robust HTTP requests
def robust_urlopen(url, timeout=20, max_retries=2):
    """
    Robust URL opener with retry logic for SSL/connection issues
    """
    last_error = None
    
    for attempt in range(max_retries + 1):
        try:
            pcat_logger.logger.info(f"Attempting to fetch {url} (attempt {attempt + 1}/{max_retries + 1})")
            with urllib.request.urlopen(url, timeout=timeout) as response:
                data = response.read()
                pcat_logger.logger.info(f"Successfully fetched {url} ({len(data)} bytes)")
                return data
        except urllib.error.URLError as e:
            last_error = e
            pcat_logger.logger.warning(f"Attempt {attempt + 1} failed for {url}: {str(e)}")
            if attempt < max_retries:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s
                pcat_logger.logger.info(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                pcat_logger.logger.error(f"All {max_retries + 1} attempts failed for {url}")
        except Exception as e:
            last_error = e
            pcat_logger.logger.error(f"Unexpected error for {url}: {str(e)}")
            break
    
    raise last_error

# Simple rate limiting for login attempts
login_attempts = {}
LOGIN_RATE_LIMIT = 5  # Max attempts per window
LOGIN_WINDOW = 300    # 5 minutes in seconds

# PIN protection settings
# PIN: 2025 -> SHA-256 hash: computed in JavaScript and validated server-side
PIN_PROTECTED_ROUTES = ['/perf']
PIN_SESSION_KEY = 'pin_authenticated'
PIN_HASH_JS = 'sha256("2025")'  # This will be computed in JavaScript

def check_rate_limit(ip):
    """Check if IP is within rate limit for login attempts"""
    now = time.time()
    if ip not in login_attempts:
        login_attempts[ip] = []
    
    # Clean old attempts
    login_attempts[ip] = [attempt_time for attempt_time in login_attempts[ip] 
                         if now - attempt_time < LOGIN_WINDOW]
    
    # Check if over limit
    if len(login_attempts[ip]) >= LOGIN_RATE_LIMIT:
        return False
    
    # Record this attempt
    login_attempts[ip].append(now)
    
    # Cleanup old entries periodically (every 100 attempts to avoid overhead)
    if len(login_attempts) % 100 == 0:
        cleanup_old_attempts()
    
    return True

def cleanup_old_attempts():
    """Clean up old login attempt records to prevent memory leaks"""
    now = time.time()
    to_remove = []
    for ip, attempts in login_attempts.items():
        login_attempts[ip] = [attempt_time for attempt_time in attempts 
                             if now - attempt_time < LOGIN_WINDOW]
        if not login_attempts[ip]:
            to_remove.append(ip)
    
    for ip in to_remove:
        del login_attempts[ip]

def generate_csrf_token():
    """Generate CSRF token for forms"""
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(16)
    return session['_csrf_token']

def validate_csrf_token(token):
    """Validate CSRF token"""
    return token and session.get('_csrf_token') == token

# Make csrf_token available in templates
# Note: This will be set after flask_app is created

def csrf_protect(f):
    """CSRF protection decorator for sensitive endpoints"""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method in ('POST', 'PUT', 'DELETE'):
            csrf_token = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token')
            if not validate_csrf_token(csrf_token):
                return jsonify({"status": "failed", "message": "Security token invalid"}), 400
        return f(*args, **kwargs)
    return decorated_function

def pin_required(f):
    """Decorator to protect routes with PIN authentication"""
    from functools import wraps
    
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # First check if user is authenticated - redirect to login with return URL if not
        if 'username' not in session:
            return redirect(f"/login?next={request.path}")
            
        # Skip PIN protection if --skip-pin flag is enabled
        if pcat_config.skip_pin:
            return f(*args, **kwargs)
        
        # Check if user is already PIN authenticated in this session
        if PIN_SESSION_KEY not in session:
            # Render PIN entry page
            return render_template("pin_entry.html", 
                                 target_url=request.path,
                                 locales=base_locales())
        
        return f(*args, **kwargs)
    return decorated_function

try:
    board_name_raw = open("/proc/device-tree/compatible", "r").read().strip()
    if "photonicat2" in board_name_raw:
        board_name = "photonicat2"
    else:
        board_name = "photonicat"
except:
    board_name = "photonicat"

# Photonicat 1 (rk3568, "ariaboard,photonicat") and Photonicat 2 (rk3576,
# "ariaboard,photonicat2") share this app but not their hardware: the MCU
# update path, LXC, the power-LED/beeper/charge-limit controls and the
# low-battery shutdown policy exist only on Photonicat 2, and firmware images
# are built per board. Everything that must differ keys off this one flag.
is_photonicat2 = (board_name == "photonicat2")

def background_install_thread(file_position, file_name, verify_tls=False):
    global install_thread
    current_path = pathlib.Path(__file__).parent.resolve()
    
    # Check for opkg-install script, prefer .pyc then .py
    cmd_pyc = current_path.joinpath("opkg-install.pyc")
    cmd_py = current_path.joinpath("opkg-install.py")
    
    if cmd_pyc.exists():
        cmd = cmd_pyc
    elif cmd_py.exists():
        cmd = cmd_py
    else:
        pcat_logger.logger.error("opkg-install script not found (neither .pyc nor .py)")
        publish('log_response', {'data': 'ERROR: opkg-install script not found\n'})
        return

    if file_position == "remote":
        file_name = pcat_config.volatile_packages_url + file_name

    cmd_args = ["python3", cmd, "install", file_name]
    if verify_tls:
        cmd_args.append("--verify-tls")
    p = subprocess.Popen(cmd_args, stdout=subprocess.PIPE, bufsize=1)
    while True:
        time.sleep(0.5)
        for line in iter(p.stdout.readline, b''):
            line_str = line.decode("utf-8")
            now = datetime.now()
            time_string = now.strftime("%H:%M:%S")
            publish('log_response', {'data': line_str, 'ts': time_string})
        p.stdout.close()
        break
    install_thread = None

class FlaskAppThread (threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)

    def run(self):
        print("Start web service on port", pcat_config.web_port)
        audit_route_auth()
        try:
            # Development listener only; in production granian serves
            # pcat_wsgi:flask_app and this thread never starts. Was
            # socketio.run(), which only ever wrapped werkzeug for us.
            flask_app.run(host="0.0.0.0", port=pcat_config.web_port,
                          threaded=True, use_reloader=False)
        except PermissionError as error:
            print("!!! Failed to start web service", error)


class TlsFlaskAppThread (threading.Thread):
    """Second listener for the same app, with TLS, on :9443.

    Exists for one reason: browsers only allow getUserMedia (the microphone,
    needed by the phone page's in-call audio) in a secure context. The cert
    is self-signed, generated at first boot by the init script with px5g;
    if it is missing this thread simply never starts and the plain-HTTP
    service is unaffected.
    """
    CERT = "/etc/pcat-manager-web/ssl/cert.pem"
    KEY = "/etc/pcat-manager-web/ssl/key.pem"
    PORT = 9443

    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True

    def run(self):
        if not (os.path.exists(self.CERT) and os.path.exists(self.KEY)):
            print("No TLS cert, https listener disabled")
            return
        print("Start https web service on port", self.PORT)
        try:
            flask_app.run(host="0.0.0.0", port=self.PORT,
                          ssl_context=(self.CERT, self.KEY),
                          threaded=True, use_reloader=False)
        except Exception as error:
            print("!!! Failed to start https web service", error)

flask_app = Flask(__name__)

# Make csrf_token available in templates
flask_app.jinja_env.globals['csrf_token'] = generate_csrf_token

# Add cache bust version based on startup time
startup_time = int(time.time())

def _voice_capable_modem():
    """True when the fitted modem actually carries in-call audio.

    RM500U-CNV and RM520N-GL are the modules that do. EM05-CN never clocks the
    PCM link (its firmware routes call audio at an absent codec chip and
    ignores AT+QDAI), and RM500Q-GL cannot enable PCM voice at all -- the one
    AT+QPCMV mode it accepts is USB audio, which re-enumerates the module
    without the AT port this client needs. See pc_call_audio.py.

    Non-blocking: the is_* helpers read the permanent ATI model cache. False
    while the model is still unknown (early boot) or the modem client isn't up
    yet."""
    try:
        mc = pc_app.modem_client
        return mc.is_500u() or mc.is_520()
    except Exception:
        return False


def _rev2_board():
    """True when the MCU reports a PCAT2-REV2 (A4) board.

    REV2 is the revision that brings the modem's PCM/SAI link out to the
    codec; on REV1 (A1~A3) there is no path for call audio at all. Older,
    jumper-blind MCU firmware answers "NT2421A4" for every board, and the
    value is "" until the first answer lands -- neither is a REV2 claim, so an
    unknown board reads as unsupported. Offering telephony where it cannot
    work is the worse failure: the user enables it, dials, and hears silence.
    """
    try:
        return "REV2" in (pc_app.socket_client.pmu_hw_version or "").upper()
    except Exception:
        return False


def phone_capable():
    """Can this box do telephony at all? Both halves have to be present: a
    module that carries call audio, and a REV2 board to carry it on.

    Gates the settings toggle -- there is no point offering a switch that
    cannot do anything -- and, together with that toggle, the nav entry.
    On a Photonicat 2 /phone itself stays reachable by URL, like the other
    optional pages; on a Photonicat 1 the page is a 404.

    The board check is not redundant with the revision check: REV1/REV2 is a
    Photonicat 2 notion, and a Photonicat 1 that ever answered a REV2-looking
    string must still not be offered telephony."""
    return is_photonicat2 and _voice_capable_modem() and _rev2_board()


def phone_menu_enabled():
    """Nav-bar visibility for /phone: the user's opt-in, honoured only on
    hardware that can deliver it. Off by default -- a nav entry for a feature
    most boxes cannot use is noise, and the toggle that turns it on only
    appears where it means something."""
    return phone_capable() and bool(
        pcat_config.get_user_config_key("phone_enabled", False))


@flask_app.context_processor
def inject_cache_bust():
    terminal_enabled = pcat_config.get_user_config_key("terminal_enabled", True)
    return {'cache_bust_version': startup_time, 'terminal_enabled': terminal_enabled,
            'board_name': board_name,
            'is_photonicat2': is_photonicat2,
            'phone_enabled': phone_menu_enabled(),
            'nas_enabled': pcat_config.get_user_config_key("nas_enabled", True),
            # LXC is a Photonicat 2 feature; a stored opt-in carried onto a
            # Photonicat 1 must not surface the nav entry there.
            'lxc_enabled': is_photonicat2 and pcat_config.get_user_config_key("lxc_enabled", False),
            'gps_enabled': pcat_config.get_user_config_key("gps_enabled", False),
            # Rendered into <head> ahead of theme.js so an explicit device
            # appearance is known before first paint. "auto" means nobody has
            # chosen and each browser follows its own prefers-color-scheme.
            'device_theme': explicit_theme() or "auto",
            'theme_accent': theme_accent()}

# Generate or load persistent SECRET_KEY to maintain sessions across restarts
def get_or_create_secret_key():
    """Get existing secret key from config or create a new persistent one"""
    secret_key_path = "/etc/pcat-secret-key"

    # First check environment variable (for development/testing)
    env_key = os.environ.get('PCAT_SECRET_KEY')
    if env_key:
        return env_key

    # Try to load existing secret key from file
    try:
        if os.path.exists(secret_key_path):
            with open(secret_key_path, 'r') as f:
                stored_key = f.read().strip()
                if stored_key and len(stored_key) >= 64:  # Ensure it's a valid hex string
                    return stored_key
    except (IOError, OSError) as e:
        pcat_logger.logger.warning(f"Could not read secret key from {secret_key_path}: {e}")

    # Generate new secret key and save it persistently
    new_key = secrets.token_hex(32)
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(secret_key_path), exist_ok=True)

        # Write new key with restricted permissions (only readable by root)
        with open(secret_key_path, 'w') as f:
            f.write(new_key)

        # Set file permissions to 600 (only readable/writable by root)
        os.chmod(secret_key_path, 0o600)

        pcat_logger.logger.info(f"Generated new persistent secret key for session management")
    except (IOError, OSError) as e:
        pcat_logger.logger.warning(f"Could not save persistent secret key: {e}")
        # Fallback to in-memory key (sessions will be lost on restart)
        pass

    return new_key

flask_app.config['SECRET_KEY'] = get_or_create_secret_key()

# Secure session cookie configuration
# Set secure cookies only when using HTTPS (check environment or assume HTTP for embedded device)
is_https = os.environ.get('HTTPS', '').lower() in ('true', '1', 'on')
flask_app.config['SESSION_COOKIE_SECURE'] = is_https and not pcat_config.skip_auth
flask_app.config['SESSION_COOKIE_HTTPONLY'] = True
flask_app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
flask_app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)  # Default session lifetime

# Persistent remember-me tokens live in SQLite at /etc/pcat-remember.db so a
# "remember me" login survives a reboot even if SECRET_KEY is rotated (which
# invalidates every signed session cookie).
REMEMBER_COOKIE_NAME = 'pcat_remember'
REMEMBER_TTL_DAYS = 180
remember_token.init_remember_db()


def _set_remember_cookie(response, username):
    """Issue a fresh remember-me token and attach it to the response."""
    selector, validator, expires_at = remember_token.issue_token(
        username, ttl_days=REMEMBER_TTL_DAYS)
    if not selector:
        return response
    response.set_cookie(
        REMEMBER_COOKIE_NAME,
        f"{selector}:{validator}",
        max_age=REMEMBER_TTL_DAYS * 86400,
        httponly=True,
        samesite='Lax',
        secure=flask_app.config['SESSION_COOKIE_SECURE'],
    )
    return response


def _clear_remember_cookie(response):
    """Revoke the presented token server-side and delete the cookie."""
    selector, _ = remember_token.parse_cookie(request.cookies.get(REMEMBER_COOKIE_NAME))
    if selector:
        remember_token.revoke_token(selector)
    response.delete_cookie(REMEMBER_COOKIE_NAME)
    return response


@flask_app.before_request
def restore_session_lifetime():
    """Restore session lifetime, and rebuild the session from a remember-me token.

    If the signed session cookie is gone (SECRET_KEY rotated, cookie expired)
    but a valid remember-me token is present, log the user back in.
    """
    if 'username' in session and session.permanent:
        # Check if user chose to be remembered
        if session.get('remember_login'):
            flask_app.permanent_session_lifetime = timedelta(days=180)
        else:
            flask_app.permanent_session_lifetime = timedelta(days=30)
        return

    if 'username' in session:
        return

    selector, validator = remember_token.parse_cookie(
        request.cookies.get(REMEMBER_COOKIE_NAME))
    if not selector:
        return
    username = remember_token.validate_token(selector, validator)
    if not username:
        return

    session['username'] = username
    session['remember_login'] = True
    session.permanent = True
    flask_app.permanent_session_lifetime = timedelta(days=REMEMBER_TTL_DAYS)
    # Rotate: the presented token is single-use, a fresh one is issued in
    # after_request. This limits the damage from a stolen cookie.
    remember_token.revoke_token(selector)
    g.reissue_remember_for = username


@flask_app.after_request
def reissue_remember_cookie(response):
    """Attach a rotated remember-me token when a token login just happened."""
    username = getattr(g, 'reissue_remember_for', None)
    if username:
        _set_remember_cookie(response, username)
    return response


# Endpoints (view fn names) that do their own CSRF handling or must be exempt.
# login_action validates its own form token before a session exists; the
# socket.io transport carries its own handshake, not a browser form POST.
_CSRF_EXEMPT_ENDPOINTS = frozenset({"login_action"})
# Endpoints a same-device (localhost) caller may POST without a CSRF token:
# lets scripts/cron sync the clock from GNSS on offline installs and keep
# AGPS assist data fresh (curl -X POST http://127.0.0.1/api/v1/gps/...).
# Browser CSRF cannot originate from the device's own loopback, so this does
# not widen that attack surface, and it mirrors the localhost trust
# api_auth_required already extends.
_CSRF_LOCALHOST_EXEMPT_ENDPOINTS = frozenset({"gps_sync_time_api",
                                              "gps_agps_update_api",
                                              "gps_celldb_download_api"})


# ── credential scrubbing for logs ────────────────────────────────────────────
# The web log (RotatingFileHandler) lands 0644 in /tmp, so request bodies must
# never carry secrets into it in cleartext. These handlers historically logged
# str(p_data) verbatim -- which for the wireless / APN / Smart-WAN saves meant
# the WiFi PSK, the APN password, and EAP credentials were written to a
# world-readable file. Scrub before logging.
_SENSITIVE_SUBSTRINGS = ("password", "passwd", "secret", "token", "psk")
_SENSITIVE_EXACT = {"key", "pin", "pass", "identity", "ca_cert"}


def _redact_sensitive(value):
    """Return a deep copy of `value` (dict/list/scalar) with any credential-
    bearing field masked, for safe logging. Matches nested structures (e.g. an
    'eap' sub-dict) and leaves non-sensitive fields untouched."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kl = str(k).lower()
            if any(s in kl for s in _SENSITIVE_SUBSTRINGS) or kl in _SENSITIVE_EXACT:
                out[k] = "***" if v not in (None, "", [], {}) else v
            else:
                out[k] = _redact_sensitive(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(v) for v in value]
    return value


@flask_app.before_request
def _mark_web_activity():
    """Record that a client is actively talking to us. The App loop reads this
    to keep polling the STM32 for live SoC/route/modem state at the fast 10s
    cadence while the browser or the always-on mini display is watching (both
    poll HTTP GETs), and to relax to a slower cadence when the device is truly
    headless. Static assets count too -- harmless, they only load on page open.
    One timestamp write; must never block or raise the request."""
    try:
        pc_app.note_web_activity()
    except Exception:
        pass


@flask_app.before_request
def csrf_guard():
    """Global CSRF enforcement on state-changing requests. Browsers attach the
    session cookie to cross-site POSTs (SameSite=Lax only blocks some), so a
    malicious page could otherwise drive reboot/reset/config changes. Requires
    a valid token (X-CSRF-Token header, injected by the fetch wrapper in
    _base.html, or a csrf_token form field) that matches the one in the signed
    session. Read-only methods and same-process localhost callers (the mini
    display polls GET-only) are unaffected."""
    if request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
        return
    if request.endpoint in _CSRF_EXEMPT_ENDPOINTS:
        return
    if (request.endpoint in _CSRF_LOCALHOST_EXEMPT_ENDPOINTS
            and request.remote_addr in ('127.0.0.1', '::1')):
        return
    # socket.io has its own endpoint(s); never a browser form we issue tokens for
    if request.path.startswith('/socket.io'):
        return
    token = (request.headers.get('X-CSRF-Token')
             or request.form.get('csrf_token'))
    if not validate_csrf_token(token):
        return jsonify({"status": "failed",
                        "message": "Security token invalid"}), 400

if os.getenv('FLASK_TEMPLATE_RELOAD', '0') in ('development','1','true','True'):
    print("SETTING TEMPLATES_AUTO_RELOAD is True")
    flask_app.config['TEMPLATES_AUTO_RELOAD'] = True
    flask_app.jinja_env.auto_reload = True
else:
    flask_app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 3600000
# Server→client push. Was flask_socketio; see pcat_util/event_bus for why it
# could not stay (every connected socket.io client permanently held one of
# granian's four request slots, issue #113). Same publish/subscribe shape,
# short polls instead of a held connection.
#
# The names below mirror the flask_socketio API they replace, so the handlers
# and background workers read the same as before:
#   socketio.emit(evt, data)       -> publish(evt, data)        broadcast
#   socketio.emit(evt, d, to=room) -> publish(evt, data, room)   one room
#   emit(evt, data)                -> reply(evt, data)           to the caller
#   join_room(room)                -> join(room)
#   @socketio.event                -> @bus_event
_EVENT_HANDLERS = {}

# Per-request scratch for a dispatched handler: what it replied to its caller
# and which rooms it joined on that caller's behalf. Thread-local because
# granian runs handlers on a pool of blocking threads, so several clients can
# be inside dispatch at once.
_dispatch_ctx = threading.local()


def bus_event(fn):
    """Register a client→server command, keyed by function name.

    Same convention as @socketio.event: the client emits the handler's own
    name. Kept so the call sites in the templates need no renaming.
    """
    _EVENT_HANDLERS[fn.__name__] = fn
    return fn


def publish(event, data=None, room=None):
    """Broadcast to every polling client, or to one room."""
    event_bus.publish(event, data, room=room)


def reply(event, data=None):
    """Answer the client that sent the command currently being dispatched.

    The old bare emit() went to the calling socket alone, so these must not go
    through the broadcast buffer -- a per-caller 'Welcome' or a scrollback
    replay landing in every other tab would be a bug, not a nicety. Outside a
    dispatch (a background thread) there is no caller, and this is a no-op.
    """
    replies = getattr(_dispatch_ctx, "replies", None)
    if replies is not None:
        replies.append({"event": event, "data": {} if data is None else data})


def join(room):
    """Subscribe the calling client to a room for its later polls."""
    rooms = getattr(_dispatch_ctx, "join", None)
    if rooms is not None and room not in rooms:
        rooms.append(room)


def _start_background_task(target, *args, **kwargs):
    """Replacement for socketio.start_background_task.

    Returns the Thread so the callers that keep a handle -- install_thread,
    firmware_upgrade_thread -- can still test it for None to single-flight.
    Daemon, because these must not hold a shutdown open; the firmware worker
    already survives that by writing its state into firmware_upgrade_status.
    """
    thread = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
    thread.start()
    return thread


# NAS push channel: the job engine and disk-hotplug pass emit through these
# instead of importing flask/the bus themselves (keeps them unit-testable).
nas_jobs.set_emitter(lambda job: publish("nas_job_status", job))
nas_manager.set_emitter(lambda event, payload: publish(event, payload))

# Suppress noisy werkzeug request logs for socket.io polling
import logging as _logging
_werkzeug_logger = _logging.getLogger('werkzeug')

class _SocketIOPollFilter(_logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if '/socket.io/' in msg and 'transport=polling' in msg:
            return False
        return True

_werkzeug_logger.addFilter(_SocketIOPollFilter())

# Add security headers to all responses
@flask_app.after_request
def add_security_headers(response):
    # SAMEORIGIN (not DENY): the /lxc page embeds /terminal in an iframe for
    # the container console. Same-origin only — third-party framing stays
    # blocked, mirrored by frame-ancestors 'self' in the CSP below (which is
    # what modern browsers actually honor).
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    # CSP allowing inline scripts for AlpineJS but restricting other sources
    response.headers['Content-Security-Policy'] = ("default-src 'self'; "
                                                   "frame-ancestors 'self'; "
                                                   "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                                                   "style-src 'self' 'unsafe-inline'; "
                                                   "img-src 'self' data:; "
                                                   "connect-src 'self' ws: wss: *; "
                                                   "font-src 'self'; "
                                                   # The phone page's TTS preview plays the synthesized
                                                   # WAV straight from the fetch response via
                                                   # URL.createObjectURL(). media-src has no default of
                                                   # its own, so without this it falls back to
                                                   # default-src 'self', which does not cover blob: --
                                                   # the <audio> load is blocked and the preview button
                                                   # silently produces no sound.
                                                   "media-src 'self' blob:; "
                                                   # The /dev 3D model is Draco-compressed and
                                                   # DRACOLoader decodes it in a Web Worker built from
                                                   # a blob: URL. Workers fall back to script-src,
                                                   # which does not allow blob:, so without this the
                                                   # worker is blocked and the model never decodes.
                                                   "worker-src 'self' blob:; "
                                                   "object-src 'none'; "
                                                   "base-uri 'self';")
    # Cache static assets for 3 months; versioned files (?v=) are cache-busted on deploy
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=7776000'
    elif 'Cache-Control' not in response.headers:
        # HTML pages / API JSON carry inline Alpine JS and live state, so they
        # must never be served from a stale browser cache. Without an explicit
        # header the browser applies heuristic caching and reuses an old page
        # (old inline JS) after a redeploy — the "I changed the template but the
        # page looks the same" trap. no-cache = always revalidate before use.
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response

def render_error_page(code):
    """Styled 404/500/etc. page (error.html, cute cat included). Must never
    raise: a second failure inside an error handler would mask the original
    error, so any problem here falls back to a bare-text body."""
    try:
        locales = _locales.get_locales("error_page", current_locale())
        locales.update(base_locales())
        return render_template("error.html", code=code, locales=locales), code
    except Exception:
        pcat_logger.logger.exception("error page render failed (code %s)", code)
        return "Internal Server Error" if code >= 500 else f"Error {code}", code


# Styled pages for aborted/unrouted requests (404, 405, CSRF 400, ...).
# RequestRedirect from the URL router is also an HTTPException (3xx) and must
# pass through untouched. API callers get JSON, not an HTML page.
@flask_app.errorhandler(HTTPException)
def handle_http_error(e):
    if not e.code or e.code < 400:
        return e
    if request.path.startswith('/api/'):
        return jsonify({"status": "error", "code": e.code, "message": e.name}), e.code
    return render_error_page(e.code)


# Log the full traceback for any unhandled exception. Without this, an uncaught
# error in a view (e.g. a template render or config read that only fails on the
# device) surfaces as a bare "500" with no detail in the browser and nothing
# actionable in the log.
@flask_app.errorhandler(Exception)
def log_unhandled_exception(e):
    # HTTPExceptions normally go to handle_http_error; this branch only runs
    # if that handler itself blew up, in which case pass the response through.
    if isinstance(e, HTTPException):
        return e
    import traceback
    pcat_logger.logger.error(
        "Unhandled exception on %s %s:\n%s",
        request.method, request.path, traceback.format_exc())
    if request.path.startswith('/api/'):
        return jsonify({"status": "error", "code": 500, "message": "Internal Server Error"}), 500
    return render_error_page(500)

## OEM image serving from boot1 base64 data
@flask_app.route("/oem/image/<key>")
def oem_image(key):
    # Only allow safe key names
    if not re.match(r'^[a-z0-9_]+$', key):
        return "Not found", 404
    data, mime = oem_config.get_oem_image(key)
    if data is None:
        return "Not found", 404
    resp = make_response(data)
    resp.headers['Content-Type'] = mime
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp

## Theme accent colour (LCD colour theme -> browser favicon)

# The /screen colour themes recolour the LCD; this mirrors the chosen colour
# onto the web favicon so both match. Stored in our own user config rather
# than read back from the display daemon, so it also works on boxes without a
# screen and needs no daemon round-trip per favicon request.
THEME_ACCENT_KEY = "theme_accent"
DEFAULT_ACCENT = "#FDE021"          # Phonicat brand yellow (the shipped icon)
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def theme_accent():
    value = pcat_config.get_user_config_key(THEME_ACCENT_KEY, "") or ""
    return value if HEX_COLOR_RE.match(value) else DEFAULT_ACCENT


_favicon_raster_cache = {}          # (kind, color) -> bytes


def _tinted_raster(kind, color):
    """The shipped favicon recoloured to `color`, as PNG/ICO bytes.

    The shipped icons are a single flat colour over transparency, so keeping
    each pixel's alpha and replacing its RGB reproduces the artwork exactly in
    the new colour — no resampling, no palette guessing.

    Returns None when Pillow is missing, so the caller can fall back to the
    static file rather than 500.
    """
    key = (kind, color)
    if key in _favicon_raster_cache:
        return _favicon_raster_cache[key]
    try:
        from PIL import Image
    except ImportError:
        return None
    src = {"png32": "favicon-32x32.png", "png16": "favicon-16x16.png",
           "ico": "favicon-32x32.png"}.get(kind)
    if not src:
        return None
    try:
        img = Image.open(os.path.join(flask_app.static_folder, src))
        img = img.convert("RGBA")
        rgb = (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
        alpha = img.getchannel("A")
        tinted = Image.new("RGBA", img.size, rgb + (255,))
        tinted.putalpha(alpha)
        buf = io.BytesIO()
        if kind == "ico":
            tinted.save(buf, format="ICO", sizes=[(32, 32), (16, 16)])
        else:
            tinted.save(buf, format="PNG")
        data = buf.getvalue()
    except Exception as e:
        pcat_logger.logger.warning("favicon tint failed: %s", e)
        return None
    _favicon_raster_cache[key] = data
    return data


@flask_app.route("/favicon-theme.png")
def favicon_theme_png():
    """Themed raster icon. Chrome prefers a raster over an SVG when both are
    offered, so these must be themed too or the tab keeps the shipped colour."""
    kind = "png16" if request.args.get("s") == "16" else "png32"
    data = _tinted_raster(kind, theme_accent())
    if data is None:
        return redirect("/static/favicon-{0}x{0}.png".format(
            16 if kind == "png16" else 32))
    resp = make_response(data)
    resp.headers['Content-Type'] = 'image/png'
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@flask_app.route("/favicon-theme.ico")
def favicon_theme_ico():
    data = _tinted_raster("ico", theme_accent())
    if data is None:
        return redirect("/static/favicon.ico")
    resp = make_response(data)
    resp.headers['Content-Type'] = 'image/x-icon'
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@flask_app.route("/favicon-theme.svg")
def favicon_theme_svg():
    """The brand cube tinted with the active theme colour.

    Public on purpose: a favicon is also fetched for /login, and it leaks
    nothing beyond a colour. Built from static/logo.svg so the geometry has a
    single source; the PNG/ICO links stay in place for browsers that do not
    take an SVG icon.
    """
    try:
        with open(os.path.join(flask_app.static_folder, "logo.svg"),
                  encoding="utf-8") as fh:
            svg = fh.read()
    except OSError:
        return "Not found", 404
    # logo.svg paints the cube in the dark brand colour; retint every fill.
    svg = re.sub(r'fill="#[0-9A-Fa-f]{6}"',
                 'fill="{}"'.format(theme_accent()), svg)
    resp = make_response(svg)
    resp.headers['Content-Type'] = 'image/svg+xml'
    # The href carries the colour as a cache-buster, so this can cache hard.
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


## Helper functions

def base_locales():
    locale_name = current_locale()
    
    # Check cache first
    cache_key = f"base_locales_{locale_name}"
    with device_cache['lock']:
        now = time.time()
        if (cache_key in device_cache and 
            now - device_cache['base_locales_timestamp'] < BASE_LOCALES_CACHE_DURATION):
            return device_cache[cache_key]

    oem_locales = {
        "logo_uri": pcat_config.get_logo_uri(),
        "vendor_site_domain": pcat_config.get_vendor_site_domain(),
        "vendor_wiki_url": pcat_config.get_vendor_wiki_url(),
        "text_logo": pcat_config.get_text_logo_uri(),
        "oem_name": pcat_config.get_oem_name(),
        "forum_url": oem_config.get_forum_url(),
        "tips_url": oem_config.get_tips_url(),
        "favicon_16": oem_config.get_favicon_uri(16),
        "favicon_32": oem_config.get_favicon_uri(32),
        "favicon_ico": oem_config.get_favicon_uri("ico"),
        "apple_touch_icon": oem_config.get_favicon_uri("apple"),
    }

    locales = _locales.get_base_locales(locale_name)
    locales.update(oem_locales)
    
    # Cache the result
    with device_cache['lock']:
        device_cache[cache_key] = locales
        device_cache['base_locales_timestamp'] = time.time()
    
    return locales

# Device-wide UI language, once someone has actually chosen one. Unset means
# "nobody has chosen", not "English".
UI_LANGUAGE_KEY = "ui_language"
VALID_LOCALES = ("EN", "CN", "zh_CN")


def explicit_locale():
    """The language an operator picked for this device, or None.

    Device-wide rather than per-session on purpose: the language is a property
    of the appliance, not of whoever is looking at it. Keeping it in the session
    meant two browsers open on the same page rendered in two different
    languages, and a language chosen on the settings page reverted the moment
    someone opened the UI from a different machine.
    """
    saved = pcat_config.get_user_config_key(UI_LANGUAGE_KEY, None)
    return saved if saved in VALID_LOCALES else None


def set_explicit_locale(locale):
    """Persist an operator's language choice for the whole device."""
    if locale not in VALID_LOCALES:
        return False
    pcat_config.set_user_config_key(UI_LANGUAGE_KEY, locale)
    return True


def current_locale():
    """The language to render in, most deliberate signal first.

    1. An explicit device-wide choice always wins, for every browser -- that is
       what "set the language" is expected to mean on an appliance.
    2. Otherwise a per-browser preference, which is all an unauthenticated
       visitor to the login page can set (see switch_locale_action).
    3. Otherwise the browser's own Accept-Language, evaluated per request so
       two machines each get their own language until someone chooses one.

    Nothing is cached into the session here. Caching the *detected* default was
    what made the first request's Accept-Language stick to that browser forever,
    so a later device-wide choice could not dislodge it.
    """
    chosen = explicit_locale()
    if chosen:
        return chosen

    per_browser = session.get("locale") or request.cookies.get('pcat_lang')
    if per_browser in VALID_LOCALES:
        return per_browser

    browser_lang = request.headers.get('Accept-Language', '').lower()
    # Default to English unless Chinese is explicitly preferred
    return "CN" if 'zh' in browser_lang else "EN"

def is_chinese_locale():
    """Helper function to check if current locale is any Chinese variant"""
    locale = current_locale()
    return locale in ["CN", "zh_CN"]


# Appearance follows the same rule as the language: "auto" is the absence of a
# choice, not a third setting to store. Kept as a string rather than a
# nullable field because "auto" is what the UI shows and what theme.js already
# means by "follow prefers-color-scheme".
UI_THEME_KEY = "ui_theme"
VALID_THEMES = ("light", "dark")


def explicit_theme():
    """The appearance an operator picked for this device, or None.

    Only light/dark are stored. "auto" is recorded by clearing the key, so a
    device that has been set back to auto is indistinguishable from one nobody
    ever touched -- which is what lets each browser follow its own
    prefers-color-scheme again.
    """
    saved = pcat_config.get_user_config_key(UI_THEME_KEY, None)
    return saved if saved in VALID_THEMES else None


def set_explicit_theme(theme):
    """Persist an appearance choice device-wide. "auto" clears it."""
    if theme == "auto":
        pcat_config.set_user_config_key(UI_THEME_KEY, None)
        return True
    if theme not in VALID_THEMES:
        return False
    pcat_config.set_user_config_key(UI_THEME_KEY, theme)
    return True

def login_required():
    if 'username' not in session:
        return redirect(url_for("login_action"))

def api_auth_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        client_ip = request.remote_addr
        if client_ip in ['127.0.0.1', '::1', 'localhost']:
            return f(*args, **kwargs)

        if 'username' not in session:
            return jsonify({"status": "failed", "message": "Login required"}), 401

        return f(*args, **kwargs)
    # Marker so the startup audit can tell a wrapped view from a raw one.
    # NB: @api_auth_required must be placed BELOW @flask_app.route (i.e. route
    # is the outermost/top decorator) or Flask registers the raw function and
    # this wrapper never runs. audit_route_auth() enforces that at startup.
    decorated_function._pcat_auth_wrapped = True
    return decorated_function


def api_session_strict(f):
    """Like api_auth_required but WITHOUT the localhost bypass. That bypass
    exists for the on-device mini display, which polls read-only status — it
    must never extend to endpoints that delete files, drop shares or manage
    credentials, where a localhost-originating request (SSRF, a compromised
    local process) would inherit full authority. Stack BELOW @flask_app.route
    exactly like api_auth_required."""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return jsonify({"status": "failed", "message": "Login required"}), 401
        return f(*args, **kwargs)
    decorated_function._pcat_auth_wrapped = True
    return decorated_function


def dev_only(f):
    """Gate a route to development runs. In production (dev_mode off and not
    --skip-auth) the endpoint returns 404 as if it did not exist, keeping
    debug/test surface out of shipped builds. Stack BELOW @flask_app.route and
    ABOVE @api_auth_required so the 404 check runs first. Preserves the auth
    marker so audit_route_auth() still sees the wrapped inner view."""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not pcat_config.dev_mode_enabled():
            from flask import abort
            abort(404)
        return f(*args, **kwargs)
    # Carry through the auth marker if the wrapped view already has it, so the
    # startup audit does not flag this route as unprotected.
    if getattr(f, "_pcat_auth_wrapped", False):
        decorated_function._pcat_auth_wrapped = True
    return decorated_function


# Views that are intentionally reachable without a session. Anything else that
# is unwrapped trips the startup audit. Endpoint names are Flask view fn names.
_PUBLIC_ENDPOINTS = frozenset({
    "login_action", "logout_action", "static",
    "oem_image", "tos_action", "switch_locale_action",
    "advanced_admin_action",  # LuCI SSO for authed users; plain redirect otherwise
    # Read-only no-login browse of samba shares the user explicitly flagged
    # public (per-share pcat_http_public option); GET-only and path-jailed.
    "public_share_action",
})


def audit_route_auth():
    """Fail loudly at startup if any route's view is neither auth-wrapped nor
    explicitly public. Catches the decorator-order footgun (auth above route =
    silently inert) before it ships an unauthenticated endpoint."""
    unprotected = []
    for rule in flask_app.url_map.iter_rules():
        view = flask_app.view_functions.get(rule.endpoint)
        if view is None:
            continue
        if rule.endpoint in _PUBLIC_ENDPOINTS:
            continue
        if getattr(view, "_pcat_auth_wrapped", False):
            continue
        # allow views that gate auth inline (they close over the session check)
        src = ""
        try:
            import inspect
            src = inspect.getsource(view)
        except Exception:
            pass
        # _socket_authed() is the event-bus pair's inline gate (skip_auth or a
        # session), the same check the socket.io handshake used before it.
        if ("'username' not in session" in src or "_wifi_wan_auth_ok" in src
                or "_socket_authed()" in src):
            continue
        unprotected.append((rule.endpoint, sorted(rule.methods - {"HEAD", "OPTIONS"})))
    if unprotected:
        msg = "SECURITY: {} route(s) reachable without auth: {}".format(
            len(unprotected), unprotected)
        pcat_logger.logger.error(msg)
        print("!!!", msg)
    return unprotected

def _socket_authed():
    """Session-based auth check for event-bus handlers. Bus commands share the
    Flask session cookie, so every handler that mutates state or reads
    privileged data must gate on this. Localhost is NOT bypassed here: the
    mini display polls plain HTTP GETs and never drives the bus, so there is no
    legitimate same-host client to exempt."""
    return pcat_config.skip_auth or ('username' in session)


@flask_app.route("/api/v1/events.json")
def events_poll_action():
    """Server→client event stream, as a short poll.

    `since` is the last sequence number this client saw; omit it on the first
    call to start at the current end of the buffer rather than replay history.
    `rooms` is the comma-separated list of rooms the client was told to join.

    This returns immediately in every case -- that is the whole point of the
    endpoint. Blocking here to wait for an event is what socket.io did, and
    with granian's fixed --blocking-threads pool it starved the server (issue
    #113); see pcat_util/event_bus."""
    if not _socket_authed():
        return jsonify({"status": "failed", "message": "Unauthorized"}), 403

    raw_since = request.args.get("since")
    try:
        since = int(raw_since) if raw_since not in (None, "") else None
    except ValueError:
        since = None
    rooms = [r for r in (request.args.get("rooms") or "").split(",") if r]

    cursor, events, resync = event_bus.poll(since, rooms)
    return jsonify({"cursor": cursor, "events": events, "resync": resync})


@flask_app.route("/api/v1/events/emit.json", methods=["POST"])
def events_emit_action():
    """Client→server command dispatch (the old socket.io emit direction).

    Replies the handler addressed to its caller, plus any rooms it joined, come
    back in this response rather than over the stream: they belong to this
    client alone and must not reach anyone else's poll."""
    if not _socket_authed():
        return jsonify({"status": "failed", "message": "Unauthorized"}), 403

    body = request.get_json(silent=True) or {}
    name = body.get("event")
    handler = _EVENT_HANDLERS.get(name)
    if handler is None:
        return jsonify({"status": "failed", "message": "Unknown event"}), 404

    # Where the buffer stood before the handler ran. A handler that subscribes
    # its caller to a room (terminal_open) usually also starts producing for
    # that room immediately -- the PTY reader publishes the shell prompt within
    # milliseconds. A poll already in flight would return a cursor past that
    # prompt while still filtering it out, and the client would never see it.
    # Handing back the pre-dispatch cursor lets a client that just joined rewind
    # to it; the client de-duplicates by sequence number, so the broadcasts it
    # already saw in between are not delivered twice.
    join_from = event_bus.cursor()
    _dispatch_ctx.replies = []
    _dispatch_ctx.join = []
    try:
        handler(body.get("data") or {})
        return jsonify({"status": "ok",
                        "replies": _dispatch_ctx.replies,
                        "join": _dispatch_ctx.join,
                        "join_from": join_from})
    finally:
        # Never leave the scratch behind on a pooled thread: the next request
        # to land on it would otherwise collect this one's replies.
        _dispatch_ctx.replies = None
        _dispatch_ctx.join = None


@bus_event
def client_connected(message):
    if not _socket_authed():
        return
    reply('connected_response', {'data': 'Welcome\n'})

@bus_event
def request_install(message):
    if not _socket_authed():
        return
    filename = message.get("filename")
    global install_thread
    with thread_lock:
        if install_thread is None:
            install_thread = _start_background_task(background_install_thread, "local", filename,
                                                    bool(message.get("verify_tls")))
    reply('log_response', {'data': "Start install {filename}\n".format(filename = filename)})

@bus_event
def request_install_package(message):
    if not _socket_authed():
        return
    package = message.get("package")
    global install_thread
    with thread_lock:
        if install_thread is None:
            install_thread = _start_background_task(background_install_thread, "remote", package,
                                                    bool(message.get("verify_tls")))
    reply('log_response', {'data': "Start download {package}\n".format(package = package)})

@flask_app.route("/tos")
def tos_action():
    return render_template(nt.tos_file_name)

@flask_app.route("/")
def home_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    c_locale = current_locale()
    locales = _locales.get_locales("home", c_locale)

    locales.update(base_locales())
    
    return render_template(nt.index_file_name, locales = locales, board_name = board_name)

@flask_app.route("/api/v1/board_name")
@api_auth_required
def board_name_action():
    return board_name

@flask_app.route("/switch_locale")
def switch_locale_action():
    c_locale = current_locale()
    
    # Cycle through languages: EN -> CN -> EN
    if c_locale == "EN":
        new_locale = "CN"
    else:  # CN or any other
        new_locale = "EN"
    
    # Update session (the per-user UI language toggle; safe to allow pre-login
    # so the login page itself is translatable).
    session["locale"] = new_locale

    # Persist device-wide, and sync the OpenWrt LuCI setting, only for an
    # authenticated user. Both are persistent writes, so an unauthenticated (or
    # cross-site GET) caller must not be able to flip the language for the whole
    # appliance. See _PUBLIC_ENDPOINTS: this route stays public for the
    # session/cookie flip, but the privileged writes are gated here.
    #
    # That gate is also why the session/cookie above still exists: it is all a
    # visitor on the login page can set, and it applies only until an operator
    # makes a real choice, which then wins everywhere (see current_locale).
    if 'username' in session:
        set_explicit_locale(new_locale)
        luci_lang = "zh_cn" if new_locale == "CN" else "en"
        _openwrt_uci_set(f"luci.main.lang={luci_lang}")
        _openwrt_uci_commit("luci")

    # Create response and set cookie to remember preference
    # Redirect back to the referring page instead of always going to /
    referrer = request.referrer
    redirect_url = referrer if referrer else "/"
    response = make_response(redirect(redirect_url))
    response.set_cookie('pcat_lang', new_locale, max_age=365*24*60*60)  # 1 year
    return response

@flask_app.route("/api/v1/theme.json", methods=["POST"])
@api_session_strict
def save_theme_api():
    """Persist the appearance picked from the nav menu.

    Separate from the general-settings save because those buttons apply
    instantly and the page has no Save step. Session-strict rather than
    api_auth_required: this is a persistent device-wide write, and the
    localhost bypass exists for the mini display's read-only polling, which has
    no business changing how everyone's browser looks.

    An unauthenticated caller (the login page's theme cycle) simply gets a 401
    and keeps its localStorage-only preference, which is the intended split.
    """
    data = request.get_json(silent=True) or {}
    theme = data.get("theme")
    if not set_explicit_theme(theme):
        return jsonify({"status": "failed", "message": "Invalid theme"}), 400
    return jsonify({"status": "ok", "theme": theme})


# ISP lookup cache - MCC/MNC data rarely changes
isp_lookup_cache = {}

@api_auth_required
def get_isp_info_from_mccmnc(mccmnc):
    """
    Get ISP information from MCC/MNC using cellular_constants mapping with caching
    Returns dict with long_name, short_name, native_name, act
    """
    # Check cache first to avoid frequent lookups
    if mccmnc in isp_lookup_cache:
        return isp_lookup_cache[mccmnc]
    
    from cellular_constants import MCCMNC_MAP
    
    if not mccmnc or mccmnc not in MCCMNC_MAP:
        result = {"long_name": "", "short_name": "", "native_name": "", "act": ""}
        isp_lookup_cache[mccmnc] = result  # Cache empty results too
        return result
    
    carrier_info = MCCMNC_MAP[mccmnc]
    
    # Check what type carrier_info actually is
    if not isinstance(carrier_info, str):
        pcat_logger.logger.error(f"MCCMNC_MAP['{mccmnc}'] returned {type(carrier_info)}: {repr(carrier_info)}")
        result = {"long_name": "", "short_name": "", "native_name": "", "act": ""}
        isp_lookup_cache[mccmnc] = result
        return result
    
    # Parse format: "中国电信 | China Telecom" or "AT&T (USA)"
    if " | " in carrier_info:
        native_name, english_name = carrier_info.split(" | ", 1)
        # Remove country info from English name if present
        english_name = english_name.split(" (")[0]
    else:
        # Single name format like "AT&T (USA)"
        english_name = carrier_info.split(" (")[0]
        native_name = english_name  # Use same name if no native provided
    
    # Use the full English name as short name
    short_name = english_name
    
    result = {
        "long_name": english_name,
        "short_name": short_name,
        "native_name": native_name,
        "act": "GSM"  # Default technology, could be enhanced based on MCC/MNC
    }
    
    # Cache the result for future lookups
    isp_lookup_cache[mccmnc] = result
    return result

def _cell_tier_label():
    """"4G" or "5G" for the fitted modem (EM05 is 4G-only)."""
    try:
        return "4G" if pc_app.modem_client.is_em05() else "5G"
    except Exception:
        return "5G"


def get_network_mode_label(network_mode):
    """Human-readable label for the user's "Internet via" selection.

    Mirrors the web UI's short labels (see locales network_mode_*_short) so the
    device mini-display can render the mode verbatim without duplicating the
    mapping. "auto" is shown as the failover chain Eth/4G or Eth/5G; the
    cellular tier follows the modem's capability (EM05 is 4G-only).

    In "priority" mode the label is the whole enabled failover chain in table
    order, e.g. "Eth/WiFi/5G" — every enabled slot, not just the two the legacy
    modes knew about. Slot names are kept short on purpose: "Eth/WiFi/5G" is
    103px in the mini-display's 15px Orbitron face, the widest string that
    still fits page 4's 106px value column without falling back to a ticker.

    Uses ASCII-only separators ("/") rather than an arrow: the mini-display
    renders with the Latin-only Orbitron font, which has no "→" glyph.
    """
    if network_mode == "priority":
        names = {"wan": "Eth", "wifi": "WiFi", "cellular": _cell_tier_label()}
        try:
            chain = [names[e["id"]] for e in wan_manager.get_priority()
                     if e["enabled"] and e["id"] in names]
        except Exception as e:
            pcat_logger.logger.warning(
                "network_mode_label: priority table unreadable: {}".format(e))
            return "Eth/{}".format(_cell_tier_label())
        if not chain:
            return "All Off"
        if len(chain) == 1:
            # Single uplink: keep the legacy phrasing ("Eth Only"/"Cell Only")
            # a device migrating from eth_only/cell_only used to show.
            return "Cell Only" if chain[0] in ("4G", "5G") else chain[0] + " Only"
        return "/".join(chain)
    if network_mode == "auto":
        return "Eth/{}".format(_cell_tier_label())
    return {
        "eth_only": "Eth Only",
        "cell_only": "Cell Only",
        "all_off": "All Off",
    }.get(network_mode, "Eth/{}".format(_cell_tier_label()))

# Dashboard data caching for efficient data sharing and performance
dashboard_cache = {
    'data': None,
    'timestamp': 0,
    'lock': threading.Lock(),
    'generation_time': 0  # Track data generation time for performance monitoring
}
DASHBOARD_CACHE_DURATION = 12  # seconds - must exceed the mini display's 10s
# poll interval, or every poll misses the cache and regenerates the dashboard

# Cache for expensive operations with longer duration
expensive_ops_cache = {
    'wifi_clients_count': {'value': 0, 'timestamp': 0},
    'wifi_interfaces': {'value': [], 'timestamp': 0},
    'dhcp_clients_count': {'value': 0, 'timestamp': 0},
    'wan_ethernet_speed': {'value': '1000M', 'timestamp': 0},
    'wifi_radio0_enabled': {'value': True, 'timestamp': 0},
    'onboard_wifi_enabled': {'value': True, 'timestamp': 0},
    'pcie_wifi_enabled': {'value': False, 'timestamp': 0},
}
EXPENSIVE_OPS_CACHE_DURATION = 5  # seconds - keeps active_egress (the LCD's top-bar icon source) fresh
WIFI_OPS_CACHE_DURATION = 60  # seconds

# Network interfaces cache for performance optimization
network_interfaces_cache = {
    'data': None,
    'timestamp': 0,
    'generation_time': 0,
    'lock': threading.Lock()
}
NETWORK_INTERFACES_CACHE_DURATION = 15  # seconds - network configs change infrequently

def get_cached_expensive_op(op_name, fetch_func):
    """Get cached result for expensive operations with longer cache duration"""
    current_time = time.time()
    cache_entry = expensive_ops_cache.get(op_name, {'value': None, 'timestamp': 0})
    
    # Use longer cache duration for WiFi operations
    cache_duration = WIFI_OPS_CACHE_DURATION if 'wifi' in op_name else EXPENSIVE_OPS_CACHE_DURATION
    
    if current_time - cache_entry['timestamp'] < cache_duration:
        return cache_entry['value']
    
    try:
        # Execute the expensive operation
        start_time = time.time()
        value = fetch_func()
        execution_time = (time.time() - start_time) * 1000
        
        # Log slow operations (>50ms)
        if execution_time > 50:
            pcat_logger.logger.warning(f"Slow dashboard operation {op_name}: {execution_time:.1f}ms")
        
        # Update cache
        expensive_ops_cache[op_name] = {'value': value, 'timestamp': current_time}
        return value
    except Exception as e:
        pcat_logger.logger.error(f"Error in dashboard operation {op_name}: {e}")
        return cache_entry['value']  # Return cached value on error

def get_network_interfaces_data_optimized():
    """Network interfaces data. The is_* helpers read a shared cached
    'uci show' snapshot (see pcat_util.openwrt.uci_show_dict), so this costs
    at most one uci fork per config per cache window instead of one per key."""

    # Process WAN interface status
    lan_enabled = pc_app.is_lan_interface_enabled()
    lan_mode = pc_app.get_lan_mode()
    wan_mode = pc_app.get_wan_mode()
    # Opening this page is a good hint a cable is about to go in; collapse the
    # power-save backoff so the port is probed now rather than up to 60s later.
    pc_app.kick_eth_powersave()
    wan_enabled = pc_app.is_wan_interface_enabled()
    onboard_wifi_enabled = pc_app.is_onboard_wifi_enabled()
    pcie_wifi_enabled = pc_app.is_pcie_wifi_enabled()
    wwan_enabled = pc_app.is_wwan_interface_enabled()

    # Device existence checks
    lan_exists = os.path.exists("/sys/class/net/eth1")
    wan_exists = os.path.exists("/sys/class/net/eth0")
    onboard_wifi_exists = os.path.exists("/sys/class/ieee80211/phy0")
    pcie_wifi_exists = os.path.exists("/sys/class/ieee80211/phy1")

    # Carrier/connection status
    wan_carrier = pc_app.does_wan_have_carrier() if wan_exists else False
    lan_carrier = pc_app.does_lan_have_carrier() if lan_exists else False

    # Gateway detection
    network_route_mode = pc_app.socket_client.network_route_mode
    # Which interface actually holds the default route (wan/lan/mobile/"") — used
    # to put the green "active" underline on exactly one icon.
    active_egress = pc_app.get_active_egress()
    # In Smart WAN mode the onboard radio is retuned as the upstream STA uplink,
    # so its own AP is parked and is_onboard_wifi_enabled() reports False. That
    # would otherwise render the header WiFi icon as "disabled" (gray + red
    # cross). This flag lets the UI show it as a live relay instead.
    # Which *physical* radio carries the STA matters for the header icons: the
    # upstream radio is user-selectable (onboard or PCIe), so the relay/green
    # state must land on the correct one of the two WiFi icons, not always the
    # onboard one. Resolve the class by device path (radioN numbering is not
    # stable), same as wifi_wan/command_parser do.
    wifi_sta_radio = ""  # "onboard" | "pcie" | "" (no active STA)
    try:
        wifi_sta_active = wifi_wan.is_active()
        if wifi_sta_active:
            sta_radio = wifi_wan.active_sta_radio()  # "radio0"/"radio1"/None
            if sta_radio:
                # _radio_by_class matches by device path (radioN numbering is
                # not stable), so this stays correct regardless of ordering.
                wifi_sta_radio = "pcie" if sta_radio == wifi_wan._radio_by_class("pcie") else "onboard"
    except Exception:
        wifi_sta_active = False

    return {
        "wan_enabled": wan_enabled,
        "wan_mode": wan_mode,
        "sim_enabled": wwan_enabled,
        "lan_enabled": lan_enabled,
        "lan_mode": lan_mode,
        "onboard_wifi_enabled": onboard_wifi_enabled,
        "pcie_wifi_enabled": pcie_wifi_enabled,
        "lan_exists": lan_exists,
        "wan_exists": wan_exists,
        "onboard_wifi_exists": onboard_wifi_exists,
        "pcie_wifi_exists": pcie_wifi_exists,
        "wan_carrier": wan_carrier,
        "lan_carrier": lan_carrier,
        "network_route_mode": network_route_mode,
        "active_egress": active_egress,
        "wifi_sta_active": wifi_sta_active,
        "wifi_sta_radio": wifi_sta_radio
    }

def get_network_interfaces_data():
    """Get network interfaces data with caching for performance optimization"""
    with network_interfaces_cache['lock']:
        current_time = time.time()

        # Return cached data if still valid
        if (network_interfaces_cache['data'] is not None and
            current_time - network_interfaces_cache['timestamp'] < NETWORK_INTERFACES_CACHE_DURATION):
            return network_interfaces_cache['data']

        # Start performance monitoring
        start_time = time.time()

        try:
            # Use optimized batch UCI operations for better performance
            interfaces_data = get_network_interfaces_data_optimized()

            # Get config values (these are typically fast)
            energy_saving_mode = pcat_config.config.getboolean('device', 'energy_saving_mode', fallback=True)
            network_mode = pcat_config.get_network_mode()

            response_json = {
                "network_mode": network_mode,
                "wan_enabled": interfaces_data["wan_enabled"],
                "wan_mode": interfaces_data.get("wan_mode", "auto"),
                "sim_enabled": interfaces_data["sim_enabled"],
                "lan_enabled": interfaces_data["lan_enabled"],
                "lan_mode": interfaces_data.get("lan_mode", "auto"),
                "onboard_wifi_enabled": interfaces_data["onboard_wifi_enabled"],
                "pcie_wifi_enabled": interfaces_data["pcie_wifi_enabled"],
                "energy_saving_mode": energy_saving_mode,
                "lan_exists": interfaces_data["lan_exists"],
                "wan_exists": interfaces_data["wan_exists"],
                "onboard_wifi_exists": interfaces_data["onboard_wifi_exists"],
                "pcie_wifi_exists": interfaces_data["pcie_wifi_exists"],
                "wan_carrier": interfaces_data["wan_carrier"],
                "lan_carrier": interfaces_data["lan_carrier"],
                "network_route_mode": interfaces_data["network_route_mode"],
                "active_egress": interfaces_data["active_egress"],
                "wifi_sta_active": interfaces_data["wifi_sta_active"],
                "wifi_sta_radio": interfaces_data["wifi_sta_radio"]
            }

            # Calculate and log performance metrics
            generation_time = (time.time() - start_time) * 1000  # Convert to milliseconds
            network_interfaces_cache['generation_time'] = generation_time

            # Log slow network interface generation (>50ms)
            if generation_time > 50:
                pcat_logger.logger.warning(f"Slow network interfaces generation: {generation_time:.1f}ms")
            elif generation_time > 25:
                pcat_logger.logger.info(f"Network interfaces generation: {generation_time:.1f}ms")

            # Cache the data
            network_interfaces_cache['data'] = response_json
            network_interfaces_cache['timestamp'] = current_time

            return response_json

        except Exception as e:
            pcat_logger.logger.error(f"Error generating network interfaces data: {e}")
            # Return cached data if available, otherwise return default values
            if network_interfaces_cache['data'] is not None:
                return network_interfaces_cache['data']
            else:
                # Return safe defaults
                return {
                    "network_mode": "auto",
                    "wan_enabled": True,
                    "sim_enabled": False,
                    "wifi_enabled": True,
                    "lan_enabled": True,
                    "onboard_wifi_enabled": True,
                    "pcie_wifi_enabled": False,
                    "energy_saving_mode": True,
                    "lan_exists": True,
                    "wan_exists": True,
                    "onboard_wifi_exists": True,
                    "pcie_wifi_exists": False,
                    "wan_carrier": False,
                    "lan_carrier": False,
                    "network_route_mode": "",
                    "active_egress": "",
                    "wifi_sta_active": False,
                    "wifi_sta_radio": ""
                }

def _overlay_live_link_fields(dashboard):
    """Patch cheap, link-sensitive fields onto a (possibly cached) dashboard
    payload. Carrier is a sysfs read, active egress is memoized for a few
    seconds, and the public/local WAN IPs live in the App thread's memory —
    none of these need the full 12s dashboard regen, but without this overlay
    the homepage can keep showing "ethernet connected" / a public IP for up
    to ~12s after the cable is yanked (homepage polls every 4s, cache is 12s).
    Returns a shallow copy so we never mutate the stored cache blob in place."""
    if not dashboard:
        return dashboard
    data = dict(dashboard)
    try:
        data["wan_carrier"] = pc_app.does_wan_have_carrier()
        data["lan_carrier"] = pc_app.does_lan_have_carrier()
        data["active_egress"] = pc_app.get_active_egress()
        # No usable uplink: force-clear IPs in the response even if the App
        # thread has not yet run its 30s flush — the UI must not show a public
        # address when the cable is out and nothing else is carrying traffic.
        if not data["active_egress"]:
            data["wan_ip"] = ""
            data["wan_ipv6"] = ""
            data["local_wan_ip"] = ""
        else:
            data["wan_ip"] = pc_app.main_app.wan_ip
            data["wan_ipv6"] = pc_app.main_app.wan_ipv6
            data["local_wan_ip"] = pc_app.main_app.local_wan_ip
        # Connection class from the daemon (wired/mobile/none); cheap memory read.
        data["connection"] = pc_app.socket_client.network_route_mode
        data["network_route_mode"] = pc_app.socket_client.network_route_mode
    except Exception as e:
        pcat_logger.logger.debug(f"live link overlay failed: {e}")
    return data


def modem_hw_present():
    """Is a 4G/5G module physically fitted? The modem AT tty node only exists
    while a module is present and powered. Rfkill-ing WWAN off also removes
    the tty, so treat powered-off as "present" and let the existing
    powered-off notice cover that state instead of a false "no modem"."""
    if pc_app.modem_client.modem_uart_connected or os.path.exists(pcat_config.modem_dev):
        return True
    return not pc_app.is_wwan_powered()


def get_dashboard_data():
    """Get dashboard data with caching to enable single refresh sharing"""
    with dashboard_cache['lock']:
        current_time = time.time()
        
        # Return cached data if still valid — but always overlay live link /
        # public-IP fields so unplug is visible on the next homepage poll.
        if (dashboard_cache['data'] is not None and 
            current_time - dashboard_cache['timestamp'] < DASHBOARD_CACHE_DURATION):
            return _overlay_live_link_fields(dashboard_cache['data'])
        
        # Start performance monitoring
        start_time = time.time()
        timing_breakdown = {}
        
        # Generate fresh data with optimized collection
        timing_start = time.time()
        # Use direct signal strength from modem client instead of socket client for better accuracy
        # Priority: CSQ first (for 2G/3G/4G), fallback to QRSRP for 5G with 2x multiplier
        signal_strength_pct = pc_app.modem_client.basic.get("cell_signal_percent_csq", 0)

        # Convert to int if it's a string
        try:
            signal_strength_pct = int(signal_strength_pct) if signal_strength_pct else 0
        except (ValueError, TypeError):
            signal_strength_pct = 0

        if signal_strength_pct == 0:
            qrsrp_pct = pc_app.modem_client.basic.get("cell_signal_percent_qrsrp", 0)
            try:
                qrsrp_pct = int(qrsrp_pct) if qrsrp_pct else 0
            except (ValueError, TypeError):
                qrsrp_pct = 0
            signal_strength_pct = min(int(qrsrp_pct * 1.5), 100)

        # Clamping
        if signal_strength_pct > 100:
            signal_strength_pct = 100
        elif signal_strength_pct < 0:
            signal_strength_pct = 0

        timing_breakdown['signal_processing'] = (time.time() - timing_start) * 1000

        
        # Add date/time fields for status bar compatibility
        timing_start = time.time()
        now = datetime.now()
        date_string = now.strftime("%Y-%m-%d")
        time_string = now.strftime("%H:%M")

        # Main dashboard data collection timing
        data_start = time.time()
        _wifi_wan_active = wifi_wan.is_active()
        _wifi_wan_state = (get_cached_expensive_op('wifi_wan_state', wifi_wan.get_state)
                           if _wifi_wan_active else {})
        dashboard = {
            "connection": pc_app.socket_client.network_route_mode, 
            "down_speed": pc_app.main_app.net_download_speed,
            "up_speed": pc_app.main_app.net_upload_speed,
            "uptime": pc_app.get_uptime(base_locales()),
            "sim_interface_enabled": pc_app.is_wwan_interface_enabled(),
            "wwan_powered": pc_app.is_wwan_powered(),
            "wan_interface_enabled": pc_app.is_wan_interface_enabled(),         
            "wan_carrier": pc_app.does_wan_have_carrier(),
            "wan_ethernet_speed": get_cached_expensive_op(f'wan_ethernet_speed_{current_locale()}', lambda: pc_app.get_wan_ethernet_speed(base_locales())),
            "lan_interface_enabled": pc_app.is_lan_interface_enabled(),
            "lan_carrier": pc_app.does_lan_have_carrier(),
            "lan_ethernet_speed": get_cached_expensive_op(f'lan_ethernet_speed_{current_locale()}', lambda: pc_app.get_lan_ethernet_speed(base_locales())),
            "hostname": pc_app.main_app.board_info.get("hostname"),
            "model": pc_app.socket_client.get_product_model(),
            "kernel": pc_app.main_app.board_info.get("kernel"),
            "firmware_version": pc_app.socket_client.stm32_firmware_version,
            "modem_model": pc_app.modem_client.basic.get("model", ""),
            "openwrt_version": pc_app.main_app.board_info.get("openwrt_version", ""),
            "machine_sn": pc_app.get_machine_sn(),
            "machine_sn_verified": pc_app.get_machine_sn_verified()[1],
            "wan_ip": pc_app.main_app.wan_ip,
            "wan_ipv6": pc_app.main_app.wan_ipv6,
            "local_wan_ip": pc_app.main_app.local_wan_ip,
            "carrier": usable_cell_tech(pc_app.modem_client.basic.get("cell_tech", "")) or pc_app.socket_client.modem_mode,
            "dhcp_clients_count": get_cached_expensive_op('dhcp_clients_count', pc_app.main_app.get_dhcp_clients_count),
            # Display coercion only: the shutdown logic reads the raw count and
            # must keep seeing None (unknown) rather than 0 (empty).
            "wifi_clients_count": get_cached_expensive_op(
                'wifi_clients_count',
                lambda: command_parser.get_wifi_clients_count() or 0),
            "wifi_interfaces": get_cached_expensive_op('wifi_interfaces', lambda: command_parser.parse_uci_show_wireless(False)),
            "wifi_wan_active": _wifi_wan_active,
            "wifi_wan_state": _wifi_wan_state,
            # Fields below are for the device mini-display (photonicat2_display_go):
            # the active egress lets it pick the right top-bar icon (wired / wifi /
            # mobile), the Smart WAN signal feeds a WiFi signal bar, and
            # network_mode is the user's "Internet via" selection.
            "active_egress": get_cached_expensive_op('active_egress', pc_app.get_active_egress),
            "network_mode": pcat_config.get_network_mode(),
            "network_mode_label": get_network_mode_label(pcat_config.get_network_mode()),
            "wifi_signal": _wifi_wan_state.get("signal"),
            "wifi_signal_percent": _wifi_wan_state.get("signal_percent"),
            "on_charging": pc_app.socket_client.on_charging,
            "charge_percent": pc_app.socket_client.charge_percent,
            "voltage": pc_app.socket_client.battery_voltage,
            "charge_voltage": pc_app.socket_client.charger_voltage,
            "board_temperature": pc_app.socket_client.board_temperature,
            "modem_temperature": pc_app.modem_client.basic.get("qtemp_sensors", ""),
            "modem_signal_strength": signal_strength_pct,
            "isp_name": pc_app.socket_client.isp_name,
            "sd_state": pc_app.sd_card_status(),
            "sd_info": get_cached_expensive_op('sd_info', pc_app.sd_card_info),
            "hw_variant": get_cached_expensive_op('hw_variant', pc_app.hw_variant),
            "nvme": get_cached_expensive_op('nvme_status', pc_app.nvme_status),
            "sim_state": pc_app.modem_client.get_inferred_sim_state(pc_app.socket_client.sim_state),

            # Additional fields for status bar compatibility
            "date": date_string,
            "time": time_string,
            "system_status": getattr(pc_app.main_app, 'system_status', 0),
            "carmode": pc_app.socket_client.charger_on_auto_start_mode,
            "network_route_mode": pc_app.socket_client.network_route_mode,
            "modem_mode": pc_app.socket_client.modem_mode or usable_cell_tech(pc_app.modem_client.basic.get("cell_tech", "")),
            "modem_installed": modem_hw_present(),

            # Idle shutdown monitoring status
            "idle_shutdown_enabled": getattr(pc_app.main_app, 'idle_shutdown_enabled', False),
            "idle_status": getattr(pc_app.main_app, 'idle_status', "unknown"),
            "idle_remaining_seconds": int(getattr(pc_app.main_app, 'idle_remaining_seconds', 0)),
            "idle_remaining_minutes": int(getattr(pc_app.main_app, 'idle_remaining_seconds', 0) / 60),

            # Low battery shutdown monitoring status
            "low_battery_shutdown_enabled": getattr(pc_app.main_app, 'low_battery_shutdown_enabled', False),
            "low_battery_threshold": getattr(pc_app.main_app, 'low_battery_threshold', 20),
            "low_battery_status": getattr(pc_app.main_app, 'low_battery_status', "unknown"),
            "sim_ccid": pc_app.modem_client.basic.get("sim_ccid", ""),
            "sim_cimi": pc_app.modem_client.basic.get("sim_cimi", ""),
            "sim_number": format_phone_number(pcat_config.get_user_config_key("custom_sim_number", None) or pc_app.modem_client.basic.get("sim_number", "")),
            "imei_number": pc_app.modem_client.basic.get("imei_num", ""),
            "cell_tech": pc_app.modem_client.basic.get("cell_tech", ""),
            "cell_signal_percent_qrsrp": pc_app.modem_client.basic.get("cell_signal_percent_qrsrp", ""),
            "cell_signal_percent_qrsrq": pc_app.modem_client.basic.get("cell_signal_percent_qrsrq", ""),
            "cell_signal_percent_csq": pc_app.modem_client.basic.get("cell_signal_percent_csq", ""),
            "cell_signal_percent_sinr": pc_app.modem_client.basic.get("cell_signal_percent_sinr", ""),
            "cell_mccmnc": pc_app.modem_client.basic.get("cell_mccmnc", ""),
            "cell_band": pc_app.modem_client.basic.get("cell_band", ""),
            "cell_id": pc_app.modem_client.basic.get("serving_cellid", ""),
            "cell_tech": pc_app.modem_client.basic.get("cell_tech", ""),
            "cell_tac": pc_app.modem_client.basic.get("serving_tac", ""),
            "cell_mccmnc": pc_app.modem_client.basic.get("cell_mccmnc", ""),
        }
        timing_breakdown['main_data_collection'] = (time.time() - data_start) * 1000

        # Power mode detection and timer info
        timing_start = time.time()
        carmode_info = pc_app.socket_client.charger_on_auto_start_mode
        schedule_events = pc_app.socket_client.schedule_power_events()
        timer_events = schedule_events.get("events", [])

        # Determine active power mode (1 of 4, mutually exclusive)
        saved_power_mode = pcat_config.get_power_mode()
        # Direct power is a Photonicat 2 MCU power-on mode; on a Photonicat 1
        # a stored "direct_power" (config carried over) means nothing and the
        # detection falls through to what the PMU is actually doing.
        if saved_power_mode == "direct_power" and is_photonicat2:
            active_power_mode = "force_power_mode"
            power_mode_remaining_minutes = 0
            car_countdown = 0
            car_timeout = 0
            next_event = None
        elif carmode_info.get("enabled") == 'true':
            active_power_mode = "car_mode"
            # Car mode: countdown is seconds remaining until shutdown after charger disconnect
            car_countdown = carmode_info.get("countdown", 0)
            car_timeout = carmode_info.get("timeout", 0)
            power_mode_remaining_minutes = round(car_countdown / 60, 1) if car_countdown > 0 else 0
            next_event = None
        elif len(timer_events) > 0:
            active_power_mode = "timer_mode"
            # Timer mode: calculate minutes until next scheduled event
            now = datetime.now()
            now_weekday_idx = (now.weekday() + 1) % 7  # Convert to 0=Sun,1=Mon,...,6=Sat
            now_minutes = now.hour * 60 + now.minute
            min_minutes_until = None
            next_event = None
            for ev in timer_events:
                ev_hour = ev.get("hour", 0)
                ev_minute = ev.get("minute", 0)
                ev_minutes = ev_hour * 60 + ev_minute
                ev_days = ev.get("days", [])
                # Check each scheduled day
                for d in ev_days:
                    d = int(d)
                    days_ahead = (d - now_weekday_idx) % 7
                    if days_ahead == 0 and ev_minutes > now_minutes:
                        minutes_until = ev_minutes - now_minutes
                    elif days_ahead == 0:
                        minutes_until = 7 * 1440 + (ev_minutes - now_minutes)
                    else:
                        minutes_until = days_ahead * 1440 + (ev_minutes - now_minutes)
                    if min_minutes_until is None or minutes_until < min_minutes_until:
                        min_minutes_until = minutes_until
                        next_event = ev
            power_mode_remaining_minutes = min_minutes_until if min_minutes_until is not None else 0
            car_countdown = 0
            car_timeout = 0
        else:
            active_power_mode = "manual_mode"
            power_mode_remaining_minutes = 0
            car_countdown = 0
            car_timeout = 0
            next_event = None

        dashboard.update({
            "active_power_mode": active_power_mode,
            "power_mode_remaining_minutes": power_mode_remaining_minutes,
            "car_mode_countdown_seconds": car_countdown,
            "car_mode_timeout_seconds": car_timeout,
            "timer_events_count": len(timer_events),
            "timer_next_event": {
                "action": next_event.get("action", "") if next_event else "",
                "time": next_event.get("time", "") if next_event else "",
                "minutes_until": power_mode_remaining_minutes if active_power_mode == "timer_mode" else 0,
            } if next_event or active_power_mode == "timer_mode" else None,
        })
        timing_breakdown['power_mode'] = (time.time() - timing_start) * 1000

        # PCAT2 battery sysfs timing
        timing_start = time.time()
        #if pcat2 machine:
        if pc_app.is_photonicat2():
            try:
                battery_voltage_us = int(open("/sys/class/power_supply/battery/voltage_now", "r").read().strip())
                battery_voltage_v = battery_voltage_us / 1000000
                battery_current_us = int(open("/sys/class/power_supply/battery/current_now", "r").read().strip())
                battery_current_a = battery_current_us / 1000000
                battery_wattage_w = battery_voltage_v * battery_current_a
                battery_soc = int(open("/sys/class/power_supply/battery/capacity", "r").read().strip())
                battery_status = pc_app.get_battery_charge_discharge_remain_time()
                remaining_time = battery_status.get("remaining_time")
                # Fallback: calculate from sysfs current if primary method failed.
                # Skipped while settling: for the first seconds after a DC plug/
                # unplug the primary estimator deliberately withholds a number
                # (power_now is still swinging), and this fallback reads the same
                # unsettled current -- computing it here would put back exactly
                # the false "0:10 left" the settling window exists to suppress.
                if (remaining_time is None and battery_current_a != 0
                        and not battery_status.get("settling")):
                    try:
                        energy_full_uWh = int(open("/sys/class/power_supply/battery/energy_full_design", "r").read().strip())
                        energy_full_Wh = energy_full_uWh / 1000000
                        power_w = abs(battery_voltage_v * battery_current_a)
                        if power_w > 0.01:
                            if battery_current_a > 0:
                                # Charging to 90%
                                hrs = energy_full_Wh * max(0, 90 - battery_soc) / 100 / power_w if battery_soc < 90 else 0
                            else:
                                # Discharging
                                hrs = energy_full_Wh * battery_soc / 100 / power_w
                            if hrs > 0:
                                h = int(hrs)
                                m = int(round((hrs - h) * 60 / 10) * 10)
                                if m == 60:
                                    h += 1
                                    m = 0
                                remaining_time = f"{h}:{m:02d}"
                    except Exception:
                        pass
                dashboard.update({
                    "battery_current_a": battery_current_a,
                    "battery_wattage_w": battery_wattage_w,
                    "battery_voltage_v": battery_voltage_v,
                    "battery_soc": battery_soc,
                    "battery_remaining_time": remaining_time,
                })
            except (FileNotFoundError, ValueError, PermissionError) as e:
                # Keep default values (0 for current/wattage, socket_client voltage for voltage)
                pcat_logger.logger.warning(f"Failed to read pcat2 battery info from sysfs: {e}")
            timing_breakdown['battery_sysfs'] = (time.time() - timing_start) * 1000
        
        # ISP lookup timing
        timing_start = time.time()
        # Get ISP info from modem data
        cell_isp_long_name = pc_app.modem_client.basic.get("cell_isp_long_name", "")
        cell_isp_short_name = pc_app.modem_client.basic.get("cell_isp_short_name", "")
        cell_isp_native_name = pc_app.modem_client.basic.get("cell_isp_native_name", "")
        cell_isp_act = pc_app.modem_client.basic.get("cell_isp_act", "")
        cell_mccmnc = dashboard["cell_mccmnc"]
        
        # If any ISP info is missing, try to fill from cellular_constants
        if not any([cell_isp_long_name, cell_isp_short_name, cell_isp_native_name]) and cell_mccmnc:
            try:
                isp_info = get_isp_info_from_mccmnc(cell_mccmnc)
                # Debug: Check what get_isp_info_from_mccmnc actually returned
                if not isinstance(isp_info, dict):
                    pcat_logger.logger.error(f"get_isp_info_from_mccmnc returned {type(isp_info)}: {repr(isp_info)}")
                    raise TypeError(f"Expected dict, got {type(isp_info)}")
                
                # Check each field before accessing
                for key in ["long_name", "short_name", "native_name", "act"]:
                    if key not in isp_info:
                        pcat_logger.logger.error(f"ISP info missing key '{key}': {isp_info}")
                        raise KeyError(f"Missing key '{key}' in ISP info")
                    if not isinstance(isp_info[key], str):
                        pcat_logger.logger.error(f"ISP info['{key}'] is {type(isp_info[key])}: {repr(isp_info[key])}")
                        raise TypeError(f"Expected string for '{key}', got {type(isp_info[key])}")
                
                cell_isp_long_name = cell_isp_long_name or isp_info["long_name"]
                cell_isp_short_name = cell_isp_short_name or isp_info["short_name"] 
                cell_isp_native_name = cell_isp_native_name or isp_info["native_name"]
                cell_isp_act = cell_isp_act or isp_info["act"]
            except Exception as e:
                pcat_logger.logger.warning(f"Failed to lookup ISP info for MCC/MNC {cell_mccmnc}: {e}")
        
        # Add ISP info to dashboard
        dashboard.update({
            "cell_isp_long_name": cell_isp_long_name,
            "cell_isp_short_name": cell_isp_short_name,
            "cell_isp_native_name": cell_isp_native_name,
            "cell_isp_act": cell_isp_act,
        })
        timing_breakdown['isp_lookup'] = (time.time() - timing_start) * 1000
        
        # Enhanced operator information timing
        timing_start = time.time()
        # Add enhanced operator information if MCC/MNC lookup was successful
        from cellular_constants import MCCMNC_MAP
        if cell_mccmnc and cell_mccmnc in MCCMNC_MAP:
            operator_full_info = MCCMNC_MAP[cell_mccmnc]
            
            # Check if operator_full_info is a string (handle potential tuple issue)
            if not isinstance(operator_full_info, str):
                pcat_logger.logger.error(f"Enhanced operator lookup - MCCMNC_MAP['{cell_mccmnc}'] returned {type(operator_full_info)}: {repr(operator_full_info)}")
                operator_full_info = ""  # Set to empty string to skip processing
            
            # Extract country information if available
            operator_country = ""
            if operator_full_info and " (" in operator_full_info and ")" in operator_full_info:
                country_match = operator_full_info.split(" (")[-1].rstrip(")")
                operator_country = country_match
            
            # Parse Chinese and English names
            enhanced_isp_full_cn = ""
            enhanced_isp_full_en = ""
            
            if operator_full_info and " | " in operator_full_info:
                # Bilingual format: "中国移动 | China Mobile"
                parts = operator_full_info.split(" | ", 1)
                enhanced_isp_full_cn = parts[0].strip()
                enhanced_isp_full_en = parts[1].strip()
                # Remove country from English name if present
                if " (" in enhanced_isp_full_en and ")" in enhanced_isp_full_en:
                    enhanced_isp_full_en = enhanced_isp_full_en.split(" (")[0].strip()
            elif operator_full_info:
                # Single language format: "AT&T (USA)" or "Vodafone (Netherlands)"
                if " (" in operator_full_info and ")" in operator_full_info:
                    enhanced_isp_full_en = operator_full_info.split(" (")[0].strip()
                else:
                    enhanced_isp_full_en = operator_full_info.strip()
                enhanced_isp_full_cn = enhanced_isp_full_en  # Use same for both if no Chinese
            
            # Determine if this is a bilingual entry
            is_bilingual = operator_full_info and " | " in operator_full_info
            
            # Add enhanced fields to dashboard
            dashboard.update({
                "enhanced_isp_available": True,
                "enhanced_isp_full": operator_full_info,
                "enhanced_isp_full_cn": enhanced_isp_full_cn,
                "enhanced_isp_full_en": enhanced_isp_full_en,
                "enhanced_isp_country": operator_country,
                "enhanced_isp_bilingual": is_bilingual,
                "enhanced_isp_mcc": cell_mccmnc[:3] if len(cell_mccmnc) >= 3 else "",
                "enhanced_isp_mnc": cell_mccmnc[3:] if len(cell_mccmnc) > 3 else "",
            })
        else:
            # No enhanced info available
            dashboard.update({
                "enhanced_isp_available": False,
                "enhanced_isp_full": "",
                "enhanced_isp_full_cn": "",
                "enhanced_isp_full_en": "",
                "enhanced_isp_country": "",
                "enhanced_isp_bilingual": False,
                "enhanced_isp_mcc": cell_mccmnc[:3] if cell_mccmnc and len(cell_mccmnc) >= 3 else "",
                "enhanced_isp_mnc": cell_mccmnc[3:] if cell_mccmnc and len(cell_mccmnc) > 3 else "",
            })
        
        # Also update the legacy isp_name field if it's empty
        if not dashboard["isp_name"] and (cell_isp_long_name or cell_isp_native_name):
            # Use Chinese name for Chinese locales, English for others
            if is_chinese_locale() and cell_isp_native_name:
                dashboard["isp_name"] = cell_isp_native_name
            elif cell_isp_long_name:
                dashboard["isp_name"] = cell_isp_long_name
        timing_breakdown['enhanced_operator'] = (time.time() - timing_start) * 1000
                
        # Calculate and log performance metrics
        generation_time = (time.time() - start_time) * 1000  # Convert to milliseconds
        dashboard_cache['generation_time'] = generation_time
        
        # Log slow dashboard generation (>100ms) with breakdown
        if generation_time > 100:
            breakdown_str = ", ".join([f"{k}: {v:.1f}ms" for k, v in timing_breakdown.items()])
            pcat_logger.logger.warning(f"Slow dashboard generation: {generation_time:.1f}ms [{breakdown_str}]")
        elif generation_time > 50:
            breakdown_str = ", ".join([f"{k}: {v:.1f}ms" for k, v in timing_breakdown.items()])
            pcat_logger.logger.info(f"Dashboard generation: {generation_time:.1f}ms [{breakdown_str}]")
        
        # Cache the data
        dashboard_cache['data'] = dashboard
        dashboard_cache['timestamp'] = current_time
        
        return dashboard

@flask_app.route("/api/v1/dashboard.json", methods=['GET'])
@api_auth_required
def dashboard_json():
    # Get request type parameter
    request_type = request.args.get('type', 'full')
    
    # Get cached dashboard data
    dashboard_data = get_dashboard_data()
    
    # Return filtered data based on request type
    if request_type == 'header':
        # Header data - essential info for status bar
        header_data = {
            "date": dashboard_data["date"],
            "time": dashboard_data["time"],
            "charge_percent": dashboard_data["charge_percent"],
            "on_charging": dashboard_data["on_charging"],
            "modem_signal_strength": dashboard_data["modem_signal_strength"],
            "system_status": dashboard_data["system_status"],
            "carmode": dashboard_data["carmode"],
            "network_route_mode": dashboard_data["network_route_mode"],
            "modem_mode": dashboard_data["modem_mode"],
            "modem_installed": dashboard_data.get("modem_installed", True),
            "sim_state": dashboard_data["sim_state"],
            "connection": dashboard_data["connection"],
            "wan_ip": dashboard_data["wan_ip"],
            "isp_name": dashboard_data["isp_name"]
        }
        rsp = make_response(header_data)
    elif request_type == 'body':
        # Body data - detailed info for main dashboard
        body_data = {k: v for k, v in dashboard_data.items() 
                    if k not in ['date', 'time', 'system_status', 'carmode']}
        rsp = make_response(body_data)
    else:
        # Full data - original behavior
        rsp = make_response(dashboard_data)
    
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/perf")
@api_auth_required
@pin_required
def performance_monitoring_page():
    """Performance monitoring and testing page"""
    locales = base_locales()
    return render_template("performance.html", locales=locales)

@flask_app.route("/api/v1/dashboard/performance.json", methods=['GET'])
@api_auth_required
def dashboard_performance():
    """Get dashboard performance metrics for monitoring and optimization"""
    performance_data = {
        "dashboard_cache": {
            "last_generation_time_ms": dashboard_cache.get('generation_time', 0),
            "cache_age_seconds": time.time() - dashboard_cache.get('timestamp', 0),
            "cache_valid": (time.time() - dashboard_cache.get('timestamp', 0)) < DASHBOARD_CACHE_DURATION,
            "cache_duration": DASHBOARD_CACHE_DURATION
        },
        "network_interfaces_cache": {
            "last_generation_time_ms": network_interfaces_cache.get('generation_time', 0),
            "cache_age_seconds": time.time() - network_interfaces_cache.get('timestamp', 0),
            "cache_valid": (time.time() - network_interfaces_cache.get('timestamp', 0)) < NETWORK_INTERFACES_CACHE_DURATION,
            "cache_duration": NETWORK_INTERFACES_CACHE_DURATION
        },
        "expensive_ops_cache": {},
        "cache_settings": {
            "dashboard_cache_duration": DASHBOARD_CACHE_DURATION,
            "network_interfaces_cache_duration": NETWORK_INTERFACES_CACHE_DURATION,
            "expensive_ops_cache_duration": EXPENSIVE_OPS_CACHE_DURATION
        }
    }
    
    # Add expensive operations cache status
    current_time = time.time()
    for op_name, cache_entry in expensive_ops_cache.items():
        cache_age = current_time - cache_entry['timestamp']
        performance_data["expensive_ops_cache"][op_name] = {
            "cache_age_seconds": cache_age,
            "cache_valid": cache_age < EXPENSIVE_OPS_CACHE_DURATION,
            "last_value": str(cache_entry['value'])[:100]  # Truncate for readability
        }
    
    rsp = make_response(performance_data)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

from html.parser import HTMLParser

# Production firmware download server (Caddy auto-index + latest_image.txt).
PROD_FIRMWARE_BASE_URL = "https://dl.photonicat.com/images/photonicat2/openwrt/"
# Photonicat 1 (rk3568) images live one directory over on both servers. The
# per-board split matters: a sysupgrade image for the other board must never
# be offered, so every firmware URL goes through the two helpers below.
PROD_FIRMWARE_BASE_URL_PCAT1 = "https://dl.photonicat.com/images/photonicat/openwrt/"
DEV_FIRMWARE_DIR = "rk3576-photonicat2-openwrt"
DEV_FIRMWARE_DIR_PCAT1 = "rk3568-photonicat-openwrt"


def prod_firmware_base_url():
    """Production image directory for this board, trailing slash included."""
    return PROD_FIRMWARE_BASE_URL if is_photonicat2 else PROD_FIRMWARE_BASE_URL_PCAT1


def dev_firmware_base_url():
    """Dev-server image directory for this board, trailing slash included."""
    directory = DEV_FIRMWARE_DIR if is_photonicat2 else DEV_FIRMWARE_DIR_PCAT1
    return f"{pcat_config.dev_server_base}/{directory}/"
# Cellular-modem firmware lives under a sibling directory. No FM350-GL image is
# published there yet (only Quectel EM05/RM500U packages, which flash via
# qfirehose); the FM350 is a MediaTek part that needs the vendor SP Flash Tool.
# The modem firmware-check endpoint probes here so it "just works" once an image
# and a per-model latest_version.txt are published.
MODEM_FIRMWARE_BASE_URL = "https://dl.photonicat.com/firmware/"
# How many versions older than the latest the /update dropdown offers for
# downgrades: stable server keeps it short, dev server goes further back
# so testers can bisect regressions.
PROD_FIRMWARE_MAX_BACK = 5
DEV_FIRMWARE_MAX_BACK = 20

class FirmwareListingParser(HTMLParser):
    """Collect photonicat sysupgrade images (plus their .sha256 companions
    and listing dates) from a Caddy/nginx auto-index page."""
    def __init__(self):
        super().__init__()
        self.firmware_files = []
        self.sha256_files = set()
        self.file_dates = {}
        self.file_datetimes = {}  # ISO timestamps from Caddy <time datetime="...">
        self.current_link = None
        self.in_link = False
        self.in_date_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.in_link = True
            for attr_name, attr_value in attrs:
                if attr_name != 'href' or not attr_value:
                    continue
                # Normalize "./file" and absolute-path hrefs to the bare name
                filename = attr_value.split('/')[-1]
                if 'photonicatwrt' not in filename or 'sysupgrade' not in filename:
                    continue
                if filename.endswith('.img.gz'):
                    self.current_link = filename
                    self.firmware_files.append(filename)
                elif filename.endswith('.img.gz.sha256'):
                    self.sha256_files.add(filename[:-len('.sha256')])
        elif tag == 'td' and self.current_link:
            # Look for date/time cells in directory listing
            self.in_date_cell = True
        elif tag == 'time' and self.current_link:
            # Caddy auto-index rows carry a machine-readable timestamp
            for attr_name, attr_value in attrs:
                if attr_name == 'datetime' and attr_value:
                    self.file_datetimes[self.current_link] = attr_value

    def handle_endtag(self, tag):
        if tag == 'a':
            self.in_link = False
        elif tag == 'td':
            self.in_date_cell = False
        elif tag == 'tr':
            self.current_link = None

    def handle_data(self, data):
        # Try to capture date/time data for the current firmware file
        if self.current_link and self.in_date_cell and not self.in_link:
            data = data.strip()
            # Look for date patterns like "2024-01-15 10:30" or similar
            if re.match(r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}', data):
                self.file_dates[self.current_link] = data

def extract_fw_revision(name):
    """Extract rXXXX revision number from a filename or version string"""
    match = re.search(r'r(\d+)', name or "")
    return int(match.group(1)) if match else 0

def extract_fw_hash(name):
    """Extract hash code from a string like r7566-973b5947b"""
    match = re.search(r'r\d+-([a-f0-9]+)', name or "")
    return match.group(1) if match else ""

def fetch_firmware_versions(listing_url, max_back):
    """Fetch a firmware auto-index page and return up to 1 + max_back
    versions, newest first, so the /update dropdown can offer downgrades."""
    with urllib.request.urlopen(listing_url, timeout=20) as response:
        html_content = response.read().decode('utf-8')
    parser = FirmwareListingParser()
    parser.feed(html_content)
    unique_files = sorted(set(parser.firmware_files),
                          key=lambda f: (extract_fw_revision(f), f),
                          reverse=True)
    versions = []
    for filename in unique_files[:max_back + 1]:
        build_date = parser.file_dates.get(filename, "")
        build_epoch = 0
        iso_ts = parser.file_datetimes.get(filename, "")
        if iso_ts:
            try:
                ts = datetime.fromisoformat(iso_ts.replace('Z', '+00:00'))
                build_epoch = int(ts.timestamp())
                # Show in local server time, matching the version string format
                build_date = ts.astimezone().strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
        versions.append({
            "filename": filename,
            "revision": extract_fw_revision(filename),
            "hash": extract_fw_hash(filename),
            "download_url": urllib.parse.urljoin(listing_url, filename),
            "build_date": build_date,
            "build_epoch": build_epoch,
            "sha256_hash": "",
            "sha256_available": filename in parser.sha256_files,
        })
    return versions

@flask_app.route("/api/v1/dev_server_check.json", methods=['GET'])
@api_auth_required
def dev_server_check():
    """
    Check if dev server is reachable and get latest firmware info.
    Returns dev server status and firmware information.
    """
    from urllib.parse import urljoin
    from email.utils import parsedate_to_datetime

    try:
        dev_server_url = dev_firmware_base_url()

        # Latest firmware plus older builds testers can downgrade to
        versions = fetch_firmware_versions(dev_server_url, DEV_FIRMWARE_MAX_BACK)

        if not versions:
            return {
                "status": "error",
                "message": "No firmware files found on dev server",
                "reachable": True
            }

        latest_file = versions[0]["filename"]
        latest_revision = versions[0]["revision"]
        
        # Get current local version for comparison
        local_version = pc_app.main_app.board_info.get("openwrt_version", "")
        local_revision = extract_fw_revision(local_version)
        local_hash = extract_fw_hash(local_version)
        dev_hash = extract_fw_hash(latest_file)
        
        # Get build date for the dev firmware file.
        # Prefer the file's Last-Modified HTTP header (authoritative server modify
        # date, works with Caddy/nginx auto-index), falling back to the date column
        # scraped from the directory listing if the header is unavailable.
        dev_build_date = versions[0]["build_date"]
        dev_build_epoch = 0
        try:
            head_req = urllib.request.Request(
                urljoin(dev_server_url, latest_file), method="HEAD")
            with urllib.request.urlopen(head_req, timeout=10) as head_resp:
                last_modified = head_resp.headers.get("Last-Modified", "")
            if last_modified:
                lm_dt = parsedate_to_datetime(last_modified)
                dev_build_epoch = int(lm_dt.timestamp())
                # Show in local server time, matching the version string format
                dev_build_date = lm_dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception as e:
            pcat_logger.logger.warning(f"Error fetching dev firmware Last-Modified: {e}")
        
        # Get local build date from version string if available
        # Format: R25.02.0 / r7566-973b5947b / 2024-01-15 10:30
        local_build_date = ""
        local_date_match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})', local_version)
        if local_date_match:
            local_build_date = local_date_match.group(1)
        
        # Try to get SHA256 checksum for dev server firmware
        dev_sha256_hash = ""
        dev_sha256_available = False
        try:
            sha256_url = urljoin(dev_server_url, latest_file + ".sha256")
            pcat_logger.logger.info(f"Checking for dev server SHA256 checksum: {sha256_url}")
            sha256_data = robust_urlopen(sha256_url, timeout=10)
            sha256_content = sha256_data.decode('utf-8').strip()
            dev_sha256_hash = sha256_content.split()[0].lower()
            dev_sha256_available = True
            pcat_logger.logger.info(f"Dev server firmware SHA256: {dev_sha256_hash}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                pcat_logger.logger.info("Dev server firmware SHA256 checksum not available")
            else:
                pcat_logger.logger.warning(f"Error fetching dev server firmware SHA256: {e}")
        except Exception as e:
            pcat_logger.logger.warning(f"Error processing dev server firmware SHA256: {e}")

        # Enrich the latest entry with the extra info fetched above so the
        # version dropdown shows the same details as the summary cards.
        versions[0]["build_date"] = dev_build_date
        versions[0]["build_epoch"] = dev_build_epoch
        versions[0]["sha256_hash"] = dev_sha256_hash

        response_data = {
            "status": "ok",
            "reachable": True,
            "versions": versions,
            "local_version": local_version,
            "local_revision": local_revision,
            "local_hash": local_hash,
            "local_build_date": local_build_date,
            "dev_filename": latest_file,
            "dev_revision": latest_revision,
            "dev_hash": dev_hash,
            "dev_build_date": dev_build_date,
            "dev_build_epoch": dev_build_epoch,
            "dev_download_url": urljoin(dev_server_url, latest_file),
            "dev_sha256_hash": dev_sha256_hash,
            "dev_sha256_available": dev_sha256_available,
            "dev_newer": latest_revision > local_revision  # Compare actual revisions
        }
        
    except urllib.error.URLError as e:
        # Dev server not reachable
        response_data = {
            "status": "unreachable",
            "reachable": False,
            "message": f"Dev server not reachable: {str(e)}"
        }
    except Exception as e:
        pcat_logger.logger.error(f"Error checking dev server: {e}")
        response_data = {
            "status": "error",
            "reachable": False,
            "message": f"Error: {str(e)}"
        }
    
    rsp = make_response(response_data)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

def _modem_firmware_subdir(model):
    """Map a modem model string to its firmware subdirectory on the download
    server. Returns None for models we don't publish modem firmware for."""
    if not model:
        return None
    m = model.upper()
    if "FM350" in m:
        return "fm350/"
    # EM05 / RM500U are Quectel and flashed via qfirehose from the /update flow,
    # not this dialog; leave them unmapped here.
    return None


@flask_app.route("/api/v1/modem/firmware_update_check.json", methods=['GET'])
@api_auth_required
def modem_firmware_update_check():
    """Check whether a newer *modem* (cellular module) firmware image is
    published on the download server for the currently detected model.

    This is distinct from the router/OpenWrt firmware check. As of now no
    FM350-GL image is hosted, so this returns status "none" cleanly; the
    endpoint probes the server so the dialog lights up automatically once a
    per-model ``latest_version.txt`` (and image) is published.
    """
    current_version = pc_app.modem_client.basic.get("firmware_version", "")
    model = pc_app.modem_client.basic.get("model", "")
    response_json = {
        "status": "none",          # none | up_to_date | update_available
        "model": model,
        "current_version": current_version,
        "latest_version": "",
    }

    subdir = _modem_firmware_subdir(model)
    if not subdir:
        # Unknown/unsupported model for this dialog — nothing to offer.
        rsp = make_response(response_json)
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

    try:
        latest_url = f"{MODEM_FIRMWARE_BASE_URL}{subdir}latest_version.txt"
        data = robust_urlopen(latest_url, timeout=15)
        latest_version = data.decode('utf-8').strip()
        response_json["latest_version"] = latest_version
        if latest_version and latest_version != current_version:
            response_json["status"] = "update_available"
        else:
            response_json["status"] = "up_to_date"
    except Exception as e:
        # 404 / network error: no image published for this model. This is the
        # expected path today for FM350 — report "none" rather than erroring.
        pcat_logger.logger.info(f"No modem firmware published for {model}: {e}")
        response_json["status"] = "none"

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp


@flask_app.route("/api/v1/firmware_user_partitions.json", methods=['GET'])
@api_auth_required
def firmware_user_partitions():
    """User-created partitions on the system disk, for the upgrade modal.

    Drives the -p default: a device carrying a partition the stock image does
    not know about must be flashed with -p (do not restore the partition
    table), or that partition is lost. Deliberately separate from
    firmware_update_check so a failed network round-trip there cannot deprive
    the modal of this.
    """
    try:
        from pcat_util import nas_manager
        parts = nas_manager.user_data_partitions()
    except Exception as e:
        # Never block the upgrade UI on a storage probe; report unknown and
        # let the modal keep its safe default.
        pcat_logger.logger.warning(f"user partition probe failed: {e!r}")
        return jsonify({"ok": False, "partitions": []})
    return jsonify({"ok": True, "partitions": parts})


@flask_app.route("/api/v1/firmware_update_check.json", methods=['GET'])
@api_auth_required
def firmware_update_check():
    """
    Check for firmware updates by comparing local version with remote version.
    Returns update availability and download information.
    """
    try:
        # Get current local version
        local_version = pc_app.main_app.board_info.get("openwrt_version", "")
        local_revision = extract_fw_revision(local_version)

        # Fetch remote version info
        prod_base = prod_firmware_base_url()
        remote_url = f"{prod_base}latest_image.txt"
        pcat_logger.logger.info(f"Checking firmware updates from: {remote_url}")

        try:
            data = robust_urlopen(remote_url, timeout=20)
            remote_filename = data.decode('utf-8').strip()
            pcat_logger.logger.info(f"Successfully fetched remote filename: {remote_filename}")
        except Exception as e:
            pcat_logger.logger.error(f"Failed to fetch firmware info: {str(e)}")
            raise

        # Extract remote revision number
        remote_revision = extract_fw_revision(remote_filename)

        # Compare versions - only show update if remote is newer
        update_available = remote_revision > local_revision

        # Try to get SHA256 checksum for production firmware. Not gated on
        # update_available: the page shows the checksum/download row even when
        # the device is up to date (or ahead, e.g. running a self-built image),
        # and gating made those cases always report "No checksum".
        sha256_hash = ""
        sha256_available = False
        try:
            sha256_url = f"{prod_base}{remote_filename}.sha256"
            pcat_logger.logger.info(f"Checking for SHA256 checksum: {sha256_url}")
            sha256_data = robust_urlopen(sha256_url, timeout=10)
            sha256_content = sha256_data.decode('utf-8').strip()
            sha256_hash = sha256_content.split()[0].lower()
            sha256_available = True
            pcat_logger.logger.info(f"Production firmware SHA256: {sha256_hash}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                pcat_logger.logger.info("Production firmware SHA256 checksum not available")
            else:
                pcat_logger.logger.warning(f"Error fetching production firmware SHA256: {e}")
        except Exception as e:
            pcat_logger.logger.warning(f"Error processing production firmware SHA256: {e}")
        
        # Get the build date from the firmware file's Last-Modified header
        # (also not gated on update_available — the summary card shows it in
        # the up-to-date state too).
        remote_build_date = ""
        remote_build_epoch = 0
        try:
            from email.utils import parsedate_to_datetime
            fw_url = f"{prod_base}{remote_filename}"
            head_req = urllib.request.Request(fw_url, method="HEAD")
            with urllib.request.urlopen(head_req, timeout=10) as head_resp:
                last_modified = head_resp.headers.get("Last-Modified", "")
            if last_modified:
                lm_dt = parsedate_to_datetime(last_modified)
                remote_build_epoch = int(lm_dt.timestamp())
                remote_build_date = lm_dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception as e:
            pcat_logger.logger.warning(f"Error fetching production firmware Last-Modified: {e}")

        # Build the downgrade list from the directory listing: the official
        # latest (from latest_image.txt) plus up to PROD_FIRMWARE_MAX_BACK
        # older builds. Files newer than the official latest may exist in the
        # listing but are not released yet, so they are filtered out.
        versions = []
        try:
            # Fetch extra entries so unreleased builds filtered out below
            # don't eat into the downgrade window.
            versions = [v for v in fetch_firmware_versions(prod_base,
                                                           PROD_FIRMWARE_MAX_BACK + 20)
                        if v["revision"] <= remote_revision]
        except Exception as e:
            pcat_logger.logger.warning(f"Error fetching production firmware listing: {e}")
        if not any(v["filename"] == remote_filename for v in versions):
            versions.insert(0, {
                "filename": remote_filename,
                "revision": remote_revision,
                "hash": extract_fw_hash(remote_filename),
                "download_url": f"{prod_base}{remote_filename}",
                "build_date": "",
                "build_epoch": 0,
                "sha256_hash": "",
                "sha256_available": False,
            })
        versions = versions[:PROD_FIRMWARE_MAX_BACK + 1]
        for v in versions:
            if v["filename"] == remote_filename:
                v["build_date"] = remote_build_date or v["build_date"]
                v["build_epoch"] = remote_build_epoch or v["build_epoch"]
                v["sha256_hash"] = sha256_hash
                v["sha256_available"] = v["sha256_available"] or sha256_available
                break

        response_data = {
            "status": "ok",
            "versions": versions,
            "local_version": local_version,
            "local_revision": local_revision,
            "remote_filename": remote_filename,
            "remote_revision": remote_revision,
            "update_available": update_available,
            "download_url": f"{prod_base}{remote_filename}",
            "sha256_hash": sha256_hash,
            "sha256_available": sha256_available,
            "remote_build_date": remote_build_date,
            "remote_build_epoch": remote_build_epoch,
            "luci_flash_url": "/cgi-bin/luci/admin/system/flash"
        }
        
    except urllib.error.URLError as e:
        pcat_logger.logger.error(f"Failed to check firmware updates: {e}")
        response_data = {
            "status": "error",
            "message": f"Failed to check for updates: {str(e)}",
            "local_version": pc_app.main_app.board_info.get("openwrt_version", ""),
            "update_available": False
        }
    except Exception as e:
        pcat_logger.logger.error(f"Unexpected error checking firmware updates: {e}")
        response_data = {
            "status": "error", 
            "message": f"Unexpected error: {str(e)}",
            "local_version": pc_app.main_app.board_info.get("openwrt_version", ""),
            "update_available": False
        }
    
    rsp = make_response(response_data)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/firmware_changelog.json", methods=['GET'])
@api_auth_required
def firmware_changelog():
    """
    Return the changelog text published next to a firmware image
    (<image>.changelog or <image>.changelog.txt), if any.
    Used by the version dropdown on /update.
    """
    source = request.args.get("source", "dev")
    filename = request.args.get("filename", "")

    # Bare image filename only — no paths, so the URL can't be redirected
    # outside the firmware directory.
    if not re.fullmatch(r'photonicatwrt-[\w.\-]+\.img\.gz', filename):
        rsp = make_response({"status": "error", "message": "Invalid firmware filename"}, 400)
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

    if source == "prod":
        base_url = prod_firmware_base_url()
    else:
        base_url = dev_firmware_base_url()

    changelog = ""
    for suffix in (".changelog", ".changelog.txt"):
        try:
            with urllib.request.urlopen(base_url + filename + suffix, timeout=8) as resp:
                # Cap the read so a bogus/huge file can't balloon the response
                changelog = resp.read(16384).decode('utf-8', errors='replace').strip()
            if changelog:
                break
        except urllib.error.HTTPError as e:
            if e.code != 404:
                pcat_logger.logger.warning(f"Error fetching firmware changelog: {e}")
        except Exception as e:
            pcat_logger.logger.warning(f"Error fetching firmware changelog: {e}")

    rsp = make_response({
        "status": "ok",
        "available": bool(changelog),
        "changelog": changelog
    })
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

# Global variables for firmware upgrade tracking
firmware_upgrade_thread = None
firmware_upgrade_lock = threading.Lock()

# sysupgrade flags the UI may tick, listed in the order they are passed to
# sysupgrade:
#   -c  keep changed files in /etc/
#   -o  keep changed files in /, except those from packages
#   -p  do not restore the partition table after flashing (SAVE_PARTITIONS=0
#       in sysupgrade): the live table is left as-is, so a partition the image
#       does not know about -- e.g. a user's pcat-data volume -- survives.
#       Unticking it lets the image rewrite the table and the bootloader.
# Ticking neither -c nor -o means nothing is preserved, which is sysupgrade -n.
ALLOWED_SYSUPGRADE_FLAGS = ('-c', '-o', '-p')
DEFAULT_SYSUPGRADE_FLAGS = ('-c', '-p')

def normalize_sysupgrade_flags(message):
    """Pick the sysupgrade flags for an upgrade request sent over the socket.

    Accepts the `sysupgrade_flags` list from the UI, falling back to the legacy
    `keep_config`/`keep_config_option` pair so older clients keep working.
    Unknown flags are dropped and an upgrade that preserves nothing gets -n.
    """
    raw = message.get("sysupgrade_flags")
    if not isinstance(raw, list):
        if "keep_config" in message or "keep_config_option" in message:
            raw = [message.get("keep_config_option")] if message.get("keep_config", True) else []
        else:
            raw = list(DEFAULT_SYSUPGRADE_FLAGS)

    flags = [flag for flag in ALLOWED_SYSUPGRADE_FLAGS if flag in raw]
    if '-c' not in flags and '-o' not in flags:
        flags.insert(0, '-n')
    return flags

# Where the detached flash writes its transcript.
#
# NOT /tmp: a sysupgrade ends in a reboot, /tmp is tmpfs, and so the only
# record of why a flash failed was destroyed by the flash itself -- which is
# exactly the state a failed OTA leaves you in, with the device back on its old
# firmware and nothing to read. /etc is on the overlay, survives a reboot, and
# is preserved by `sysupgrade -c` across a successful one. The start marker
# truncates, so this holds the most recent attempt and cannot grow without
# bound. Live progress still goes to the /tmp status file, which is state
# rather than history and is correct to lose on reboot.
SYSUPGRADE_LOG = '/etc/pcat-sysupgrade.log'

# Pidfile written by start-stop-daemon for the detached flash shell. Distinct
# from the '/tmp/sysupgrade.pid' the shell writes itself: this one is filled in
# by the launcher, so it is readable the moment start-stop-daemon returns and
# lets us confirm the flash was actually spawned before we start waiting on it.
SYSUPGRADE_LAUNCH_PID = '/tmp/sysupgrade_launch.pid'


def _sysupgrade_running(pid_file):
    """True while the detached sysupgrade shell is still alive.

    The shell writes its own $$ to pid_file before exec'ing sysupgrade, so this
    tracks the whole flash, not just one child of it. A pid that has exited (or
    a pid_file that was never written) reads as not running.
    """
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_for_sysupgrade(status_file, pid_file, timeout=900):
    """Block until the detached sysupgrade finishes, then report what it did.

    A successful flash never returns from here: stage 2 reboots the device and
    this process dies with it. So reaching the end of this function at all
    means the upgrade did NOT complete, and the only correct thing to do is say
    so and leave the box running its current (still intact) firmware.

    Deliberately does not reboot on any path. See background_firmware_upgrade_thread().
    """
    import json

    waited = 0
    poll = 2
    last_stage = None

    while waited < timeout:
        stage = None
        try:
            with open(status_file) as f:
                status = json.load(f)
            stage = status.get('stage')
            message = status.get('message', 'Flashing firmware...')
        except Exception:
            # A torn read while the shell rewrites the file is expected and not
            # a failure; fall through to the liveness check below.
            message = 'Flashing firmware...'

        if stage != last_stage and stage:
            pcat_logger.logger.info(f"Sysupgrade stage: {stage} - {message}")
            last_stage = stage

        if stage == 'error':
            exit_code = 'unknown'
            try:
                exit_code = status.get('exit_code', 'unknown')
            except Exception:
                pass
            msg = (f'Firmware flash failed (sysupgrade exit {exit_code}). '
                   f'The device is still running its previous firmware. '
                   f'See {SYSUPGRADE_LOG} for the full transcript.')
            pcat_logger.logger.error(msg)
            update_firmware_upgrade_status('error', 0, msg, error=msg)
            return False

        if stage == 'completed':
            # Stage 2 is flashing and will reboot; hold the "in progress" UI
            # until the box goes down under us.
            update_firmware_upgrade_status(
                'restart', 95,
                'Firmware written. Device is restarting -- this page will '
                'reconnect once it is back.')
        else:
            # Real progress from the flash itself, not a cosmetic timer.
            update_firmware_upgrade_status(
                'restart', min(50 + waited // 10, 90),
                f'{message} (do not power off)')

        if not _sysupgrade_running(pid_file) and waited > 5:
            # The shell is gone. If it had succeeded we would have rebooted
            # rather than got here, so treat a silent disappearance as failure.
            if stage == 'completed':
                update_firmware_upgrade_status(
                    'restart', 95,
                    'Firmware written. Waiting for the device to restart...')
                time.sleep(30)
                return True
            msg = (f'Firmware flash ended without completing. The device is '
                   f'still running its previous firmware. See {SYSUPGRADE_LOG}.')
            pcat_logger.logger.error(msg)
            update_firmware_upgrade_status('error', 0, msg, error=msg)
            return False

        time.sleep(poll)
        waited += poll

    msg = (f'Firmware flash did not finish within {timeout}s. The device has '
           f'NOT been rebooted; check {SYSUPGRADE_LOG} before power-cycling.')
    pcat_logger.logger.error(msg)
    update_firmware_upgrade_status('error', 0, msg, error=msg)
    return False


class FirmwareIntegrityError(Exception):
    """The downloaded image is not the image the server published.

    A distinct type because the verification block around it deliberately
    swallows Exception -- a missing or unreachable .sha256 must not block an
    upgrade. A checksum that was fetched and did *not* match is the opposite
    situation: it is the one result that must stop the flash, so it has to be
    distinguishable from "could not verify".
    """


# Global status tracking for firmware upgrade (persists across page reloads)
firmware_upgrade_status = {
    'active': False,
    'stage': '',
    'progress': 0,
    'message': '',
    'firmware_url': '',
    'sysupgrade_flags': list(DEFAULT_SYSUPGRADE_FLAGS),
    'start_time': None,
    'error': None
}

def update_firmware_upgrade_status(stage, progress, message, error=None):
    """Update global firmware upgrade status and emit to clients"""
    global firmware_upgrade_status
    
    firmware_upgrade_status.update({
        'stage': stage,
        'progress': progress,
        'message': message,
        'error': error,
        'active': stage not in ['completed', 'error'] if not error else False
    })
    
    # Debug logging
    pcat_logger.logger.info(f"Firmware upgrade: {stage} - {progress}% - {message}")
    if error:
        pcat_logger.logger.error(f"Firmware upgrade error: {error}")

    # sysupgrade is (about to be) running detached and will take the device
    # down without another status update; flag the restart so every open page
    # shows the "restarting" overlay via the dashboard poll, not just the
    # update page that owns the socket.
    if stage == 'restart' and not error:
        try:
            if getattr(pc_app.main_app, 'system_status', 0) == 0:
                pc_app.main_app.system_status = 20  # prepare_rebooting
        except Exception:
            pass

    publish('firmware_upgrade_status', firmware_upgrade_status.copy())

def background_firmware_upgrade_thread(firmware_url, sysupgrade_flags, download_method="wget"):
    """Background thread for firmware download and upgrade process"""
    global firmware_upgrade_thread, firmware_upgrade_status
    import tempfile
    import os
    import json
    import threading
    import time

    # Initialize status
    firmware_upgrade_status.update({
        'active': True,
        'firmware_url': firmware_url,
        'sysupgrade_flags': list(sysupgrade_flags),
        'start_time': time.time(),
        'error': None
    })

    # Debug logging
    pcat_logger.logger.info(f"Starting firmware upgrade process: {firmware_url}")
    pcat_logger.logger.info(f"sysupgrade flags: {' '.join(sysupgrade_flags)}")
    
    try:
        # Stage 1: Download with improved progress tracking
        update_firmware_upgrade_status('download', 0, 'Initializing firmware download...')
        time.sleep(0.3)

        # Use /tmp directory for downloads as requested
        temp_dir = '/tmp'
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir, exist_ok=True)

        filename = firmware_url.split('/')[-1]
        temp_file = os.path.join(temp_dir, f'firmware_upgrade_{filename}')

        pcat_logger.logger.info(f"Download target: {temp_file}")
        pcat_logger.logger.info(f"Temp directory: {temp_dir}")

        update_firmware_upgrade_status('download', 0, f'Target file: {filename}')
        time.sleep(0.3)
        update_firmware_upgrade_status('download', 0, f'Download directory: {temp_dir}')
        time.sleep(0.3)

        # Check available disk space
        try:
            statvfs = os.statvfs(temp_dir)
            free_mb = (statvfs.f_bavail * statvfs.f_frsize) // (1024 * 1024)
            update_firmware_upgrade_status('download', 0, f'Available disk space: {free_mb}MB')
            time.sleep(0.3)
        except Exception:
            pass

        # First, get the file size for accurate progress tracking
        update_firmware_upgrade_status('download', 0, 'Querying remote server for firmware size...')
        try:
            import urllib.request
            pcat_logger.logger.info(f"Fetching file size from: {firmware_url}")
            # timeout is required: an unreachable server would otherwise hang
            # this download thread indefinitely on connect/read.
            with urllib.request.urlopen(firmware_url, timeout=15) as response:
                total_size = int(response.headers.get('Content-Length', 0))
                pcat_logger.logger.info(f"Remote file size: {total_size} bytes ({total_size // (1024*1024)}MB)")
                update_firmware_upgrade_status('download', 0, f'Remote firmware size: {total_size // (1024*1024)}MB ({total_size:,} bytes)')
        except Exception as e:
            total_size = 20 * 1024 * 1024  # Default to 20MB if we can't get size
            pcat_logger.logger.warning(f"Could not get file size, using default: {e}")
            pcat_logger.logger.info(f"Using default size: {total_size} bytes")
            update_firmware_upgrade_status('download', 0, f'Could not determine file size, estimating ~20MB')
        
        # Choose download method based on user selection
        firmware_filename = f'firmware_upgrade_{filename}'
        temp_file = os.path.join(temp_dir, firmware_filename)
        pcat_logger.logger.info(f"Download target: {temp_file}")
        pcat_logger.logger.info(f"Download method: {download_method}")

        time.sleep(0.3)
        update_firmware_upgrade_status('download', 1, f'Using download method: {download_method}')
        time.sleep(0.3)

        # Clean up previous download if exists.
        #
        # This said "Removing previous download" and removed nothing, while the
        # downloaders below run with --continue. A leftover part-file was
        # therefore resumed rather than replaced -- and on the dev server the
        # bytes behind a URL change whenever a build is re-cut, so resuming
        # across a rebuild splices two different images into one file. The
        # result passes wget (the byte count is right) and fails its checksum,
        # which until now was only a warning. Start from a clean file instead:
        # a re-download costs seconds on a LAN and removes the whole class.
        if os.path.exists(temp_file):
            old_size = os.path.getsize(temp_file)
            update_firmware_upgrade_status('download', 1, f'Removing previous download ({old_size // (1024*1024)}MB)...')
            try:
                os.remove(temp_file)
                pcat_logger.logger.info(f"Removed stale download: {temp_file} ({old_size} bytes)")
            except OSError as e:
                pcat_logger.logger.warning(f"Could not remove stale download {temp_file}: {e}")
            time.sleep(0.3)

        if download_method == "aria2c":
            # Use aria2c for download with JSON-RPC progress tracking
            download_log = os.path.join(temp_dir, 'firmware_upgrade_aria2.log')
            
            cmd_args = [
                'aria2c',
                '--dir=' + temp_dir,
                '--out=' + firmware_filename,
                '--max-connection-per-server=4',
                '--split=4',
                '--console-log-level=warn',
                '--summary-interval=1',
                '--log=' + download_log,
                '--log-level=info',
                '--enable-rpc=false',
                '--continue=true',  # Enable resume
                '--allow-overwrite=true',
                firmware_url
            ]
            
            pcat_logger.logger.info(f"aria2c command: {' '.join(cmd_args)}")
            pcat_logger.logger.info(f"aria2c log file: {download_log}")
        else:
            # Use wget for download (default, more reliable)
            download_log = os.path.join(temp_dir, 'firmware_upgrade_wget.log')
            
            cmd_args = [
                'wget',
                '--progress=dot:giga',
                '--timeout=30',
                '--tries=3',
                '--continue',  # Enable resume
                '-O', temp_file,
                firmware_url
            ]
            
            pcat_logger.logger.info(f"wget command: {' '.join(cmd_args)}")
            pcat_logger.logger.info(f"wget log file: {download_log}")
        
        # Start download process
        update_firmware_upgrade_status('download', 2, f'Launching {download_method} process...')
        pcat_logger.logger.info(f"Starting {download_method} download process...")
        # Redirect the downloader's stdout+stderr to its log file rather than
        # PIPEs. wget --progress=dot:giga streams progress to stderr; with an
        # undrained PIPE a long download fills the ~64KB pipe buffer, the child
        # blocks on write, and download_process.wait() below then deadlocks
        # (the classic PIPE + wait() pitfall). A file never blocks — and this
        # finally makes the wget log file (computed all along but never wired
        # up) actually capture output for the error path.
        download_logf = open(download_log, 'w')
        download_process = subprocess.Popen(
            cmd_args,
            stdout=download_logf,
            stderr=subprocess.STDOUT,
            text=True
        )
        pcat_logger.logger.info(f"{download_method} process started with PID: {download_process.pid}")
        update_firmware_upgrade_status('download', 3, f'{download_method} process started (PID: {download_process.pid})')
        time.sleep(0.3)
        
        # Background thread to monitor download progress
        def monitor_download_progress():
            last_size = 0
            stall_count = 0
            update_count = 0
            
            pcat_logger.logger.info("Starting download progress monitoring thread")
            
            while download_process.poll() is None:
                try:
                    update_count += 1
                    
                    if os.path.exists(temp_file):
                        current_size = os.path.getsize(temp_file)
                        
                        # Log detailed progress every 10 updates (10 seconds)
                        if update_count % 10 == 0:
                            pcat_logger.logger.info(f"Download progress: {current_size} bytes downloaded, file exists at {temp_file}")
                        
                        # Calculate progress percentage
                        if total_size > 0:
                            progress = min(int((current_size / total_size) * 95), 95)
                        else:
                            # Fallback estimation if we don't have total size
                            progress = min(int((current_size / (20 * 1024 * 1024)) * 95), 95)
                        
                        # Calculate download speed
                        speed = current_size - last_size  # bytes per second
                        speed_mb = speed / (1024 * 1024)
                        
                        # Check if download is stalling
                        if current_size == last_size:
                            stall_count += 1
                            if stall_count == 10:  # Log stall warning once
                                pcat_logger.logger.warning(f"Download appears to be stalled at {current_size} bytes")
                        else:
                            if stall_count > 0:  # Log resume if we were stalled
                                pcat_logger.logger.info(f"Download resumed, now at {current_size} bytes")
                            stall_count = 0
                            
                        last_size = current_size
                        
                        # Format message with size and speed info
                        size_mb = current_size / (1024 * 1024)
                        total_mb = total_size / (1024 * 1024)
                        
                        if speed_mb > 0:
                            message = f'Downloading... {size_mb:.1f}MB/{total_mb:.1f}MB ({speed_mb:.1f}MB/s)'
                        else:
                            message = f'Downloading... {size_mb:.1f}MB/{total_mb:.1f}MB'
                            
                        if stall_count > 10:  # More than 10 seconds stall
                            message += ' - Connection seems slow'
                        
                        update_firmware_upgrade_status('download', progress, message)
                    else:
                        if update_count <= 5:  # Only log for first few attempts
                            pcat_logger.logger.info(f"Download file {temp_file} does not exist yet, waiting...")
                        update_firmware_upgrade_status('download', 5, 'Initializing download...')
                        
                    time.sleep(1)  # Update every second
                    
                except Exception as e:
                    pcat_logger.logger.error(f"Progress monitoring error: {e}")
                    time.sleep(1)
                    
            pcat_logger.logger.info("Download process finished, stopping progress monitoring")
        
        # Start progress monitoring in a separate thread
        progress_thread = threading.Thread(target=monitor_download_progress, daemon=True)
        progress_thread.start()
        pcat_logger.logger.info("Started download progress monitoring thread")
        
        # Wait for download to complete
        pcat_logger.logger.info("Waiting for download process to complete...")
        download_process.wait()
        pcat_logger.logger.info(f"{download_method} process completed with return code: {download_process.returncode}")
        
        # Stop progress monitoring
        progress_thread.join(timeout=2)

        # Release our write handle now the child has exited (its stdout/stderr
        # were redirected here); the OS has already flushed the child's writes.
        try:
            download_logf.close()
        except Exception:
            pass

        if download_process.returncode != 0:
            # stdout+stderr were redirected to download_log; read it back for
            # the failure detail (there are no pipes to drain anymore).
            log_content = ""
            if os.path.exists(download_log):
                try:
                    with open(download_log, 'r') as f:
                        log_content = f.read()
                    pcat_logger.logger.error(f"{download_method} log file content:\n{log_content}")
                except Exception:
                    pcat_logger.logger.warning(f"Could not read {download_method} log file")

            tail = " | ".join(log_content.strip().splitlines()[-5:]) if log_content.strip() else "see log"
            raise Exception(f"Download failed (rc={download_process.returncode}): {tail}")
            
        # Verify download completed successfully
        final_size = 0
        if os.path.exists(temp_file):
            final_size = os.path.getsize(temp_file)
            pcat_logger.logger.info(f"Download completed. Final file size: {final_size} bytes ({final_size // (1024*1024)}MB)")
        else:
            pcat_logger.logger.error(f"Download file missing: {temp_file}")
            
        if not os.path.exists(temp_file) or final_size == 0:
            pcat_logger.logger.error("Download verification failed: file is empty or missing")
            raise Exception("Download failed: file is empty or missing")
            
        update_firmware_upgrade_status('download', 100, f'Download completed successfully ({final_size // (1024*1024)}MB, {final_size:,} bytes)')
        time.sleep(0.5)

        # Stage 1.5: SHA256 Verification (if available)
        try:
            sha256_url = firmware_url + '.sha256'
            pcat_logger.logger.info(f"Attempting to download SHA256 checksum from: {sha256_url}")
            update_firmware_upgrade_status('verify', 0, 'Starting integrity verification...')
            time.sleep(0.3)
            update_firmware_upgrade_status('verify', 10, 'Downloading SHA256 checksum file...')
            
            try:
                sha256_data = robust_urlopen(sha256_url, timeout=10)
                sha256_content = sha256_data.decode('utf-8').strip()
                pcat_logger.logger.info(f"Downloaded SHA256 content: {sha256_content}")
                
                # Parse SHA256 content (format: "hash filename" or just "hash")
                expected_sha256 = sha256_content.split()[0].lower()
                pcat_logger.logger.info(f"Expected SHA256: {expected_sha256}")
                
                update_firmware_upgrade_status('verify', 30, f'Expected SHA256: {expected_sha256[:16]}...')
                time.sleep(0.3)
                update_firmware_upgrade_status('verify', 40, 'Calculating SHA256 hash of downloaded firmware...')

                # Calculate SHA256 of downloaded file
                import hashlib
                calculated_sha256 = hashlib.sha256()
                with open(temp_file, 'rb') as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        calculated_sha256.update(chunk)

                calculated_sha256 = calculated_sha256.hexdigest().lower()
                pcat_logger.logger.info(f"Calculated SHA256: {calculated_sha256}")
                update_firmware_upgrade_status('verify', 80, f'Calculated SHA256: {calculated_sha256[:16]}...')
                time.sleep(0.3)
                update_firmware_upgrade_status('verify', 90, 'Comparing checksums...')
                time.sleep(0.3)

                if calculated_sha256 == expected_sha256:
                    pcat_logger.logger.info("✅ SHA256 verification PASSED - firmware integrity verified")
                    update_firmware_upgrade_status('verify', 100, '✅ SHA256 verification PASSED - firmware integrity verified')
                else:
                    error_msg = f"❌ SHA256 verification FAILED! Expected: {expected_sha256}, Got: {calculated_sha256}"
                    pcat_logger.logger.error(error_msg)
                    raise FirmwareIntegrityError(error_msg)

            except FirmwareIntegrityError:
                # Must escape both broad handlers below. Flashing an image that
                # failed its own published checksum is how a device ends up
                # rebooting into the firmware it started with, or worse.
                raise
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    pcat_logger.logger.warning("SHA256 checksum file not found - skipping verification")
                    update_firmware_upgrade_status('verify', 100, '⚠️ SHA256 checksum not available - skipping verification')
                else:
                    raise e
            except Exception as e:
                pcat_logger.logger.warning(f"SHA256 verification failed: {str(e)} - continuing without verification")
                update_firmware_upgrade_status('verify', 100, f'⚠️ SHA256 verification failed: {str(e)} - continuing')

        except FirmwareIntegrityError:
            raise
        except Exception as e:
            pcat_logger.logger.warning(f"SHA256 verification process failed: {str(e)} - continuing without verification")
            update_firmware_upgrade_status('verify', 100, '⚠️ SHA256 verification unavailable - continuing without verification')
        
        # Stage 2: Prepare
        pcat_logger.logger.info("Starting firmware preparation stage")
        update_firmware_upgrade_status('prepare', 0, 'Starting firmware preparation...')
        time.sleep(0.3)

        # Verify file integrity
        update_firmware_upgrade_status('prepare', 10, 'Checking firmware file exists...')
        pcat_logger.logger.info(f"Verifying downloaded file: {temp_file}")
        if not os.path.exists(temp_file):
            pcat_logger.logger.error(f"File does not exist: {temp_file}")
            raise Exception("Downloaded firmware file is missing")

        time.sleep(0.3)
        file_size = os.path.getsize(temp_file)
        pcat_logger.logger.info(f"File size verification: {file_size} bytes")
        update_firmware_upgrade_status('prepare', 30, f'Firmware file size: {file_size // (1024*1024)}MB ({file_size:,} bytes)')
        time.sleep(0.3)

        update_firmware_upgrade_status('prepare', 50, 'Validating firmware file size...')
        if file_size < 1024*1024:  # Less than 1MB
            pcat_logger.logger.error(f"File too small: {file_size} bytes")
            raise Exception(f"Downloaded firmware file is too small: {file_size} bytes")

        time.sleep(0.3)
        pcat_logger.logger.info("File verification passed")
        update_firmware_upgrade_status('prepare', 70, 'File size validation passed')
        time.sleep(0.3)

        # Report the flags the flash will run with
        update_firmware_upgrade_status('prepare', 85, f'sysupgrade flags: {" ".join(sysupgrade_flags)}')
        time.sleep(0.3)

        update_firmware_upgrade_status('prepare', 100, 'Firmware prepared for installation')
        
        # Stage 3: Install
        pcat_logger.logger.info("Starting firmware installation stage")
        pcat_logger.logger.warning("CRITICAL: Firmware installation starting - DO NOT POWER OFF!")
        update_firmware_upgrade_status('install', 0, 'Starting firmware installation...')
        time.sleep(0.5)
        update_firmware_upgrade_status('install', 5, '⚠️ DO NOT POWER OFF THE DEVICE!')
        time.sleep(0.3)
        update_firmware_upgrade_status('install', 10, 'Building sysupgrade command...')
        time.sleep(0.3)

        # Prepare sysupgrade command
        sysupgrade_cmd = ['sysupgrade'] + list(sysupgrade_flags) + [temp_file]
        pcat_logger.logger.info(f"sysupgrade command: {' '.join(sysupgrade_cmd)}")
        update_firmware_upgrade_status('install', 20, f'Command: {" ".join(sysupgrade_cmd)}')
        time.sleep(0.3)

        # Execute sysupgrade detached from Flask process
        pcat_logger.logger.info("Starting detached sysupgrade process...")
        update_firmware_upgrade_status('install', 30, 'Setting up detached sysupgrade process...')
        time.sleep(0.3)

        # Create status file for progress reporting
        status_file = '/tmp/sysupgrade_status.json'
        pid_file = '/tmp/sysupgrade.pid'
        log_file = SYSUPGRADE_LOG

        update_firmware_upgrade_status('install', 40, 'Creating status tracking files...')
        time.sleep(0.3)

        # Initialize status file
        import json
        status_data = {
            'stage': 'starting',
            'progress': 0,
            'message': 'Initializing firmware installation...',
            'pid': None,
            'started': time.time()
        }
        with open(status_file, 'w') as f:
            json.dump(status_data, f)

        update_firmware_upgrade_status('install', 50, 'Setting environment variables for sysupgrade...')
        time.sleep(0.3)
        update_firmware_upgrade_status('install', 60, 'Preparing to hand off to sysupgrade process...')

        # Give UI time to update before starting sysupgrade
        time.sleep(2)

        # Transition to restart stage
        update_firmware_upgrade_status('restart', 0, 'Launching sysupgrade in detached mode...')
        time.sleep(1)  # Allow UI to update

        # Build detached sysupgrade command with nohup
        detached_cmd = [
            'nohup', 'sh', '-c',
            f'''
            # Update status to running
            echo '{{
                "stage": "running",
                "progress": 10,
                "message": "Firmware installation in progress...",
                "pid": $$,
                "started": {time.time()}
            }}' > {status_file}

            # Store PID
            echo $$ > {pid_file}

            # Set proper environment
            export PATH="/usr/sbin:/usr/bin:/sbin:/bin"
            export USER="root"
            export HOME="/root"
            export SHELL="/bin/sh"

            # Log start ($* expands the positional args as data, never executed)
            echo "[$(date)] Starting sysupgrade: $*" > {log_file}

            # Update status to installing
            echo '{{
                "stage": "installing",
                "progress": 30,
                "message": "Running sysupgrade...",
                "pid": $$,
                "started": {time.time()}
            }}' > {status_file}

            # Execute sysupgrade and capture output. The command is passed as
            # positional parameters (see the argv appended to this sh -c below),
            # so the firmware path reaches sysupgrade as a single literal arg and
            # is never re-parsed by the shell -- a filename like 'x;reboot' can't
            # inject commands here.
            "$@" >> {log_file} 2>&1
            RESULT=$?
            
            # Log completion
            echo "[$(date)] sysupgrade completed with exit code: $RESULT" >> {log_file}
            
            # Update final status
            if [ $RESULT -eq 0 ]; then
                echo '{{
                    "stage": "completed",
                    "progress": 100,
                    "message": "Firmware installation completed successfully. System restarting...",
                    "pid": $$,
                    "started": {time.time()},
                    "completed": '$(date +%s)'
                }}' > {status_file}
            else
                echo '{{
                    "stage": "error",
                    "progress": 0,
                    "message": "Firmware installation failed. Check logs.",
                    "pid": $$,
                    "started": {time.time()},
                    "error": '$(date +%s)',
                    "exit_code": '$RESULT'
                }}' > {status_file}
                exit $RESULT
            fi
            
            # A successful sysupgrade does not return: stage 2 reboots the box
            # itself. Reaching this point means the flash exited 0 without
            # restarting, which is not a state to "help" along -- the old
            # firmware is still the running one and is intact.
            #
            # What used to be here (pkill the web app, sleep 2, /sbin/reboot)
            # was wrong twice over: the pkill pattern matched
            # pcat-manager-web-main.py, which is not how the app is started any
            # more (granian serves pcat_wsgi:flask_app), so it silently matched
            # nothing; and the reboot raced the flash it was supposed to be
            # following. Leave the PID file in place as the completion marker
            # the waiter reads, and let the device stay up.
            rm -f {pid_file}
            ''',
            # argv passed to the script above: $0 is a dummy 'sh', and the
            # sysupgrade command (including the untrusted firmware path) follows
            # as $1..$N. Keeping the path in argv instead of the script text is
            # what makes the "$@"/$* references above injection-proof.
            'sh',
        ] + sysupgrade_cmd

        # Start detached process.
        #
        # start-stop-daemon -b is what actually gets the flash out from under
        # this service. setsid()+nohup alone only detach the controlling
        # terminal and signal group -- the shell stays a PID-child of the
        # granian worker, and /etc/init.d/pcat-manager-web's stop() walks
        # /proc/*/stat for children of the pidfile PID and SIGKILLs every one
        # of them. So anything that restarts the web app mid-flash (including
        # the upgrade's own former `pkill`) would take a live sysupgrade down
        # with it. -b forks and lets init reap, leaving nothing for stop() to
        # match. -m writes the pidfile so we can still track the flash.
        update_firmware_upgrade_status('restart', 5, 'Starting detached sysupgrade process...')
        pcat_logger.logger.info("Starting detached sysupgrade command")
        # detached_cmd is ['nohup', 'sh', '-c', <script>, 'sh', <sysupgrade...>];
        # -b already backgrounds and redirects, so drop the nohup wrapper and
        # hand start-stop-daemon the shell itself.
        #
        # -n pcatflash is load-bearing, not cosmetic. -S means "start unless a
        # matching process is found", and with only -x /bin/sh the match is any
        # running /bin/sh on the box -- busybox answers "/bin/sh is already
        # running", exits 1 and never launches the flash. Verified on-device.
        # -n narrows the match to this name, which nothing else uses.
        detached_process = subprocess.Popen(
            ['start-stop-daemon', '-b', '-m', '-p', SYSUPGRADE_LAUNCH_PID,
             '-n', 'pcatflash', '-S', '-x', '/bin/sh', '--']
            + detached_cmd[2:],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # Not detached_process.pid -- that is the short-lived start-stop-daemon
        # launcher, which exits as soon as it has forked. The flash writes its
        # own $$ to pid_file, and that is what the waiter below tracks.
        #
        # Check the launcher's exit status rather than assuming it worked: a
        # refusal here (see the -n note above) would otherwise leave the UI
        # cheerfully reporting a flash that was never started, and the box
        # would sit on the old firmware with no error anywhere.
        launch_rc = detached_process.wait()
        if launch_rc != 0:
            raise Exception(
                f'Failed to launch sysupgrade (start-stop-daemon exit '
                f'{launch_rc}). The device has not been modified.')
        pcat_logger.logger.info("Detached sysupgrade launched (reparented to init)")
        update_firmware_upgrade_status('restart', 10, 'Sysupgrade process launched')
        time.sleep(0.3)
        update_firmware_upgrade_status('restart', 15, 'Monitoring sysupgrade startup...')

        # Monitor progress via status file for initial confirmation
        monitor_time = 0
        max_monitor_time = 10  # Monitor for 10 seconds to confirm start

        while monitor_time < max_monitor_time:
            try:
                if os.path.exists(status_file):
                    with open(status_file, 'r') as f:
                        status = json.load(f)

                    stage = status.get('stage', 'starting')
                    progress = status.get('progress', 0)
                    message = status.get('message', 'Processing...')

                    pcat_logger.logger.info(f"Sysupgrade status: {stage} - {progress}% - {message}")
                    update_firmware_upgrade_status('restart', max(progress, 15 + monitor_time * 3), message)

                    # If we see it's running or beyond, we can exit monitoring
                    if stage in ['running', 'installing', 'completed', 'error']:
                        update_firmware_upgrade_status('restart', 45, f'Sysupgrade entered {stage} stage')
                        break

            except Exception as e:
                pcat_logger.logger.warning(f"Error reading status file: {e}")

            monitor_time += 1
            time.sleep(1)

        # Final status update
        pcat_logger.logger.info("Sysupgrade process detached successfully - Flask can now safely terminate")
        update_firmware_upgrade_status('restart', 50, 'Firmware flashing in progress - process running independently')
        time.sleep(0.3)
        update_firmware_upgrade_status('restart', 55, 'Cleaning up temporary files...')

        # NOTE: Do NOT delete temp_file here! The detached sysupgrade process
        # needs the firmware file. The system will reboot after sysupgrade anyway,
        # so /tmp cleanup happens automatically.
        pcat_logger.logger.info(f"Firmware file kept for sysupgrade: {temp_file}")
        if os.path.exists(download_log):
            try:
                os.remove(download_log)
                pcat_logger.logger.info(f"Removed {download_method} log: {download_log}")
            except Exception as e:
                pcat_logger.logger.warning(f"Failed to cleanup log file: {e}")

        # Hand the box over to sysupgrade and stop touching it.
        #
        # This used to walk a fixed ~6s of cosmetic progress updates and then
        # call reboot() unconditionally, on the theory that it was a harmless
        # "fallback" if sysupgrade had somehow not restarted on its own. It was
        # not harmless: a real flash takes 30-90s, so the timer always expired
        # while stage 1 was still running and rebooted the device mid-upgrade.
        # Stage 1 hands off to procd via `ubus call system sysupgrade`, and the
        # reboot tore down ubus underneath it -- the observed failure was
        #   upgrade: Commencing upgrade. Closing all shell sessions.
        #   Command failed: ubus call system sysupgrade {...} (Connection failed)
        #   sysupgrade completed with exit code: 246
        # with the device coming back up 9s later on the OLD firmware. Nothing
        # had been written to flash yet, which is the only reason this was
        # recoverable rather than a brick.
        #
        # sysupgrade owns the reboot. It reboots on success as the last step of
        # stage 2, so there is nothing to fall back to: if the box is still here
        # the flash did not finish, and rebooting is the one thing that must not
        # happen. Wait on the flash instead and only report what it did.
        _wait_for_sysupgrade(status_file, pid_file)

    except Exception as e:
        update_firmware_upgrade_status('error', 0, f'Firmware upgrade failed: {str(e)}', error=str(e))
        
    finally:
        # NOTE: Do NOT delete temp_file here - sysupgrade may still need it.
        # The system reboots after sysupgrade, so /tmp is cleared automatically.
        firmware_upgrade_thread = None

def update_firmware_checkpoint_status(stage, message, file_path='', sha256='', logs=None):
    """Update firmware checkpoint status and emit to clients"""
    if logs is None:
        logs = []
    
    status = {
        'stage': stage,
        'message': message,
        'file_path': file_path,
        'sha256': sha256,
        'logs': logs
    }
    
    publish('firmware_checkpoint_status', status)

def background_firmware_checkpoint_thread(firmware_url, download_method="wget"):
    """Background thread for firmware checkpoint - download and calculate SHA256 only"""
    import tempfile
    import hashlib
    import subprocess
    import os
    import time
    
    try:
        pcat_logger.logger.info("Starting firmware checkpoint download...")
        update_firmware_checkpoint_status('downloading', 'Starting firmware download...')
        
        # Create temporary directory for download
        temp_dir = tempfile.mkdtemp(prefix="firmware_checkpoint_")
        temp_file = os.path.join(temp_dir, "firmware_checkpoint.img")
        
        pcat_logger.logger.info(f"Downloading firmware to: {temp_file}")
        
        # Download firmware with real-time progress logging
        download_logs = []
        
        if download_method == "aria2c":
            download_cmd = [
                'aria2c',
                '--console-log-level=info',
                '--summary-interval=1',
                '--download-result=full',
                '--file-allocation=none',
                '--allow-overwrite=true',
                '-o', os.path.basename(temp_file),
                '-d', temp_dir,
                firmware_url
            ]
        else:  # wget
            download_cmd = [
                'wget',
                '--progress=dot:giga',
                '-O', temp_file,
                firmware_url
            ]
        
        pcat_logger.logger.info(f"Download command: {' '.join(download_cmd)}")
        download_logs.append(f"Download command: {' '.join(download_cmd)}")
        
        # Execute download with real-time logging
        process = subprocess.Popen(
            download_cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT, 
            text=True, 
            bufsize=1, 
            universal_newlines=True
        )
        
        # Read output line by line
        for line in process.stdout:
            line = line.strip()
            if line:
                pcat_logger.logger.info(f"Download: {line}")
                download_logs.append(line)
                update_firmware_checkpoint_status('downloading', f'Downloading: {line}', logs=download_logs)
        
        process.wait()
        
        if process.returncode != 0:
            error_msg = f"Download failed with return code {process.returncode}"
            pcat_logger.logger.error(error_msg)
            update_firmware_checkpoint_status('error', error_msg)
            return
            
        # Verify file was downloaded
        if not os.path.exists(temp_file) or os.path.getsize(temp_file) == 0:
            error_msg = "Downloaded file is empty or doesn't exist"
            pcat_logger.logger.error(error_msg)
            update_firmware_checkpoint_status('error', error_msg)
            return
            
        file_size = os.path.getsize(temp_file)
        pcat_logger.logger.info(f"Download completed successfully. File size: {file_size} bytes")
        download_logs.append(f"✅ Download completed successfully. File size: {file_size} bytes")
        
        # Calculate SHA256
        update_firmware_checkpoint_status('verifying', 'Calculating SHA256 hash...', logs=download_logs)
        
        sha256_hash = hashlib.sha256()
        with open(temp_file, 'rb') as f:
            while chunk := f.read(8192):
                sha256_hash.update(chunk)
        
        calculated_sha256 = sha256_hash.hexdigest()
        pcat_logger.logger.info(f"Calculated SHA256: {calculated_sha256}")
        download_logs.append(f"✅ SHA256 calculated: {calculated_sha256}")
        
        # Checkpoint completed successfully
        update_firmware_checkpoint_status('completed', 'Firmware downloaded and verified', 
                                        temp_file, calculated_sha256, download_logs)
        
    except Exception as e:
        error_msg = f"Checkpoint failed: {str(e)}"
        pcat_logger.logger.error(error_msg)
        pcat_logger.logger.error(f"Checkpoint error details: {e}", exc_info=True)
        update_firmware_checkpoint_status('error', error_msg)
        
        # Cleanup on error
        try:
            if 'temp_file' in locals() and os.path.exists(temp_file):
                os.remove(temp_file)
            if 'temp_dir' in locals() and os.path.exists(temp_dir):
                os.rmdir(temp_dir)
        except:
            pass

def background_firmware_install_thread(file_path, sha256, sysupgrade_flags):
    """Background thread for firmware installation from checkpoint file"""
    import subprocess
    import hashlib
    import os
    import time

    # Bound before the try: the error handler consults it to decide whether the
    # image is safe to delete, and must not itself raise NameError on a failure
    # that happens before the flash is set up.
    pid_file = '/tmp/sysupgrade.pid'

    try:
        pcat_logger.logger.info(f"Starting firmware installation from checkpoint: {file_path}")
        update_firmware_upgrade_status('prepare', 0, 'Starting checkpoint firmware installation...')
        time.sleep(0.3)
        update_firmware_upgrade_status('prepare', 5, f'Checkpoint file: {os.path.basename(file_path)}')
        time.sleep(0.3)

        # Verify file exists
        update_firmware_upgrade_status('prepare', 10, 'Checking checkpoint file exists...')
        if not os.path.exists(file_path):
            raise Exception(f"Checkpoint file not found: {file_path}")

        file_size = os.path.getsize(file_path)
        update_firmware_upgrade_status('prepare', 15, f'Checkpoint file size: {file_size // (1024*1024)}MB ({file_size:,} bytes)')
        time.sleep(0.3)

        # Re-verify SHA256 if provided
        if sha256:
            pcat_logger.logger.info("Re-verifying SHA256 before installation...")
            update_firmware_upgrade_status('verify', 0, 'Re-verifying firmware integrity before installation...')
            time.sleep(0.3)
            update_firmware_upgrade_status('verify', 20, f'Expected SHA256: {sha256[:16]}...')
            time.sleep(0.3)
            update_firmware_upgrade_status('verify', 40, 'Calculating SHA256 hash of checkpoint file...')

            file_sha256 = hashlib.sha256()
            with open(file_path, 'rb') as f:
                while chunk := f.read(8192):
                    file_sha256.update(chunk)

            calculated_sha256 = file_sha256.hexdigest()
            update_firmware_upgrade_status('verify', 70, f'Calculated SHA256: {calculated_sha256[:16]}...')
            time.sleep(0.3)
            update_firmware_upgrade_status('verify', 80, 'Comparing checksums...')
            time.sleep(0.3)
            if calculated_sha256.lower() != sha256.lower():
                raise Exception(f"SHA256 verification failed. Expected: {sha256}, Got: {calculated_sha256}")

            pcat_logger.logger.info("SHA256 verification passed")
            update_firmware_upgrade_status('verify', 100, 'SHA256 verification PASSED')
        else:
            update_firmware_upgrade_status('verify', 100, 'No SHA256 provided - skipping verification')

        time.sleep(0.3)
        update_firmware_upgrade_status('prepare', 40, 'Integrity verification completed')
        time.sleep(0.3)

        # Report the flags the flash will run with
        update_firmware_upgrade_status('prepare', 50, f'sysupgrade flags: {" ".join(sysupgrade_flags)}')
        time.sleep(0.3)

        # Prepare for installation
        pcat_logger.logger.warning("CRITICAL: Firmware installation starting - DO NOT POWER OFF!")
        update_firmware_upgrade_status('install', 0, 'Starting firmware installation...')
        time.sleep(0.5)
        update_firmware_upgrade_status('install', 5, 'DO NOT POWER OFF THE DEVICE!')
        time.sleep(0.3)
        update_firmware_upgrade_status('install', 10, 'Building sysupgrade command...')
        time.sleep(0.3)

        # Prepare sysupgrade command
        sysupgrade_cmd = ['sysupgrade'] + list(sysupgrade_flags) + [file_path]
        pcat_logger.logger.info(f"sysupgrade command: {' '.join(sysupgrade_cmd)}")
        update_firmware_upgrade_status('install', 20, f'Command: {" ".join(sysupgrade_cmd)}')
        time.sleep(0.3)

        # Execute sysupgrade detached from Flask process
        pcat_logger.logger.info("Starting detached sysupgrade process...")
        update_firmware_upgrade_status('install', 30, 'Setting up detached sysupgrade process...')
        time.sleep(0.3)

        # Create status file for progress reporting
        import json
        status_file = '/tmp/sysupgrade_status.json'
        pid_file = '/tmp/sysupgrade.pid'
        log_file = SYSUPGRADE_LOG

        update_firmware_upgrade_status('install', 40, 'Creating status tracking files...')
        time.sleep(0.3)

        # Initialize status file
        status_data = {
            'stage': 'starting',
            'progress': 0,
            'message': 'Initializing firmware installation...',
            'pid': None,
            'started': time.time()
        }
        with open(status_file, 'w') as f:
            json.dump(status_data, f)

        update_firmware_upgrade_status('install', 50, 'Setting environment variables for sysupgrade...')
        time.sleep(0.3)
        update_firmware_upgrade_status('install', 60, 'Preparing to hand off to sysupgrade process...')

        time.sleep(2)

        update_firmware_upgrade_status('restart', 0, 'Launching sysupgrade in detached mode...')
        time.sleep(1)
        
        # Build detached sysupgrade command with nohup
        detached_cmd = [
            'nohup', 'sh', '-c',
            f'''
            # Update status to running
            echo '{{
                "stage": "running",
                "progress": 10,
                "message": "Firmware installation in progress...",
                "pid": $$,
                "started": {time.time()}
            }}' > {status_file}
            
            # Store PID
            echo $$ > {pid_file}
            
            # Set proper environment
            export PATH="/usr/sbin:/usr/bin:/sbin:/bin"
            export USER="root"
            export HOME="/root"
            export SHELL="/bin/sh"
            
            # Log start ($* expands the positional args as data, never executed)
            echo "[$(date)] Starting sysupgrade: $*" > {log_file}
            
            # Update status to installing
            echo '{{
                "stage": "installing",
                "progress": 30,
                "message": "Running sysupgrade...",
                "pid": $$,
                "started": {time.time()}
            }}' > {status_file}
            
            # Execute sysupgrade and capture output. The command is passed as
            # positional parameters (see the argv appended to this sh -c below),
            # so the firmware path reaches sysupgrade as a single literal arg and
            # is never re-parsed by the shell -- a filename like 'x;reboot' can't
            # inject commands here.
            "$@" >> {log_file} 2>&1
            RESULT=$?
            
            # Log completion
            echo "[$(date)] sysupgrade completed with exit code: $RESULT" >> {log_file}
            
            # Update final status
            if [ $RESULT -eq 0 ]; then
                echo '{{
                    "stage": "completed",
                    "progress": 100,
                    "message": "Firmware installation completed successfully. System restarting...",
                    "pid": $$,
                    "started": {time.time()},
                    "completed": '$(date +%s)'
                }}' > {status_file}
            else
                echo '{{
                    "stage": "error",
                    "progress": 0,
                    "message": "Firmware installation failed. Check logs.",
                    "pid": $$,
                    "started": {time.time()},
                    "error": '$(date +%s)',
                    "exit_code": '$RESULT'
                }}' > {status_file}
                exit $RESULT
            fi
            
            # A successful sysupgrade does not return: stage 2 reboots the box
            # itself. Reaching this point means the flash exited 0 without
            # restarting, which is not a state to "help" along -- the old
            # firmware is still the running one and is intact.
            #
            # What used to be here (pkill the web app, sleep 2, /sbin/reboot)
            # was wrong twice over: the pkill pattern matched
            # pcat-manager-web-main.py, which is not how the app is started any
            # more (granian serves pcat_wsgi:flask_app), so it silently matched
            # nothing; and the reboot raced the flash it was supposed to be
            # following. Leave the PID file in place as the completion marker
            # the waiter reads, and let the device stay up.
            rm -f {pid_file}
            ''',
            # argv passed to the script above: $0 is a dummy 'sh', and the
            # sysupgrade command (including the untrusted firmware path) follows
            # as $1..$N. Keeping the path in argv instead of the script text is
            # what makes the "$@"/$* references above injection-proof.
            'sh',
        ] + sysupgrade_cmd

        # Start detached process.
        #
        # start-stop-daemon -b is what actually gets the flash out from under
        # this service. setsid()+nohup alone only detach the controlling
        # terminal and signal group -- the shell stays a PID-child of the
        # granian worker, and /etc/init.d/pcat-manager-web's stop() walks
        # /proc/*/stat for children of the pidfile PID and SIGKILLs every one
        # of them. So anything that restarts the web app mid-flash (including
        # the upgrade's own former `pkill`) would take a live sysupgrade down
        # with it. -b forks and lets init reap, leaving nothing for stop() to
        # match. -m writes the pidfile so we can still track the flash.
        update_firmware_upgrade_status('restart', 5, 'Starting detached sysupgrade process...')
        pcat_logger.logger.info("Starting detached sysupgrade command")
        # detached_cmd is ['nohup', 'sh', '-c', <script>, 'sh', <sysupgrade...>];
        # -b already backgrounds and redirects, so drop the nohup wrapper and
        # hand start-stop-daemon the shell itself.
        #
        # -n pcatflash is load-bearing, not cosmetic. -S means "start unless a
        # matching process is found", and with only -x /bin/sh the match is any
        # running /bin/sh on the box -- busybox answers "/bin/sh is already
        # running", exits 1 and never launches the flash. Verified on-device.
        # -n narrows the match to this name, which nothing else uses.
        detached_process = subprocess.Popen(
            ['start-stop-daemon', '-b', '-m', '-p', SYSUPGRADE_LAUNCH_PID,
             '-n', 'pcatflash', '-S', '-x', '/bin/sh', '--']
            + detached_cmd[2:],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # Not detached_process.pid -- that is the short-lived start-stop-daemon
        # launcher, which exits as soon as it has forked. The flash writes its
        # own $$ to pid_file, and that is what the waiter below tracks.
        #
        # Check the launcher's exit status rather than assuming it worked: a
        # refusal here (see the -n note above) would otherwise leave the UI
        # cheerfully reporting a flash that was never started, and the box
        # would sit on the old firmware with no error anywhere.
        launch_rc = detached_process.wait()
        if launch_rc != 0:
            raise Exception(
                f'Failed to launch sysupgrade (start-stop-daemon exit '
                f'{launch_rc}). The device has not been modified.')
        pcat_logger.logger.info("Detached sysupgrade launched (reparented to init)")
        update_firmware_upgrade_status('restart', 10, 'Sysupgrade process launched')
        time.sleep(0.3)
        update_firmware_upgrade_status('restart', 15, 'Monitoring sysupgrade startup...')

        # Monitor progress via status file for initial confirmation
        monitor_time = 0
        max_monitor_time = 10  # Monitor for 10 seconds to confirm start

        while monitor_time < max_monitor_time:
            try:
                if os.path.exists(status_file):
                    with open(status_file, 'r') as f:
                        status = json.load(f)

                    stage = status.get('stage', 'starting')
                    progress = status.get('progress', 0)
                    message = status.get('message', 'Processing...')

                    pcat_logger.logger.info(f"Sysupgrade status: {stage} - {progress}% - {message}")
                    update_firmware_upgrade_status('restart', max(progress, 15 + monitor_time * 3), message)

                    # If we see it's running or beyond, we can exit monitoring
                    if stage in ['running', 'installing', 'completed', 'error']:
                        update_firmware_upgrade_status('restart', 45, f'Sysupgrade entered {stage} stage')
                        break

            except Exception as e:
                pcat_logger.logger.warning(f"Error reading status file: {e}")

            monitor_time += 1
            time.sleep(1)

        # Final status update
        pcat_logger.logger.info("Sysupgrade process detached successfully - Flask can now safely terminate")
        update_firmware_upgrade_status('restart', 50, 'Firmware flashing in progress - process running independently')
        time.sleep(0.3)
        update_firmware_upgrade_status('restart', 55, 'Cleaning up temporary files...')

        # Do NOT delete file_path here. sysupgrade is reading this image right
        # now -- it was launched detached a few lines above and has not even
        # reached "Reading partition table from image" yet. Unlinking it out
        # from under the flash raced the read and, unlike the reboot bug below,
        # could lose the image mid-write. The reboot at the end of a successful
        # sysupgrade clears /tmp (tmpfs) for us, so there is nothing to clean.
        pcat_logger.logger.info(f"Firmware file kept for sysupgrade: {file_path}")

        # sysupgrade owns the reboot -- see the long note in
        # background_firmware_upgrade_thread(). The unconditional reboot that
        # used to sit here fired ~6s after launch and killed the flash during
        # its procd handoff ("ubus call system sysupgrade ... (Connection
        # failed)", exit 246), dropping the device back onto its old firmware.
        _wait_for_sysupgrade(status_file, pid_file)

    except Exception as e:
        error_msg = f"Firmware installation failed: {str(e)}"
        pcat_logger.logger.error(error_msg)
        pcat_logger.logger.error(f"Installation error details: {e}", exc_info=True)
        update_firmware_upgrade_status('error', 0, error_msg, str(e))
        
        # Cleanup on error, but never out from under a live flash. Once
        # sysupgrade has been launched it is reparented to init and keeps
        # running regardless of what happens to this thread, so an exception
        # here (or a waiter that gave up) does not mean the image is free --
        # deleting it then would corrupt a flash that is still reading it.
        try:
            if _sysupgrade_running(pid_file):
                pcat_logger.logger.warning(
                    f"Not removing {file_path}: sysupgrade is still running")
            elif os.path.exists(file_path):
                os.remove(file_path)
                temp_dir = os.path.dirname(file_path)
                try:
                    os.rmdir(temp_dir)
                except:
                    pass
        except:
            pass

@flask_app.route("/api/v1/firmware_upgrade_status.json", methods=['GET'])
@api_auth_required
def get_firmware_upgrade_status():
    """Get current firmware upgrade status, including detached sysupgrade process"""
    global firmware_upgrade_status
    
    response_data = firmware_upgrade_status.copy()
    
    # Check for detached sysupgrade status
    detached_status_file = '/tmp/sysupgrade_status.json'
    if os.path.exists(detached_status_file):
        try:
            with open(detached_status_file, 'r') as f:
                detached_status = json.load(f)
            
            # If detached process is running, update our status
            if detached_status.get('stage') in ['running', 'installing', 'completed', 'error']:
                response_data.update({
                    'stage': 'restart' if detached_status['stage'] in ['running', 'installing'] else detached_status['stage'],
                    'progress': detached_status.get('progress', response_data.get('progress', 0)),
                    'message': detached_status.get('message', response_data.get('message', 'Processing...')),
                    'active': detached_status['stage'] not in ['completed', 'error'],
                    'detached_process': True,
                    'detached_pid': detached_status.get('pid'),
                    'detached_started': detached_status.get('started')
                })
                
                # Handle error state
                if detached_status['stage'] == 'error':
                    response_data['error'] = detached_status.get('message', 'Detached sysupgrade process failed')
                    response_data['active'] = False
                
                # Update global status to match detached status
                firmware_upgrade_status.update({
                    'stage': response_data['stage'],
                    'progress': response_data['progress'],
                    'message': response_data['message'],
                    'active': response_data['active']
                })
                
                # Emit updated status to connected clients
                publish('firmware_upgrade_status', response_data.copy())
                
        except Exception as e:
            pcat_logger.logger.warning(f"Error reading detached sysupgrade status: {e}")
    
    response_data['status'] = 'ok'
    
    rsp = make_response(response_data)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@bus_event
def request_firmware_upgrade(message):
    """Socket handler for automatic firmware upgrade"""
    if not _socket_authed():
        return
    firmware_url = message.get("firmware_url")
    sysupgrade_flags = normalize_sysupgrade_flags(message)
    download_method = message.get("download_method", "wget")  # Default to wget

    if not firmware_url:
        update_firmware_upgrade_status('error', 0, 'Invalid firmware URL', error='Invalid firmware URL')
        return
        
    global firmware_upgrade_thread, firmware_upgrade_status
    # A firmware upgrade and an MCU update must never run together — refuse if
    # an MCU update is already in progress.
    if mcu_update_status.get('active', False):
        update_firmware_upgrade_status('error', 0, 'MCU update in progress',
                                       error='An MCU update is already running')
        return
    with firmware_upgrade_lock:
        if firmware_upgrade_thread is None and not firmware_upgrade_status.get('active', False):
            update_firmware_upgrade_status('starting', 0, 'Firmware upgrade request received')
            firmware_upgrade_thread = _start_background_task(
                background_firmware_upgrade_thread, firmware_url, sysupgrade_flags, download_method
            )
            update_firmware_upgrade_status('starting', 0, f'Upgrade thread started for: {firmware_url.split("/")[-1]}')
        else:
            update_firmware_upgrade_status('error', 0, 'Firmware upgrade already in progress', 
                                         error='Another upgrade is already running')

@bus_event
def get_firmware_upgrade_status_socket(message):
    """Socket handler to get current firmware upgrade status"""
    if not _socket_authed():
        return
    global firmware_upgrade_status
    reply('firmware_upgrade_status', firmware_upgrade_status.copy())

@bus_event
def request_firmware_checkpoint(message):
    """Socket handler for firmware checkpoint mode - download only"""
    if not _socket_authed():
        return
    firmware_url = message.get("firmware_url")
    download_method = message.get("download_method", "wget")

    if not firmware_url:
        publish('firmware_checkpoint_status', {
            'stage': 'error',
            'message': 'No firmware URL provided'
        })
        return

    # Never start a firmware checkpoint/download while an MCU update runs.
    if mcu_update_status.get('active', False):
        publish('firmware_checkpoint_status', {
            'stage': 'error',
            'message': 'An MCU update is in progress'
        })
        return

    pcat_logger.logger.info(f"Starting firmware checkpoint mode: {firmware_url}")
    
    # Start checkpoint thread
    checkpoint_thread = _start_background_task(
        background_firmware_checkpoint_thread,
        firmware_url, download_method
    )

@bus_event
def request_firmware_install_from_checkpoint(message):
    """Socket handler to install firmware from checkpoint"""
    if not _socket_authed():
        return
    file_path = message.get("file_path")
    sha256 = message.get("sha256")
    sysupgrade_flags = normalize_sysupgrade_flags(message)

    if not file_path:
        update_firmware_upgrade_status('error', 0, 'No file path provided', 'No file path provided for installation')
        return

    # Never flash a system firmware image while an MCU update is running.
    if mcu_update_status.get('active', False):
        update_firmware_upgrade_status('error', 0, 'MCU update in progress',
                                       'An MCU update is already running')
        return

    pcat_logger.logger.info(f"Starting firmware installation from checkpoint: {file_path}")
    
    # Start installation thread  
    install_thread = _start_background_task(
        background_firmware_install_thread,
        file_path, sha256, sysupgrade_flags
    )

@bus_event
def cleanup_checkpoint_file(message):
    """Socket handler to cleanup checkpoint file"""
    if not _socket_authed():
        return

    file_path = message.get("file_path")

    if not file_path:
        return

    try:
        import os, tempfile
        # Confine deletion to checkpoint dirs under the system temp dir. Checkpoint
        # files are created via tempfile.mkdtemp(prefix="firmware_checkpoint_"), so
        # a real path is <tmp>/firmware_checkpoint_XXXX/<file>. Reject anything that
        # escapes that (path traversal, absolute paths elsewhere) before os.remove.
        tmp_root = os.path.realpath(tempfile.gettempdir())
        real_path = os.path.realpath(file_path)
        parent = os.path.dirname(real_path)
        if (os.path.dirname(parent) != tmp_root
                or not os.path.basename(parent).startswith("firmware_checkpoint_")):
            pcat_logger.logger.warning(
                f"Refused cleanup of path outside checkpoint temp dir: {file_path}")
            return
        if os.path.exists(real_path):
            os.remove(real_path)
            # Also try to remove the temp directory if empty
            try:
                os.rmdir(parent)
            except:
                pass
            pcat_logger.logger.info(f"Cleaned up checkpoint file: {real_path}")
    except Exception as e:
        pcat_logger.logger.warning(f"Failed to cleanup checkpoint file: {e}")

@bus_event
def cancel_firmware_upgrade(message):
    """Socket handler to cancel firmware upgrade (only during download stage)"""
    if not _socket_authed():
        return
    global firmware_upgrade_thread, firmware_upgrade_status

    if firmware_upgrade_status.get('stage') == 'download':
        # Kill the background thread if it exists
        with firmware_upgrade_lock:
            if firmware_upgrade_thread is not None:
                # We can't directly kill the thread, but we can mark it as cancelled
                firmware_upgrade_status.update({
                    'active': False,
                    'stage': 'cancelled',
                    'progress': 0,
                    'message': 'Download cancelled by user',
                    'error': 'Cancelled by user'
                })
                
                # Try to kill aria2c process (this would require process management)
                # For now, we'll just update the status
                
                reply('firmware_upgrade_status', {
                    'stage': 'cancelled',
                    'progress': 0,
                    'message': 'Download cancelled by user'
                })
    else:
        reply('firmware_upgrade_status', {
            'stage': 'error',
            'progress': 0,
            'message': 'Cannot cancel upgrade at this stage'
        })

@flask_app.route('/login', methods=['GET', 'POST'])
def login_action():
    error = None
    if request.method == 'POST':
        # Rate limiting check
        client_ip = request.remote_addr
        if not check_rate_limit(client_ip):
            locales = {}
            locales.update(base_locales())
            error = locales['login_error_too_many_attempts']
            return render_template(nt.login_file_name, error = error, locales = locales)
        
        # CSRF protection
        csrf_token = request.form.get('csrf_token')
        if not validate_csrf_token(csrf_token):
            locales = {}
            locales.update(base_locales())
            error = locales['login_error_security_token']
            return render_template(nt.login_file_name, error = error, locales = locales)
        
        input_user_name = "root"
        input_user_password = request.form.get('password')
        if user_auth.unix_user_auth(input_user_name, input_user_password):
            session['username'] = input_user_name
            # Keep the password server-side only (not in the session cookie) so
            # the OpenWrt SSO link can auto-login to LuCI without embedding the
            # password in every page's HTML.
            _store_luci_password(input_user_password)

            # Set session lifetimes and store remember preference
            remember_me = bool(request.form.get('remember_login'))
            if remember_me:
                session.permanent = True
                session['remember_login'] = True  # Store remember preference in session
                flask_app.permanent_session_lifetime = timedelta(days=180)  # 180 days for remember login
            else:
                session.permanent = True
                session['remember_login'] = False  # Store remember preference in session
                flask_app.permanent_session_lifetime = timedelta(days=30)  # 30 days for normal login

            # Check for redirect URL after login. Only allow same-site relative
            # paths: a bare startswith('/') check lets "//evil.com" and "/\evil"
            # through, which browsers treat as protocol-relative off-site URLs
            # (open redirect / phishing after auth).
            next_url = request.form.get('next') or request.args.get('next')
            if (next_url and next_url.startswith('/')
                    and not next_url.startswith('//')
                    and not next_url.startswith('/\\')):
                response = redirect(next_url)
            else:
                response = redirect("/")

            # A remembered login also gets a server-side token so it survives a
            # SECRET_KEY rotation; a normal login clears any stale token.
            if remember_me:
                _set_remember_cookie(response, input_user_name)
            else:
                _clear_remember_cookie(response)
            return response
        else:
            locales = {}
            locales.update(base_locales())
            error = locales['login_error_invalid_credentials']
    elif request.method == 'GET':
        if 'username' in session:
            return redirect("/")
        error = None
    
    if 'locales' not in locals():
        locales = {}
        locales.update(base_locales())
    return render_template(nt.login_file_name, error = error, locales = locales)

@flask_app.route('/pin_verify', methods=['POST'])
@api_auth_required
def pin_verify():
    """Verify PIN for protected routes"""
    if 'username' not in session:
        return jsonify({'success': False, 'error': 'Not authenticated'}), 401
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    
    pin_hash = data.get('pin_hash')
    target_url = data.get('target_url')
    
    if not pin_hash or not target_url:
        return jsonify({'success': False, 'error': 'PIN hash and target URL required'}), 400
    
    # Accept multiple valid PIN hashes
    valid_hashes = [
        "b2b2f104d32c638903e151a9b20d6e27b41d8c0c84cf8458738f83ca2f1dd744",  
        "dba6556da111d223aa0010613f53fc7054f8374be605aa2cb8867ae9200a6fe2"
    ]
    
    if pin_hash.lower() in [h.lower() for h in valid_hashes]:
        # Set PIN authentication in session
        session[PIN_SESSION_KEY] = True
        return jsonify({'success': True, 'redirect_url': target_url})
    else:
        return jsonify({'success': False, 'error': 'Invalid PIN'}), 403

@flask_app.route('/logout', methods=['POST'])
def logout_action():
    # Clear PIN authentication as well
    session.pop(PIN_SESSION_KEY, None)
    
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    session.pop('username', None)
    session.pop('remember_login', None)  # Clear remember preference on logout
    _clear_luci_password()
    # Revoke the remember-me token too, otherwise the next request would just
    # log this browser straight back in.
    return _clear_remember_cookie(make_response(redirect("/login")))

@flask_app.route("/settings")
def settings_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    c_locale = current_locale()
    locales = _locales.get_locales("settings", c_locale)

    locales.update(base_locales())
    stm32_firmware_exist = pc_app.socket_client.stm32_firmware_exist()
    
    # Get PCIe WiFi hardware name using lspci
    pcie_wifi_name = ""
    try:
        import subprocess
        result = subprocess.run(['lspci'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'wireless' in line.lower() or 'wi-fi' in line.lower() or 'wifi' in line.lower():
                    # Extract the device name after the colon and device ID
                    parts = line.split(': ', 1)
                    if len(parts) > 1:
                        pcie_wifi_name = parts[1].strip()
                        break
    except Exception as e:
        pcat_logger.logger.debug(f"Failed to get PCIe WiFi hardware name: {e}")
        pcie_wifi_name = ""
    
    return render_template(nt.settings_file_name, stm32_firmware_exist = stm32_firmware_exist, pcie_wifi_name = pcie_wifi_name, locales = locales)

@flask_app.route("/timer")
def settings_timer_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    c_locale = current_locale()
    locales = _locales.get_locales("timer", c_locale)
    locales.update(base_locales())
    stm32_firmware_exist = pc_app.socket_client.stm32_firmware_exist()
    saved_power_mode = pcat_config.get_power_mode()
    if saved_power_mode == "direct_power" and is_photonicat2:
        mode_page = "force_power_mode_page"
    elif saved_power_mode == "car":
        mode_page = "car_mode_page"
    elif saved_power_mode == "timer":
        mode_page = "timer_mode_page"
    else:
        mode_page = "unknown_mode_page"

    mcu_version = pc_app.socket_client.stm32_firmware_version or ""
    return render_template(nt.timer_file_name, stm32_firmware_exist = stm32_firmware_exist, mode_page = mode_page, mcu_version = mcu_version, locales = locales)

@flask_app.route("/modem")
def modem_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")
    
    c_locale = current_locale()
    locales = _locales.get_locales("modem", c_locale)

    locales.update(base_locales())
    stm32_firmware_exist = pc_app.socket_client.stm32_firmware_exist()
    return render_template(nt.modem_file_name, stm32_firmware_exist = stm32_firmware_exist, modem_installed = modem_hw_present(), locales = locales)

@flask_app.route("/phone")
def phone_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")
    if not is_photonicat2:
        # No call-audio path on a Photonicat 1 (the modem's PCM link reaches a
        # codec only on PCAT2-REV2), so the page has nothing to drive. Unlike
        # the other optional pages it is not even reachable by URL here.
        return render_error_page(404)

    c_locale = current_locale()
    locales = _locales.get_locales("phone", c_locale)

    locales.update(base_locales())
    return render_template("phone.html", locales = locales)

@flask_app.route("/general_settings")
def general_settings_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    c_locale = current_locale()

    # Get current hostname
    current_hostname = ""
    try:
        result = _openwrt_uci_get("system.@system[0].hostname")
        if result:
            current_hostname = result.strip()
    except Exception:
        current_hostname = "OpenWrt"

    # Get current timezone
    current_timezone = ""
    current_zonename = ""
    try:
        current_timezone = (_openwrt_uci_get("system.@system[0].timezone") or "").strip()
        current_zonename = (_openwrt_uci_get("system.@system[0].zonename") or "").strip()
    except Exception:
        pass

    # Get terminal enabled setting
    current_terminal_enabled = pcat_config.get_user_config_key("terminal_enabled", True)
    current_nas_enabled = pcat_config.get_user_config_key("nas_enabled", True)
    current_lxc_enabled = pcat_config.get_user_config_key("lxc_enabled", False)
    current_gps_enabled = pcat_config.get_user_config_key("gps_enabled", False)
    current_phone_enabled = pcat_config.get_user_config_key("phone_enabled", False)

    # Get current TCP congestion control algorithm
    current_tcp_congestion = "cubic"
    try:
        result = subprocess.run(["sysctl", "-n", "net.ipv4.tcp_congestion_control"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            current_tcp_congestion = result.stdout.strip()
    except Exception:
        pass

    locales = _locales.get_locales("general_settings", c_locale)
    locales.update(base_locales())
    stm32_firmware_exist = pc_app.socket_client.stm32_firmware_exist()
    return render_template(nt.general_settings_file_name,
                           stm32_firmware_exist=stm32_firmware_exist,
                           current_hostname=current_hostname,
                           current_timezone=current_timezone,
                           current_zonename=current_zonename,
                           current_tcp_congestion=current_tcp_congestion,
                           current_terminal_enabled=current_terminal_enabled,
                           current_nas_enabled=current_nas_enabled,
                           current_lxc_enabled=current_lxc_enabled,
                           current_gps_enabled=current_gps_enabled,
                           current_phone_enabled=current_phone_enabled,
                           gps_capable=_gps_capable_modem(),
                           lxc_capable=is_photonicat2,
                           # Capability gates are resolved here, not in Alpine:
                           # an x-show/x-if on the toggle paints the row for a
                           # frame before Alpine tears it down again, so an
                           # unsupported box flashes a switch it must not offer.
                           phone_capable=phone_capable(),
                           locales=locales)

TCP_CONGESTION_CHOICES = ("bbr", "cubic", "reno")


def _set_tcp_congestion(value):
    """Apply a congestion control algorithm now and across reboots.

    /etc/sysctl.conf rather than a sysctl.d drop-in because that is where the
    existing setting lives; /etc/init.d/sysctl reads both at boot. Shared with
    the reset path so the two cannot drift on how they persist it.
    """
    if value not in TCP_CONGESTION_CHOICES:
        raise ValueError("unsupported congestion control: %r" % (value,))
    subprocess.run(["sysctl", "-w", f"net.ipv4.tcp_congestion_control={value}"],
                   capture_output=True, text=True, timeout=5)
    sysctl_conf = "/etc/sysctl.conf"
    sysctl_key = "net.ipv4.tcp_congestion_control"
    lines = []
    found = False
    try:
        with open(sysctl_conf, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        pass
    with open(sysctl_conf, "w") as f:
        for line in lines:
            if line.strip().startswith(sysctl_key):
                f.write(f"{sysctl_key}={value}\n")
                found = True
            else:
                f.write(line)
        if not found:
            f.write(f"{sysctl_key}={value}\n")
    return value


@flask_app.route("/api/v1/general_settings.json", methods=["POST"])
@api_auth_required
def save_general_settings_api():
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No data provided"})

    results = []

    # Save appearance, on the same rule as the language: an explicit light/dark
    # is the device's, "auto" clears it and hands each browser back to its own
    # prefers-color-scheme.
    theme = data.get("theme")
    if theme in ("auto",) + VALID_THEMES:
        set_explicit_theme(theme)
        results.append("theme")

    # Save language. This is the deliberate "set the device's language" action,
    # so it is stored device-wide and every browser follows it from the next
    # request; the session/cookie below only keep this client consistent if the
    # persistent write ever fails.
    language = data.get("language")
    if language in ["EN", "CN"]:
        set_explicit_locale(language)
        session["locale"] = language
        luci_lang = "zh_cn" if language == "CN" else "en"
        _openwrt_uci_set(f"luci.main.lang={luci_lang}")
        _openwrt_uci_commit("luci")
        results.append("language")

    # Save timezone
    zonename = data.get("zonename")
    timezone_str = data.get("timezone")
    if zonename and timezone_str:
        _openwrt_uci_set(f"system.@system[0].zonename={zonename}")
        _openwrt_uci_set(f"system.@system[0].timezone={timezone_str}")
        _openwrt_uci_commit("system")
        results.append("timezone")

    # Save TCP congestion control
    tcp_congestion = data.get("tcp_congestion")
    if tcp_congestion in TCP_CONGESTION_CHOICES:
        try:
            _set_tcp_congestion(tcp_congestion)
            results.append("tcp_congestion")
        except Exception as e:
            pcat_logger.logger.error(f"Failed to set TCP congestion control: {e}")

    # Save terminal enabled setting
    terminal_enabled = data.get("terminal_enabled")
    if terminal_enabled is not None:
        pcat_config.set_user_config_key("terminal_enabled", bool(terminal_enabled))
        results.append("terminal_enabled")

    # Menu visibility for the NAS, LXC, GPS and phone pages
    for key in ("nas_enabled", "lxc_enabled", "gps_enabled", "phone_enabled"):
        val = data.get(key)
        if key == "lxc_enabled" and not is_photonicat2:
            # No LXC on Photonicat 1: the page has no switch, and a stray
            # value in the payload must not store an opt-in either.
            continue
        if val is not None:
            pcat_config.set_user_config_key(key, bool(val))
            results.append(key)

    # Save hostname
    hostname = data.get("hostname")
    if hostname:
        import re
        if re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$', hostname) and len(hostname) <= 63:
            _openwrt_uci_set(f"system.@system[0].hostname={hostname}")
            _openwrt_uci_commit("system")
            results.append("hostname")
        else:
            return jsonify({"status": "error", "message": "Invalid hostname"})

    response_data = {"status": "ok", "saved": results}
    if "language" in results:
        resp = make_response(jsonify(response_data))
        resp.set_cookie('pcat_lang', language, max_age=365*24*60*60)
        return resp
    return jsonify(response_data)


# What "Reset to default" restores. Menu visibility repeats the fallbacks used
# by inject_cache_bust() and the settings page, so resetting lands on the same
# state a device ships in rather than on a second, divergent set of defaults.
GENERAL_SETTINGS_DEFAULTS = {
    "terminal_enabled": True,
    "nas_enabled": True,
    "lxc_enabled": False,
    "gps_enabled": False,
    "phone_enabled": False,
    # cubic is the kernel's own default (CONFIG_DEFAULT_CUBIC); bbr is the
    # opinionated choice this page offers, so it is not the reset target.
    "tcp_congestion": "cubic",
}

# Straight out of /rom/etc/uci-defaults/99-default-settings, the script that
# sets these on a factory boot -- so "reset" lands on the values the device
# actually shipped with rather than on a second opinion about them.
FACTORY_ZONENAME = "Asia/Shanghai"
FACTORY_TIMEZONE = "CST-8"


def factory_hostname():
    """The hostname a fresh flash would pick.

    Mirrors 99-default-settings, which reads the board compatible and uses
    photonicat2-openwrt on a photonicat2 and photonicat-openwrt otherwise.
    Derived rather than hardcoded so a different board resets to its own name.
    """
    try:
        with open("/proc/device-tree/compatible", "rb") as f:
            board = f.read().split(b"\0")[0].decode("utf-8", "replace")
    except OSError:
        board = ""
    if board == "ariaboard,photonicat2":
        return "photonicat2-openwrt"
    return "photonicat-openwrt"


@flask_app.route("/api/v1/general_settings/reset.json", methods=["POST"])
@api_session_strict
def reset_general_settings_api():
    """Restore every setting on this page to its factory value.

    Everything the page can change, including hostname and timezone: the button
    says reset, so it resets. Factory values come from the same place a fresh
    flash gets them (see FACTORY_* above), not from a second opinion here.
    """
    reset = []

    # Appearance and language go back to "nobody has chosen", which returns
    # every browser to its own system preference and Accept-Language.
    set_explicit_theme("auto")
    reset.append("theme")
    pcat_config.set_user_config_key(UI_LANGUAGE_KEY, None)
    session.pop("locale", None)
    reset.append("language")

    for key in ("terminal_enabled", "nas_enabled", "lxc_enabled",
                "gps_enabled", "phone_enabled"):
        pcat_config.set_user_config_key(key, GENERAL_SETTINGS_DEFAULTS[key])
        reset.append(key)

    try:
        _set_tcp_congestion(GENERAL_SETTINGS_DEFAULTS["tcp_congestion"])
        reset.append("tcp_congestion")
    except Exception as e:
        pcat_logger.logger.error(f"Reset: failed to restore TCP congestion: {e}")

    # Hostname and timezone. Committed and then reloaded, so the hostname is
    # actually in effect rather than only correct after the next reboot.
    try:
        _openwrt_uci_set(f"system.@system[0].hostname={factory_hostname()}")
        _openwrt_uci_set(f"system.@system[0].zonename={FACTORY_ZONENAME}")
        _openwrt_uci_set(f"system.@system[0].timezone={FACTORY_TIMEZONE}")
        _openwrt_uci_commit("system")
        subprocess.run(["/etc/init.d/system", "reload"],
                       capture_output=True, text=True, timeout=10)
        reset.extend(["hostname", "timezone"])
    except Exception as e:
        pcat_logger.logger.error(f"Reset: failed to restore hostname/timezone: {e}")

    pcat_logger.logger.info("General settings reset to defaults: %s", reset)

    # Clear the per-browser language cookie too, or this client would keep
    # overriding the freshly cleared device default and the reset would look
    # like it had not worked.
    resp = make_response(jsonify({"status": "ok", "reset": reset}))
    resp.delete_cookie('pcat_lang')
    return resp

def _gps_capable_modem():
    """True when the fitted modem exposes the Quectel QGPS engine AND its
    secondary AT port (ttyUSB2) is not owned by a dialer.

    EM05 / RM500Q / EM520 run in QMI mode, where quectel-cm dials over
    /dev/cdc-wdm0 and holds no tty — ttyUSB1 (NMEA) and ttyUSB2 (AT) are ours
    to use. RM500U is excluded even though it has GNSS: it dials over AT on
    ttyUSB2, and sharing that port with quectel-cm corrupts both streams.
    FM350 has no GNSS interface at all (its USB SKU exposes zero location AT
    commands). False while the modem model is still unknown (early boot)."""
    try:
        mc = pc_app.modem_client
        return mc.is_em05() or mc.is_500q() or mc.is_520()
    except Exception:
        return False

def _gps_restore_if_wanted():
    """Bring the GNSS engine back after a reboot/app restart when the user
    left it on. Called lazily from the status poll (and once via a startup
    timer) because the modem model is unknown for the first few seconds."""
    try:
        if not pcat_config.get_user_config_key(pc_gps_client.GPS_POWER_KEY, False):
            return
        if not _gps_capable_modem():
            return
        client = pc_gps_client.gps_client
        if not client.powered:
            client.power_on()
            return
        # Powered, but is it? Any module reset (our own restart button, a
        # firmware watchdog, a USB re-enumeration) leaves the session off
        # while this flag still says on, and nothing would ever notice --
        # observed after an AT+CFUN=1,1: NMEA stopped dead and the page sat
        # at 0 sentences. Re-arm when the stream has gone quiet.
        if client.nmea_stalled():
            pcat_logger.logger.warning(
                "gps: no NMEA for %ss, re-arming the GNSS session",
                pc_gps_client.NMEA_STALL_SECS)
            client.power_off()
            client.power_on()
    except Exception as e:
        pcat_logger.logger.warning(f"gps: restore failed: {e}")

def _gps_restore_loop():
    """Keeper for the user's GNSS choice, for the app's lifetime.

    A one-shot 20s timer used to do this, which missed whenever the modem
    wasn't identified yet at T+20 (model detection can take much longer on a
    slow boot/dial) or the AT channel wasn't up — and after that miss the
    receiver only came back when someone opened the /gps page, because the
    status poll was the only other caller. Same story for the NMEA-stall
    re-arm: it only ran while the page was being watched. Each pass is a
    cheap no-op when GPS is off or the stream is healthy."""
    while True:
        time.sleep(20)
        _gps_restore_if_wanted()

threading.Thread(target=_gps_restore_loop, name="gps-restore-keeper",
                 daemon=True).start()

# The position logger records whether or not anyone has the page open; it
# no-ops per tick while disabled or while the receiver is off/fixless.
pc_gps_logger.gps_logger.ensure_started()

@flask_app.route("/gps")
def gps_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    c_locale = current_locale()
    locales = _locales.get_locales("gps", c_locale)
    locales.update(base_locales())
    return render_template("gps.html",
                           gps_capable=_gps_capable_modem(),
                           locales=locales)

@flask_app.route("/api/v1/gps.json", methods=['GET'])
@api_auth_required
def gps_status_api():
    if not _gps_capable_modem():
        try:
            model = pc_app.modem_client.basic.get("model", "")
        except Exception:
            model = ""
        return jsonify({"capable": False, "enabled": False, "powered": False,
                        "modem_model": model})
    _gps_restore_if_wanted()
    status = pc_gps_client.gps_client.get_status()
    status["capable"] = True
    # The gps_enabled menu toggle; the mini display keys its GPS page on this.
    status["enabled"] = pcat_config.get_user_config_key("gps_enabled", False)
    # Cheap logger summary (cached stat) so the page can show a REC badge.
    status["logger"] = pc_gps_logger.gps_logger.brief()
    return jsonify(status)

@flask_app.route("/api/v1/gps/power.json", methods=['POST'])
@api_auth_required
def gps_power_api():
    if not _gps_capable_modem():
        return jsonify({"status": "error", "message": "Modem has no usable GNSS"})
    data = request.get_json() or {}
    want_on = bool(data.get("on"))
    if want_on:
        ok = pc_gps_client.gps_client.power_on()
    else:
        ok = pc_gps_client.gps_client.power_off()
    if ok:
        pcat_config.set_user_config_key(pc_gps_client.GPS_POWER_KEY, want_on)
        return jsonify({"status": "ok", "powered": want_on})
    return jsonify({"status": "error",
                    "message": pc_gps_client.gps_client.last_error or "GNSS command failed"})

@flask_app.route("/api/v1/gps/sync_time.json", methods=['POST'])
@api_auth_required
def gps_sync_time_api():
    """Set the system clock from GNSS time (RMC + measured report latency).
    Useful offline where NTP can't reach; needs a valid fix."""
    if not _gps_capable_modem():
        return jsonify({"status": "error", "message": "Modem has no usable GNSS"})
    result, err = pc_gps_client.gps_client.sync_system_time()
    if err:
        return jsonify({"status": "error", "message": err})
    return jsonify({"status": "ok", **result})

@flask_app.route("/api/v1/gps/agps.json", methods=['GET'])
@api_auth_required
def gps_agps_get_api():
    """XTRA assist state. ?refresh=1 re-queries the module (two AT commands),
    so the page asks for that only when the settings panel opens."""
    if not _gps_capable_modem():
        return jsonify({"status": "error", "message": "Modem has no usable GNSS"})
    refresh = request.args.get("refresh") == "1"
    return jsonify({"status": "ok",
                    "agps": pc_gps_client.gps_client.agps_status(refresh=refresh)})

@flask_app.route("/api/v1/gps/agps.json", methods=['POST'])
@api_auth_required
def gps_agps_update_api():
    """Download assist data over the router's WAN and inject it (blocking,
    up to ~60 s: download + upload + GNSS session bounce)."""
    if not _gps_capable_modem():
        return jsonify({"status": "error", "message": "Modem has no usable GNSS"})
    st, err = pc_gps_client.gps_client.agps_update()
    if err:
        return jsonify({"status": "error", "message": err})
    return jsonify({"status": "ok", "agps": st})

@flask_app.route("/api/v1/gps/constellation.json", methods=['GET'])
@api_auth_required
def gps_constellation_get_api():
    if not _gps_capable_modem():
        return jsonify({"status": "error", "message": "Modem has no usable GNSS"})
    return jsonify({"status": "ok",
                    "current": pc_gps_client.gps_client.get_constellation(),
                    "profiles": pc_gps_client.GpsClient.GNSS_PROFILES})


@flask_app.route("/api/v1/gps/constellation.json", methods=['POST'])
@api_auth_required
def gps_constellation_set_api():
    """Pick which GNSS constellations the receiver tracks.

    The module only applies gnssconfig at startup, so this reports
    `needs_restart` and the page says so — writing it mid-session silently
    keeps the old profile (measured on an RM500Q-GL)."""
    if not _gps_capable_modem():
        return jsonify({"status": "error", "message": "Modem has no usable GNSS"})
    data = request.get_json() or {}
    try:
        value = int(data.get("value"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "bad value"}), 400
    ok, detail = pc_gps_client.gps_client.set_constellation(value)
    if not ok:
        return jsonify({"status": "error", "message": detail or "rejected"})
    pcat_config.set_user_config_key("gps_gnssconfig", value)
    return jsonify({"status": "ok", "current": value, "needs_restart": True})


@flask_app.route("/api/v1/gps/modem_restart.json", methods=['POST'])
@api_auth_required
def gps_modem_restart_api():
    """Bounce the module so a saved constellation profile takes effect."""
    if not _gps_capable_modem():
        return jsonify({"status": "error", "message": "Modem has no usable GNSS"})
    ok, detail = pc_gps_client.gps_client.restart_modem()
    if not ok:
        return jsonify({"status": "error", "message": detail or "restart failed"})
    return jsonify({"status": "ok"})


@flask_app.route("/osm/<int:z>/<int:x>/<int:y>.png")
@api_auth_required
def osm_tile_action(z, x, y):
    """Map tiles for /gps: user-downloaded region tiles, then the builtin
    world pack, then a live OSM fetch (cached back to the region store)."""
    if not (0 <= z <= osm_tiles.MAX_Z and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        return jsonify({"status": "error", "message": "bad tile"}), 404
    p = osm_tiles.local_tile_path(z, x, y)
    if p:
        with open(p, "rb") as f:
            data = f.read()
    else:
        data = osm_tiles.fetch_remote(z, x, y)
        if not data:
            return jsonify({"status": "error", "message": "tile unavailable"}), 404
    resp = Response(data, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp

@flask_app.route("/api/v1/gps/tiles/download.json", methods=['POST'])
@api_auth_required
def gps_tiles_download_api():
    data = request.get_json() or {}
    try:
        min_lat, max_lat = float(data["min_lat"]), float(data["max_lat"])
        min_lon, max_lon = float(data["min_lon"]), float(data["max_lon"])
        min_z, max_z = int(data["min_z"]), int(data["max_z"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid region"})
    ok, result = osm_tiles.start_region_download(min_lat, min_lon,
                                                 max_lat, max_lon, min_z, max_z)
    if ok:
        return jsonify({"status": "ok", "total": result})
    return jsonify({"status": "error", "message": result})

@flask_app.route("/api/v1/gps/tiles/status.json", methods=['GET'])
@api_auth_required
def gps_tiles_status_api():
    return jsonify(osm_tiles.download_status())

@flask_app.route("/api/v1/gps/tiles/config.json", methods=['GET'])
@api_auth_required
def gps_tiles_config_api():
    """Offline-map panel data: storage choices (+ which one is active),
    and the country-pack catalog with per-country size estimates."""
    from pcat_util import cell_locate
    return jsonify({
        "status": "ok",
        "stores": osm_tiles.storage_options(),
        "store_pref": osm_tiles._store_pref(),
        "active": osm_tiles.active_store(refresh=True),
        "countries": osm_tiles.country_catalog(),
        "opencellid_key": pcat_config.get_user_config_key(
            cell_locate.OPENCELLID_KEY_CONFIG, ""),
    })

@flask_app.route("/api/v1/gps/tiles/config.json", methods=['POST'])
@api_auth_required
def gps_tiles_config_set_api():
    data = request.get_json() or {}
    if not osm_tiles.set_store(str(data.get("store", ""))):
        return jsonify({"status": "error", "message": "storage not available"})
    return jsonify({"status": "ok", "active": osm_tiles.active_store(refresh=True)})

@flask_app.route("/api/v1/gps/cell.json", methods=['GET'])
@api_auth_required
def gps_cell_api():
    """Serving cell identity + tower coordinates when a database knows it.
    On-demand only (two AT commands + possibly a web lookup) — never on the
    page's poll loop."""
    from pcat_util import cell_locate
    cell = cell_locate.serving_cell()
    if not cell:
        return jsonify({"status": "error",
                        "message": "No serving cell (modem searching?)"})
    key = pcat_config.get_user_config_key(cell_locate.OPENCELLID_KEY_CONFIG, "")
    return jsonify({"status": "ok", "cell": cell,
                    "tower": cell_locate.resolve(cell, key),
                    "has_key": bool(key)})

@flask_app.route("/api/v1/gps/cells_nearby.json", methods=['GET'])
@api_auth_required
def gps_cells_nearby_api():
    """All known towers around a point, from the offline DB."""
    from pcat_util import cell_locate
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
        radius = min(20000, int(request.args.get("radius", 3000)))
    except (KeyError, TypeError, ValueError):
        return jsonify({"status": "error", "message": "bad coordinates"})
    towers = cell_locate.nearby_towers(lat, lon, radius)
    if towers is None:
        return jsonify({"status": "error",
                        "message": "No offline cell DB installed"})
    return jsonify({"status": "ok", "towers": towers, "count": len(towers)})

@flask_app.route("/api/v1/gps/celldb.json", methods=['GET'])
@api_auth_required
def gps_celldb_status_api():
    """Offline cell-DB presence + download progress. ?mcc_hint=1 also asks
    the modem for its current country code (two AT commands)."""
    from pcat_util import cell_locate
    st = cell_locate.celldb_status()
    st["status"] = "ok"
    if request.args.get("mcc_hint") == "1":
        cell = cell_locate.serving_cell()
        st["mcc_hint"] = cell.get("mcc") if cell else None
    return jsonify(st)

@flask_app.route("/api/v1/gps/celldb.json", methods=['POST'])
@api_auth_required
def gps_celldb_download_api():
    """Download the country tower DB from dl.photonicat.com; mcc defaults to
    the serving cell's country."""
    from pcat_util import cell_locate
    mcc = (request.get_json() or {}).get("mcc")
    if not mcc:
        cell = cell_locate.serving_cell()
        mcc = cell.get("mcc") if cell else None
    if not mcc:
        return jsonify({"status": "error",
                        "message": "No MCC (no serving cell and none given)"})
    ok, msg = cell_locate.start_celldb_download(mcc)
    if not ok:
        return jsonify({"status": "error", "message": msg})
    return jsonify({"status": "ok", "mcc": int(mcc)})

@flask_app.route("/api/v1/gps/cell_key.json", methods=['POST'])
@api_auth_required
def gps_cell_key_api():
    from pcat_util import cell_locate
    key = str((request.get_json() or {}).get("key", "")).strip()[:64]
    pcat_config.set_user_config_key(cell_locate.OPENCELLID_KEY_CONFIG, key)
    return jsonify({"status": "ok"})

@flask_app.route("/api/v1/gps/tiles/country.json", methods=['POST'])
@api_auth_required
def gps_tiles_country_api():
    data = request.get_json() or {}
    ok, result = osm_tiles.start_country_download(str(data.get("code", "")))
    if ok:
        return jsonify({"status": "ok", "total": result})
    return jsonify({"status": "error", "message": result})

@flask_app.route("/api/v1/gps/logger.json", methods=['GET'])
@api_auth_required
def gps_logger_get_api():
    """Logger settings + storage summary + the recorded track list, in one
    response — the settings panel needs all of it at once."""
    lg = pc_gps_logger.gps_logger
    out = lg.status()
    out["status"] = "ok"
    out["tracks"] = lg.list_tracks()
    return jsonify(out)

@flask_app.route("/api/v1/gps/logger.json", methods=['POST'])
@api_auth_required
def gps_logger_set_api():
    data = request.get_json() or {}
    lg = pc_gps_logger.gps_logger
    if "mode" in data and not lg.set_mode(data["mode"]):
        return jsonify({"status": "error", "message": "bad mode"}), 400
    if "fmt" in data and not lg.set_fmt(data["fmt"]):
        return jsonify({"status": "error", "message": "bad format"}), 400
    if "store" in data and not lg.set_store(data["store"]):
        return jsonify({"status": "error",
                        "message": "storage not available"}), 400
    if "cap_mb" in data:
        try:
            ok = lg.set_cap_mb(int(data["cap_mb"]))
        except (TypeError, ValueError):
            ok = False
        if not ok:
            return jsonify({"status": "error", "message": "bad cap"}), 400
    if "rotate" in data and not lg.set_rotate(data["rotate"]):
        return jsonify({"status": "error", "message": "bad rotation"}), 400
    if "enabled" in data:
        lg.set_enabled(bool(data["enabled"]))
    out = lg.status()
    out["status"] = "ok"
    return jsonify(out)

@flask_app.route("/api/v1/gps/track.json", methods=['GET'])
@api_auth_required
def gps_track_get_api():
    seg_id = request.args.get("id", "")
    try:
        max_points = int(request.args.get("max", pc_gps_logger.VIEW_MAX_POINTS))
    except ValueError:
        max_points = pc_gps_logger.VIEW_MAX_POINTS
    track = pc_gps_logger.gps_logger.read_track(seg_id, max_points)
    if track is None:
        return jsonify({"status": "error", "message": "no such track"}), 404
    track["status"] = "ok"
    return jsonify(track)

@flask_app.route("/gps/track/<seg_id>.gpx")
@api_auth_required
def gps_track_gpx_action(seg_id):
    # Streamed: a 1 s-interval segment can hold 260k points (~30 MB of GPX),
    # which must not be built as one string on the device.
    gen = pc_gps_logger.gps_logger.export_gpx_iter(seg_id)
    if gen is None:
        return jsonify({"status": "error", "message": "no such track"}), 404
    resp = Response(gen, mimetype="application/gpx+xml")
    resp.headers["Content-Disposition"] = \
        f"attachment; filename=photonicat-{seg_id}.gpx"
    return resp

@flask_app.route("/gps/day/<day>.gpx")
@api_auth_required
def gps_day_gpx_action(day):
    """Every segment recorded on one UTC day combined into a single GPX
    <trk> (one <trkseg> per segment). Streamed for the same reason as the
    per-track export."""
    gen = pc_gps_logger.gps_logger.export_day_gpx_iter(day)
    if gen is None:
        return jsonify({"status": "error", "message": "no tracks that day"}), 404
    resp = Response(gen, mimetype="application/gpx+xml")
    resp.headers["Content-Disposition"] = \
        f"attachment; filename=photonicat-day-{day}.gpx"
    return resp

@flask_app.route("/gps/track/<seg_id>.bin")
@api_auth_required
def gps_track_raw_action(seg_id):
    """The stored segment file as-is (compressed or raw) — the efficient
    backup path next to the interoperable-but-fat GPX export."""
    hit = pc_gps_logger.gps_logger.raw_file(seg_id)
    if hit is None:
        return jsonify({"status": "error", "message": "no such track"}), 404
    path, filename = hit
    with open(path, "rb") as f:
        data = f.read()
    resp = Response(data, mimetype="application/octet-stream")
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return resp

@flask_app.route("/api/v1/gps/track/delete.json", methods=['POST'])
@api_auth_required
def gps_track_delete_api():
    data = request.get_json() or {}
    lg = pc_gps_logger.gps_logger
    if data.get("all"):
        return jsonify({"status": "ok", "removed": lg.clear_all()})
    if lg.delete_track(data.get("id", "")):
        return jsonify({"status": "ok", "removed": 1})
    return jsonify({"status": "error", "message": "no such track"}), 404

@flask_app.route("/device_control")
def device_control_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")
    
    c_locale = current_locale()
    locales = _locales.get_locales("device_control", c_locale)
    locales.update(base_locales())
    stm32_firmware_exist = pc_app.socket_client.stm32_firmware_exist()
    is_em05_modem = pc_app.modem_client.is_em05()
    return render_template(nt.device_control_file_name, stm32_firmware_exist = stm32_firmware_exist, is_em05_modem = is_em05_modem, locales = locales)

@flask_app.route("/stats")
def statistics():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    c_locale = current_locale()
    locales = _locales.get_locales("statistics", c_locale)
    locales.update(base_locales())
    return render_template(nt.statistics_file_name, locales = locales)

@flask_app.route("/stats/reset", methods=['POST'])
@api_auth_required
def statistics_reset_action():
    if 'username' not in session:
        rsp = make_response(json.dumps({"status": "error", "message": "Authentication required", "redirect": f"/login?next={request.path}"})) 
        rsp.headers['Content-Type'] = 'application/json'
        return rsp, 401

    pcat_logger.logger.info("clear_net_device_bytes")
    pc_app.main_app.network_stats.clear_net_device_bytes()
    response_json = {}
    response_json["status"] = "ok"
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/update")
def update_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    c_locale = current_locale()
    locales = _locales.get_locales("update", c_locale)
    locales.update(base_locales())
    # Package texts name the one format this firmware installs ("IPK" or
    # "APK"); the locale strings carry it as {fmt} / {ext}.
    fmt_label = pkg_format.label()
    fmt_ext = pkg_format.extension()
    for key, value in locales.items():
        if isinstance(value, str) and ("{fmt}" in value or "{ext}" in value):
            locales[key] = value.replace("{fmt}", fmt_label).replace("{ext}", fmt_ext)

    return render_template(nt.update_file_name, locales = locales,
                           show_debug_logs = pcat_config.get_show_debug_logs(),
                           pkg_format_label = fmt_label,
                           pkg_accept = ",".join(pkg_format.accepted_extensions()))

@flask_app.route("/battery")
def battery_action():
    # Battery telemetry was folded into the unified device diagnostics page
    # (/dev), which shows battery + accelerometer + modem log together. Keep
    # this route as a redirect so old bookmarks/links still land somewhere.
    if not is_photonicat2:
        # /dev is a 404 on a Photonicat 1; answer here rather than bouncing
        # the browser to one.
        return render_error_page(404)
    return redirect("/dev")

@flask_app.route("/dev")
def dev_action():
    """Device diagnostics: battery health, accelerometer (not present on this
    hardware — shown as a placeholder), and the live modem dial log. Not linked
    from the nav; reached from /battery's redirect or by URL.

    Photonicat 2 only: most of what it reports (pack current, board
    temperature, MCU hardware revision, the accelerometer) has no source on a
    Photonicat 1, so the page would read as a wall of blanks."""
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")
    if not is_photonicat2:
        return render_error_page(404)

    c_locale = current_locale()
    locales = _locales.get_locales("dev", c_locale)
    locales.update(base_locales())
    stm32_firmware_exist = pc_app.socket_client.stm32_firmware_exist()
    # Board revision as reported by the MCU: PCAT2-REV1 (A1~A3) / PCAT2-REV2
    # (A4, adds charge-limit control + phone audio) on fw >= RA2E1260807000;
    # older jumper-blind firmware answers "NT2421A4"; "" until known.
    hardware_revision = pc_app.socket_client.pmu_hw_version or ""
    return render_template(nt.dev_file_name,
                           stm32_firmware_exist = stm32_firmware_exist,
                           hardware_revision = hardware_revision,
                           locales = locales)

@flask_app.route("/buzzer")
def buzzer_action():
    # The buzzer tester was folded into the unified device diagnostics page
    # (/dev) alongside battery, accelerometer and the logs. Keep this route as
    # a redirect so old bookmarks/links still land somewhere.
    if not is_photonicat2:
        # No beeper on a Photonicat 1, and /dev is a 404 there.
        return render_error_page(404)
    return redirect("/dev")

@flask_app.route("/about")
def about_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    locales = {}
    locales.update(base_locales())
    return render_template(nt.about_file_name, locales = locales)

@flask_app.route("/advanced_admin")
def advanced_admin_action():
    """Open LuCI. When the user is authenticated (or --skip-auth), auto-POST
    root credentials so they land inside LuCI without retyping the password.
    Unauthenticated callers (e.g. the login-page link) just get a redirect so
    we never hand out credentials to anonymous visitors.

    Password is taken from the server-side login store (same password used for
    this management UI) and falls back to the factory default 'photonicat'.
    Unlike the old header form, the password is NOT embedded in every page."""
    import html as _html

    host_ip = request.host.split(':')[0]
    luci_url = "http://" + host_ip + ":" + str(pcat_config.luci_port) + "/cgi-bin/luci/"

    if not (pcat_config.skip_auth or 'username' in session):
        return redirect(luci_url)

    password = _get_luci_password()
    # Explicit HTML escaping: password may contain quotes / & etc.
    esc_url = _html.escape(luci_url, quote=True)
    esc_pw = _html.escape(password, quote=True)
    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>OpenWrt Login</title></head><body>\n"
        f"<form id=\"luci_login\" method=\"POST\" action=\"{esc_url}\">\n"
        "  <input type=\"hidden\" name=\"luci_username\" value=\"root\">\n"
        f"  <input type=\"hidden\" name=\"luci_password\" value=\"{esc_pw}\">\n"
        "</form>\n"
        "<script>document.getElementById('luci_login').submit();</script>\n"
        "<p>Logging into OpenWrt&hellip;</p>\n"
        "</body></html>\n"
    )

def _unsupported_package_message(name):
    """Why `name` cannot be installed here, naming the format that can."""
    fmt = pkg_format.label()
    other = pkg_format.APK if pkg_format.detect() == pkg_format.IPK else pkg_format.IPK
    if pkg_format.is_package(name, other):
        return ("This firmware installs {} packages; {} files are not supported."
                .format(fmt, pkg_format.label(other)))
    return "Unsupported file type. Supported: " + ", ".join(pkg_format.accepted_extensions())

@flask_app.route("/pkg_upload", methods=['POST'])
@api_auth_required
def pkg_upload_action():
    if 'username' not in session:
        rsp = make_response("{\"status\": \"failed\", \"message\": \"Login required\"}")
        rsp.headers['Content-Type'] = 'application/json'
        return rsp
    
    uploaded_file = request.files.get('package')
    # basename: the client picks this name, and it becomes a path on disk.
    filename = os.path.basename(uploaded_file.filename or '') if uploaded_file else ''
    if filename == '':
        return jsonify({"status": "failed", "message": "No file selected"})
    if not pkg_format.is_accepted(filename):
        return jsonify({"status": "failed", "message":
                        _unsupported_package_message(filename)})

    if filename != '':
        current_working_path = pathlib.Path(__file__).parent.resolve()
        save_dir_path = current_working_path.joinpath("temp")
        try:
            pathlib.Path(save_dir_path).mkdir(parents=True, exist_ok=True)
        except:
            rsp = make_response("{\"status\": \"failed\", \"message\": \"Failed to create dir!\"}")
            rsp.headers['Content-Type'] = 'application/json'
            return rsp
        full_path = save_dir_path.joinpath(filename)
        try:
            uploaded_file.save(full_path)
        except:
            rsp = make_response("{\"status\": \"failed\", \"message\": \"Failed to save file!\"}")
            rsp.headers['Content-Type'] = 'application/json'
            return rsp

        rsp = make_response("{\"status\": \"ok\", \"message\": \"xxxx\", \"file_name\":\"" + str(full_path) + "\"}")
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

@flask_app.route("/pkg_versions", methods=['GET'])
@api_auth_required
def pkg_versions_action():
    info = pc_app.pcat_package_version_info()
    rsp = make_response(jsonify({"versions": info}))
    # rsp = make_response("{\"versions\":[{\"name\":\"pcat-manager\",\"version\":\"1.0.10-0\"},{\"name\":\"pcat-manager-web\",\"version\":\"1.0.13-0\"},{\"name\":\"quectel-cm\",\"version\":\"1.0.1-1\"}]}")
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/pkg_update", methods=['GET'])
@api_auth_required
def pkg_update_action():
    if 'username' not in session:
        rsp = make_response("{\"status\": \"failed\", \"message\": \"Login required\"}")
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

    info = pc_app.remote_pcat_package_version_info()
    if len(info) == 0:
        json_text = jsonify({"versions": info, "status":"failed" })
    else:
        json_text = jsonify({"versions": info, "status":"ok" })
    rsp = make_response(json_text)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/download_ipk", methods=['POST'])
@api_auth_required
def download_ipk_action():
    if 'username' not in session:
        rsp = make_response("{\"status\": \"failed\", \"message\": \"Login required\"}")
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

    try:
        data = request.get_json()
        if not data or 'url' not in data:
            rsp = make_response("{\"status\": \"failed\", \"message\": \"URL required\"}")
            rsp.headers['Content-Type'] = 'application/json'
            return rsp

        download_url = data['url'].strip()
        if not download_url:
            rsp = make_response("{\"status\": \"failed\", \"message\": \"URL cannot be empty\"}")
            rsp.headers['Content-Type'] = 'application/json'
            return rsp

        # Validate URL format
        try:
            from urllib.parse import urlparse
            parsed = urlparse(download_url)
            if not parsed.scheme or not parsed.netloc:
                rsp = make_response("{\"status\": \"failed\", \"message\": \"Invalid URL format\"}")
                rsp.headers['Content-Type'] = 'application/json'
                return rsp
        except Exception:
            rsp = make_response("{\"status\": \"failed\", \"message\": \"Invalid URL format\"}")
            rsp.headers['Content-Type'] = 'application/json'
            return rsp

        # Only this firmware's package format (plus archives of it).
        if not pkg_format.is_accepted(parsed.path):
            return jsonify({"status": "failed",
                            "message": _unsupported_package_message(parsed.path)})
        # Certificate checks are off unless the user ticks "verify"; see
        # pkg_format.http_get().
        verify_tls = bool(data.get("verify_tls"))

        # Extract filename from URL
        import os
        url_path = parsed.path
        filename = os.path.basename(url_path)
        if not filename:
            # If no filename in URL, create one based on URL
            filename = f"downloaded_package_{int(time.time())}{pkg_format.extension()}"

        # Download the file
        import requests
        import tempfile
        
        # Use same directory as custom IPK uploads
        current_working_path = pathlib.Path(__file__).parent.resolve()
        upload_dir = current_working_path.joinpath("temp")
        try:
            pathlib.Path(upload_dir).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            rsp = make_response(jsonify({"status": "failed", "message": f"Failed to create temp directory: {str(e)}"}))
            rsp.headers['Content-Type'] = 'application/json'
            return rsp
            
        local_path = upload_dir.joinpath(filename)
        
        # Download with timeout and size limits
        response = pkg_format.http_get(download_url, verify_tls=verify_tls,
                                       stream=True, timeout=30)
        response.raise_for_status()
        
        # Get filename from Content-Disposition header if available
        content_disposition = response.headers.get('content-disposition', '')
        if content_disposition:
            import re
            matches = re.findall("filename=([^;]+)", content_disposition)
            if matches:
                server_filename = matches[-1].strip('\'"')
                server_filename = os.path.basename(server_filename)
                if server_filename and pkg_format.is_accepted(server_filename):
                    filename = server_filename

        # Check content length if available
        content_length = response.headers.get('content-length')
        if content_length and int(content_length) > 500 * 1024 * 1024:  # 500MB limit
            rsp = make_response("{\"status\": \"failed\", \"message\": \"File too large (max 500MB)\"}")
            rsp.headers['Content-Type'] = 'application/json'
            return rsp

        # Update local path with potentially new filename
        local_path = upload_dir.joinpath(filename)

        # Write file to disk
        total_size = 0
        with open(local_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    total_size += len(chunk)
                    # Size check during download
                    if total_size > 500 * 1024 * 1024:  # 500MB limit
                        f.close()
                        os.remove(local_path)
                        rsp = make_response("{\"status\": \"failed\", \"message\": \"File too large (max 500MB)\"}")
                        rsp.headers['Content-Type'] = 'application/json'
                        return rsp

        # For archives, check they hold packages of this firmware's format but
        # don't extract: opkg-install.py extracts and installs them all.
        final_filename = filename
        
        if pkg_format.is_archive(filename):
            try:
                import tarfile
                fmt = pkg_format.label()
                with tarfile.open(local_path, 'r:*') as tar:
                    pkg_members = [m for m in tar.getmembers()
                                   if m.isfile() and pkg_format.is_package(m.name)]
                    
                    if not pkg_members:
                        local_path.unlink(missing_ok=True)
                        return jsonify({"status": "failed",
                                        "message": f"No {fmt} files found in archive"})
                    
                    print(f"Archive contains {len(pkg_members)} {fmt} files: {[m.name for m in pkg_members[:5]]}{'...' if len(pkg_members) > 5 else ''}")
                        
            except Exception as e:
                # Clean up on error
                if local_path.exists():
                    local_path.unlink(missing_ok=True)
                rsp = make_response(jsonify({"status": "failed", "message": f"Archive validation failed: {str(e)}"}))
                rsp.headers['Content-Type'] = 'application/json'
                return rsp

        # Return full path like the upload process does (pass original archive to opkg-install.py)
        final_path = upload_dir / final_filename
        rsp = make_response(jsonify({"status": "ok", "filename": str(final_path), "size": total_size}))
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

    except requests.exceptions.RequestException as e:
        rsp = make_response(jsonify({"status": "failed", "message": f"Download failed: {str(e)}"}))
        rsp.headers['Content-Type'] = 'application/json'
        return rsp
    except Exception as e:
        rsp = make_response(jsonify({"status": "failed", "message": f"Error: {str(e)}"}))
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

@flask_app.route("/system/reboot", methods=['POST'])
@api_auth_required
def system_reboot():
    if 'username' not in session:
        rsp = make_response(json.dumps({"status": "error", "message": "Authentication required", "redirect": f"/login?next={request.path}"})) 
        rsp.headers['Content-Type'] = 'application/json'
        return rsp, 401

    pc_app.main_app.system_status = 20 # prepare_rebooting
    power_event_log.log_event("system_shutdown", "User initiated reboot")
    threading.Timer(0.1, lambda: subprocess.run(["reboot"])).start()
    rsp = make_response("{\"status\": \"ok\"}")
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/system/poweroff", methods=['POST'])
@api_auth_required
def system_poweroff():
    if 'username' not in session:
        rsp = make_response(json.dumps({"status": "error", "message": "Authentication required", "redirect": f"/login?next={request.path}"}))
        rsp.headers['Content-Type'] = 'application/json'
        return rsp, 401

    pc_app.main_app.system_status = 20 # prepare_rebooting (device goes down shortly)
    power_event_log.log_event("system_shutdown", "User initiated poweroff")
    threading.Timer(0.1, lambda: subprocess.run(["poweroff"])).start()
    rsp = make_response("{\"status\": \"ok\"}")
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

#factory reset modem, reset openwrt
@flask_app.route("/system/reset", methods=['POST'])
@api_auth_required
def system_reset():
    if 'username' not in session:
        rsp = make_response(json.dumps({"status": "error", "message": "Authentication required", "redirect": f"/login?next={request.path}"})) 
        rsp.headers['Content-Type'] = 'application/json'
        return rsp, 401

    pcat_logger.logger.info("User {} request system factory reset with SMS clear".format(request.remote_addr))
    try:
        # First clear all SMS messages from both database and modem
        pc_app.sms_client.clear_all_sms()
        pcat_logger.logger.info("SMS messages cleared before system reset")
        
        # Then perform the modem factory reset
        pc_app.modem_client.factory_reset_modem()
        pcat_logger.logger.info("Modem factory reset command sent before system reset")
    except Exception as e:
        pcat_logger.logger.error(f"Failed to clear SMS before system reset: {e}")

    reset_file = pathlib.Path("/usr/bin/pcat-factory-reset.sh")
    if not reset_file.exists():
        rsp = make_response("{\"status\": \"failed\", \"message\": \"Feature missing.\"}")
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

    threading.Timer(3.0, lambda: subprocess.run([str(reset_file)])).start()
    rsp = make_response("{\"status\": \"ok\"}")
    rsp.headers['Content-Type'] = 'application/json'
    return rsp  

@flask_app.route("/system/shutdown", methods=['POST'])
@api_auth_required
def system_shutdown():
    if 'username' not in session:
        rsp = make_response(json.dumps({"status": "error", "message": "Authentication required", "redirect": f"/login?next={request.path}"})) 
        rsp.headers['Content-Type'] = 'application/json'
        return rsp, 401

    threading.Timer(3.0, lambda: subprocess.run(["poweroff"])).start()
    rsp = make_response("{\"status\": \"ok\"}")
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

## JSON API acitons
@flask_app.route("/api/v1/status2.json")
def status2_json_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    now = datetime.now()
    dt_string = now.strftime("%Y-%m-%d %H:%M:%S")
    date_string = now.strftime("%Y-%m-%d")
    time_string = now.strftime("%H:%M")

    return {
        "firmware_version": pc_app.socket_client.stm32_firmware_version,
        "system_status": getattr(pc_app.main_app, 'system_status', 0),
        "on_charging": pc_app.socket_client.on_charging,
        "charge_percent": pc_app.socket_client.charge_percent,
        "date": date_string,
        "time": time_string,
        "carmode": pc_app.socket_client.charger_on_auto_start_mode,
        "network_route_mode": pc_app.socket_client.network_route_mode,
        "network_mode": pcat_config.get_network_mode(),
        "modem_mode": pc_app.socket_client.modem_mode or usable_cell_tech(pc_app.modem_client.basic.get("cell_tech", "")),
        "modem_signal_strength": pc_app.socket_client.modem_signal_strength,
        "isp_name": pc_app.socket_client.isp_name,
        "sim_state": pc_app.modem_client.get_inferred_sim_state(pc_app.socket_client.sim_state)
    }

@flask_app.route("/api/v1/system/board.json")
@api_auth_required
def system_board_json_action():
    rsp = make_response(_openwrt_ubus_call("system", "board"))
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/system/debug_openwrt.json")
@dev_only
@api_auth_required
def system_debug_openwrt_action():
    return jsonify(_openwrt_debug_status())

@flask_app.route("/api/v1/system/info.json")
@api_auth_required
def system_info_json_action():
    rsp = make_response(_openwrt_ubus_call("system", "info"))
    rsp.headers['Content-Type'] = 'application/json'
    return rsp


@flask_app.route("/api/v2/wireless.json", methods=['GET'])
@api_auth_required
def wireless_v2_action():
    wireless_list = {}
    mock_data = request.args.get('mock') == 'true'
    wireless_list["interfaces"] = command_parser.parse_uci_show_wireless(mock_data)
    json_str = json.dumps(wireless_list)  
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/wireless.json", methods=['POST'])
@api_auth_required
def save_wireless_action():
    if 'username' not in session:
        json_dict = {"status": "failed", "message":"Login required", "redirect": f"/login?next={request.path}"}
        rsp = make_response(json.dumps(json_dict))
        rsp.headers['Content-Type'] = 'application/json'
        return rsp
    
    json_dict = {"status": "ok"}
    p_data = request.get_json()
    pcat_logger.logger.info("Get user {} posted wireless info:{}".format(request.remote_addr, _redact_sensitive(p_data)))

    ssid = str(p_data.get("ssid", ""))
    frequency  = str(p_data.get("frequency", ""))
    encryption = str(p_data.get("encryption", ""))
    ssid_key   = str(p_data.get("password", ""))
    device     = str(p_data.get("device", ""))
    if not ssid:
        json_dict = {"status": "failed", "message": "Missing params."}
    elif device != "radio0" and device != "radio1":
        json_dict = {"status": "failed", "message": "Params invalid."}
    elif frequency not in ("2g", "5g", "5g_ax", "5g_be", "6g_be"):
        json_dict = {"status": "failed", "message": "Params invalid."}
    elif encryption not in ("none", "psk", "psk2", "sae"):
        json_dict = {"status": "failed", "message": "Params invalid."}
    elif device == wifi_wan.active_sta_radio():
        # The radio is retuned to the upstream's band/channel while relaying;
        # rewriting it here would break the upstream WiFi uplink.
        # active_sta_radio() reads the STA section's *actual* device and is
        # None unless the relay is live, so it covers the is_active() check
        # too. get_sta_radio() would be wrong here: it returns the configured
        # radio *preference*, which diverges from the radio the STA really
        # sits on (e.g. a preference change that could not move a missing
        # section), and would then guard the wrong radio -- refusing edits to
        # an idle AP while letting the live relay's radio be overwritten.
        json_dict = {"status": "failed",
                     "message": "Onboard WiFi is relaying an upstream hotspot. Disable the WiFi upstream first. / 板载WiFi正在中继上游热点，请先关闭 WiFi 上游连接。"}

    if json_dict["status"] == "ok":
        toggle_changed = False
        if device == "radio0":
            radio0_enabled = command_parser.is_wifi_interface_enabled("radio0")
            if radio0_enabled != p_data["enabled"]:
                toggle_changed = True
                pc_app.enable_wifi_interface("radio0", p_data["enabled"])
        elif device == "radio1":
            radio1_enabled = command_parser.is_wifi_interface_enabled("radio1")
            if radio1_enabled != p_data["enabled"]:
                toggle_changed = True
                pc_app.enable_wifi_interface("radio1", p_data["enabled"])

        wireless_cfg = _openwrt_uci_show_dict("wireless", max_age=0)

        # Always save WiFi configuration, regardless of whether toggle changed
        # Get current channel value before setting it
        freq_defaults = {
            "2g":    {"band": "2g", "htmode": "HT20",   "default_channel": "10"},
            "5g":    {"band": "5g", "htmode": "VHT40",  "default_channel": "149"},
            "5g_ax": {"band": "5g", "htmode": "HE80",   "default_channel": "149"},
            "5g_be": {"band": "5g", "htmode": "EHT80",  "default_channel": "149"},
            "6g_be": {"band": "6g", "htmode": "EHT320", "default_channel": "201"},
        }
        try:
            current_channel = _openwrt_uci_get("wireless.{}.channel".format(device))
            current_channel_i = int(current_channel)
        except:
            # Fallback to default channel values based on frequency
            if p_data["frequency"] == "2g":
                current_channel = "10"
            elif p_data["frequency"] in ("5g", "5g_ax", "5g_be"):
                current_channel = "149"
            elif p_data["frequency"] == "6g_be":
                current_channel = "201"
            else:
                current_channel = "149"
            current_channel_i = int(current_channel)
            pcat_logger.logger.warning("Failed to get current channel for {}, using default: {}".format(device, current_channel))

        fdef = freq_defaults[p_data["frequency"]]

        # Preserve current htmode only when the user is re-saving the exact same
        # frequency mode (e.g. re-saving 6g_be shouldn't downgrade EHT320 -> EHT80).
        # When the user switches modes (e.g. 5g_ax -> 5g_be), always apply the new htmode.
        current_band = _openwrt_uci_get("wireless.{}.band".format(device))
        current_htmode = _openwrt_uci_get("wireless.{}.htmode".format(device))
        current_freq = command_parser._frequency_from_band_htmode(current_band, current_htmode) if current_htmode else ""
        if current_freq == p_data["frequency"] and current_htmode:
            target_htmode = current_htmode
        else:
            target_htmode = fdef["htmode"]

        # Reuse the user's current channel only if it's plausible for this band.
        if fdef["band"] == "2g":
            if current_channel_i > 14:
                current_channel = fdef["default_channel"]
        elif fdef["band"] == "5g":
            if current_channel_i < 36 or current_channel_i > 177:
                current_channel = fdef["default_channel"]
        elif fdef["band"] == "6g":
            # 6 GHz uses odd channels 1..233 (plus ch 2); reject anything from 2.4/5 GHz.
            # Use default when switching bands or channel doesn't fit 6 GHz spacing.
            if current_band != "6g" or current_channel_i < 1 or current_channel_i > 233:
                current_channel = fdef["default_channel"]
        # 6 GHz mandates WPA3-SAE with PMF; force it regardless of user selection.
        if fdef["band"] == "6g":
            encryption = "sae"

        # Written, verified and remembered in one place (wifi_radio): the AP
        # section is resolved rather than assumed to be default_<radio>, the
        # commit is checked against /etc/config/wireless instead of being fired
        # off into a background thread nobody hears back from, and the settings
        # are kept so the boot reconcile can put them back when the firmware
        # regenerates the config.
        saved, _vif = wifi_radio.save_ap_config(
            device, wireless_cfg,
            {"band": fdef["band"], "htmode": target_htmode,
             "channel": current_channel, "country": "US"},
            {"ssid": ssid, "encryption": encryption,
             # An empty password field means "leave the passphrase alone".
             "key": ssid_key if ssid_key != "" else None,
             "hidden": "1" if p_data.get("hide_ssid") == True else ""})

        if saved:
            # The reload is what drops every client on this AP, so it stays
            # deferred: the response has to reach the browser first.
            cmdtool = CmdTool()
            cmdtool.prepare(["luci-reload"])
            cmdtool.prepare(["/sbin/wifi", "reload"])
            threading.Timer(2.0, lambda: cmdtool.start()).start()
        else:
            json_dict = {
                "status": "failed",
                "message": _locales.get_locales("settings", current_locale()).get(
                    "wifi_save_not_persisted", "Wi-Fi settings could not be saved."),
            }

    json_str = json.dumps(json_dict)  
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp  

# ── WiFi-as-WAN ("Smart WAN" 智能多网): upstream STA on the onboard radio ────

def _wifi_wan_auth_ok():
    """Session auth honoring --skip-auth and localhost, mirroring page auth."""
    if pcat_config.skip_auth or 'username' in session:
        return True
    return request.remote_addr in ('127.0.0.1', '::1', 'localhost')


@flask_app.route("/api/v1/wifi_wan/scan.json", methods=['GET'])
@api_auth_required
def wifi_wan_scan_api():
    if not _wifi_wan_auth_ok():
        return jsonify({"status": "failed", "message": "Login required"}), 401
    networks, error = wifi_wan.scan()
    if error:
        return jsonify({"status": "failed", "reason": error, "networks": []})
    saved = {n.get("ssid") for n in pcat_config.get_wifi_wan_networks()}
    for net in networks:
        net["saved"] = net["ssid"] in saved
    return jsonify({"status": "ok", "networks": networks})


@flask_app.route("/api/v1/wifi_wan.json", methods=['GET'])
@api_auth_required
def wifi_wan_status_api():
    if not _wifi_wan_auth_ok():
        return jsonify({"status": "failed", "message": "Login required"}), 401
    # Saved networks are exposed without their secrets (key/password/identity)
    saved_networks = [{"ssid": n.get("ssid", ""),
                       "encryption": n.get("encryption", "none"),
                       "band": n.get("band", ""),
                       "random_mac": bool(n.get("random_mac")),
                       "custom_mac": n.get("custom_mac", ""),
                       "has_login_cmd": bool(n.get("login_cmd"))}
                      for n in pcat_config.get_wifi_wan_networks()]
    return jsonify({
        "status": "ok",
        "available": wifi_wan.is_available(),
        "eap_supported": wifi_wan.eap_supported(),
        "state": wifi_wan.get_state(),
        "saved_networks": saved_networks,
        "network_mode": pcat_config.get_network_mode(),
    })


@flask_app.route("/api/v1/wifi_wan.json", methods=['POST'])
@api_auth_required
def wifi_wan_save_api():
    if not _wifi_wan_auth_ok():
        return jsonify({"status": "failed", "message": "Login required"}), 401
    if not wifi_wan.is_available():
        return jsonify({"status": "failed",
                        "message": "Smart WAN requires two WiFi radios (onboard + PCIe). / 智能多网需要两个 WiFi（板载 + PCIe）。"})
    p_data = request.get_json() or {}
    ssid = str(p_data.get("ssid", "")).strip()
    encryption = str(p_data.get("encryption", "none"))
    password = str(p_data.get("password", ""))
    band = str(p_data.get("band", ""))
    channel = p_data.get("channel")
    use_saved = bool(p_data.get("use_saved"))
    random_mac = bool(p_data.get("random_mac"))
    custom_mac = str(p_data.get("custom_mac", "")).strip()
    login_cmd = str(p_data.get("login_cmd", ""))
    # EAP (802.1x) params; only used when encryption == "eap".
    eap = {
        "type": str(p_data.get("eap_type", "peap")),
        "identity": str(p_data.get("identity", "")),
        "anonymous_identity": str(p_data.get("anonymous_identity", "")),
        "ca_cert": str(p_data.get("ca_cert", "")),
    }

    if use_saved:
        saved = next((n for n in pcat_config.get_wifi_wan_networks()
                      if n.get("ssid") == ssid), None)
        if saved is None:
            return jsonify({"status": "failed", "message": "Unknown saved network."})
        encryption = saved.get("encryption", "none")
        password = saved.get("key", "")
        band = band or saved.get("band", "")
        random_mac = bool(saved.get("random_mac"))
        custom_mac = saved.get("custom_mac", "")
        login_cmd = saved.get("login_cmd", "")
        eap = {
            "type": saved.get("eap_type", "peap"),
            "identity": saved.get("identity", ""),
            "anonymous_identity": saved.get("anonymous_identity", ""),
            "ca_cert": saved.get("ca_cert", ""),
        }

    if not ssid or len(ssid.encode("utf-8")) > 32:
        return jsonify({"status": "failed", "message": "Invalid SSID."})
    if encryption not in wifi_wan.SUPPORTED_ENCRYPTIONS:
        return jsonify({"status": "failed", "message": "Unsupported encryption."})
    if encryption == "eap":
        if not wifi_wan.eap_supported():
            return jsonify({"status": "failed",
                            "message": "Enterprise (802.1x) WiFi is not supported by this firmware (needs full wpad). / 本固件不支持企业级 (802.1x) WiFi（需要完整 wpad）。"})
        if eap["type"] not in wifi_wan.EAP_TYPES:
            return jsonify({"status": "failed", "message": "Invalid EAP type."})
        if not eap["identity"]:
            return jsonify({"status": "failed", "message": "EAP identity (username) is required."})
        if eap["type"] in ("peap", "ttls") and not password:
            return jsonify({"status": "failed", "message": "EAP password is required."})
        if len(login_cmd) > 1024:
            return jsonify({"status": "failed", "message": "Login command too long."})
    elif encryption != "none":
        if not (8 <= len(password) <= 63):
            return jsonify({"status": "failed", "message": "Password must be 8-63 characters."})
    if len(login_cmd) > 1024:
        return jsonify({"status": "failed", "message": "Login command too long."})
    if band not in ("", "2g", "5g"):
        return jsonify({"status": "failed", "message": "Invalid band."})
    if custom_mac:
        custom_mac = wifi_wan.valid_custom_mac(custom_mac)
        if not custom_mac:
            return jsonify({"status": "failed",
                            "message": "Invalid MAC address (want aa:bb:cc:dd:ee:ff, unicast). / MAC 地址无效（格式 aa:bb:cc:dd:ee:ff，且须为单播地址）。"})
    try:
        channel = int(channel) if channel else None
    except (TypeError, ValueError):
        channel = None

    pcat_logger.logger.info(
        "User {} joining Smart WAN upstream '{}' ({} {}, random_mac={}, custom_mac={})".format(
            request.remote_addr, ssid, encryption, band or "auto", random_mac,
            custom_mac or "no"))

    eap_arg = eap if encryption == "eap" else None
    wifi_wan.apply_config(ssid, password, encryption, band, channel,
                          random_mac=random_mac, custom_mac=custom_mac,
                          eap=eap_arg)
    pcat_config.remember_wifi_wan_network(ssid, encryption, password, band,
                                          channel or 0, random_mac=random_mac,
                                          custom_mac=custom_mac,
                                          login_cmd=login_cmd, eap=eap_arg)

    # Joining an upstream enables the wifi slot. Priority mode is the new
    # Smart WAN: a device not already in priority mode is moved into it (the
    # table's route metrics keep eth + cellular as fallbacks), rather than the
    # retired 'smart_wan' mode.
    if pcat_config.get_network_mode() == "priority":
        try:
            wan_manager.set_slot_enabled("wifi", True)
        except Exception as e:
            pcat_logger.logger.warning("WAN priority: wifi slot enable failed: {}".format(e))
    else:
        try:
            priority = wan_manager.get_priority()
            for entry in priority:
                if entry["id"] == "wifi":
                    entry["enabled"] = True
            wan_manager.apply(priority)
        except Exception as e:
            pcat_logger.logger.warning("WAN priority: switch on wifi join failed: {}".format(e))

    return jsonify({"status": "ok"})


@flask_app.route("/api/v1/wifi_wan/forget.json", methods=['POST'])
@api_auth_required
def wifi_wan_forget_api():
    if not _wifi_wan_auth_ok():
        return jsonify({"status": "failed", "message": "Login required"}), 401
    p_data = request.get_json() or {}
    ssid = str(p_data.get("ssid", "")).strip()
    if not ssid:
        return jsonify({"status": "failed", "message": "Missing SSID."})
    pcat_logger.logger.info("User {} forgetting Smart WAN network '{}'".format(
        request.remote_addr, ssid))
    pcat_config.forget_wifi_wan_network(ssid)
    if wifi_wan.is_configured() and wifi_wan.get_state().get("ssid") == ssid:
        wifi_wan.remove_sta()
    return jsonify({"status": "ok"})


# ── WAN priority table (metric-based multi-WAN; replaces modes + mwan3) ──────

# get_active_egress() device classes -> priority slot ids
_EGRESS_TO_SLOT = {"wan": "wan", "wifi": "wifi", "mobile": "cellular"}


@flask_app.route("/api/v1/wan_priority.json", methods=['GET'])
@api_auth_required
def wan_priority_status_api():
    if not _wifi_wan_auth_ok():
        return jsonify({"status": "failed", "message": "Login required"}), 401
    data = wan_manager.status()
    data["status"] = "ok"
    data["active_slot"] = _EGRESS_TO_SLOT.get(pc_app.get_active_egress(), "")
    return jsonify(data)


@flask_app.route("/api/v1/wan_priority.json", methods=['POST'])
@api_auth_required
def wan_priority_save_api():
    if not _wifi_wan_auth_ok():
        return jsonify({"status": "failed", "message": "Login required"}), 401
    p_data = request.get_json() or {}
    priority = p_data.get("priority")
    wifi_radio = p_data.get("wifi_radio")

    if not isinstance(priority, list) or not priority:
        return jsonify({"status": "failed", "message": "Missing priority list."})
    ids = [(item or {}).get("id") for item in priority]
    if len(set(ids)) != len(ids) or any(i not in wan_manager.SLOTS for i in ids):
        return jsonify({"status": "failed", "message": "Invalid priority list."})
    if wifi_radio is not None and wifi_radio not in ("onboard", "pcie"):
        return jsonify({"status": "failed", "message": "Invalid wifi radio."})

    pcat_logger.logger.info("User {} saving WAN priority: {} (wifi_radio={})".format(
        request.remote_addr,
        " > ".join("{}{}".format(i, "" if (item or {}).get("enabled") else "(off)")
                   for i, item in zip(ids, priority)),
        wifi_radio or "unchanged"))

    try:
        wan_manager.apply(priority, wifi_radio)
    except Exception as e:
        pcat_logger.logger.error("WAN priority apply failed: {}".format(e))
        return jsonify({"status": "failed",
                        "message": "Failed to apply priority. / 应用优先级失败。"})
    # The daemon must not down modem interfaces behind the table's back.
    try:
        pc_app.socket_client.set_auto_wan(0)
    except Exception as e:
        pcat_logger.logger.warning("WAN priority: modem auto-stop sync failed: {}".format(e))

    data = wan_manager.status()
    data["status"] = "ok"
    data["active_slot"] = _EGRESS_TO_SLOT.get(pc_app.get_active_egress(), "")
    return jsonify(data)


@flask_app.route("/api/v1/wan_portal_login.json", methods=['POST'])
@api_auth_required
def wan_portal_login_api():
    """Open (or cancel) a captive-portal login window on one priority slot.

    A portal-locked uplink is demoted, which is what makes its login page
    unreachable: every request leaves via the working uplink. This holds the
    slot up as the primary uplink for a bounded time so the user can sign in;
    see wan_manager.start_portal_login for how it ends itself."""
    if not _wifi_wan_auth_ok():
        return jsonify({"status": "failed", "message": "Login required"}), 401
    p_data = request.get_json() or {}
    action = str(p_data.get("action", "start")).strip()
    slot = str(p_data.get("slot", "")).strip()

    if action == "cancel":
        pcat_logger.logger.info("User {} cancelling portal login window".format(
            request.remote_addr))
        wan_manager.end_portal_login("cancelled by user")
        return jsonify({"status": "ok",
                        "portal_login": wan_manager.portal_login_state()})

    pcat_logger.logger.info("User {} starting portal login window on {}".format(
        request.remote_addr, slot or "?"))
    try:
        state = wan_manager.start_portal_login(slot, p_data.get("duration"))
    except ValueError as e:
        return jsonify({"status": "failed", "message": str(e)})
    except Exception as e:
        pcat_logger.logger.error("Portal login window failed: {}".format(e))
        return jsonify({"status": "failed",
                        "message": "Failed to start the login window. / 无法开始登录。"})
    return jsonify({"status": "ok", "portal_login": state})


# ── Modem log viewer (daemon's in-memory dial log, chrome-devtools style) ────

@flask_app.route("/modem_log")
def modem_log_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")
    c_locale = current_locale()
    locales = _locales.get_locales("modem_log", c_locale)
    locales.update(base_locales())
    return render_template(nt.modem_log_file_name, locales=locales)


@flask_app.route("/api/v1/modem/log.json", methods=['GET'])
@api_auth_required
def modem_log_api():
    """Chunked reads of the pcat-manager daemon's in-memory modem dial log
    (fm350-mm / quectel-cm output, up to 16MB retained). Absolute offsets:
    pass none for the tail, then poll with offset=next_offset for increments."""
    if not _wifi_wan_auth_ok():
        return jsonify({"status": "failed", "message": "Login required"}), 401
    offset = request.args.get("offset", None, type=int)
    max_size = request.args.get("max_size", 65536, type=int)
    max_size = min(max(max_size, 4096), 262144)

    sc = pc_app.socket_client
    seq = sc.modem_log_seq
    sc.modem_log_get(offset, max_size)
    for _ in range(40):  # wait up to ~2s for the daemon reply
        if sc.modem_log_seq != seq:
            break
        time.sleep(0.05)
    resp = sc.modem_log_response if sc.modem_log_seq != seq else None
    if not resp or resp.get("code") != 0:
        return jsonify({"status": "failed",
                        "message": "Modem log not available (pcat-manager too old?) / 守护进程不支持调制解调器日志。"})

    dev_paths = sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*")) \
        + sorted(glob.glob("/dev/wwan*"))
    netdevs = [d for d in ("eth2", "usb0", "wwan0")
               if os.path.exists("/sys/class/net/" + d)]
    return jsonify({
        "status": "ok",
        "log": resp.get("log", ""),
        "offset": resp.get("offset", 0),
        "next_offset": resp.get("next-offset", 0),
        "total_offset": resp.get("total-offset", 0),
        "tail_offset": resp.get("tail-offset", 0),
        # Source descriptor: one entry today (the cellular modem); shaped so
        # more sources (GPS, MCU, hostapd...) can join later dev-tools style.
        "source": {
            "id": "modem",
            "exec": resp.get("exec", ""),
            "usb_id": resp.get("usb-id", ""),
            "dev_paths": dev_paths,
            "netdevs": netdevs,
        },
    })


# ── SOC gauge diagnostic log (MCU RAM ring, drained by the daemon) ───────────

# Event codes mirror SOC_LOG_EVT_* in the MCU firmware
# (photonicat_v2/freertos_ek_ra2e1_ep/src/soc_log.h). Anything not listed here
# renders as its raw number, so a newer MCU adding events degrades readably.
_SOC_LOG_EVENTS = {
    0:  "periodic",      1:  "mcu-boot",       2:  "rk-on",
    3:  "rk-off",        6:  "full-anchor",    7:  "low-voltage-anchor",
    8:  "empty-learn",   9:  "ocv-calibrate", 10:  "gross-mismatch-fix",
    11: "nv-restore",   12:  "sleep-discharge", 13: "boot-ocv-reseed",
    14: "gauge-init",
}
# Events that move SoC are written as a before/after pair; these are the ones
# worth highlighting in the UI because they are what makes SoC jump.
_SOC_LOG_ANCHOR_EVENTS = {6, 7, 8, 9, 10, 11, 12, 13, 14}


@flask_app.route("/api/v1/soc_log.json", methods=['GET'])
@api_auth_required
def soc_log_api():
    """SOC gauge diagnostic records from the pcat-manager daemon.

    The MCU keeps a 32-slot RAM ring covering the window nothing else can see
    (RK powered down, MCU awake or in standby), writing a before/after pair at
    every anchor that moves SoC. The daemon drains it over com=173 and stitches
    it into a longer RAM history. Poll with from_seq=next_seq for increments."""
    if not _wifi_wan_auth_ok():
        return jsonify({"status": "failed", "message": "Login required"}), 401
    from_seq = request.args.get("from_seq", None, type=int)
    max_records = request.args.get("max_records", 256, type=int)
    max_records = min(max(max_records, 1), 1024)

    sc = pc_app.socket_client
    # Guards the mock socket client used by app/test, not an old daemon: this
    # only sees our own Python object, so it says nothing about what
    # pcat-manager on the other end of the socket supports.
    if not hasattr(sc, "soc_log_get"):
        return jsonify({"status": "failed",
                        "message": "SOC log not available: this socket client has no soc_log_get. / 当前 socket 客户端不支持 soc_log_get。"})
    seq = sc.soc_log_seq
    sc.soc_log_get(from_seq, max_records)
    for _ in range(40):  # wait up to ~2s for the daemon reply
        if sc.soc_log_seq != seq:
            break
        time.sleep(0.05)
    resp = sc.soc_log_response if sc.soc_log_seq != seq else None

    # Three different failures used to share one message that blamed the MCU,
    # which sent people to reflash firmware that was already new enough. Keep
    # them apart:
    #   no reply      the daemon does not implement soc-log-get and dropped an
    #                 unknown command silently (pcat-manager before the
    #                 soc-log-get commit). This is the common case.
    #   supported 0   the daemon asked, the MCU never answered -> MCU firmware
    #                 really is older than RA2E1260727000.
    #   code != 0     the daemon tried and failed.
    if not resp:
        return jsonify({"status": "failed",
                        "message": "SOC log not available: pcat-manager did not answer soc-log-get, so it is too old for this feature. Update pcat-manager. / pcat-manager 版本过旧，不支持 soc-log-get，请升级 pcat-manager。"})
    if resp.get("code") != 0:
        return jsonify({"status": "failed",
                        "message": "SOC log request failed in pcat-manager (code %s). / pcat-manager 读取 SOC 日志失败（代码 %s）。" % (
                            resp.get("code"), resp.get("code"))})
    if not resp.get("supported"):
        return jsonify({"status": "failed",
                        "message": "SOC log not available: this MCU firmware does not keep one (needs RA2E1260727000 or newer). / MCU 固件过旧，无 SOC 诊断日志（需 RA2E1260727000 及以上）。"})

    records = []
    for r in resp.get("records", []):
        event = r.get("event", 0)
        flags = r.get("flags", 0)
        records.append({
            "seq": r.get("seq"),
            # MCU uptime seconds (awake + standby), not wall clock: the MCU has
            # no calendar the RK can trust across its own power cycles.
            "t": r.get("t", 0),
            "v_mv": r.get("v", 0),
            "i_ma": r.get("i", 0),
            "vbus_mv": r.get("vbus", 0),
            "soc_raw": round(r.get("soc-raw-x10", 0) / 10.0, 1),
            "soc_disp": r.get("soc-disp", 0),
            "temp_c": r.get("temp", 0),
            "event": event,
            "event_name": _SOC_LOG_EVENTS.get(event, "evt-%d" % event),
            "pre": bool(r.get("pre")),
            "anchor": event in _SOC_LOG_ANCHOR_EVENTS,
            "charger": bool(flags & 0x01),
            "trusted": bool(flags & 0x02),
            "rk_on": bool(flags & 0x04),
        })

    seq_next = resp.get("seq-next", 0)
    return jsonify({
        "status": "ok",
        "records": records,
        "seq_oldest": resp.get("seq-oldest", 0),
        "seq_next": seq_next,
        # Cursor for the next incremental poll: one past the last record we got,
        # or seq_next when this reply was empty.
        "next_seq": (records[-1]["seq"] + 1) if records else seq_next,
        "log_version": resp.get("log-version", 0),
    })


# ── Captive portal gateway (OpenNDS) ─────────────────────────────────────────

@flask_app.route("/api/v1/captive_portal.json", methods=['GET'])
@api_auth_required
def captive_portal_status_api():
    if not _wifi_wan_auth_ok():
        return jsonify({"status": "failed", "message": "Login required"}), 401
    return jsonify({"status": "ok", "state": captive.get_state()})


@flask_app.route("/api/v1/captive_portal.json", methods=['POST'])
@api_auth_required
def captive_portal_set_api():
    if not _wifi_wan_auth_ok():
        return jsonify({"status": "failed", "message": "Login required"}), 401
    if not captive.is_installed():
        return jsonify({"status": "failed",
                        "message": "Captive portal is not available on this firmware. / 本固件不支持强制门户。"})
    p_data = request.get_json() or {}
    enabled = bool(p_data.get("enabled"))
    pcat_logger.logger.info("User {} {} captive portal".format(
        request.remote_addr, "enabling" if enabled else "disabling"))
    state, ok = captive.set_enabled(enabled)
    if not ok:
        return jsonify({
            "status": "failed",
            "message": "Failed to {} captive portal. / 强制门户{}失败。".format(
                "enable" if enabled else "disable", "启用" if enabled else "禁用"),
            "state": state,
        })
    return jsonify({"status": "ok", "state": state})


_wifi_clients_cache = {'data': None, 'timestamp': 0}
_WIFI_CLIENTS_CACHE_DURATION = 60  # seconds

@flask_app.route("/api/v1/theme_accent.json", methods=['GET'])
@api_auth_required
def api_theme_accent_get():
    return jsonify({"status": "ok", "color": theme_accent()})


@flask_app.route("/api/v1/theme_accent.json", methods=['POST'])
@api_auth_required
def api_theme_accent_post():
    body = request.get_json() or {}
    color = (body.get("color") or "").strip()
    if not HEX_COLOR_RE.match(color):
        return jsonify({"status": "failed", "message": "bad color"}), 400
    pcat_config.set_user_config_key(THEME_ACCENT_KEY, color.upper())
    return jsonify({"status": "ok", "color": color.upper()})


@flask_app.route("/api/v1/wifi_clients.json", methods=['GET'])
@api_auth_required
def wifi_clients_json_action():
    current_time = time.time()
    if (_wifi_clients_cache['data'] is not None and
        current_time - _wifi_clients_cache['timestamp'] < _WIFI_CLIENTS_CACHE_DURATION):
        rsp = make_response(_wifi_clients_cache['data'])
    else:
        response_json = pc_app.main_app.get_clients()
        _wifi_clients_cache['data'] = response_json
        _wifi_clients_cache['timestamp'] = current_time
        rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

# /stats device table: every DHCP lease plus the Wi-Fi stations, one row per
# MAC (see pcat_util/lan_clients.py). Cached briefly: the page polls every 5s
# and the lease file + hostapd ubus calls are cheap but not free.
_lan_clients_cache = {'data': None, 'timestamp': 0}
_LAN_CLIENTS_CACHE_DURATION = 4  # seconds

@flask_app.route("/api/v1/lan_clients.json", methods=['GET'])
@api_auth_required
def lan_clients_json_action():
    now = time.time()
    if (_lan_clients_cache['data'] is None or
            now - _lan_clients_cache['timestamp'] >= _LAN_CLIENTS_CACHE_DURATION):
        _lan_clients_cache['data'] = pc_app.main_app.get_lan_clients()
        _lan_clients_cache['timestamp'] = now
    rsp = make_response(_lan_clients_cache['data'])
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

# Live throughput for /stats: cumulative egress byte counters, sampled by the
# browser once a second, which computes the rates itself and keeps the last
# minute in memory. Deliberately no server-side history and no cache: a 1s
# poll wants fresh counters, and the read is a handful of sysfs files.
@flask_app.route("/api/v1/net_rate.json", methods=['GET'])
@api_auth_required
def net_rate_json_action():
    rsp = make_response(pc_app.main_app.get_egress_counters())
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

# System badges on /stats (CPU, memory, disks, TCP connections). Cheap
# /proc reads; a short cache still collapses several open tabs into one read.
_sys_stats_cache = {'data': None, 'timestamp': 0}
_SYS_STATS_CACHE_DURATION = 2  # seconds

@flask_app.route("/api/v1/sys_stats.json", methods=['GET'])
@api_auth_required
def sys_stats_json_action():
    now = time.time()
    if (_sys_stats_cache['data'] is None or
            now - _sys_stats_cache['timestamp'] >= _SYS_STATS_CACHE_DURATION):
        _sys_stats_cache['data'] = pc_app.main_app.get_sys_stats()
        _sys_stats_cache['timestamp'] = now
    rsp = make_response(_sys_stats_cache['data'])
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/time.json", methods=['GET'])
@api_auth_required
def get_time_json_action():
    response_json = {}

    now = datetime.now()
    dt_string = now.strftime("%Y-%m-%d %H:%M:%S")
    date_string = now.strftime("%Y-%m-%d")
    time_string = now.strftime("%H:%M:%S")

    response_json["date_time"] = dt_string
    response_json["date"] = date_string
    response_json["time"] = time_string
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

#  ubus -v list
#  ubus call system board
#  ubus call system info
#  ubus call system reboot

@flask_app.route("/api/v1/timers.json", methods=['GET'])
@api_auth_required
def get_timers_json_action():
    list_data = pc_app.socket_client.schedule_power_events()
    rsp = make_response(list_data)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

# [
#    {
#        "id": 1699865475068,
#        "time": "00:00",
#        "action": "startup",
#        "days": [
#            "1"
#        ]
#    }
# ]
##
# [
#     {
#         "id": 1699868645442,
#         "time": "06:00",
#         "action": "startup",
#         "days": [
#             "6",
#             "2"
#         ]
#     }
# ]
#
#[
#     {
#         "id": 1699868645442,
#         "time": "06:00",
#         "action": "startup",
#         "days": [
#             "6",
#             "2"
#         ]
#     },
#     {
#         "id": 1699868712698,
#         "time": "05:00",
#         "action": "shutdown",
#         "days": [
#             "0"
#         ]
#     }
# ]
# Cancel all timers with POST /api/v1/timers.json with payload []
@flask_app.route("/api/v1/timers.json", methods=['POST'])
@api_auth_required
def save_timers_json_action():
    json_data = request.get_json()
    # [{'hour': 0, 'minute': 0, 'actiont': 'shutdown', 'days': [3, 5]}, 
    #  {'hour': 5, 'minute': 0, 'actiont': 'startup', 'days': [1, 2, 3, 4, 5, 6, 7]}] <class 'list'>
    pcat_logger.logger.info("Get user {} posted timer info:{}".format(request.remote_addr, _redact_sensitive(json_data)))

    data_valid = True
    event_list = []
    for job_j in json_data:
        time_arr = job_j.get("time", "").split(":")
        if len(time_arr) != 2:
            data_valid = False
            break
        hour = int(time_arr[0])
        minute = int(time_arr[1])
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            data_valid = False
            break
        actiontype = job_j.get("action", "shutdown")
        days = job_j.get("days", [])
        if len(days) == 0:
            data_valid = False
            break
        days = [int(day) for day in days]

        action = 0 if actiontype == "shutdown" else 1 # startup
        enabled = 1
        enable_bits = 0 # See define in PCatManagerTimeEnableBits in pcat-manager-controller project
        enable_bits = enable_bits | (1 << 3) # hour
        enable_bits = enable_bits | (1 << 4) # minute
        enable_bits = enable_bits | (1 << 5) # Day of week
        dow_bits = 0 # bit 0 -> weekend, bit 1 -> Monday
        if 0 in days:
            dow_bits = dow_bits | (1 << 0)
        if 1 in days:
            dow_bits = dow_bits | (1 << 1)
        if 2 in days:
            dow_bits = dow_bits | (1 << 2)
        if 3 in days:
            dow_bits = dow_bits | (1 << 3)
        if 4 in days:
            dow_bits = dow_bits | (1 << 4)
        if 5 in days:
            dow_bits = dow_bits | (1 << 5)
        if 6 in days:
            dow_bits = dow_bits | (1 << 6)
        event_job = {"action":action, "enabled":enabled,"enable-bits":enable_bits,"hour":hour,"minute":minute,"dow-bits":dow_bits}
        event_list.append(event_job)
    if data_valid:
        pc_app.socket_client.set_schedule_power_events(event_list)
        pc_app.socket_client.set_charger_on_auto_start(False, 0)
        pc_app.socket_client.set_power_on_mode(0)
        pcat_config.set_power_mode("timer")
        power_event_log.log_event("power_mode_changed", f"Timer mode set with {len(event_list)} events", extra={
            "mode": "timer_mode", "event_count": len(event_list),
        })

    response_json = {}
    response_json["status"] = "ok" if data_valid else "failed"
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/carbootmode.json", methods=['POST'])
@api_auth_required
def save_carboot_mode_action():
    json_data = request.get_json()
    state = json_data.get("carmode") == 'true'
    delay_timeout = int(json_data.get("timeout", 30))
    if delay_timeout < 0:
        delay_timeout = 0
    pc_app.socket_client.set_charger_on_auto_start(state, delay_timeout)
    pc_app.socket_client.set_schedule_power_events([])
    pc_app.socket_client.set_power_on_mode(0)

    # Persist power mode to user config
    pcat_config.set_power_mode("car" if state else "manual")

    if state:
        power_event_log.log_event("power_mode_changed", "Car/UPS mode enabled", extra={
            "mode": "car_mode", "timeout_seconds": delay_timeout,
        })
    else:
        power_event_log.log_event("power_mode_changed", "Manual mode set", extra={"mode": "manual_mode"})

    response_json = {}
    response_json["status"] = "ok"
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/forcepowermode.json", methods=['POST'])
@api_auth_required
def save_force_power_mode_action():
    if not is_photonicat2:
        rsp = make_response({
            "status": "error",
            "message": "Direct power mode is only supported on Photonicat 2",
        }, 400)
        rsp.headers['Content-Type'] = 'application/json'
        return rsp
    json_data = request.get_json()
    state = json_data.get("forcemode") == 'true'

    # Set power-on-mode to 2 (force power mode)
    # This mode enables auto power-on when external power connects
    # and instant power-off when disconnected (ignoring battery checks)
    pc_app.socket_client.set_power_on_mode(2 if state else 0)

    # Clear any timer-based power events and charger auto-start
    pc_app.socket_client.set_schedule_power_events([])
    pc_app.socket_client.set_charger_on_auto_start(False, 0)

    # Persist power mode to user config
    pcat_config.set_power_mode("direct_power" if state else "manual")

    power_event_log.log_event("power_mode_changed", "Direct power mode " + ("enabled" if state else "disabled"), extra={
        "mode": "force_power_mode" if state else "manual_mode",
    })

    response_json = {}
    response_json["status"] = "ok"
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/idle_shutdown.json", methods=['GET'])
@api_auth_required
def get_idle_shutdown_settings():
    """Get idle shutdown configuration"""
    enabled = pcat_config.get_user_config_key("idle_shutdown_enabled", False)
    timeout = pcat_config.get_user_config_key("idle_shutdown_timeout", 10)
    battery_only = pcat_config.get_user_config_key("idle_shutdown_battery_only", False)

    response_json = {
        "enabled": enabled,
        "timeout": timeout,
        "battery_only": battery_only
    }
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/idle_shutdown.json", methods=['POST'])
@api_auth_required
def save_idle_shutdown_settings():
    """Save idle shutdown configuration"""
    json_data = request.get_json()
    enabled = json_data.get("enabled", False)
    timeout = int(json_data.get("timeout", 10))
    battery_only = json_data.get("battery_only", False)

    # Validate timeout range
    if timeout < 1 or timeout > 120:
        response_json = {"status": "failed", "message": "Timeout must be between 1 and 120 minutes"}
        rsp = make_response(response_json)
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

    # Save to config
    pcat_config.set_user_config_key("idle_shutdown_enabled", enabled)
    pcat_config.set_user_config_key("idle_shutdown_timeout", timeout)
    pcat_config.set_user_config_key("idle_shutdown_battery_only", battery_only)

    # Reset the idle monitoring state in the app
    if pc_app.main_app and hasattr(pc_app.main_app, 'reset_idle_monitoring'):
        pc_app.main_app.reset_idle_monitoring()

    power_event_log.log_event("settings_changed", f"Idle shutdown {'enabled' if enabled else 'disabled'}, timeout={timeout}min, battery_only={battery_only}", extra={
        "setting": "idle_shutdown", "enabled": enabled, "timeout_minutes": timeout, "battery_only": battery_only,
    })

    response_json = {"status": "ok"}
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/low_battery_shutdown.json", methods=['GET'])
@api_auth_required
def get_low_battery_shutdown_settings():
    """Get low battery shutdown configuration"""
    enabled = pcat_config.get_user_config_key("low_battery_shutdown_enabled", False)
    threshold = pcat_config.get_user_config_key("low_battery_threshold", 20)

    response_json = {
        "enabled": enabled,
        "threshold": threshold
    }
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/low_battery_shutdown.json", methods=['POST'])
@api_auth_required
def save_low_battery_shutdown_settings():
    """Save low battery shutdown configuration"""
    json_data = request.get_json()
    enabled = json_data.get("enabled", False)
    threshold = int(json_data.get("threshold", 20))

    # Validate threshold range
    if threshold < 5 or threshold > 95:
        response_json = {"status": "failed", "message": "Threshold must be between 5 and 95 percent"}
        rsp = make_response(response_json)
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

    # Save to config
    pcat_config.set_user_config_key("low_battery_shutdown_enabled", enabled)
    pcat_config.set_user_config_key("low_battery_threshold", threshold)

    # Reset the low battery monitoring state in the app
    if pc_app.main_app and hasattr(pc_app.main_app, 'reset_low_battery_monitoring'):
        pc_app.main_app.reset_low_battery_monitoring()

    power_event_log.log_event("settings_changed", f"Low battery shutdown {'enabled' if enabled else 'disabled'}, threshold={threshold}%", extra={
        "setting": "low_battery_shutdown", "enabled": enabled, "threshold": threshold,
    })

    response_json = {"status": "ok"}
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/power_events.json", methods=['GET'])
@api_auth_required
def get_power_events():
    """Get power event log entries"""
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    limit = min(limit, 200)

    events = power_event_log.get_events(limit=limit, offset=offset)
    total = power_event_log.get_event_count()

    response_json = {
        "events": events,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/power_events.json", methods=['DELETE'])
@api_auth_required
def clear_power_events():
    """Clear all power event log entries"""
    power_event_log.clear_events()
    response_json = {"status": "ok"}
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/battery_history.json", methods=['GET'])
@api_auth_required
def get_battery_history():
    """Get battery history data for charting"""
    hours = request.args.get('hours', 24, type=int)
    hours = min(hours, 720)  # Max 30 days
    data = battery_history.get_history(hours=hours)
    if not is_photonicat2:
        # A Photonicat 1 measures neither battery current nor temperature;
        # rows recorded before the recorder learned that carry a literal 0.
        # Never hand those to the chart -- they draw as flat fake lines.
        for row in data:
            row["current"] = None
            row["temperature"] = None

    # Include power on/off events for chart markers
    now_ts = int(time.time())
    cutoff_ts = now_ts - (hours * 3600)
    all_events = power_event_log.get_events(limit=200, offset=0)
    power_events = []
    for ev in all_events:
        if ev.get("ts", 0) >= cutoff_ts and ev.get("type") in (
            "system_start", "system_shutdown",
            "idle_shutdown_triggered", "low_battery_shutdown_triggered",
            "car_mode_shutdown", "car_mode_power_on",
            "timer_shutdown", "timer_power_on",
            "dc_plugged_in", "dc_unplugged",
        ):
            power_events.append({"ts": ev["ts"], "type": ev["type"]})

    response_json = {
        "data": data,
        "count": len(data),
        "hours": hours,
        "now": now_ts,
        "power_events": power_events,
    }
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/network_interfaces.json", methods=['GET'])
@api_auth_required
def network_interfaces_api():
    response_json = get_network_interfaces_data()
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/network_interfaces.json", methods=['POST'])
@api_auth_required
def save_network_interfaces_api():
    
    json_data = request.get_json()
    pcat_logger.logger.info("Get user {} posted network interfaces toggle info:{}".format(request.remote_addr, _redact_sensitive(json_data)))

    # Handle new network_mode parameter
    network_mode = json_data.get("network_mode", "auto")
    
    # Save network mode to persistent config
    pcat_config.set_network_mode(network_mode)
    
    wan_flag = json_data.get("wan", False)
    wan_mode = json_data.get("wan_mode")
    if wan_mode in pc_app.LAN_MODES:
        pc_app.set_wan_mode(wan_mode)
    sim_flag = json_data.get("sim", False)
    lan_mode = json_data.get("lan_mode")
    if lan_mode in pc_app.LAN_MODES:
        pc_app.set_lan_mode(lan_mode, restart=False)
        lan_flag = lan_mode != "off"
    else:
        lan_flag = json_data.get("lan", False)
    onboard_wifi_flag = json_data.get("onboard_wifi", False)
    pcie_wifi_flag = json_data.get("pcie_wifi", False)
    
    pc_app.config_network_toggle(network_mode, wan_flag, sim_flag, lan_flag, onboard_wifi_flag, pcie_wifi_flag)

    # The page re-fetches a few seconds after this POST, inside the 15s cache
    # window; without this it reads the pre-toggle snapshot and the switches
    # visibly snap back even though the change applied.
    with network_interfaces_cache['lock']:
        network_interfaces_cache['timestamp'] = 0

    response_json = {}
    response_json["status"] = "ok"
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/wwan_power.json", methods=['GET'])
@api_auth_required
def wwan_power_status_api():
    powered = pc_app.is_wwan_powered()
    return jsonify({"status": "ok", "wwan_powered": powered})

@flask_app.route("/api/v1/wwan_power.json", methods=['POST'])
@api_auth_required
def wwan_power_set_api():
    json_data = request.get_json()
    powered = json_data.get("powered", True)
    try:
        if powered:
            subprocess.call(["rfkill", "unblock", "wwan"], timeout=10)
        else:
            subprocess.call(["rfkill", "block", "wwan"], timeout=10)
        # Invalidate cache so modem/sms threads pick up the change immediately
        pc_app._wwan_power_cache['time'] = 0
        pcat_logger.logger.info("WWAN power set to: %s by %s", "on" if powered else "off", request.remote_addr)
        return jsonify({"status": "ok", "wwan_powered": powered})
    except Exception as e:
        pcat_logger.logger.error("Failed to set rfkill wwan: %s", e)
        return jsonify({"status": "error", "message": str(e)})

@flask_app.route("/api/v1/apn_settings.json", methods=['GET'])
@api_auth_required
def apn_settings_api():
    # pc_app.socket_client.modem_apn_get()
    modem_apn = pc_app.socket_client.modem_apn
    response_json = {}
    if not modem_apn:
        response_json["apn"] = ""
        response_json["user"] = ""
        response_json["password"] = ""
        response_json["auth"] = ""
    else:
        response_json["apn"] = modem_apn["apn"]
        response_json["user"] = modem_apn["user"]
        response_json["password"] = modem_apn["password"]
        response_json["auth"] = modem_apn["auth"]

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/apn_settings.json", methods=['POST'])
@api_auth_required
def save_apn_settings_api():
    p_data = request.get_json()
    pcat_logger.logger.info("Get user {} posted APN info:{}".format(request.remote_addr, _redact_sensitive(p_data)))

    apn_addr = str(p_data.get("apn_addr", ""))
    apn_user  = str(p_data.get("user", ""))
    apn_password = str(p_data.get("password", ""))
    apn_auth   = str(p_data.get("auth", ""))

    pc_app.socket_client.modem_apn_setup(apn_addr, apn_user, apn_password, apn_auth)  
    pc_app.socket_client.modem_apn_get()

    json_dict = {"status": "ok"}
    json_str = json.dumps(json_dict)  
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp  

@flask_app.route("/api/v1/settings.json", methods=['POST'])
@api_auth_required
def save_settings_api():
    p_data = request.get_json()
    pcat_logger.logger.info("Get user {} posted settings info:{}".format(request.remote_addr, _redact_sensitive(p_data)))

    energy_saving_mode = p_data.get("energy_saving_mode", False)
    if pcat_config.config.has_section('device'):
        pcat_config.config['device']['energy_saving_mode'] = str(energy_saving_mode)
    else:
        pcat_config.config['device'] = {}
        pcat_config.config['device']['energy_saving_mode'] = str(energy_saving_mode)
    pcat_config.save_config()
    
    json_dict = {"status": "ok"}
    json_str = json.dumps(json_dict)  
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp    

@flask_app.route("/api/v1/power_led_settings.json", methods=['GET'])
@api_auth_required
def power_led_settings_api():
    """Get Power LED settings"""
    mode = pc_app.timer_manager.get_power_led_mode()
    json_dict = {
        "status": "ok",
        "mode": mode,
        "start_time": pc_app.timer_manager.get_power_led_start_time(),
        "end_time": pc_app.timer_manager.get_power_led_end_time()
    }
    json_str = json.dumps(json_dict)
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/power_led_settings.json", methods=['POST'])
@api_auth_required
def save_power_led_settings_api():
    """Save Power LED settings"""
    p_data = request.get_json()
    pcat_logger.logger.info("Get user {} posted Power LED settings info:{}".format(request.remote_addr, _redact_sensitive(p_data)))
    
    mode = p_data.get('mode', 'on')  # off, on, timed
    start_time = p_data.get('start_time', '08:00')
    end_time = p_data.get('end_time', '22:00')
    
    # Validate mode
    if mode not in ['off', 'on', 'timed']:
        json_dict = {"status": "error", "message": "Invalid mode. Must be 'off', 'on', or 'timed'"}
        json_str = json.dumps(json_dict)
        rsp = make_response(json_str)
        rsp.headers['Content-Type'] = 'application/json'
        return rsp
    
    # Save to timer manager
    pc_app.timer_manager.set_power_led_settings(mode, start_time, end_time)
    
    # Apply immediately based on settings
    pc_app.timer_manager.apply_power_led_setting()
    
    json_dict = {"status": "ok"}
    json_str = json.dumps(json_dict)
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/beeper_settings.json", methods=['GET'])
@api_auth_required
def beeper_settings_api():
    """Get Beeper settings"""
    mode = pc_app.timer_manager.get_beeper_mode()
    json_dict = {
        "status": "ok",
        "mode": mode,
        "start_time": pc_app.timer_manager.get_beeper_start_time(),
        "end_time": pc_app.timer_manager.get_beeper_end_time()
    }
    json_str = json.dumps(json_dict)
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/beeper_settings.json", methods=['POST'])
@api_auth_required
def save_beeper_settings_api():
    """Save Beeper settings"""
    p_data = request.get_json()
    pcat_logger.logger.info("Get user {} posted Beeper settings info:{}".format(request.remote_addr, _redact_sensitive(p_data)))
    
    mode = p_data.get('mode', 'on')  # off, on, timed
    start_time = p_data.get('start_time', '08:00')
    end_time = p_data.get('end_time', '22:00')
    
    # Validate mode
    if mode not in ['off', 'on', 'timed']:
        json_dict = {"status": "error", "message": "Invalid mode. Must be 'off', 'on', or 'timed'"}
        json_str = json.dumps(json_dict)
        rsp = make_response(json_str)
        rsp.headers['Content-Type'] = 'application/json'
        return rsp
    
    # Save to timer manager
    pc_app.timer_manager.set_beeper_settings(mode, start_time, end_time)
    
    # Apply immediately based on settings
    pc_app.timer_manager.apply_beeper_setting()
    
    json_dict = {"status": "ok"}
    json_str = json.dumps(json_dict)
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/power_led_status.json", methods=['GET'])
@api_auth_required
def power_led_status_api():
    """Get current Power LED runtime status"""
    settings = pc_app.timer_manager.settings.get('power_led', {})
    mode = settings.get('mode', 'on')
    schedule_owner = pc_app.timer_manager.schedule_owner()

    # Prefer MCU schedule get when the MCU owns the schedule; else re-query
    # the immediate mask. max_age shares the read with beeper_status. On a
    # daemon proven to drop the named commands (delivery raw/host-reassert),
    # the schedule get would just burn its timeout -- ask for the mask.
    try:
        if (schedule_owner == "mcu"
                and pc_app.timer_manager.schedule_delivery() in (None, "named")):
            pc_app.socket_client.pmu_io_schedule_get(wait=True, timeout=0.8)
        else:
            pc_app.socket_client.pmu_io_get(wait=True, timeout=0.8, max_age=2.0)
    except Exception as e:
        pcat_logger.logger.warning(f"power_led_status refresh failed: {e}")

    current_status = pc_app.socket_client.status_led_v2_enabled
    # If current status is still None, fall back to schedule desired state
    if current_status is None:
        if mode == 'off':
            current_status = False
        elif mode == 'on':
            current_status = True
        elif mode == 'timed':
            start_time = settings.get('start_time', '08:00')
            end_time = settings.get('end_time', '22:00')
            current_status = pc_app.timer_manager.is_time_in_range(start_time, end_time)
    else:
        # PMU returns 0/1; normalize so the Alpine badge treats it as a bool
        current_status = bool(current_status)

    json_dict = {
        "status": "ok",
        "current_enabled": current_status,
        "mode": mode,
        "start_time": settings.get('start_time', '08:00'),
        "end_time": settings.get('end_time', '22:00'),
        "schedule_owner": schedule_owner,
        "schedule_delivery": pc_app.timer_manager.schedule_delivery(),
        # Legacy fields for backward compatibility
        "enabled": settings.get('enabled', True),
        "all_day": settings.get('all_day', True)
    }
    json_str = json.dumps(json_dict)
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/beeper_status.json", methods=['GET'])
@api_auth_required
def beeper_status_api():
    """Get current Beeper runtime status"""
    settings = pc_app.timer_manager.settings.get('beeper', {})
    mode = settings.get('mode', 'on')
    schedule_owner = pc_app.timer_manager.schedule_owner()

    # LED status already re-queries; schedule get / max_age keep this cheap.
    # Same named-delivery gate as power_led_status above.
    try:
        if (schedule_owner == "mcu"
                and pc_app.timer_manager.schedule_delivery() in (None, "named")):
            pc_app.socket_client.pmu_io_schedule_get(wait=True, timeout=0.8)
        else:
            pc_app.socket_client.pmu_io_get(wait=True, timeout=0.8, max_age=2.0)
    except Exception as e:
        pcat_logger.logger.warning(f"beeper_status refresh failed: {e}")

    current_status = pc_app.socket_client.beeper_enabled
    if current_status is None:
        if mode == 'off':
            current_status = False
        elif mode == 'on':
            current_status = True
        elif mode == 'timed':
            start_time = settings.get('start_time', '08:00')
            end_time = settings.get('end_time', '22:00')
            current_status = pc_app.timer_manager.is_time_in_range(start_time, end_time)
    else:
        current_status = bool(current_status)

    json_dict = {
        "status": "ok",
        "current_enabled": current_status,
        "mode": mode,
        "start_time": settings.get('start_time', '08:00'),
        "end_time": settings.get('end_time', '22:00'),
        "schedule_owner": schedule_owner,
        "schedule_delivery": pc_app.timer_manager.schedule_delivery(),
        # Legacy fields for backward compatibility
        "enabled": settings.get('enabled', True),
        "all_day": settings.get('all_day', True)
    }
    json_str = json.dumps(json_dict)
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/battery_info.json", methods=['GET'])
@api_auth_required
def battery_info_get_api():
    """Get battery health: learned capacity, cycle count and aging.

    The MCU measures the pack's real capacity over a full discharge and keeps it
    (with the cycle count) in its data flash. "supported" is false on MCU
    firmware older than RA2E1260715004, which does not implement the query --
    the UI shows that rather than pretending the values are zero.
    """
    client = pc_app.socket_client
    supported = bool(client.battery_info_supported)

    json_dict = {
        "status": "ok",
        "supported": supported,
    }

    if supported:
        full_capacity = client.battery_full_capacity
        nominal_capacity = client.battery_nominal_capacity
        # Aging: how much of the rated capacity the pack still holds. MCU fw
        # >= RA2E1260719003 computes this itself (EMA-smoothed learned
        # capacity, capped at 100 like Apple) -- prefer that; fall back to
        # deriving it from full/nominal on older firmware.
        health_pct = client.battery_health_pct_mcu
        # MCU fw >= RA2E1261016000 also reports whether the pack has ever been
        # charged to a real 100%. If not (e.g. a charge limit below 100% is
        # set), the full->empty capacity measurement never ran, so the MCU
        # withholds health and the derived fallback below must not fill it in.
        # The fallback is also limited to firmware that predates MCU-computed
        # health (battery-info proto < 6): from proto 6 on, an absent health is
        # the MCU's own verdict, even when the daemon is too old to forward
        # full-charge-seen.
        full_charge_seen = getattr(client, "battery_full_charge_seen", None)
        proto_ver = client.battery_proto_ver
        mcu_computes_health = isinstance(proto_ver, int) and proto_ver >= 6
        if full_charge_seen is False:
            health_pct = None
        elif (health_pct is None and not mcu_computes_health and
                client.battery_capacity_learned and
                full_capacity and nominal_capacity):
            try:
                health_pct = round(
                    (int(full_capacity) / int(nominal_capacity)) * 100.0, 1)
            except (TypeError, ValueError, ZeroDivisionError):
                health_pct = None
        # Extended fields (MCU fw >= RA2E1260717006): learned pack internal
        # resistance and cumulative machine-on time. None on older firmware;
        # the page shows the rows only when present.
        runtime_hours = None
        if client.battery_runtime_seconds is not None:
            try:
                runtime_hours = round(int(client.battery_runtime_seconds) / 3600.0, 1)
            except (TypeError, ValueError):
                runtime_hours = None
        # Middle-point tap voltage (MCU fw >= RA2E1260718001): the 2P2S pack
        # center-tap in volts (measured on P015 via a 180K/100K divider). None
        # on older firmware; the page shows the row only when present.
        mid_voltage_v = None
        if client.battery_mid_voltage is not None:
            try:
                mid_voltage_v = round(int(client.battery_mid_voltage) / 1000.0, 3)
            except (TypeError, ValueError):
                mid_voltage_v = None

        # SOC display-factor diagnostics (MCU fw >= RA2E1260719001): the MCU
        # auto-learns the per-unit "display empty point" (raw SoC at which the
        # machine actually died from an empty battery) and remaps displayed SoC
        # so that point reads 0%, plus a Tesla-style curve (real 4%->1%,
        # 96%->100%). All ax+b; scaled ints from the MCU are converted to
        # plain numbers here. None on older firmware.
        def _scaled(v, div, digits=3):
            try:
                return round(int(v) / div, digits)
            except (TypeError, ValueError):
                return None
        soc_raw_pct = _scaled(client.battery_soc_raw_x10, 10.0, 1)
        soc_disp_a = _scaled(client.battery_soc_disp_a_x1000, 1000.0)
        soc_disp_b = _scaled(client.battery_soc_disp_b_x100, 100.0, 2)
        chg_gain = _scaled(client.battery_chg_gain_x1000, 1000.0)
        cur_trim_a = _scaled(client.battery_cur_trim_a_x1000, 1000.0)

        # Measured capacity looks low (e.g. 5825mAh vs 6800 nominal) because
        # the current meter gain is off; suggest the ax+b pump-up factor to
        # compile into the MCU once verified with the bench multimeter.
        suggested_capacity_factor = None
        if (client.battery_capacity_learned and full_capacity and
                nominal_capacity):
            try:
                suggested_capacity_factor = round(
                    int(nominal_capacity) / int(full_capacity), 3)
            except (TypeError, ValueError, ZeroDivisionError):
                suggested_capacity_factor = None

        # Remaining capacity / energy counters (MCU fw >= RA2E1260719002):
        # raw coulomb-counter values (not display-factor adjusted). Energy
        # accumulators reset at RK boot re-anchor and full-charge anchor.
        consumed_wh = _scaled(client.battery_consumed_mwh, 1000.0, 2)
        charged_wh = _scaled(client.battery_charged_mwh, 1000.0, 2)

        # Gauge temperature (MCU fw >= RA2E1260719009, proto ver >= 7): the
        # on-board NTC reading the gauge runs its compensation on. The derived
        # cold-empty uplift and R_int temperature multiplier are computed by
        # the MCU from this and are no longer surfaced on the page.
        # None on older firmware; the row shows only when present.

        # Awake-on-battery time for the current session (since DC was last
        # unplugged), with powered-off gaps excluded. Read from the persisted
        # power event log rather than an in-memory timer so it survives a
        # reboot in the middle of a battery run.
        on_battery = None
        try:
            on_battery = power_event_log.get_on_battery_session(
                charger_connected=bool(client.charger_connected))
        except Exception:
            on_battery = None

        json_dict.update({
            "full_capacity": full_capacity,
            "nominal_capacity": nominal_capacity,
            "cycle_count": client.battery_cycle_count,
            "capacity_learned": bool(client.battery_capacity_learned),
            "learning_active": bool(client.battery_learning_active),
            "health_pct": health_pct,
            # None = unknown (MCU fw < RA2E1261016000); False = never charged
            # to 100%, so health_pct is withheld.
            "full_charge_seen": full_charge_seen,
            # Whole-loop measured resistance (cells + wiring/connector + MOSFET).
            "internal_resistance_mohm": client.battery_internal_resistance,
            # Resistance split (MCU fw >= RA2E1260721001): fixed board parasitic
            # and the cell-only value (total - parasitic). None on older
            # firmware; the page shows the battery-only row only when present.
            "internal_resistance_parasitic_mohm": client.battery_internal_resistance_parasitic,
            "internal_resistance_battery_mohm": client.battery_internal_resistance_battery,
            "runtime_hours": runtime_hours,
            "mid_voltage_v": mid_voltage_v,
            "soc_raw_pct": soc_raw_pct,
            "soc_empty_learned_pct": client.battery_soc_empty_pct,
            "soc_disp_a": soc_disp_a,
            "soc_disp_b": soc_disp_b,
            "chg_gain": chg_gain,
            "cur_trim_a": cur_trim_a,
            "cur_trim_b_ma": client.battery_cur_trim_b_ma,
            "suggested_capacity_factor": suggested_capacity_factor,
            "remain_mah": client.battery_remain_mah,
            "consumed_wh": consumed_wh,
            "charged_wh": charged_wh,
            "protocol_ver": client.battery_proto_ver,
            "minutes_to_empty": client.battery_minutes_to_empty,
            "minutes_to_full": client.battery_minutes_to_full,
            "gauge_temp_c": client.battery_gauge_temp_c,
            # Current on-battery run: seconds awake since DC was unplugged,
            # whether that run is still going (False = on DC, value is the last
            # completed run), and the unplug timestamp. None when the log has
            # no unplug event to measure from.
            "on_battery_seconds": on_battery["seconds"] if on_battery else None,
            "on_battery_active": on_battery["active"] if on_battery else None,
            "on_battery_since": on_battery["since"] if on_battery else None,
            # MCU uptime diagnostics (MCU fw >= RA2E1260721006, proto ver >=
            # 9): seconds since the MCU last reset (awake + standby time,
            # survives RK reboots -- "how long without a reset"), seconds
            # since the MCU last woke from standby, and the standby-entry
            # count. None on older firmware; the page shows the rows only
            # when present. Raw seconds -- the page formats days/hours.
            "mcu_uptime_seconds": client.mcu_uptime_seconds,
            "mcu_since_sleep_seconds": client.mcu_since_sleep_seconds,
            "mcu_sleep_count": client.mcu_sleep_count,
        })

        # Gauge self-calibration state -> /tmp/guage_learning.log. Written
        # here, on demand, rather than from a periodic tick: the numbers are
        # bench diagnostics nobody reads unless they are on /dev, so a device
        # that never serves this endpoint spends nothing on it. The writer
        # skips unchanged state, so the 5s poll behind /dev appends only when
        # the MCU actually re-learns something.
        gauge_learning_log.record(client)

    rsp = make_response(json.dumps(json_dict))
    rsp.headers['Content-Type'] = 'application/json'
    return rsp


# G-sensor (accelerometer) exposed by the photonicat-pm kernel driver as three
# signed axis files in units of 1/16384 g (so 16384 == 1g). movement_trigger is
# the driver's lift-to-wake flag (0/1). Present on photonicat2; absent on the
# Home model, where "supported" comes back false and the UI hides the section.
_GSENSOR_DIR = "/sys/kernel/photonicat-pm"
_GSENSOR_LSB_PER_G = 16384.0


def _read_gsensor_axis(name):
    """Read one signed integer axis file; None if the driver/file is absent."""
    try:
        with open("{}/{}".format(_GSENSOR_DIR, name), "r") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


@flask_app.route("/api/v1/gsensor.json", methods=['GET'])
@api_auth_required
def gsensor_api():
    """Live 3-axis accelerometer reading for the orientation view on /dev.

    Returns raw counts, the same values in g, and derived pitch/roll (degrees)
    so the browser can tilt the device model without re-deriving the geometry.
    supported=false when the photonicat-pm g-sensor nodes are not present (e.g.
    the Home model), so the page shows a "no sensor" note instead of zeros."""
    x = _read_gsensor_axis("gs_x")
    y = _read_gsensor_axis("gs_y")
    z = _read_gsensor_axis("gs_z")
    if x is None or y is None or z is None:
        return jsonify({"status": "ok", "supported": False})

    gx, gy, gz = x / _GSENSOR_LSB_PER_G, y / _GSENSOR_LSB_PER_G, z / _GSENSOR_LSB_PER_G
    # Pitch/roll from the gravity vector. atan2 keeps full range and avoids the
    # divide-by-zero a plain asin/atan would hit when an axis is 0.
    pitch = math.degrees(math.atan2(-gx, math.sqrt(gy * gy + gz * gz)))
    roll = math.degrees(math.atan2(gy, gz))
    magnitude = math.sqrt(gx * gx + gy * gy + gz * gz)

    mv = _read_gsensor_axis("movement_trigger")

    return jsonify({
        "status": "ok",
        "supported": True,
        "raw": {"x": x, "y": y, "z": z},
        "g": {"x": round(gx, 3), "y": round(gy, 3), "z": round(gz, 3)},
        "pitch": round(pitch, 1),
        "roll": round(roll, 1),
        "magnitude": round(magnitude, 3),
        "movement": bool(mv) if mv is not None else None,
        # Persisted device→model orientation offset as a unit quaternion
        # [w,x,y,z]; the page renders model = calibration · sensor_orientation.
        # null (identity) until the user calibrates on /dev.
        "calibration": _get_gsensor_calibration(),
    })


@flask_app.route("/api/v1/bluetooth/status.json", methods=['GET'])
@api_auth_required
def bluetooth_status_api():
    """Bluetooth radio state for the /dev page.

    Always 200 with a diagnosis: an absent adapter is a fact about the device,
    not a request error, and returning 5xx would make the page show a network
    failure for a board whose radio is simply not up."""
    return jsonify({"status": "ok", "bluetooth": bt_util.status()})


@flask_app.route("/api/v1/bluetooth/scan.json", methods=['POST'])
@api_session_strict
def bluetooth_scan_api():
    """Scan for nearby Bluetooth devices, first trying to bring the radio up.

    POST (not GET) because it drives the radio and can unblock rfkill; and
    @api_session_strict rather than @api_auth_required so the mini display's
    localhost bypass cannot start a scan."""
    body = request.get_json(silent=True) or {}
    seconds = body.get("seconds", 10)
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = 10

    recovery = None
    if not bt_util.adapters():
        ok, steps = bt_util.recover()
        recovery = {"attempted": True, "ok": ok, "steps": steps}

    result = bt_util.scan(seconds)
    if recovery:
        result["recovery"] = recovery
    return jsonify(result)


def _get_gsensor_calibration():
    """Stored calibration quaternion [w,x,y,z], or None if unset/invalid."""
    q = pcat_config.get_user_config_key("gsensor_calibration", None)
    if (isinstance(q, list) and len(q) == 4 and
            all(isinstance(v, (int, float)) for v in q)):
        return q
    return None


@flask_app.route("/api/v1/gsensor_calibration.json", methods=['GET', 'POST'])
@api_auth_required
def gsensor_calibration_api():
    """Get or save the device→model orientation calibration (unit quaternion
    [w,x,y,z]). POST body {"quaternion":[w,x,y,z]} persists it; {"reset":true}
    clears it. Set by the calibration tool on /dev."""
    if request.method == 'GET':
        return jsonify({"status": "ok", "calibration": _get_gsensor_calibration()})

    data = request.get_json(silent=True) or {}
    if data.get("reset"):
        pcat_config.set_user_config_key("gsensor_calibration", None)
        return jsonify({"status": "ok", "calibration": None})

    q = data.get("quaternion")
    if (not isinstance(q, list) or len(q) != 4 or
            not all(isinstance(v, (int, float)) for v in q)):
        return jsonify({"status": "failed",
                        "message": "quaternion must be [w,x,y,z]"}), 400
    # Normalize to a unit quaternion before storing so the client can send raw
    # values; a zero-norm quaternion is meaningless, reject it.
    norm = math.sqrt(sum(v * v for v in q))
    if norm < 1e-6:
        return jsonify({"status": "failed", "message": "degenerate quaternion"}), 400
    q = [round(v / norm, 6) for v in q]
    pcat_config.set_user_config_key("gsensor_calibration", q)
    return jsonify({"status": "ok", "calibration": q})


@flask_app.route("/api/v1/buzzer_play.json", methods=['POST'])
@api_auth_required
def buzzer_play_api():
    """Play a tone sequence on the MCU buzzer.

    Body: {"tones": [{"hz": 2700, "ms": 300}, ...], "loop": 0}. hz 0 is a rest.
    The MCU accepts at most 20 steps per command, so the page chunks longer
    sequences (MIDI playback) and paces the chunks itself; a new sequence
    interrupts the current one, which also makes {"tones":[{"hz":0,"ms":1}]}
    the stop command. Requires pcat-manager with pmu-beep-play support.
    """
    def error(message):
        rsp = make_response(json.dumps({"status": "error", "message": message}))
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

    client = pc_app.socket_client
    if not client.stm32_firmware_exist():
        return error("no MCU on this model")

    p_data = request.get_json(silent=True) or {}
    tones = p_data.get('tones')
    loop = bool(p_data.get('loop'))

    if not isinstance(tones, list) or not 1 <= len(tones) <= 20:
        return error("tones must be a list of 1 to 20 steps")

    tones_clean = []
    for tone in tones:
        if not isinstance(tone, dict):
            return error("each tone must be an object with hz and ms")
        try:
            hz = int(tone.get('hz', 0))
            ms = int(tone.get('ms', 0))
        except (TypeError, ValueError):
            return error("hz and ms must be integers")
        if not 0 <= hz <= 20000:
            return error("hz must be 0 (rest) to 20000")
        if not 1 <= ms <= 65535:
            return error("ms must be 1 to 65535")
        tones_clean.append({"hz": hz, "ms": ms})

    client.beep_play(tones_clean, loop)

    rsp = make_response(json.dumps({"status": "ok"}))
    rsp.headers['Content-Type'] = 'application/json'
    return rsp


@flask_app.route("/api/v1/charge_threshold.json", methods=['GET'])
@api_auth_required
def charge_threshold_get_api():
    """Get battery charge threshold.

    Defaults to 100 (charge fully, no limit) whenever the limit has not been
    set before. The daemon reports an unset limit as None or 0 (ChargerLimitVoltage=0
    in /etc/pcat-manager.conf), and any value outside the valid 80-100 range is
    treated as unset too, so the UI always starts at 100%.

    Every read also fires an async refresh chain toward the MCU (web client ->
    daemon -> CHARGE_THRESHOLD_GET), so the value converges on the MCU's
    actual limit: the fresh answer lands in the caches for the next poll.
    The page polls this endpoint periodically, which keeps it synced.
    """
    client = pc_app.socket_client
    try:
        client.charge_threshold_get()
    except Exception as e:
        pcat_logger.logger.warning(f"charge_threshold refresh failed: {e}")
    value = client.charge_threshold
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = None
    if value is None or value < 80 or value > 100:
        value = 100
    json_dict = {
        "status": "ok",
        "value": value
    }
    json_str = json.dumps(json_dict)
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/charge_threshold.json", methods=['POST'])
@api_auth_required
def charge_threshold_set_api():
    """Set battery charge threshold"""
    try:
        data = request.get_json()
        value = int(data.get("value", 100))
        if value < 80 or value > 100:
            json_dict = {"status": "error", "message": "Value must be between 80 and 100"}
            rsp = make_response(json.dumps(json_dict))
            rsp.headers['Content-Type'] = 'application/json'
            return rsp
        pc_app.socket_client.charge_threshold_set(value)
        json_dict = {"status": "ok", "value": value}
    except Exception as e:
        pcat_logger.logger.error(f"charge_threshold_set error: {e}")
        json_dict = {"status": "error", "message": str(e)}
    json_str = json.dumps(json_dict)
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/timer_debug.json", methods=['POST'])
@dev_only
@api_auth_required
def timer_debug_api():
    """Debug API to force apply timer settings"""
    pcat_logger.logger.info("Timer debug API called by user {}".format(request.remote_addr))
    
    try:
        # Force apply all timer settings
        pc_app.timer_manager.force_apply_settings()
        
        # Get current settings for debugging
        power_led_settings = pc_app.timer_manager.settings.get('power_led', {})
        beeper_settings = pc_app.timer_manager.settings.get('beeper', {})
        
        json_dict = {
            "status": "ok",
            "message": "Timer settings force applied",
            "power_led_settings": power_led_settings,
            "beeper_settings": beeper_settings,
            "socket_connected": pc_app.socket_client.sock_connected if pc_app.socket_client else False
        }
    except Exception as e:
        pcat_logger.logger.error(f"Timer debug API error: {e}")
        json_dict = {
            "status": "error",
            "message": f"Timer debug failed: {str(e)}"
        }
    
    json_str = json.dumps(json_dict)
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

# data_stats.json runs 4 sqlite SUM aggregations per hit (~60ms) and is polled
# by the mini display every 10s. The underlying data is only written every 30s
# (network_stats.log_stats), so cache results per network_type just under that.
_data_stats_cache = {}  # network_type -> {'data': dict, 'timestamp': float}
_data_stats_cache_lock = threading.Lock()
DATA_STATS_CACHE_DURATION = 25  # seconds

@flask_app.route("/api/v1/data_stats.json", methods=['GET'])
@api_auth_required
def data_stats_api():
    network_type = request.args.get("network_type")
    if network_type != "mobile" and network_type != "wired":
        response_json = {}
        response_json["today_used"] = 0
        response_json["week_used"]  = 0
        response_json["month_used"] = 0
        response_json["last_month_used"] = 0
        rsp = make_response(response_json)
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

    with _data_stats_cache_lock:
        cached = _data_stats_cache.get(network_type)
        if cached and time.time() - cached['timestamp'] < DATA_STATS_CACHE_DURATION:
            rsp = make_response(cached['data'])
            rsp.headers['Content-Type'] = 'application/json'
            return rsp

    response_json = {}
    response_json["today_used"] = pc_app.main_app.network_stats.today_bytes(network_type) or 0
    response_json["week_used"]  = pc_app.main_app.network_stats.this_week_bytes(network_type) or 0
    response_json["month_used"] = pc_app.main_app.network_stats.month_bytes(date.today(), network_type) or 0

    today = date.today()
    first = today.replace(day=1)
    last_month = first - timedelta(days=1)
    response_json["last_month_used"] = pc_app.main_app.network_stats.month_bytes(last_month, network_type) or 0

    with _data_stats_cache_lock:
        _data_stats_cache[network_type] = {'data': response_json, 'timestamp': time.time()}

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/modem/basic.json", methods=['GET'])
@api_auth_required
def api_model_basic():
    # The modem page polls this while open; that's the demand signal that
    # enables the heavy routine AT queries (serving cell, CA, band prefs).
    pc_app.modem_client.note_ui_activity()
    response_json = {}
    # Thread-safe access to modem_valid
    with pc_app.modem_client.mutex:
        response_json["modem_valid"] = pc_app.modem_client.modem_valid
    response_json["wwan_powered"] = pc_app.is_wwan_powered()
    response_json["modem_installed"] = modem_hw_present()
    # Check for custom user-set SIM number first, then fall back to detected number
    custom_sim_number = pcat_config.get_user_config_key("custom_sim_number", None)
    response_json["sim_number"] = format_phone_number(custom_sim_number if custom_sim_number else pc_app.modem_client.basic.get("sim_number", ""))
    response_json["sim_state"] = pc_app.modem_client.get_inferred_sim_state(pc_app.socket_client.sim_state)
    response_json["imei_num"] = pc_app.modem_client.basic.get("imei_num", "")
    response_json["modem_model"] = pc_app.modem_client.basic.get("model", "")
    response_json["firmware_version"] = pc_app.modem_client.basic.get("firmware_version", "")
    #response_json["messages"] = pc_app.modem_client.get_messages()
    response_json["querying"] = pc_app.modem_client.querying
    response_json["sms_check_interval"] = pcat_config.config.getint('modem', 'sms_check_interval', fallback = 30)
    response_json["sms_forward"] = pcat_config.config.getboolean('modem', 'sms_forward', fallback = False)
    response_json["sms_forward_to"] = pcat_config.config.get('modem', 'sms_forward_to', fallback = '')

    # Return temperature in a consistent dict format for all modems
    # Both FM350 and Qualcomm now store in qtemp_sensors as {"modem-ambient-usr": value, ...}
    response_json["modem_temperature"] = pc_app.modem_client.basic.get("qtemp_sensors", "")
    response_json["modem_temperature_decimal"] = pc_app.modem_client.basic.get("modem_temperature_decimal", "")
    response_json["modem_isp_details"] = pc_app.modem_client.basic.get("isp_details", "")
    response_json["modem_network_info"] = pc_app.modem_client.basic.get("network_info", "")
    response_json["modem_cell_id"] = pc_app.modem_client.basic.get("serving_cellid", "")
    response_json["modem_cell_info"] = pc_app.modem_client.basic.get("serving_cell_info", "")
    response_json["modem_serving_info"] = pc_app.modem_client.basic.get("modem_serving_info", "")
    response_json["modem_serving_quality"] = pc_app.modem_client.basic.get("modem_serving_quality", "")
    response_json["modem_cell_signal_details"] = pc_app.modem_client.basic.get("modem_serving_quality", "")
    response_json["modem_usb_speed"] = pc_app.modem_client.basic.get("modem_usb_speed", "")
    response_json["modem_cell_signals"] = str(pc_app.modem_client.basic.get("cell_signal_percent_sinr", "-")) + "%, " + str(pc_app.modem_client.basic.get("cell_signal_percent_qrsrq", "-"))+ "%, " + str(pc_app.modem_client.basic.get("cell_signal_percent_qrsrp", "-")) + "%, " + str(pc_app.modem_client.basic.get("cell_signal_percent_csq", "-")) + "%"
    usb_standard = pc_app.modem_client.basic.get("modem_usb_speed", "").strip('"')
    modem_mode = pc_app.modem_client.basic.get("usbnet_mode", "")
    response_json["modem_cpin"] = pcat_config.get_cpin()
    
    if pc_app.modem_client.is_520():
        if usb_standard:
            usb_standard = usb_standard[0] + "." + usb_standard[1:]
        if modem_mode and usb_standard:
            usb_display = modem_mode + ", USB " + usb_standard
        else:
            usb_display = ""
    elif pc_app.modem_client.is_500u():
        usb_display = modem_mode
    elif pc_app.modem_client.is_fm350():
        # FM350-GL display: show modem mode and USB speed if available
        if modem_mode and usb_standard:
            usb_display = modem_mode + ", USB " + usb_standard
        elif modem_mode:
            usb_display = modem_mode
        else:
            usb_display = ""
    else:
        usb_display = ""

    response_json["modem_usbnet_mode"] = usb_display
    response_json["cell_carrier_info"] = pc_app.modem_client.basic.get("cell_carrier_info", "")
    # Prefer the user's saved RAT intent over the live AT+GTACT? readback: the
    # FM350 dialer temporarily switches the modem's actual RAT (e.g. drops to
    # LTE to keep data up when 5G rejects it), so the live register would show
    # "4G only" even when the user picked AUTO. The intent file is what the user
    # chose; show that. Falls back to the readback when no intent is saved yet.
    response_json["modem_tech_pref"] = pc_app.modem_client.get_saved_tech_pref()
    response_json["modem_roam_pref"] = pc_app.modem_client.basic.get("roam_pref", "")
    response_json["policy_lte_bands"] = pc_app.modem_client.basic.get("policy_lte_bands", [])
    response_json["policy_nr5g_bands"] = pc_app.modem_client.basic.get("policy_nr5g_bands", [])
    response_json["selected_lte_bands"] = pc_app.modem_client.basic.get("setting_lte_bands", [])
    response_json["selected_nr5g_bands"] = pc_app.modem_client.basic.get("setting_nr5g_bands", [])

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp  

@flask_app.route("/api/v1/modem/basic.json", methods=['POST'])
@api_auth_required
def api_modem_refresh():
    pc_app.modem_client.note_ui_activity()
    if pc_app.modem_client.querying:
        pass
    else:
        p_data = request.get_json()
        print("Get", p_data)
        pc_app.modem_client.start_query(p_data.get("refresh"))
    response_json = {}
    response_json["status"] = "ok"
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v2/modem_advanced.json", methods=['POST'])
@api_auth_required
def api_modem_advanced():
    """
    Receive advanced modem settings (tech, roaming, LTE/NR band lists),
    dispatch to the modem client, and return a JSON status.
    """
    """ smaple post data:
    {
    "modem_tech_pref": "LTE",
    "modem_roam_pref": "no",
    "desired_lte_bands": "1:2:3:4:5:7:8:12:13:14:19:20:25:26:28:29:30:32:34:38:39:40:41:42:43:46:48:66:71",
    "desired_nr5g_bands": "1:2:3:5:7:8:12:13:14:18:20:25:26:28:29:30:38:40:41:48:66:70:71:75:76:77:78:79"
    }
    """
    # start_set_modem_config only queues AT commands (mutex-protected) and
    # writes the FM350 preference file, so it is safe while the modem thread
    # is mid-query. It used to be skipped whenever that thread was busy
    # (several seconds out of every 30 while the page polls) while this
    # route still answered "ok": that is how band saves silently vanished.
    data = request.get_json() or {}
    pc_app.modem_client.start_set_modem_config(
        tech_pref   = data.get("modem_tech_pref"),
        roam_pref   = data.get("modem_roam_pref"),
        lte_bands   = data.get("desired_lte_bands"),
        nr5g_bands  = data.get("desired_nr5g_bands"),
        usb_speed   = data.get("modem_usb_speed"),
    )
    try:
        pcat_config.set_cpin(data.get("modem_cpin"))
            
    except Exception as e:
        print(f"Failed to set config: {e}")

    # always reply with status ok
    response_json = {"status": "ok"}
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/modem/sim_number.json", methods=['POST'])
@api_auth_required
def api_modem_update_sim_number():
    """Update the SIM card number displayed in the modem page"""
    try:
        p_data = request.get_json()
        sim_number = p_data.get("sim_number", "").strip()
        
        if not sim_number:
            return make_response({
                "status": "error",
                "message": "SIM number cannot be empty"
            }), 400
        
        # Basic phone number validation
        import re
        phone_regex = r'^\+?[0-9\s\-\(\)]{10,20}$'
        if not re.match(phone_regex, sim_number):
            return make_response({
                "status": "error", 
                "message": "Invalid phone number format"
            }), 400
            
        # Update the SIM number in the modem client
        # This updates the displayed value and can be persisted if needed
        with pc_app.modem_client.mutex:
            # Store the custom SIM number in config
            pcat_config.set_user_config_key("custom_sim_number", sim_number)
            
            # Update the modem client's sim_number field
            pc_app.modem_client.sim_number = sim_number
            
        pcat_logger.logger.info(f"User {request.remote_addr} updated SIM number to: {sim_number}")
        
        response_json = {
            "status": "ok", 
            "message": "SIM number updated successfully",
            "sim_number": sim_number
        }
        
    except Exception as e:
        pcat_logger.logger.error(f"Error updating SIM number: {e}")
        response_json = {
            "status": "error", 
            "message": f"Failed to update SIM number: {str(e)}"
        }
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v2/modem/reset.json", methods=['POST'])
@api_auth_required
def api_modem_reset():
    pcat_logger.logger.info("User {} request modem factory reset with SMS clear".format(request.remote_addr))
    try:
        # First clear all SMS messages from both database and modem
        pc_app.sms_client.clear_all_sms()
        pcat_logger.logger.info("SMS messages cleared before modem reset")
        
        # Then perform the modem factory reset
        pc_app.modem_client.factory_reset_modem()
        pcat_logger.logger.info("Modem factory reset command sent")
        
        response_json = {"status": "ok", "message": "SMS cleared and modem reset initiated"}
    except Exception as e:
        pcat_logger.logger.error(f"Failed to reset modem: {e}")
        response_json = {"status": "error", "message": f"Failed to reset modem: {str(e)}"}
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/modem/config.json", methods=['GET'])
@api_auth_required
def api_model_config():
    response_json = {}
    response_json["sms_check_interval"] = pcat_config.config.getint('modem', 'sms_check_interval', fallback = 30)
    response_json["sms_forward"] = pcat_config.config.getboolean('modem', 'sms_forward', fallback = False)
    response_json["sms_forward_to"] = pcat_config.config.get('modem', 'sms_forward_to', fallback = '')
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp  

@flask_app.route("/api/v1/modem/config.json", methods=['POST'])
@api_auth_required
def api_modem_save_config():
    response_json = {}
    p_data = request.get_json()
    print("p_data", p_data)
    pcat_logger.logger.info("Get config data: {}".format(_redact_sensitive(p_data)))

    params_valid = True
    if int(p_data.get('sms_check_interval', 30)) < 5:
        params_valid = False
    if p_data.get('sms_forward', False):
        num_str = p_data.get('sms_forward_to', '')
        number_matched = re.search(r"^\+?[0-9]{3,20}$", num_str)
        if not number_matched:
            params_valid = False
    if not params_valid:
        response_json["status"] = "failed"
        response_json["message"] = "Params invalid"
        rsp = make_response(response_json)
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

    pcat_config.config['modem']['sms_check_interval'] = str(p_data.get('sms_check_interval', 30))
    pcat_config.config['modem']['sms_forward'] = str(p_data.get('sms_forward', False))
    pcat_config.config['modem']['sms_forward_to'] = str(p_data.get('sms_forward_to', ''))
    pcat_config.save_config()

    response_json["status"] = "ok"
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp    

# curl -X POST http://172.16.0.1/api/v1/modem/sms/send.json -H 'Content-Type: application/json' -d '{"send_to":"13800138001","msg":"Test from API"}'  
@flask_app.route("/api/v1/modem/sms/send.json", methods=['POST'])
@api_auth_required
def api_model_sms_send():
    p_data = request.get_json()
    send_to_number = p_data.get("send_to", "")
    send_msg = p_data.get("msg", "")
    send_success = pc_app.modem_client.send_sms(send_to_number, send_msg)
    response_json = {}
    if send_success:
        response_json["status"] = "ok"
        # Invalidate cache after successful SMS send
        invalidate_sms_cache()
    else:
        response_json["status"] = "failed"
    print("Send to", send_to_number, send_msg)
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

# curl -X POST http://172.16.0.1/api/v1/modem/sms/send.json -H 'Content-Type: application/json' -d '{"send_to":"13800138001","msg":"Test from API"}'  
@flask_app.route("/api/v2/modem/sms/send.json", methods=['POST'])
@api_auth_required
def api_model_sms_send_v2():
    p_data = request.get_json()
    send_to_number = p_data.get("send_to", "")
    send_msg = p_data.get("msg", "")
    send_success = pc_app.sms_client.send_sms(send_to_number, send_msg)
    response_json = {}
    if send_success:
        response_json["status"] = "ok"
        # Invalidate cache after successful SMS send
        invalidate_sms_cache()
    else:
        response_json["status"] = "failed"
    print("Send to", send_to_number, send_msg)
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

#need to fix to new api
@flask_app.route("/api/v1/modem/sms/read.json", methods=['POST'])
@api_auth_required
def api_model_sms_read():
    p_data = request.get_json()
    pc_app.modem_client.read_sms(p_data.get("hash_id", None))
    response_json = {}
    response_json["status"] = "ok"
    response_json["topic_messages"] = pc_app.modem_client.get_topic_messages(p_data.get("topic", None))
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp 

#need to fix to new api
@flask_app.route("/api/v1/sms/delete.json", methods=['POST'])
@api_auth_required
def api_model_sms_delete():
    p_data = request.get_json()
    pcat_logger.logger.info("User {} request delete message:{}".format(request.remote_addr, _redact_sensitive(p_data)))
    
    response_json = {}
    try:
        # Get the database ID from the request
        sms_id = p_data.get("msg_id", None)
        if sms_id is None:
            response_json["status"] = "error"
            response_json["message"] = "Missing SMS ID"
        else:
            # Use the SMS client to delete from database
            success = pc_app.sms_client.delete_sms_from_db(int(sms_id))
            if success:
                response_json["status"] = "ok"
                response_json["message"] = "SMS deleted successfully"
                # Invalidate cache after successful SMS delete
                invalidate_sms_cache()
            else:
                response_json["status"] = "error"
                response_json["message"] = "Failed to delete SMS"
    except Exception as e:
        pcat_logger.logger.error(f"Failed to delete SMS: {e}")
        response_json["status"] = "error"
        response_json["message"] = f"Error: {str(e)}"

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp  

@flask_app.route("/api/v2/sms/list.json", methods=['GET'])
@api_auth_required
def api_sms_list():
    current_time = time.time()
    force_refresh = request.args.get('force') == 'true'

    # Parse pagination parameters
    requested_limit = None
    try:
        # Support both 'n' and 'limit' parameters for compatibility
        n_param = request.args.get('n')
        limit_param = request.args.get('limit')
        if n_param:
            requested_limit = int(n_param)
        elif limit_param:
            requested_limit = int(limit_param)
        if requested_limit and requested_limit < 1:
            requested_limit = None
    except (ValueError, TypeError):
        requested_limit = None

    # Check cache first (skip if force refresh requested)
    cached_result = None
    if not force_refresh:
        with sms_cache['lock']:
            # If cache is valid (less than 15 seconds old), use cached data
            if (sms_cache['data'] is not None and
                current_time - sms_cache['timestamp'] < SMS_CACHE_DURATION):
                # debug: the modem page polls this every 5s; printing per hit
                # is constant stdout/log churn
                pcat_logger.logger.debug("SMS list served from cache (age: %.1fs)", current_time - sms_cache['timestamp'])
                cached_result = sms_cache['data']

            # Cache expired - check if we should fetch or serve old cache
            # If another fetch is in progress or last fetch was < 10 seconds ago, serve old cache
            elif (sms_cache['fetch_in_progress'] or
                  (sms_cache['data'] is not None and
                   current_time - sms_cache['last_fetch_time'] < SMS_FETCH_THROTTLE)):
                if sms_cache['data'] is not None:
                    pcat_logger.logger.debug("SMS list served from old cache due to throttle (last fetch: %.1fs ago)", current_time - sms_cache['last_fetch_time'])
                    cached_result = sms_cache['data']

    if cached_result:
        # Apply limit to cached result if requested
        if requested_limit and isinstance(cached_result, dict) and 'msg' in cached_result:
            limited_result = dict(cached_result)  # Create a copy
            if len(limited_result["msg"]) > requested_limit:
                limited_result["msg"] = limited_result["msg"][:requested_limit]
                pcat_logger.logger.debug("SMS list limited to %s messages from cache", requested_limit)
            rsp = make_response(limited_result)
        else:
            rsp = make_response(cached_result)
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

    # Force refresh or cache miss - acquire lock and fetch fresh data
    with sms_cache['lock']:
        if force_refresh:
            print("SMS list force refresh requested - bypassing cache and throttle")
            # Clear SMS client throttling to force hardware refresh
            if hasattr(pc_app.sms_client, 'last_check_time'):
                pc_app.sms_client.last_check_time = None
            if hasattr(pc_app.sms_client, 'last_recv_time'):
                pc_app.sms_client.last_recv_time = None

        # Mark fetch in progress to prevent concurrent calls
        sms_cache['fetch_in_progress'] = True
        sms_cache['last_fetch_time'] = current_time

    try:
        # Cache miss or expired, fetch fresh data
        sms_list = pc_app.sms_client.get_sms()
        print("sms_list length:   ", len(sms_list["msg"]))
        if sms_list is None:
            sms_list = {}
            sms_list["msg"] = []

        # Apply SMS limit for screen requests (check if request comes from localhost or has screen user agent)
        is_screen_request = (
            (request.referrer and 'localhost' in request.referrer) or
            (request.remote_addr in ['127.0.0.1', 'localhost']) or
            (request.headers.get('User-Agent', '').lower().find('screen') != -1)
        )

        # Store full result in cache first
        full_result = dict(sms_list)  # Create a copy for cache

        # Apply screen limit only if no explicit limit requested
        if is_screen_request and not requested_limit:
            # This is a request from screen client without explicit limit
            screen_limit = pcat_config.get_sms_limit_for_screen()
            if len(sms_list["msg"]) > screen_limit:
                sms_list["msg"] = sms_list["msg"][:screen_limit]
                print(f"SMS list limited to {screen_limit} messages for screen display")

        # Apply explicit limit if requested (overrides screen limit)
        if requested_limit and len(sms_list["msg"]) > requested_limit:
            sms_list["msg"] = sms_list["msg"][:requested_limit]
            print(f"SMS list limited to {requested_limit} messages as requested")

        # Update cache with full data (not limited version)
        with sms_cache['lock']:
            sms_cache['data'] = full_result
            sms_cache['timestamp'] = current_time
            sms_cache['fetch_in_progress'] = False

        rsp = make_response(sms_list)
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

    finally:
        # Always clear fetch_in_progress flag
        with sms_cache['lock']:
            sms_cache['fetch_in_progress'] = False

@flask_app.route("/api/v2/sms/clear_all.json", methods=['POST'])
@api_auth_required
def api_sms_clear_all():
    pcat_logger.logger.info("User {} request clear all SMS messages".format(request.remote_addr))
    try:
        # Clear all SMS messages from both database and modem
        pc_app.sms_client.clear_all_sms()
        
        # Invalidate cache after clearing all SMS
        invalidate_sms_cache()
        
        response_json = {"status": "ok", "message": "All SMS messages cleared successfully"}
    except Exception as e:
        pcat_logger.logger.error(f"Failed to clear all SMS: {e}")
        response_json = {"status": "error", "message": f"Failed to clear SMS messages: {str(e)}"}
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp


# MCU Update Management APIs

# latest_img.json (dev + prod are the same schema, produced by upload_pcat2_ota.php)
# has keys: version, url, date, changelog-md, sha256sum, message, upload_timestamp.
# It has no "sha256" or "build_date" key -- reading those silently yielded "".
def _mcu_remote_sha256(remote_data):
    return remote_data.get("sha256sum") or remote_data.get("sha256", "")


def _mcu_format_yymmdd(yymmdd):
    """"260715" -> "2026-07-15". Returns "" if it isn't a 6-digit date."""
    if len(yymmdd) == 6 and yymmdd.isdigit():
        return f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
    return ""


def _mcu_remote_build_date(remote_data):
    """Build date of the remote firmware, as YYYY-MM-DD.

    Prefer "date" (YYMMDD, matches the date embedded in the version string) over
    "upload_timestamp", which is UTC and would read ~8h off in CN local time.
    """
    formatted = _mcu_format_yymmdd(remote_data.get("date", ""))
    if formatted:
        return formatted
    timestamp = remote_data.get("upload_timestamp", "")
    if timestamp:
        return timestamp.split(" ")[0]
    return remote_data.get("build_date", "")


def _mcu_local_build_date(version):
    """Build date of the running MCU firmware, derived from its version string.

    Format is <prefix>YYMMDD<3-digit seq>, 14 chars -- "RA2E1260715002" for the
    app, "AA2E1250903000" for the bootloader (which answers the same version
    command when the app image is invalid and it sits in forced-OTA recovery).
    """
    if not version or len(version) != 14:
        return ""
    return _mcu_format_yymmdd(version[5:11])


def _mcu_version_sort_key(version):
    """Sortable key for an MCU version string.

    Versions are <prefix>YYMMDD<3-digit seq> (14 chars), so the trailing 9 digits
    (YYMMDDsss) order chronologically. Returns those digits as an int, else None
    when the string doesn't match the expected shape (caller falls back).
    """
    if not version or len(version) != 14:
        return None
    digits = version[5:14]
    if not digits.isdigit():
        return None
    return int(digits)


def _mcu_remote_is_newer(local_version, remote_version):
    """True only when the remote MCU firmware is strictly newer than local.

    Compares the date/sequence embedded in the version string. Falls back to a
    plain string comparison for versions that don't match the expected format.
    A remote that is older or equal (e.g. local built after the published stable
    image) is NOT an update.
    """
    if not remote_version:
        return False
    local_key = _mcu_version_sort_key(local_version)
    remote_key = _mcu_version_sort_key(remote_version)
    if local_key is not None and remote_key is not None:
        return remote_key > local_key
    return remote_version > local_version


# How many versions older than the latest the /update MCU dropdowns offer
# for downgrades (mirrors the rootfs firmware dropdowns above): stable
# server keeps it short, dev server goes further back for bisecting.
MCU_PROD_MAX_BACK = 3
MCU_DEV_MAX_BACK = 20


def _mcu_fetch_versions(base_url, max_back):
    """Fetch the versions.json manifest published next to latest_img.json
    (maintained by upload_pcat2_ota.php / rebuild_versions_json.py on the
    server) and return up to 1 + max_back entries, newest first, for the
    /update MCU version dropdowns. Each entry carries its upload "message"
    (changelog). Returns [] when the manifest is unavailable so callers fall
    back to the single latest_img.json entry.
    """
    try:
        with urllib.request.urlopen(f"{base_url}/versions.json", timeout=10) as response:
            manifest = json.loads(response.read().decode('utf-8'))
    except Exception as e:
        pcat_logger.logger.info(f"MCU versions manifest unavailable at {base_url}: {e}")
        return []
    entries = manifest.get("versions") if isinstance(manifest, dict) else None
    versions = []
    seen = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        version = entry.get("version", "")
        firmware_url = entry.get("url", "")
        if not version or not firmware_url or version in seen:
            continue
        seen.add(version)
        if not firmware_url.startswith("http"):
            firmware_url = f"{base_url}/{firmware_url}"
        versions.append({
            "version": version,
            "firmware_url": firmware_url,
            "sha256": _mcu_remote_sha256(entry),
            "build_date": _mcu_remote_build_date(entry),
            "message": entry.get("message", "") or "",
        })
    versions.sort(key=lambda v: _mcu_version_sort_key(v["version"]) or 0, reverse=True)
    return versions[:max_back + 1]


def _mcu_versions_with_latest(versions, latest_entry, max_back):
    """Put the official latest (from latest_img.json) at entry 0, drop
    anything newer than it (not released yet), and cap the list length."""
    latest_key = _mcu_version_sort_key(latest_entry["version"])
    if latest_key is not None:
        versions = [v for v in versions
                    if (_mcu_version_sort_key(v["version"]) or 0) <= latest_key]
    versions = [v for v in versions if v["version"] != latest_entry["version"]]
    versions.insert(0, latest_entry)
    return versions[:max_back + 1]


def _mcu_unsupported_response(http_status=200):
    """The MCU firmware images (photonicat2_mcu_firmware / pcat2_mcu) are
    for the Photonicat 2 controller only; a Photonicat 1 must never be
    offered one. The check endpoints answer 'unsupported' (not 'ok'), which
    the page treats as 'nothing to show'; the start endpoint refuses."""
    rsp = make_response({
        "status": "unsupported",
        "reachable": False,
        "message": "MCU firmware update is only supported on Photonicat 2",
    }, http_status)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp


@flask_app.route("/api/v1/mcu_dev_check.json", methods=['GET'])
@api_auth_required
def mcu_dev_check():
    """
    Check development server for MCU firmware updates.
    Returns dev server status and MCU firmware information.
    """
    if not is_photonicat2:
        return _mcu_unsupported_response()
    try:
        # Get current local MCU version
        local_version = pc_app.socket_client.stm32_firmware_version or "Unknown"
        
        # Dev server URL - should contain JSON with latest firmware info
        dev_url = f"{pcat_config.dev_server_base}/photonicat2_mcu_firmware/latest_img.json"
        
        with urllib.request.urlopen(dev_url, timeout=20) as response:
            remote_data = json.loads(response.read().decode('utf-8'))
        
        # Extract information from remote JSON
        # Expected format: {"version": "1.2.3", "url": "relative/or/absolute/path.bin", "sha256": "hash", "build_date": "2025-01-01"}
        remote_version = remote_data.get("version", "")
        firmware_url = remote_data.get("url", "")
        sha256_hash = _mcu_remote_sha256(remote_data)
        build_date = _mcu_remote_build_date(remote_data)

        # Make URL absolute if it's relative
        if firmware_url and not firmware_url.startswith("http"):
            firmware_url = f"{pcat_config.dev_server_base}/photonicat2_mcu_firmware/{firmware_url}"

        # An update is only offered when the remote firmware is strictly newer.
        # When local is newer than remote (e.g. a locally-built image ahead of
        # the published one) this is False, so the UI shows "up to date" instead
        # of a spurious "update available".
        newer_available = _mcu_remote_is_newer(local_version, remote_version)
        update_available = newer_available

        # Version dropdown: latest + older builds testers can downgrade to,
        # each with its upload changelog message.
        versions = _mcu_versions_with_latest(
            _mcu_fetch_versions(f"{pcat_config.dev_server_base}/photonicat2_mcu_firmware",
                                MCU_DEV_MAX_BACK),
            {
                "version": remote_version,
                "firmware_url": firmware_url,
                "sha256": sha256_hash,
                "build_date": build_date,
                "message": remote_data.get("message", "") or "",
            },
            MCU_DEV_MAX_BACK)

        response_data = {
            "status": "ok",
            "reachable": True,
            "versions": versions,
            "local_version": local_version,
            "local_build_date": _mcu_local_build_date(local_version),
            "dev_version": remote_version,
            "dev_firmware_url": firmware_url,
            "dev_sha256": sha256_hash,
            "dev_build_date": build_date,
            "update_available": update_available,
            "newer_available": newer_available,
            "same_version": local_version == remote_version,
            "local_is_newer": bool(remote_version) and local_version != remote_version and not newer_available
        }
        
    except urllib.error.URLError as e:
        pcat_logger.logger.warning(f"Dev server unreachable for MCU check: {e}")
        response_data = {
            "status": "unreachable",
            "reachable": False,
            "message": f"Dev server unreachable: {str(e)}",
            "local_version": pc_app.socket_client.stm32_firmware_version or "Unknown",
            "update_available": False
        }
    except json.JSONDecodeError as e:
        pcat_logger.logger.error(f"Invalid JSON from dev server MCU check: {e}")
        response_data = {
            "status": "error",
            "reachable": False,
            "message": f"Invalid JSON response: {str(e)}",
            "local_version": pc_app.socket_client.stm32_firmware_version or "Unknown",
            "update_available": False
        }
    except Exception as e:
        pcat_logger.logger.error(f"Unexpected error checking dev server MCU: {e}")
        response_data = {
            "status": "error",
            "reachable": False,
            "message": f"Unexpected error: {str(e)}",
            "local_version": pc_app.socket_client.stm32_firmware_version or "Unknown",
            "update_available": False
        }
    
    rsp = make_response(response_data)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp


@flask_app.route("/api/v1/mcu_prod_check.json", methods=['GET'])
@api_auth_required
def mcu_prod_check():
    """
    Check production server for MCU firmware updates.
    Returns production server status and MCU firmware information.
    """
    if not is_photonicat2:
        return _mcu_unsupported_response()
    try:
        # Get current local MCU version
        local_version = pc_app.socket_client.stm32_firmware_version or "Unknown"
        
        # Production server URL
        prod_base = "https://dl.photonicat.com/firmware/pcat2_mcu"
        prod_url = f"{prod_base}/latest_img.json"
        pcat_logger.logger.info(f"Checking MCU production updates from: {prod_url}")
        
        try:
            data = robust_urlopen(prod_url, timeout=20)
            remote_data = json.loads(data.decode('utf-8'))
            pcat_logger.logger.info(f"Successfully fetched MCU production data: {remote_data.get('version', 'Unknown')}")
        except Exception as e:
            pcat_logger.logger.error(f"Failed to fetch MCU production info: {str(e)}")
            raise
        
        # Extract information from remote JSON  
        remote_version = remote_data.get("version", "")
        firmware_url = remote_data.get("url", "")
        sha256_hash = _mcu_remote_sha256(remote_data)
        build_date = _mcu_remote_build_date(remote_data)

        # Make URL absolute if it's relative
        if firmware_url and not firmware_url.startswith("http"):
            firmware_url = f"{prod_base}/{firmware_url}"

        # An update is only offered when the remote firmware is strictly newer.
        # When local is newer than remote (e.g. a locally-built image ahead of
        # the published stable one) this is False, so the UI shows "up to date"
        # instead of a spurious "update available".
        newer_available = _mcu_remote_is_newer(local_version, remote_version)
        update_available = newer_available

        # Version dropdown: latest + a few previous stable releases for
        # downgrades.
        versions = _mcu_versions_with_latest(
            _mcu_fetch_versions(prod_base, MCU_PROD_MAX_BACK),
            {
                "version": remote_version,
                "firmware_url": firmware_url,
                "sha256": sha256_hash,
                "build_date": build_date,
                "message": remote_data.get("message", "") or "",
            },
            MCU_PROD_MAX_BACK)

        response_data = {
            "status": "ok",
            "reachable": True,
            "versions": versions,
            "local_version": local_version,
            "local_build_date": _mcu_local_build_date(local_version),
            "prod_version": remote_version,
            "prod_firmware_url": firmware_url,
            "prod_sha256": sha256_hash,
            "prod_build_date": build_date,
            "update_available": update_available,
            "newer_available": newer_available,
            "same_version": local_version == remote_version,
            "local_is_newer": bool(remote_version) and local_version != remote_version and not newer_available
        }
        
    except urllib.error.URLError as e:
        pcat_logger.logger.warning(f"Production server unreachable for MCU check: {e}")
        response_data = {
            "status": "unreachable",
            "reachable": False,
            "message": f"Production server unreachable: {str(e)}",
            "local_version": pc_app.socket_client.stm32_firmware_version or "Unknown",
            "update_available": False
        }
    except json.JSONDecodeError as e:
        pcat_logger.logger.error(f"Invalid JSON from production server MCU check: {e}")
        response_data = {
            "status": "error",
            "reachable": False,
            "message": f"Invalid JSON response: {str(e)}",
            "local_version": pc_app.socket_client.stm32_firmware_version or "Unknown",
            "update_available": False
        }
    except Exception as e:
        pcat_logger.logger.error(f"Unexpected error checking production server MCU: {e}")
        response_data = {
            "status": "error",
            "reachable": False,
            "message": f"Unexpected error: {str(e)}",
            "local_version": pc_app.socket_client.stm32_firmware_version or "Unknown",
            "update_available": False
        }
    
    rsp = make_response(response_data)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp


# MCU Update Status and Management
mcu_update_status = {
    "active": False,
    "stage": "",  # "download", "verify", "update", "complete", "error"
    "progress": 0,
    "message": "",
    "firmware_url": "",
    "start_time": 0,
    "error": "",
    "debug_logs": []  # Store debug log messages
}

@flask_app.route("/api/v1/mcu_update_status.json", methods=['GET'])
@api_auth_required
def mcu_update_status_api():
    """Get current MCU update status"""
    response_data = dict(mcu_update_status)
    response_data["status"] = "ok"
    
    rsp = make_response(response_data)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp


@flask_app.route("/api/v1/mcu_update.json", methods=['POST'])
@api_auth_required
def mcu_update_start():
    """Start MCU firmware update process"""
    global mcu_update_status

    if not is_photonicat2:
        return _mcu_unsupported_response(400)

    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No data provided"}), 400
            
        firmware_url = data.get("firmware_url")
        if not firmware_url:
            return jsonify({"status": "error", "message": "Firmware URL required"}), 400
            
        # A system firmware upgrade and an MCU update must never run at the
        # same time — both touch the device and would collide. Refuse the MCU
        # update if a firmware upgrade is being prepared or is already running.
        if firmware_upgrade_status.get("active") or firmware_upgrade_thread is not None:
            return jsonify({"status": "error",
                            "message": "A firmware upgrade is in progress"}), 409

        # Check if update is already in progress
        if mcu_update_status["active"]:
            return jsonify({"status": "error", "message": "MCU update already in progress"}), 409
            
        # Start the update process in a background thread
        import threading
        import time
        import hashlib
        import tempfile
        import os
        import subprocess
        
        def add_debug_log(message):
            """Add debug message to logs with timestamp"""
            timestamp = time.strftime("%H:%M:%S")
            log_entry = f"[{timestamp}] {message}"
            mcu_update_status["debug_logs"].append(log_entry)
            # Keep only last 50 log entries to prevent memory issues
            if len(mcu_update_status["debug_logs"]) > 50:
                mcu_update_status["debug_logs"] = mcu_update_status["debug_logs"][-50:]
            pcat_logger.logger.info(f"MCU Update: {message}")

        def update_thread():
            global mcu_update_status
            
            try:
                # Initialize status
                mcu_update_status.update({
                    "active": True,
                    "stage": "download",
                    "progress": 0,
                    "message": "Downloading MCU firmware...",
                    "firmware_url": firmware_url,
                    "start_time": time.time(),
                    "error": "",
                    "debug_logs": []
                })
                
                add_debug_log(f"Starting MCU update process")
                add_debug_log(f"Firmware URL: {firmware_url}")
                add_debug_log(f"Expected SHA256: {data.get('sha256', 'Not provided')}")
                
                # Download firmware
                temp_dir = tempfile.mkdtemp(prefix="mcu_update_")
                firmware_path = os.path.join(temp_dir, "mcu_firmware.bin")
                add_debug_log(f"Created temp directory: {temp_dir}")
                add_debug_log(f"Firmware will be saved to: {firmware_path}")
                
                # Download with progress tracking
                add_debug_log("Starting firmware download...")
                with urllib.request.urlopen(firmware_url, timeout=300) as response:
                    total_size = int(response.headers.get('Content-Length', 0))
                    downloaded = 0
                    add_debug_log(f"Firmware size: {total_size} bytes ({total_size//1024}KB)")
                    
                    with open(firmware_path, 'wb') as f:
                        while True:
                            chunk = response.read(8192)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            if total_size > 0:
                                progress = int((downloaded / total_size) * 50)  # 50% for download
                                mcu_update_status["progress"] = progress
                                mcu_update_status["message"] = f"Downloading... {downloaded//1024}KB/{total_size//1024}KB"
                
                add_debug_log(f"Download completed: {downloaded} bytes downloaded")
                
                # Verify SHA256 if provided
                mcu_update_status.update({
                    "stage": "verify",
                    "progress": 60,
                    "message": "Verifying firmware integrity..."
                })
                
                expected_sha256 = data.get("sha256")
                if expected_sha256:
                    add_debug_log("Starting SHA256 verification...")
                    with open(firmware_path, 'rb') as f:
                        file_hash = hashlib.sha256(f.read()).hexdigest()
                    
                    add_debug_log(f"Expected SHA256: {expected_sha256}")
                    add_debug_log(f"Actual SHA256:   {file_hash}")
                    
                    if file_hash.lower() != expected_sha256.lower():
                        add_debug_log("❌ SHA256 verification FAILED!")
                        raise Exception(f"SHA256 mismatch: expected {expected_sha256}, got {file_hash}")
                    else:
                        add_debug_log("✅ SHA256 verification PASSED!")
                else:
                    add_debug_log("⚠️  No SHA256 provided - skipping verification")
                
                # Execute MCU update
                mcu_update_status.update({
                    "stage": "update",
                    "progress": 70,
                    "message": "Updating MCU firmware... DO NOT POWER OFF!"
                })
                
                # Determine if we need force flag (for dev server same version reinstalls)
                use_force_flag = False
                if firmware_url:
                    # Check if this is a dev server URL (local network). Derive the
                    # dev host from the configured dev_server_base so this tracks
                    # --dev-server-base instead of a hardcoded IP.
                    from urllib.parse import urlparse as _urlparse
                    _dev_host = _urlparse(pcat_config.dev_server_base).hostname or ""
                    is_dev_server = ((_dev_host and _dev_host in firmware_url)
                                     or "localhost" in firmware_url
                                     or "127.0.0.1" in firmware_url)
                    
                    # For dev server, always use force flag to allow same version reinstalls
                    if is_dev_server:
                        use_force_flag = True
                        add_debug_log("🔧 Dev server detected - using force flag (-F)")
                    else:
                        add_debug_log("🏭 Production server - standard update mode")
                
                # Run pcat-pmu-updater with appropriate flags
                cmd = ["/usr/bin/pcat-pmu-updater", firmware_path]
                if use_force_flag:
                    cmd.append("-F")
                
                add_debug_log(f"Executing command: {' '.join(cmd)}")
                add_debug_log("⚠️  DO NOT POWER OFF DURING THIS STEP!")
                
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                
                add_debug_log(f"Command return code: {result.returncode}")
                if result.stdout:
                    add_debug_log(f"Command stdout: {result.stdout.strip()}")
                if result.stderr:
                    add_debug_log(f"Command stderr: {result.stderr.strip()}")
                
                if result.returncode != 0:
                    add_debug_log("❌ MCU update command FAILED!")
                    raise Exception(f"MCU update failed: {result.stderr}")
                else:
                    add_debug_log("✅ MCU update command SUCCESS!")
                
                # Complete
                add_debug_log("🎉 MCU firmware update completed successfully!")
                mcu_update_status.update({
                    "stage": "complete",
                    "progress": 100,
                    "message": "MCU firmware updated successfully! System will maintain power.",
                    "active": False
                })
                
                # Query new MCU version
                add_debug_log("Querying new MCU version...")
                time.sleep(2)
                pc_app.socket_client.query_stm32_version()
                
                # Clean up
                add_debug_log("Cleaning up temporary files...")
                try:
                    os.remove(firmware_path)
                    add_debug_log(f"Removed: {firmware_path}")
                    os.rmdir(temp_dir)
                    add_debug_log(f"Removed: {temp_dir}")
                except Exception as cleanup_error:
                    add_debug_log(f"⚠️  Cleanup warning: {cleanup_error}")
                
                add_debug_log("✅ MCU update process completed successfully!")
                    
            except Exception as e:
                error_msg = str(e)
                add_debug_log(f"❌ FATAL ERROR: {error_msg}")
                pcat_logger.logger.error(f"MCU update failed: {e}")
                mcu_update_status.update({
                    "stage": "error",
                    "progress": 0,
                    "message": f"Update failed: {error_msg}",
                    "error": error_msg,
                    "active": False
                })
                
                # Clean up on error
                add_debug_log("Cleaning up after error...")
                try:
                    if 'firmware_path' in locals():
                        os.remove(firmware_path)
                        add_debug_log(f"Cleaned up: {firmware_path}")
                    if 'temp_dir' in locals():
                        os.rmdir(temp_dir)
                        add_debug_log(f"Cleaned up: {temp_dir}")
                except Exception as cleanup_error:
                    add_debug_log(f"⚠️  Error cleanup warning: {cleanup_error}")
        
        # Start update thread
        thread = threading.Thread(target=update_thread, daemon=True)
        thread.start()
        
        return jsonify({
            "status": "ok", 
            "message": "MCU update started",
            "update_id": str(int(time.time()))
        })
        
    except Exception as e:
        pcat_logger.logger.error(f"Failed to start MCU update: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@flask_app.route("/screen")
def screen_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    c_locale = current_locale()
    locales = _locales.get_locales("screen", c_locale)
    locales.update(base_locales())

    return render_template(nt.screen_file_name, locales = locales)


@flask_app.route("/sms")
def sms_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    c_locale = current_locale()
    locales = _locales.get_locales("sms", c_locale)
    locales.update(base_locales())

    return render_template(nt.sms_file_name, locales = locales)

@flask_app.route("/api/v1/screen/user_config.json", methods=['GET'])
@api_auth_required
def api_screen_user_config():
    screen_config = pc_app.screen_client.get_user_config()
    rsp = make_response(screen_config)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/screen/user_config.json", methods=['POST'])
@api_auth_required
def api_screen_user_config_post():
    p_data = request.get_json()
    pc_app.screen_client.save_user_config(p_data)
    response_json = {}
    response_json["status"] = "ok"
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/screen/default_config.json", methods=['GET'])
@api_auth_required
def api_screen_default_config():
    screen_config = pc_app.screen_client.get_default_config()
    rsp = make_response(screen_config)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/screen/custom_metrics/status", methods=['GET'])
@api_auth_required
def api_screen_custom_metrics_status():
    status = pc_app.screen_client.get_custom_metrics_status()
    if status is None:
        status = {"sources": []}
    rsp = make_response(status)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/screen/custom_metrics/<source_name>/execute", methods=['POST'])
@api_auth_required
def api_screen_custom_metrics_execute(source_name):
    result = pc_app.screen_client.execute_custom_metrics_source(source_name)
    if result is None:
        result = {"status": "error"}
    rsp = make_response(result)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/screen/data.json", methods=['GET'])
@api_auth_required
def api_screen_data():
    """Live globalData values (data_key -> current value) from the display."""
    data = pc_app.screen_client.get_data()
    if data is None:
        data = {}
    rsp = make_response(data)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/screen/frame.png", methods=['GET'])
@api_auth_required
def api_screen_frame():
    screen_frame = pc_app.screen_client.get_frame()
    # make_response(None) raises, so a display service that is down used to
    # turn into a 500 with an empty body. Say 503 instead: the preview treats
    # any failed load as "no frame yet" and keeps its spinner up.
    if not screen_frame:
        return jsonify({"status": "error", "message": "Screen frame unavailable"}), 503
    rsp = make_response(screen_frame)
    rsp.headers['Content-Type'] = 'image/png'
    # Every poll uses a fresh ?ts=, so this never serves a stale frame — it
    # only lets the four consumers of one frameSrc (two <img>, two CSS slices)
    # share a single fetch instead of hitting the device four times.
    rsp.headers['Cache-Control'] = 'private, max-age=10'
    return rsp


@flask_app.route("/api/v1/modem/hook_script.json", methods=['GET'])
@api_auth_required
def api_modem_hook_script():
    script = pcat_config.load_sms_hook_script()
    response_json = {}
    response_json["status"] = "ok"
    response_json["script"] = script
    response_json["enabled"] = pcat_config.get_sms_hook_enabled()
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/modem/hook_script.json", methods=['POST'])
@api_auth_required
def api_modem_hook_script_post():
    p_data = request.get_json()
    script = p_data.get("script", "")
    enabled = p_data.get("enabled", False)
    print("api_modem_hook_script_post:", p_data)
    pcat_config.enable_sms_hook(enabled)
    pcat_config.save_sms_hook_script(script)
    response_json = {}
    response_json["status"] = "ok"
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/modem/hook_history_insert_test.json", methods=['POST'])
@dev_only
@api_auth_required
def api_modem_hook_history_insert_test():
    """Insert a test hook history record to verify database insertion works"""
    try:
        from database import connect_db
        import datetime
        modem_db_path = pc_app.sms_client.modem_db_path
        
        # Insert a test record
        current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        test_sql = f"""INSERT INTO sms_hook_history(
            sms_id, sender, message_content, message_timestamp,
            hook_script_content, hook_command, execution_start, execution_end,
            execution_duration_ms, return_code, stdout, stderr,
            success, error_message, created_at
        ) VALUES(
            999, 'TEST_SENDER', 'Test message content', '{current_time}',
            '#!/bin/sh\necho "test hook"', '/etc/pcat_hook_cmd.sh TEST_SENDER "Test message" "{current_time}"',
            '{current_time}', '{current_time}',
            100, 0, 'test hook output', '',
            1, '', '{current_time}'
        );"""
        
        connect_db(modem_db_path, [test_sql])
        
        response_json = {
            "status": "ok",
            "message": "Test hook history record inserted successfully",
            "db_path": modem_db_path,
            "test_sql": test_sql[:200] + "..."
        }
        
    except Exception as e:
        pcat_logger.logger.error(f"Failed to insert test hook history: {e}")
        response_json = {
            "status": "error",
            "message": f"Failed to insert test hook history: {str(e)}"
        }
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/modem/hook_history_create_table.json", methods=['POST'])
@dev_only
@api_auth_required
def api_modem_hook_history_create_table():
    """Manually create the hook history table"""
    try:
        from database import connect_db
        modem_db_path = pc_app.sms_client.modem_db_path
        
        # Create the hook history table
        hook_history_sql = """
CREATE TABLE IF NOT EXISTS sms_hook_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sms_id INTEGER,
    sender VARCHAR(64),
    message_content TEXT,
    message_timestamp DATETIME,
    hook_script_content TEXT,
    hook_command TEXT,
    execution_start DATETIME NOT NULL,
    execution_end DATETIME,
    execution_duration_ms INTEGER,
    return_code INTEGER,
    stdout TEXT,
    stderr TEXT,
    success INTEGER DEFAULT 0,
    error_message TEXT,
    created_at DATETIME NOT NULL,
    FOREIGN KEY (sms_id) REFERENCES sms (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_sms_hook_history_created_at ON sms_hook_history(created_at);
CREATE INDEX IF NOT EXISTS idx_sms_hook_history_sms_id ON sms_hook_history(sms_id);
CREATE INDEX IF NOT EXISTS idx_sms_hook_history_success ON sms_hook_history(success);
"""
        
        # Execute the creation commands
        commands = [cmd.strip() for cmd in hook_history_sql.split(';') if cmd.strip()]
        connect_db(modem_db_path, commands)
        
        response_json = {
            "status": "ok",
            "message": "Hook history table created successfully",
            "db_path": modem_db_path
        }
        
    except Exception as e:
        pcat_logger.logger.error(f"Failed to create hook history table: {e}")
        response_json = {
            "status": "error",
            "message": f"Failed to create hook history table: {str(e)}"
        }
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/modem/hook_history_test.json", methods=['GET'])
@dev_only
@api_auth_required
def api_modem_hook_history_test():
    """Test endpoint to check hook history table existence and basic data"""
    try:
        from database import get_rows
        modem_db_path = pc_app.sms_client.modem_db_path
        
        # Test table existence and get raw count
        try:
            count_result = get_rows(modem_db_path, "SELECT COUNT(*) as total FROM sms_hook_history")
            total_count = count_result[0]['total'] if count_result else 0
        except Exception as e:
            total_count = f"Error accessing table: {str(e)}"
        
        # Get recent records
        try:
            recent_records = get_rows(modem_db_path, "SELECT * FROM sms_hook_history ORDER BY created_at DESC LIMIT 5")
            recent_list = [dict(row) for row in recent_records] if recent_records else []
        except Exception as e:
            recent_list = f"Error getting records: {str(e)}"
            
        # Check for specific SMS ID 27 record
        try:
            sms_27_records = get_rows(modem_db_path, "SELECT * FROM sms_hook_history WHERE sms_id = 27")
            sms_27_list = [dict(row) for row in sms_27_records] if sms_27_records else []
        except Exception as e:
            sms_27_list = f"Error getting SMS 27 records: {str(e)}"
            
        # Check table schema
        try:
            schema_result = get_rows(modem_db_path, "PRAGMA table_info(sms_hook_history)")
            schema_list = [dict(row) for row in schema_result] if schema_result else []
        except Exception as e:
            schema_list = f"Error getting schema: {str(e)}"
            
        response_json = {
            "status": "ok",
            "db_path": modem_db_path,
            "total_records": total_count,
            "recent_records": recent_list,
            "sms_27_records": sms_27_list,
            "table_schema": schema_list
        }
        
    except Exception as e:
        pcat_logger.logger.error(f"Failed to test hook history: {e}")
        response_json = {
            "status": "error",
            "message": f"Failed to test hook history: {str(e)}"
        }
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/modem/hook_history.json", methods=['GET'])
@api_auth_required
def api_modem_hook_history():
    """Get hook execution history with pagination and filtering"""
    try:
        # Get query parameters
        page = int(request.args.get('page', 1))
        limit = min(int(request.args.get('limit', 50)), 200)  # Max 200 records per page
        success_filter = request.args.get('success')  # 'true', 'false', or None for all
        sender_filter = request.args.get('sender')  # Filter by sender phone number
        days_filter = request.args.get('days')  # Filter by days back
        
        offset = (page - 1) * limit
        
        # Build WHERE clause
        where_conditions = []
        where_params = []
        
        if success_filter == 'true':
            where_conditions.append("success = 1")
        elif success_filter == 'false':
            where_conditions.append("success = 0")
            
        if sender_filter:
            where_conditions.append("sender = ?")
            where_params.append(sender_filter)
            
        if days_filter:
            try:
                days = int(days_filter)
                where_conditions.append("datetime(execution_start) >= datetime('now', '-{} days')".format(days))
            except (ValueError, TypeError):
                pass  # Ignore invalid days filter
        
        where_clause = ""
        if where_conditions:
            where_clause = "WHERE " + " AND ".join(where_conditions)
        
        # Get total count
        count_sql = f"SELECT COUNT(*) as total FROM sms_hook_history {where_clause}"
        
        # Debug: Print the query details
        print(f"DEBUG API: where_clause='{where_clause}', limit={limit}, offset={offset}")
        print(f"DEBUG API: where_params={where_params}")
        
        # Get paginated results - Use parameterized queries
        simple_data_sql = f"""
        SELECT * FROM sms_hook_history 
        {where_clause}
        ORDER BY created_at DESC 
        LIMIT ? OFFSET ?
        """
        
        from database import get_rows_param
        modem_db_path = pc_app.sms_client.modem_db_path
        
        print(f"DEBUG API: Database path: {modem_db_path}")
        print(f"DEBUG API: Count SQL: {count_sql}")
        print(f"DEBUG API: Data SQL: {simple_data_sql}")
        
        # Execute count query with parameterized queries
        try:
            count_result = get_rows_param(modem_db_path, count_sql, where_params)
            total_count = count_result[0]['total'] if count_result else 0
            print(f"DEBUG API: Count result: {total_count}")
        except Exception as count_error:
            print(f"DEBUG API: Count query error: {count_error}")
            total_count = 0
        
        # Execute data query with parameterized queries
        try:
            # Add limit and offset parameters to the where_params
            data_params = where_params + [limit, offset]
            history_data = get_rows_param(modem_db_path, simple_data_sql, data_params)
            print(f"DEBUG API: Found {len(history_data) if history_data else 0} history records")
        except Exception as data_error:
            print(f"DEBUG API: Data query error: {data_error}")
            history_data = []
        
        # Convert rows to dict format
        history_list = []
        for row in history_data:
            row_dict = dict(row)
            # Format execution duration
            if row_dict.get('execution_duration_ms'):
                duration_ms = row_dict['execution_duration_ms']
                if duration_ms < 1000:
                    row_dict['execution_duration_formatted'] = f"{duration_ms}ms"
                elif duration_ms < 60000:
                    row_dict['execution_duration_formatted'] = f"{duration_ms/1000:.1f}s"
                else:
                    minutes = duration_ms // 60000
                    seconds = (duration_ms % 60000) // 1000
                    row_dict['execution_duration_formatted'] = f"{minutes}m {seconds}s"
            else:
                row_dict['execution_duration_formatted'] = "N/A"
            
            # Truncate long outputs for list view
            if row_dict.get('stdout') and len(row_dict['stdout']) > 200:
                row_dict['stdout_preview'] = row_dict['stdout'][:200] + "..."
            else:
                row_dict['stdout_preview'] = row_dict.get('stdout', '')
                
            if row_dict.get('stderr') and len(row_dict['stderr']) > 200:
                row_dict['stderr_preview'] = row_dict['stderr'][:200] + "..."
            else:
                row_dict['stderr_preview'] = row_dict.get('stderr', '')
            
            history_list.append(row_dict)
        
        # Calculate pagination info
        total_pages = (total_count + limit - 1) // limit
        
        response_json = {
            "status": "ok",
            "data": history_list,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total_count,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_prev": page > 1
            },
            "filters": {
                "success": success_filter,
                "sender": sender_filter,
                "days": days_filter
            }
        }
        
    except Exception as e:
        pcat_logger.logger.error(f"Failed to get hook history: {e}")
        response_json = {
            "status": "error",
            "message": f"Failed to get hook history: {str(e)}",
            "data": [],
            "pagination": {"page": 1, "limit": limit, "total": 0, "total_pages": 0}
        }
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/modem/hook_history/<int:history_id>.json", methods=['GET'])
@api_auth_required
def api_modem_hook_history_detail(history_id):
    """Get detailed information for a specific hook execution"""
    try:
        from database import get_rows_param
        modem_db_path = pc_app.sms_client.modem_db_path
        
        sql = """
        SELECT h.*, s.from_num as original_sender, s.content as original_content, s.sent_at as original_timestamp
        FROM sms_hook_history h 
        LEFT JOIN sms s ON h.sms_id = s.id 
        WHERE h.id = ?
        """
        
        result = get_rows_param(modem_db_path, sql, [history_id])
        
        if not result:
            response_json = {
                "status": "error",
                "message": "Hook execution record not found"
            }
        else:
            row_dict = dict(result[0])
            
            # Format execution duration
            if row_dict.get('execution_duration_ms'):
                duration_ms = row_dict['execution_duration_ms']
                if duration_ms < 1000:
                    row_dict['execution_duration_formatted'] = f"{duration_ms}ms"
                elif duration_ms < 60000:
                    row_dict['execution_duration_formatted'] = f"{duration_ms/1000:.1f}s"
                else:
                    minutes = duration_ms // 60000
                    seconds = (duration_ms % 60000) // 1000
                    row_dict['execution_duration_formatted'] = f"{minutes}m {seconds}s"
            else:
                row_dict['execution_duration_formatted'] = "N/A"
            
            response_json = {
                "status": "ok",
                "data": row_dict
            }
    
    except Exception as e:
        pcat_logger.logger.error(f"Failed to get hook history detail: {e}")
        response_json = {
            "status": "error", 
            "message": f"Failed to get hook history detail: {str(e)}"
        }
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/modem/hook_history.json", methods=['DELETE'])
@api_auth_required  
def api_modem_hook_history_cleanup():
    """Cleanup old hook history records"""
    try:
        days_to_keep = int(request.args.get('days', 90))  # Default keep 90 days
        
        from database import connect_db
        modem_db_path = pc_app.sms_client.modem_db_path
        
        # Delete old records
        cleanup_sql = f"DELETE FROM sms_hook_history WHERE created_at < datetime('now', '-{days_to_keep} days')"
        connect_db(modem_db_path, [cleanup_sql])
        
        # Get remaining count
        from database import get_rows
        count_result = get_rows(modem_db_path, "SELECT COUNT(*) as total FROM sms_hook_history")
        remaining_count = count_result[0]['total'] if count_result else 0
        
        response_json = {
            "status": "ok",
            "message": f"Cleaned up hook history older than {days_to_keep} days",
            "remaining_records": remaining_count
        }
        
    except Exception as e:
        pcat_logger.logger.error(f"Failed to cleanup hook history: {e}")
        response_json = {
            "status": "error",
            "message": f"Failed to cleanup hook history: {str(e)}"
        }
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/cpu_scheduling.json", methods=['GET'])
@api_auth_required
def api_cpu_scheduling():
    response_json = {}
    response_json["status"] = "ok"
    # The toggle reflects the saved preference, not the live core state: with
    # the preference on and DC connected the big cores are deliberately back
    # online, and reporting hardware here would flip the switch off by itself.
    # cpu_scheduling_active is the live state, for the "currently on/off" hint.
    response_json["cpu_scheduling_enabled"] = pcat_config.get_saved_cpu_scheduling_enabled()
    response_json["cpu_scheduling_active"] = pcat_config.get_cpu_scheduling_enabled()
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/cpu_scheduling.json", methods=['POST'])
@api_auth_required
def api_cpu_scheduling_post():
    p_data = request.get_json()
    cpu_scheduling_enabled = p_data.get("cpu_scheduling_enabled", False)
    pcat_config.set_cpu_scheduling_enabled(cpu_scheduling_enabled)
    response_json = {}
    response_json["status"] = "ok"
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/fan_speed.json", methods=['GET'])
@api_auth_required
def api_fan_speed():
    response_json = {}
    response_json["status"] = "ok"
    response_json["fan_speed"] = pcat_config.get_fan_speed()
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/fan_speed_actual.json", methods=['GET'])
@api_auth_required
def api_fan_speed_actual():
    response_json = {}
    response_json["status"] = "ok"
    response_json["fan_speed_rpm"] = pcat_config.get_actual_fan_speed()
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/sms_limit_for_screen.json", methods=['GET'])
@api_auth_required
def api_sms_limit_for_screen():
    response_json = {}
    response_json["status"] = "ok"
    response_json["sms_limit_for_screen"] = pcat_config.get_sms_limit_for_screen()
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/sms_limit_for_screen.json", methods=['POST'])
@api_auth_required
def api_sms_limit_for_screen_post():
    p_data = request.get_json()
    limit = p_data.get("sms_limit_for_screen", 10)
    pcat_config.set_sms_limit_for_screen(limit)
    response_json = {}
    response_json["status"] = "ok"
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

def _sanitize_ip_sources(raw):
    """Coerce request data into a clean list of {url, parser}, dropping blanks."""
    sources = []
    if not isinstance(raw, list):
        return sources
    for item in raw:
        if isinstance(item, dict):
            url = str(item.get("url", "")).strip()
            parser = str(item.get("parser", "stdout")).strip() or "stdout"
        else:
            url = str(item).strip()
            parser = "stdout"
        if url:
            sources.append({"url": url, "parser": parser})
    return sources

@flask_app.route("/api/v1/public_ip_lookup.json", methods=['GET'])
@api_auth_required
def api_public_ip_lookup_get():
    cfg = pcat_config.get_user_config_key("public_ip_lookup", {}) or {}
    rsp = make_response(cfg if isinstance(cfg, dict) else {})
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/public_ip_lookup.json", methods=['POST'])
@api_auth_required
def api_public_ip_lookup_post():
    p_data = request.get_json() or {}
    cfg = {
        "user_agent": str(p_data.get("user_agent", "")).strip(),
        "ipv4": _sanitize_ip_sources(p_data.get("ipv4")),
        "ipv6": _sanitize_ip_sources(p_data.get("ipv6")),
    }
    pcat_config.set_user_config_key("public_ip_lookup", cfg)
    response_json = {"status": "ok"}
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/fan_speed.json", methods=['POST'])
@api_auth_required
def api_fan_speed_post():
    p_data = request.get_json(silent=True) or {}
    user_max_speed = p_data.get("fan_speed", 5)

    # Persist preference (and apply direct hardware write). set_fan_speed
    # saves the JSON first so a later smart-mode recalculation or a sysfs
    # glitch cannot leave the preference at the default 5 (issue #107).
    pcat_config.set_fan_speed(user_max_speed)
    # Re-read the clamped value that was actually saved
    user_max_speed = pcat_config.get_fan_speed()

    # Get current fan mode
    fan_mode = pcat_config.get_fan_mode()

    # If in manual mode, hardware already has the user speed from set_fan_speed.
    # If in smart mode, re-apply the smart algorithm with user_max_speed as cap
    # (overwrites the direct write with the temperature-based speed).
    if fan_mode == "manual":
        pcat_logger.logger.info(f"[Manual Fan] Set fan speed to {user_max_speed}")
    else:
        # Apply smart fan control algorithm using current temperature
        try:
            temperature = pc_app.socket_client.board_temperature or 25  # Default to 25°C if unavailable

            # Apply smart algorithm with current mode
            # Note: set_fan_speed_smart now calls is_device_charging() internally
            actual_speed = pcat_config.set_fan_speed_smart(temperature, user_max_speed, fan_mode)

            # Get charging status for logging only
            is_charging = pcat_config.is_device_charging()
            pcat_logger.logger.info(f"[Smart Fan] Manual trigger: mode={fan_mode}, temp={temperature}°C, charging={is_charging}, user_max={user_max_speed}, actual_speed={actual_speed}")

        except Exception as e:
            # Fallback to manual control if smart control fails
            pcat_logger.logger.warning(f"Smart fan control failed, using manual control: {e}")
            pcat_config.set_fan_speed_direct(user_max_speed)

    response_json = {}
    response_json["status"] = "ok"
    response_json["fan_speed"] = user_max_speed
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/fan_mode.json", methods=['GET'])
@api_auth_required
def api_fan_mode():
    response_json = {}
    response_json["status"] = "ok"
    response_json["fan_mode"] = pcat_config.get_fan_mode()
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/fan_mode.json", methods=['POST'])
@api_auth_required
def api_fan_mode_post():
    try:
        p_data = request.get_json()
        fan_mode = p_data.get("fan_mode", "smart_quiet")

        # Validate fan mode
        valid_modes = ['manual', 'smart_quiet', 'smart_cool']
        if fan_mode not in valid_modes:
            response_json = {
                "status": "error",
                "message": f"Invalid fan mode: {fan_mode}"
            }
            rsp = make_response(response_json)
            rsp.headers['Content-Type'] = 'application/json'
            return rsp

        # Save fan mode
        pcat_config.set_fan_mode(fan_mode)

        # If switching to manual mode, apply current user fan speed
        # If switching to smart mode, trigger smart algorithm
        if fan_mode == "manual":
            user_speed = pcat_config.get_fan_speed()
            pcat_config.set_fan_speed_direct(user_speed)
            pcat_logger.logger.info(f"[Fan Mode] Switched to manual mode, set speed to {user_speed}")
        else:
            # Trigger smart fan control immediately
            try:
                temperature = pc_app.socket_client.board_temperature or 25
                user_max_speed = pcat_config.get_fan_speed()
                # Note: set_fan_speed_smart now calls is_device_charging() internally
                actual_speed = pcat_config.set_fan_speed_smart(temperature, user_max_speed, fan_mode)
                pcat_logger.logger.info(f"[Fan Mode] Switched to {fan_mode}, actual_speed={actual_speed}")
            except Exception as e:
                pcat_logger.logger.warning(f"Failed to apply smart fan control on mode switch: {e}")

        response_json = {}
        response_json["status"] = "ok"
        rsp = make_response(response_json)
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

    except Exception as e:
        pcat_logger.logger.error(f"[Fan Mode] Error in api_fan_mode_post: {e}")
        response_json = {
            "status": "error",
            "message": str(e)
        }
        rsp = make_response(response_json)
        rsp.headers['Content-Type'] = 'application/json'
        return rsp

@flask_app.route("/api/v1/screen/restart", methods=['POST'])
@api_auth_required
def api_screen_restart():
    res = pc_app.screen_client.restart_screen()
    rsp = make_response(res)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/screen/reset", methods=['POST'])
@api_auth_required
def api_screen_reset():
    res = pc_app.screen_client.reset_config()
    rsp = make_response(res)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/screen/short_click", methods=['POST'])
@api_auth_required
def api_screen_short_click():
    res = pc_app.screen_client.short_click()
    rsp = make_response(res)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

# Voice Call API Endpoints
@flask_app.route("/api/v1/voice_call/make", methods=['POST'])
@api_auth_required  
def api_voice_call_make():
    try:
        p_data = request.get_json()
        number = p_data.get("number", "").strip()
        
        if not number:
            return make_response({
                "success": False,
                "error": "Phone number is required"
            }), 400
        
        # Use pysms_tool to make the call
        success = pc_app.sms_client.make_voice_call(number)
        
        if success:
            # No history row here: pc_call_log writes one when the call ends,
            # by which point it knows whether it connected and for how long.
            # make_voice_call() has already told the tracker the number.
            response_json = {
                "success": True,
                "message": f"Call initiated to {number}"
            }
        else:
            response_json = {
                "success": False,
                "error": "Failed to initiate call"
            }
            
    except Exception as e:
        pcat_logger.logger.error(f"Error making voice call: {str(e)}")
        response_json = {
            "success": False,
            "error": str(e)
        }
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/answer", methods=['POST'])
@api_auth_required
def api_voice_call_answer():
    try:
        # Silence the ring buzzer before even touching the modem, so pickup
        # feels instant.
        import pc_ringer
        pc_ringer.ringer_service.stop()
        # Use pysms_tool to answer the call
        success = pc_app.sms_client.answer_call()
        
        if success:
            response_json = {
                "success": True,
                "message": "Call answered successfully"
            }
        else:
            response_json = {
                "success": False,
                "error": "Failed to answer call"
            }
            
    except Exception as e:
        pcat_logger.logger.error(f"Error answering voice call: {str(e)}")
        response_json = {
            "success": False,
            "error": str(e)
        }
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/hangup", methods=['POST'])
@api_auth_required
def api_voice_call_hangup():
    try:
        # Declining an incoming call must silence the buzzer immediately.
        import pc_ringer
        pc_ringer.ringer_service.stop()
        # Use pysms_tool to hang up the call
        success = pc_app.sms_client.hangup_call()
        call_audio_bridge.stop()
        
        if success:
            response_json = {
                "success": True,
                "message": "Call ended successfully"
            }
        else:
            response_json = {
                "success": False,
                "error": "Failed to hang up call"
            }
            
    except Exception as e:
        pcat_logger.logger.error(f"Error hanging up voice call: {str(e)}")
        response_json = {
            "success": False,
            "error": str(e)
        }
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

def _sync_auto_record(has_active, calls):
    """Auto-record every call when the user has opted in (default off).
    Driven by the phone page's 2s status poll -- calls can only be placed or
    answered from that page, so it is always polling while a call exists.
    Also closes any recording (manual or auto) once the call ends."""
    try:
        rec = call_audio_bridge.record_status()
        if has_active:
            # belt-and-suspenders: a live call means ringing is over
            import pc_ringer
            pc_ringer.ringer_service.stop()
            if not rec["recording"] and \
                    bool(pcat_config.get_user_config_key("phone_auto_record", False)):
                number = calls[0].get("number", "") if calls else ""
                call_audio_bridge.start_record(number)
        elif rec["recording"]:
            call_audio_bridge.stop_record()
    except Exception as e:
        pcat_logger.logger.error(f"auto-record sync failed: {e}")

@flask_app.route("/api/v1/voice_call/status", methods=['GET'])
@api_auth_required
def api_voice_call_status():
    try:
        # Use pysms_tool to get call status
        call_status = pc_app.sms_client.get_call_status()
        calls = call_status.get("calls", [])
        _sync_auto_record(len(calls) > 0, calls)
        import pc_call_log
        pc_call_log.call_session_tracker.on_status(calls)

        response_json = {
            "success": True,
            "calls": calls,
            "has_active_calls": len(calls) > 0,
            # Seconds since the call actually connected, measured here rather
            # than from whenever the browser's poll first saw it -- that was
            # up to a poll interval short, and reset on every page reload.
            "call_seconds": pc_call_log.call_session_tracker.call_seconds(),
            "recording": call_audio_bridge.record_status()
        }

    except Exception as e:
        pcat_logger.logger.error(f"Error getting voice call status: {str(e)}")
        response_json = {
            "success": False,
            "error": str(e),
            "calls": []
        }
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/audio_path", methods=['POST'])
@api_auth_required
def api_voice_call_audio_path():
    try:
        p_data = request.get_json()
        path = p_data.get("path", 1)
        
        # Use pysms_tool to set audio path
        success = pc_app.sms_client.set_audio_path(path)
        
        if success:
            response_json = {
                "success": True,
                "message": f"Audio path set to {path}"
            }
        else:
            response_json = {
                "success": False,
                "error": "Failed to set audio path"
            }
            
    except Exception as e:
        pcat_logger.logger.error(f"Error setting audio path: {str(e)}")
        response_json = {
            "success": False,
            "error": str(e)
        }
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/history", methods=['GET'])
@api_auth_required
def api_voice_call_history():
    try:
        # Get call history from SMS client
        history = pc_app.sms_client.get_call_history()
        for row in history:
            row["number"] = intl_phone_number(row.get("number"))

        response_json = {
            "success": True,
            "history": history
        }
            
    except Exception as e:
        pcat_logger.logger.error(f"Error getting call history: {str(e)}")
        response_json = {
            "success": False,
            "error": str(e),
            "history": []
        }
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/history", methods=['DELETE'])
@api_auth_required
def api_voice_call_history_clear():
    try:
        # Clear call history
        success = pc_app.sms_client.clear_call_history()
        
        if success:
            response_json = {
                "success": True,
                "message": "Call history cleared successfully"
            }
        else:
            response_json = {
                "success": False,
                "error": "Failed to clear call history"
            }
            
    except Exception as e:
        pcat_logger.logger.error(f"Error clearing call history: {str(e)}")
        response_json = {
            "success": False,
            "error": str(e)
        }
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/history/<call_id>", methods=['DELETE'])
@api_auth_required
def api_voice_call_history_delete(call_id):
    try:
        # Delete specific call record
        success = pc_app.sms_client.delete_call_record(call_id)
        
        if success:
            response_json = {
                "success": True,
                "message": "Call record deleted successfully"
            }
        else:
            response_json = {
                "success": False,
                "error": "Failed to delete call record"
            }
            
    except Exception as e:
        pcat_logger.logger.error(f"Error deleting call record: {str(e)}")
        response_json = {
            "success": False,
            "error": str(e)
        }
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

# --- Phone address book ---------------------------------------------------

@flask_app.route("/api/v1/voice_call/contacts", methods=['GET'])
@api_auth_required
def api_voice_call_contacts():
    try:
        response_json = {
            "success": True,
            "contacts": pc_app.sms_client.get_contacts()
        }
    except Exception as e:
        pcat_logger.logger.error(f"Error getting contacts: {str(e)}")
        response_json = {"success": False, "error": str(e), "contacts": []}

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

def _validate_contact_fields(name, number):
    """Shared add/update validation. Returns an error string or None."""
    if not name or not number:
        return "Name and number are required"
    if len(name) > 64 or len(number) > 32:
        return "Name or number too long"
    if not re.match(r'^\+?[0-9\s\-\(\)\*#]{2,32}$', number):
        return "Invalid phone number format"
    return None

@flask_app.route("/api/v1/voice_call/contacts", methods=['POST'])
@api_auth_required
def api_voice_call_contacts_add():
    try:
        p_data = request.get_json()
        name = (p_data.get("name") or "").strip()
        number = (p_data.get("number") or "").strip()

        error = _validate_contact_fields(name, number)
        if error:
            return make_response({"success": False, "error": error}), 400

        success = pc_app.sms_client.add_contact(name, number)
        if success:
            response_json = {"success": True, "message": "Contact added"}
        else:
            response_json = {"success": False, "error": "Failed to add contact"}
    except Exception as e:
        pcat_logger.logger.error(f"Error adding contact: {str(e)}")
        response_json = {"success": False, "error": str(e)}

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/contacts/<contact_id>", methods=['PUT'])
@api_auth_required
def api_voice_call_contacts_update(contact_id):
    try:
        p_data = request.get_json()
        name = (p_data.get("name") or "").strip()
        number = (p_data.get("number") or "").strip()

        error = _validate_contact_fields(name, number)
        if error:
            return make_response({"success": False, "error": error}), 400

        success = pc_app.sms_client.update_contact(contact_id, name, number)
        if success:
            response_json = {"success": True, "message": "Contact updated"}
        else:
            response_json = {"success": False, "error": "Failed to update contact"}
    except Exception as e:
        pcat_logger.logger.error(f"Error updating contact: {str(e)}")
        response_json = {"success": False, "error": str(e)}

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/contacts/<contact_id>", methods=['DELETE'])
@api_auth_required
def api_voice_call_contacts_delete(contact_id):
    try:
        success = pc_app.sms_client.delete_contact(contact_id)
        if success:
            response_json = {"success": True, "message": "Contact deleted"}
        else:
            response_json = {"success": False, "error": "Failed to delete contact"}
    except Exception as e:
        pcat_logger.logger.error(f"Error deleting contact: {str(e)}")
        response_json = {"success": False, "error": str(e)}

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

# --- Call recording -------------------------------------------------------
# Recordings live in pc_call_audio.RECORD_DIR as call_<stamp>[_<number>].wav
# (in-flight) or .opus (compressed after the call); voicemail messages use a
# vm_ prefix. The name pattern below is the only thing ever joined to the
# directory, so no path traversal.

RECORDING_NAME_RE = re.compile(
    r'^(?P<kind>call|vm)_(?P<stamp>[0-9]{8}-[0-9]{6})'
    r'(_(?P<num>[0-9+]{1,20}))?\.(wav|opus)$')

@flask_app.route("/api/v1/voice_call/record/config", methods=['GET'])
@api_auth_required
def api_voice_call_record_config():
    rsp = make_response({
        "success": True,
        "auto_record": bool(pcat_config.get_user_config_key("phone_auto_record", False))
    })
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/record/config", methods=['POST'])
@api_auth_required
def api_voice_call_record_config_set():
    try:
        p_data = request.get_json() or {}
        enabled = bool(p_data.get("auto_record", False))
        pcat_config.set_user_config_key("phone_auto_record", enabled)
        response_json = {"success": True, "auto_record": enabled}
    except Exception as e:
        pcat_logger.logger.error(f"Error saving record config: {str(e)}")
        response_json = {"success": False, "error": str(e)}

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/record/start", methods=['POST'])
@api_auth_required
def api_voice_call_record_start():
    number = ""
    try:
        call_status = pc_app.sms_client.get_call_status()
        calls = call_status.get("calls", [])
        if calls:
            number = calls[0].get("number", "") or ""
    except Exception:
        pass
    name = call_audio_bridge.start_record(number)
    if name:
        response_json = {"success": True, "file": name}
    else:
        response_json = {"success": False, "error": "Failed to start recording"}

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/record/stop", methods=['POST'])
@api_auth_required
def api_voice_call_record_stop():
    call_audio_bridge.stop_record()
    rsp = make_response({"success": True})
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/recordings", methods=['GET'])
@api_auth_required
def api_voice_call_recordings():
    import pc_call_audio
    import pc_voicemail
    recordings = []
    try:
        rec_now = call_audio_bridge.record_status()
        in_flight = rec_now.get("file") if rec_now.get("recording") else None
        vm_in_flight = pc_voicemail.voicemail_service.active_file
        rec_dir = pc_call_audio.RECORD_DIR
        for name in os.listdir(rec_dir):
            m = RECORDING_NAME_RE.match(name)
            if not m or name == in_flight or name == vm_in_flight:
                continue
            st = os.stat(os.path.join(rec_dir, name))
            try:
                nice_time = datetime.strptime(
                    m.group("stamp"), "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                nice_time = ""
            if name.endswith(".wav"):
                seconds = max(0, st.st_size - 44) / (16000 * 2.0)
            else:
                seconds = st.st_size / 3000.0   # ~24kbps opus
            import pc_transcribe
            recordings.append({
                "name": name,
                "kind": m.group("kind"),
                "number": m.group("num") or "",
                "time": nice_time,
                "size": st.st_size,
                "seconds": round(seconds, 1),
                "transcript": pc_transcribe.read_transcript(name),
                "transcribing": pc_transcribe.transcribe_service.is_pending(name)
            })
        # newest first regardless of call/vm prefix
        recordings.sort(key=lambda r: r["name"].split("_", 1)[1], reverse=True)
    except OSError:
        pass

    rsp = make_response({"success": True, "recordings": recordings})
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/recordings/<name>", methods=['GET'])
@api_auth_required
def api_voice_call_recording_get(name):
    import pc_call_audio
    if not RECORDING_NAME_RE.match(name):
        return make_response({"success": False, "error": "Invalid name"}), 400
    from flask import send_from_directory
    mimetype = "audio/ogg" if name.endswith(".opus") else "audio/wav"
    return send_from_directory(
        pc_call_audio.RECORD_DIR, name, mimetype=mimetype,
        as_attachment=(request.args.get("download") == "1"), conditional=True)

@flask_app.route("/api/v1/voice_call/recordings/<name>", methods=['DELETE'])
@api_auth_required
def api_voice_call_recording_delete(name):
    import pc_call_audio
    if not RECORDING_NAME_RE.match(name):
        return make_response({"success": False, "error": "Invalid name"}), 400
    try:
        os.unlink(os.path.join(pc_call_audio.RECORD_DIR, name))
        # transcript (if any) goes with the audio
        try:
            os.unlink(os.path.join(pc_call_audio.RECORD_DIR, name + ".txt"))
        except OSError:
            pass
        response_json = {"success": True}
    except OSError as e:
        response_json = {"success": False, "error": str(e)}

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

# --- Voicemail ------------------------------------------------------------
# Answering machine: pc_voicemail's monitor thread answers unanswered
# incoming calls, plays the greeting stored below, and records the caller
# into the shared recordings directory (vm_ prefix).

import pc_voicemail
# getattr, not attribute access: both services document their getter as
# returning None while the modem stack is still coming up, but these clients
# are only bound onto the app module partway through startup -- a bare
# pc_app.sms_client raises AttributeError until then, which surfaces as
# "voicemail: tick failed" instead of the quiet no-op the loops expect.
pc_voicemail.voicemail_service.start(lambda: getattr(pc_app, "sms_client", None))

# Incoming-call buzzer: event-driven off modem URCs (RING / NO CARRIER), so
# it starts within ~150ms of the network paging us and stops instantly on
# answer/decline (those routes call stop() before touching the modem).
import pc_ringer
import pc_modem_client as _pc_modem_mod
pc_ringer.ringer_service.start(lambda: getattr(pc_app, "socket_client", None))
_pc_modem_mod.register_urc_listener(pc_ringer.ringer_service.on_urc)

# Call history. Also URC-driven, so a call that rings while nobody has the
# phone page open still ends up in the list as a missed call.
import pc_call_log
pc_call_log.call_session_tracker.start(lambda: getattr(pc_app, "sms_client", None))
_pc_modem_mod.register_urc_listener(pc_call_log.call_session_tracker.on_urc)

@flask_app.route("/api/v1/voice_call/ringer/config", methods=['GET'])
@api_auth_required
def api_ringer_config():
    rsp = make_response({
        "success": True,
        "enabled": pc_ringer.ringer_service.enabled(),
        "melody": pc_ringer.ringer_service.melody(),
        "melodies": list(pc_ringer.RING_MELODIES.keys())
    })
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/ringer/config", methods=['POST'])
@api_auth_required
def api_ringer_config_set():
    try:
        p_data = request.get_json() or {}
        if "enabled" in p_data:
            pcat_config.set_user_config_key(
                "phone_ring_buzzer_enabled", bool(p_data.get("enabled")))
            if not p_data.get("enabled"):
                pc_ringer.ringer_service.stop()
        melody = p_data.get("melody")
        if melody is not None:
            if melody not in pc_ringer.RING_MELODIES:
                return make_response({"success": False,
                                      "error": "Unknown melody"}), 400
            pcat_config.set_user_config_key("phone_ring_melody", melody)
        response_json = {
            "success": True,
            "enabled": pc_ringer.ringer_service.enabled(),
            "melody": pc_ringer.ringer_service.melody()
        }
    except Exception as e:
        pcat_logger.logger.error(f"Error saving ringer config: {str(e)}")
        response_json = {"success": False, "error": str(e)}

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/ringer/silence", methods=['POST'])
@api_auth_required
def api_ringer_silence():
    """Silence the buzzer for the call that is ringing, without touching the
    call itself -- the caller keeps hearing ringback and voicemail still picks
    up on schedule. Distinct from declining, which hangs up on them."""
    pc_ringer.ringer_service.silence()
    rsp = make_response({"success": True})
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/ringer/preview", methods=['POST'])
@api_auth_required
def api_ringer_preview():
    p_data = request.get_json() or {}
    melody = p_data.get("melody", pc_ringer.ringer_service.melody())
    if pc_ringer.ringer_service.preview(melody):
        response_json = {"success": True}
    else:
        response_json = {"success": False, "error": "Preview unavailable"}
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

# --- Transcription (whisper.cpp on CPU) -----------------------------------

import pc_transcribe

@flask_app.route("/api/v1/voice_call/transcribe/status", methods=['GET'])
@api_auth_required
def api_transcribe_status():
    st = pc_transcribe.transcribe_service.status()
    st["success"] = True
    rsp = make_response(st)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/transcribe/config", methods=['POST'])
@api_auth_required
def api_transcribe_config_set():
    try:
        p_data = request.get_json() or {}
        model = p_data.get("model")
        if model is not None:
            if model not in pc_transcribe.MODELS:
                return make_response({"success": False,
                                      "error": "Unknown model"}), 400
            pcat_config.set_user_config_key("phone_transcribe_model", model)
        lang = p_data.get("lang")
        if lang is not None:
            if lang not in pc_transcribe.LANGS:
                return make_response({"success": False,
                                      "error": "Unknown language"}), 400
            pcat_config.set_user_config_key("phone_transcribe_lang", lang)
        if "auto_vm" in p_data:
            pcat_config.set_user_config_key(
                "phone_vm_transcribe", bool(p_data.get("auto_vm")))
        response_json = {"success": True}
    except Exception as e:
        pcat_logger.logger.error(f"Error saving transcribe config: {str(e)}")
        response_json = {"success": False, "error": str(e)}

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/transcribe/download", methods=['POST'])
@api_auth_required
def api_transcribe_download():
    p_data = request.get_json() or {}
    model = p_data.get("model", pc_transcribe.transcribe_service.model())
    ok, msg = pc_transcribe.transcribe_service.start_download(model)
    rsp = make_response({"success": ok,
                         "message" if ok else "error": msg})
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/transcribe/engine", methods=['POST'])
@api_auth_required
def api_transcribe_engine_install():
    ok, msg = pc_transcribe.transcribe_service.start_engine_download()
    rsp = make_response({"success": ok,
                         "message" if ok else "error": msg})
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/transcribe/run", methods=['POST'])
@api_auth_required
def api_transcribe_run():
    p_data = request.get_json() or {}
    name = p_data.get("name", "")
    if not RECORDING_NAME_RE.match(name):
        return make_response({"success": False, "error": "Invalid name"}), 400
    ok, msg = pc_transcribe.transcribe_service.enqueue(name)
    rsp = make_response({"success": ok,
                         "message" if ok else "error": msg})
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

# --- Text to speech (espeak-ng) -------------------------------------------

import pc_tts

@flask_app.route("/api/v1/voice_call/tts/status", methods=['GET'])
@api_auth_required
def api_tts_status():
    st = pc_tts.tts_service.status()
    st["success"] = True
    rsp = make_response(st)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/tts/engine", methods=['POST'])
@api_auth_required
def api_tts_engine_install():
    """Install a synthesizer. kind=espeak (3MB robotic), kokoro-engine +
    kokoro-model (neural, natural Chinese)."""
    p_data = request.get_json(silent=True) or {}
    kind = p_data.get("kind", "espeak")
    if kind == "kokoro-engine":
        ok, msg = pc_tts.tts_service.start_kokoro_engine_download()
    elif kind == "kokoro-model":
        ok, msg = pc_tts.tts_service.start_kokoro_model_download(
            p_data.get("model") or pc_tts.tts_service.kokoro_model())
    else:
        ok, msg = pc_tts.tts_service.start_download()
    rsp = make_response({"success": ok,
                         "message" if ok else "error": msg})
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/tts/config", methods=['POST'])
@api_auth_required
def api_tts_config_set():
    try:
        p_data = request.get_json() or {}
        engine = p_data.get("engine")
        if engine in ("espeak", "kokoro"):
            pcat_config.set_user_config_key("phone_tts_engine", engine)
        model = p_data.get("kokoro_model")
        if model in pc_tts.KOKORO_MODELS:
            pcat_config.set_user_config_key("phone_tts_kokoro_model", model)
        kvoice = p_data.get("kokoro_voice")
        if kvoice is not None and any(v["id"] == str(kvoice)
                                      for v in pc_tts.KOKORO_VOICES):
            pcat_config.set_user_config_key("phone_tts_kokoro_voice", str(kvoice))
        response_json = {"success": True}
    except Exception as e:
        pcat_logger.logger.error(f"Error saving tts config: {str(e)}")
        response_json = {"success": False, "error": str(e)}

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

def _tts_engine_and_voice(p_data):
    """Resolve which synthesizer and voice a synth request wants, remembering
    the choice. The two engines have unrelated voice namespaces (espeak uses
    language codes, kokoro numeric speaker ids), so they are stored apart."""
    engine = p_data.get("engine") or pc_tts.tts_service.engine()
    if engine not in ("espeak", "kokoro"):
        engine = "espeak"
    voice = p_data.get("voice")
    if engine == "kokoro":
        voice = str(voice) if voice is not None else pc_tts.tts_service.kokoro_voice()
        if any(v["id"] == voice for v in pc_tts.KOKORO_VOICES):
            pcat_config.set_user_config_key("phone_tts_kokoro_voice", voice)
    else:
        voice = voice or pc_tts.tts_service.voice()
        if any(v["id"] == voice for v in pc_tts.VOICES):
            pcat_config.set_user_config_key("phone_tts_voice", voice)
    return engine, voice

@flask_app.route("/api/v1/voice_call/tts/greeting", methods=['POST'])
@api_auth_required
def api_tts_greeting():
    """Synthesize typed text into the voicemail greeting -- the no-microphone
    (and no-HTTPS) way to set one up."""
    try:
        p_data = request.get_json() or {}
        text = p_data.get("text", "")
        engine, voice = _tts_engine_and_voice(p_data)

        os.makedirs(pc_voicemail.RECORD_DIR, exist_ok=True)
        tmp = pc_voicemail.GREETING_WAV + ".tts"
        ok, err = pc_tts.tts_service.synth(text, voice, tmp, engine=engine)
        if not ok:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return make_response({"success": False, "error": err}), 400
        os.replace(tmp, pc_voicemail.GREETING_WAV)
        response_json = {"success": True}
    except Exception as e:
        pcat_logger.logger.error(f"Error synthesizing greeting: {str(e)}")
        response_json = {"success": False, "error": str(e)}

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/tts/preview", methods=['POST'])
@api_auth_required
def api_tts_preview():
    """Synthesize text and stream the WAV straight back for the browser to
    play, without touching the stored greeting."""
    p_data = request.get_json() or {}
    text = p_data.get("text", "")
    engine, voice = _tts_engine_and_voice(p_data)
    # Per-request name: a shared /tmp/tts-preview.wav lets two overlapping
    # previews (or an impatient double-click) clobber and unlink each other's
    # file, so one of them 500s on a file that is no longer there.
    tmp = os.path.join("/tmp", "tts-preview-%d.wav" % threading.get_ident())
    ok, err = pc_tts.tts_service.synth(text, voice, tmp, engine=engine)
    if not ok:
        return make_response({"success": False, "error": err}), 400
    try:
        with open(tmp, "rb") as fh:
            data = fh.read()
    except OSError as e:
        return make_response({"success": False, "error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    rsp = make_response(data)
    rsp.headers['Content-Type'] = 'audio/wav'
    rsp.headers['Cache-Control'] = 'no-store'
    return rsp

@flask_app.route("/api/v1/voice_call/voicemail/config", methods=['GET'])
@api_auth_required
def api_voicemail_config():
    rsp = make_response({
        "success": True,
        "enabled": pc_voicemail.voicemail_service.enabled(),
        "answer_secs": pc_voicemail.voicemail_service.answer_secs(),
        "greeting": os.path.exists(pc_voicemail.GREETING_WAV)
    })
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/voicemail/config", methods=['POST'])
@api_auth_required
def api_voicemail_config_set():
    try:
        p_data = request.get_json() or {}
        if "enabled" in p_data:
            pcat_config.set_user_config_key(
                "phone_voicemail_enabled", bool(p_data.get("enabled")))
        if "answer_secs" in p_data:
            secs = min(max(int(p_data.get("answer_secs")), 3), 60)
            pcat_config.set_user_config_key("phone_voicemail_answer_secs", secs)
        response_json = {
            "success": True,
            "enabled": pc_voicemail.voicemail_service.enabled(),
            "answer_secs": pc_voicemail.voicemail_service.answer_secs()
        }
    except Exception as e:
        pcat_logger.logger.error(f"Error saving voicemail config: {str(e)}")
        response_json = {"success": False, "error": str(e)}

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/voicemail/greeting", methods=['POST'])
@api_auth_required
def api_voicemail_greeting_upload():
    """Accepts whatever the browser's MediaRecorder produced (webm/opus,
    ogg, wav, ...) and lets the device's ffmpeg turn it into the 16k mono
    WAV that gets played into the call."""
    try:
        data = request.get_data()
        if not data:
            return make_response({"success": False, "error": "Empty upload"}), 400
        if len(data) > 8 * 1024 * 1024:
            return make_response({"success": False, "error": "Too large"}), 400
        os.makedirs(pc_voicemail.RECORD_DIR, exist_ok=True)
        tmp_path = pc_voicemail.GREETING_WAV + ".upload"
        with open(tmp_path, "wb") as f:
            f.write(data)
        import subprocess
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", tmp_path,
             "-ar", "16000", "-ac", "1", "-t", str(pc_voicemail.GREETING_MAX_SECS),
             "-c:a", "pcm_s16le", pc_voicemail.GREETING_WAV],
            timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace")[:200]
            pcat_logger.logger.error(f"greeting convert failed: {err}")
            return make_response({"success": False,
                                  "error": "Audio conversion failed"}), 400
        response_json = {"success": True}
    except Exception as e:
        pcat_logger.logger.error(f"Error saving greeting: {str(e)}")
        response_json = {"success": False, "error": str(e)}

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/voicemail/greeting", methods=['GET'])
@api_auth_required
def api_voicemail_greeting_get():
    if not os.path.exists(pc_voicemail.GREETING_WAV):
        return make_response({"success": False, "error": "No greeting"}), 404
    from flask import send_from_directory
    return send_from_directory(
        pc_voicemail.RECORD_DIR, "greeting.wav",
        mimetype="audio/wav", conditional=True)

@flask_app.route("/api/v1/voice_call/voicemail/greeting", methods=['DELETE'])
@api_auth_required
def api_voicemail_greeting_delete():
    try:
        os.unlink(pc_voicemail.GREETING_WAV)
        response_json = {"success": True}
    except FileNotFoundError:
        response_json = {"success": True}
    except OSError as e:
        response_json = {"success": False, "error": str(e)}

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp


# --- In-call audio bridge -------------------------------------------------
# Raw 16kHz mono S16_LE PCM between the browser and the modem. The sample
# path itself now lives in the pcat-call-audio daemon on :9444 (src/callaudio)
# rather than here: it drives ALSA directly, which removes the arecord/aplay
# subprocesses, their 64KB pipes and the interpreter from between the modem
# and the socket. See pc_call_audio.py for the design notes and the fallback.
#
# Flask keeps everything else about a call, and mints the token the daemon
# checks -- it is Flask that knows the user is authenticated. See
# pc_audio_token.py.

@flask_app.route("/api/v1/voice_call/audio/token.json", methods=['GET'])
@api_auth_required
def api_voice_call_audio_token():
    """Hands the phone page a short-lived credential for the audio daemon,
    plus where to reach it. `port` is reported so the page does not have to
    hard-code it, and `available` lets the page fall back to the in-process
    bridge when the daemon is not running."""
    import pc_audio_token
    token = pc_audio_token.mint()
    return make_response({
        "success": True,
        "token": token,
        "port": AUDIO_DAEMON_PORT,
        "available": bool(token) and _audio_daemon_running(),
    })


AUDIO_DAEMON_PORT = 9444


def _audio_daemon_running():
    """True when something is listening on the daemon's port.

    Checked rather than assumed so a device where the daemon failed to start
    (no cert, no ALSA card, an old package) still gets working audio through
    the Python bridge instead of a silent failure on the phone page.
    """
    import socket
    s = socket.socket()
    s.settimeout(0.2)
    try:
        return s.connect_ex(("127.0.0.1", AUDIO_DAEMON_PORT)) == 0
    except OSError:
        return False
    finally:
        s.close()


@flask_app.route("/api/v1/voice_call/audio/status", methods=['GET'])
@api_auth_required
def api_voice_call_audio_status():
    st = call_audio_bridge.status()
    st["success"] = True
    rsp = make_response(st)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/voice_call/audio/stream", methods=['GET'])
@api_auth_required
def api_voice_call_audio_stream():
    if not call_audio_bridge.card_present():
        return make_response({"success": False,
                              "error": "no modem audio card"}, 503)
    rsp = Response(stream_with_context(call_audio_bridge.rx_stream()),
                   mimetype="application/octet-stream", direct_passthrough=True)
    rsp.headers['Cache-Control'] = 'no-store'
    rsp.headers['X-Accel-Buffering'] = 'no'
    return rsp

@flask_app.route("/api/v1/voice_call/audio/tx", methods=['POST'])
@api_auth_required
def api_voice_call_audio_tx():
    if not call_audio_bridge.card_present():
        return make_response({"success": False,
                              "error": "no modem audio card"}, 503)
    ok = call_audio_bridge.tx_write(request.get_data())
    return make_response({"success": ok})

@flask_app.route("/api/v1/voice_call/audio/stop", methods=['POST'])
@api_auth_required
def api_voice_call_audio_stop():
    call_audio_bridge.stop()
    return make_response({"success": True})

@flask_app.route("/api/v1/voice_call/incoming", methods=['GET'])
@api_auth_required
def api_voice_call_incoming():
    try:
        # Get incoming call status using pysms_tool
        incoming_call = pc_app.sms_client.get_incoming_call_status()

        if incoming_call:
            incoming_call = dict(incoming_call)
            incoming_call["number"] = intl_phone_number(incoming_call.get("number"))
            response_json = {
                "success": True,
                "incoming_call": incoming_call
            }
        else:
            # Return default inactive state
            response_json = {
                "success": True,
                "incoming_call": {
                    "active": False,
                    "number": None,
                    "ring_count": 0,
                    "duration": 0
                }
            }
            
    except Exception as e:
        pcat_logger.logger.error(f"Error checking incoming call status: {str(e)}")
        response_json = {
            "success": False,
            "error": str(e),
            "incoming_call": {
                "active": False,
                "number": None,
                "ring_count": 0,
                "duration": 0
            }
        }

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

# ── Terminal (Web Shell) ──────────────────────────────────────────────────

@flask_app.route("/terminal")
def terminal_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")
    if not pcat_config.get_user_config_key("terminal_enabled", True):
        return redirect("/")

    c_locale = current_locale()
    locales = _locales.get_locales("terminal", c_locale)

    locales.update(base_locales())
    # ?embed=1 (iframe pane) and ?popup=1 (console popup window) both want the
    # terminal alone: the nav header and footer are pure overhead in a small
    # window, and the terminal sizes itself off the space they would occupy.
    chromeless = (request.args.get("embed") == "1"
                  or request.args.get("popup") == "1")
    return render_template(nt.terminal_file_name, locales=locales,
                           chromeless=chromeless)


# Terminal: PTY output goes to an event-bus room, not to every client, so two
# open terminals do not read each other's shells.
# Room name = "terminal:" + session_id
_terminal_readers = {}       # session_id -> True (reader running)
_terminal_lock = threading.Lock()

def _terminal_room(session_id):
    return "terminal:" + session_id

def _terminal_reader_thread(app_context, session_id):
    """Background thread that reads PTY output and broadcasts to the room."""
    room = _terminal_room(session_id)
    pcat_logger.logger.info(f"Terminal reader ENTER for {session_id}")
    with app_context:
        sess = terminal_manager.get(session_id)
        if not sess:
            with _terminal_lock:
                _terminal_readers.pop(session_id, None)
            return
        try:
            while sess._alive:
                try:
                    data = sess.read(timeout=0.1)
                except Exception:
                    break
                if data is None:
                    break
                if data:
                    sess.append_scrollback(data)
                    publish('terminal_output',
                            {'data': data.decode('utf-8', errors='replace')},
                            room=room)
        except Exception as e:
            pcat_logger.logger.error(f"Terminal reader error for {session_id}: {e}")
        finally:
            with _terminal_lock:
                _terminal_readers.pop(session_id, None)
            publish('terminal_exit', {'session_id': session_id}, room=room)
            pcat_logger.logger.info(f"Terminal reader EXIT for {session_id}")


@bus_event
def terminal_open(message):
    """Client requests to open/attach a terminal session."""
    if not pcat_config.skip_auth and 'username' not in session:
        reply('terminal_exit', {'error': 'Not authenticated'})
        return

    session_id = message.get('session_id', 'default')
    cols = message.get('cols', 80)
    rows = message.get('rows', 24)
    # LXC console: the client may name a container (NOT a command) and the
    # session becomes `lxc-attach -n <name>` instead of a host shell. The
    # name gate keeps this from ever widening into an exec API.
    command = None
    lxc_name = message.get('lxc_attach', '')
    if lxc_name:
        from pcat_util import lxc_manager
        if not lxc_manager.NAME_RE.match(lxc_name):
            reply('terminal_exit', {'error': 'Invalid container name'})
            return
        command = ['lxc-attach', '-n', lxc_name]
    room = _terminal_room(session_id)
    pcat_logger.logger.info(f"Terminal open: session_id={session_id}, cols={cols}, rows={rows}")

    # Subscribe this client to the room, so it receives this PTY's output and
    # not some other tab's. The room name goes back in the reply and the
    # client repeats it on every poll.
    join(room)

    is_new = terminal_manager.get(session_id) is None
    sess = terminal_manager.get_or_create(session_id, cols, rows,
                                          command=command)
    sess.resize(cols, rows)

    if not is_new:
        # Replay saved output so client sees previous session content
        scrollback = sess.get_scrollback()
        if scrollback:
            reply('terminal_output', {'data': scrollback.decode('utf-8', errors='replace')})

    # Start reader thread if not already running
    with _terminal_lock:
        if session_id not in _terminal_readers:
            _terminal_readers[session_id] = True
            _start_background_task(
                _terminal_reader_thread,
                flask_app.app_context(),
                session_id
            )
            pcat_logger.logger.info(f"Terminal reader started for {session_id}")


@bus_event
def terminal_input(message):
    """Client sends input to the terminal."""
    if not _socket_authed():
        return
    session_id = message.get('session_id', 'default')
    data = message.get('data', '')
    sess = terminal_manager.get(session_id)
    if sess:
        sess.write(data)


@bus_event
def terminal_resize(message):
    """Client resizes the terminal."""
    if not _socket_authed():
        return
    session_id = message.get('session_id', 'default')
    cols = message.get('cols', 80)
    rows = message.get('rows', 24)
    sess = terminal_manager.get(session_id)
    if sess:
        sess.resize(cols, rows)


@bus_event
def terminal_kill(message):
    """Client requests to kill a terminal session (for reconnect with fresh shell)."""
    if not _socket_authed():
        return
    session_id = message.get('session_id', 'default')
    pcat_logger.logger.info(f"Terminal kill request: session_id={session_id}")
    terminal_manager.kill(session_id)
    with _terminal_lock:
        _terminal_readers.pop(session_id, None)


# ─── NAS: storage / file manager / samba shares ──────────────────────────────
# Backends: pcat_util/nas_manager (disks, mounts, shares), nas_files (path
# jail + file ops), nas_jobs (background jobs). Conventions: GET and POST on
# the same /api/v1/nas/*.json URL, POST returns the fresh GET payload;
# destructive endpoints use @api_session_strict (no localhost bypass).

from flask import send_file, abort as _flask_abort


def _nas_fail(code, message_en, message_cn, http=200):
    return jsonify({"status": "failed", "code": code,
                    "message": "{} / {}".format(message_en, message_cn)}), http


def _nas_jail(user_path):
    """Resolve a browser path against the live jail roots, or None."""
    try:
        return nas_files.resolve_jailed(user_path,
                                        nas_manager.managed_mount_roots())
    except nas_files.PathJailError:
        return None


@flask_app.route("/nas")
def nas_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")
    c_locale = current_locale()
    locales = _locales.get_locales("nas", c_locale)
    locales.update(base_locales())
    return render_template("nas.html", locales=locales)


@flask_app.route("/api/v1/nas/disks.json", methods=['GET'])
@api_auth_required
def nas_disks_api():
    data = {"status": "ok",
            "tools": nas_manager.tools_available(),
            "disks": nas_manager.list_disks()}
    return jsonify(data)


@flask_app.route("/api/v1/nas/smart.json", methods=['GET'])
@api_auth_required
def nas_smart_api():
    dev = request.args.get("dev", "")
    if not re.match(r"^/dev/[a-z0-9]+$", dev):
        return _nas_fail("bad_dev", "Invalid device", "无效设备")
    data = nas_manager.smart_summary(dev)
    if data is None:
        return _nas_fail("no_smart", "SMART not available", "SMART 不可用")
    data = dict(data)
    data["status"] = "ok"
    data["dev"] = dev
    return jsonify(data)


@flask_app.route("/api/v1/nas/mount.json", methods=['POST'])
@api_auth_required
def nas_mount_api():
    p = request.get_json() or {}
    dev = p.get("dev", "")
    action = p.get("action")
    if not re.match(r"^/dev/[a-z0-9]+$", dev):
        return _nas_fail("bad_dev", "Invalid device", "无效设备")
    if action == "mount":
        out = nas_manager.mount_now(dev)
    elif action == "unmount":
        _disk, part = nas_manager._part_info(dev)
        if part is None or not part.get("mounted"):
            return _nas_fail("bad_dev", "Not mounted", "未挂载")
        out = nas_manager.unmount(part["mountpoint"],
                                  force=bool(p.get("force")))
    else:
        return _nas_fail("bad_action", "Invalid action", "无效操作")
    if not out.get("ok"):
        rsp = {"status": "failed", "code": out.get("code", "error"),
               "message": out.get("message", "operation failed")}
        if out.get("pids"):
            rsp["pids"] = out["pids"]
        return jsonify(rsp)
    pcat_logger.logger.info("NAS: %s %s by %s", action, dev,
                            request.remote_addr)
    return nas_disks_api()


@flask_app.route("/api/v1/nas/automount.json", methods=['POST'])
@api_auth_required
def nas_automount_api():
    p = request.get_json() or {}
    dev = p.get("dev", "")
    if not re.match(r"^/dev/[a-z0-9]+$", dev):
        return _nas_fail("bad_dev", "Invalid device", "无效设备")
    if p.get("enabled"):
        out = nas_manager.enable_automount(dev)
    else:
        out = nas_manager.disable_automount(dev)
    if not out.get("ok"):
        return _nas_fail(out.get("code", "error"),
                         out.get("message", "failed"), "操作失败")
    pcat_logger.logger.info("NAS: automount %s -> %s by %s", dev,
                            bool(p.get("enabled")), request.remote_addr)
    return nas_disks_api()


@flask_app.route("/api/v1/nas/files/list.json", methods=['GET'])
@api_auth_required
def nas_files_list_api():
    user_path = request.args.get("path", "")
    if not user_path:
        roots = nas_manager.managed_mount_roots()
        volumes = []
        for root in roots:
            try:
                vol = nas_files.volume_of(root)
            except OSError:
                continue
            vol["mountpoint"] = root
            vol["name"] = os.path.basename(root) or root
            volumes.append(vol)
        return jsonify({"status": "ok", "volumes": volumes})
    path = _nas_jail(user_path)
    if path is None:
        return _nas_fail("bad_path", "Path not allowed", "路径不允许")
    try:
        offset = max(0, int(request.args.get("offset", 0)))
        listing = nas_files.list_dir(path, offset=offset)
    except nas_files.FileOpError as e:
        return _nas_fail(e.code, str(e), "读取目录失败")
    except ValueError:
        return _nas_fail("bad_path", "Bad offset", "参数错误")
    listing["status"] = "ok"
    listing["path"] = path
    try:
        listing["volume"] = nas_files.volume_of(path)
    except OSError:
        pass
    return jsonify(listing)


@flask_app.route("/api/v1/nas/files/check.json", methods=['POST'])
@api_auth_required
def nas_files_check_api():
    p = request.get_json() or {}
    dest = _nas_jail(p.get("dest_dir", ""))
    if dest is None:
        return _nas_fail("bad_path", "Path not allowed", "路径不允许")
    existing = []
    for name in (p.get("names") or [])[:2000]:
        try:
            parts = nas_files.safe_relpath(name)
        except nas_files.PathJailError:
            continue
        if os.path.lexists(os.path.join(dest, *parts)):
            existing.append(name)
    return jsonify({"status": "ok", "existing": existing})


@flask_app.route("/api/v1/nas/files/mkdir.json", methods=['POST'])
@api_auth_required
def nas_files_mkdir_api():
    p = request.get_json() or {}
    parent = _nas_jail(p.get("path", ""))
    if parent is None:
        return _nas_fail("bad_path", "Path not allowed", "路径不允许")
    try:
        nas_files.make_dir(parent, p.get("name", ""))
    except nas_files.FileOpError as e:
        return _nas_fail(e.code, str(e), "创建文件夹失败")
    return jsonify({"status": "ok"})


@flask_app.route("/api/v1/nas/files/rename.json", methods=['POST'])
@api_auth_required
def nas_files_rename_api():
    p = request.get_json() or {}
    path = _nas_jail(p.get("path", ""))
    if path is None or path in nas_manager.managed_mount_roots():
        return _nas_fail("bad_path", "Path not allowed", "路径不允许")
    try:
        nas_files.rename(path, p.get("new_name", ""))
    except nas_files.FileOpError as e:
        return _nas_fail(e.code, str(e), "重命名失败")
    return jsonify({"status": "ok"})


def _nas_collect_sources(p):
    """Jail every source path in a move/copy/delete request; None on any
    violation (all-or-nothing — partial batches surprise users)."""
    sources = []
    for raw in (p.get("sources") or p.get("paths") or [])[:500]:
        path = _nas_jail(raw)
        if path is None or path in nas_manager.managed_mount_roots():
            return None
        sources.append(path)
    return sources or None


@flask_app.route("/api/v1/nas/files/move.json", methods=['POST'])
@api_auth_required
def nas_files_move_api():
    p = request.get_json() or {}
    sources = _nas_collect_sources(p)
    dest = _nas_jail(p.get("dest_dir", ""))
    if sources is None or dest is None:
        return _nas_fail("bad_path", "Path not allowed", "路径不允许")
    cross = [s for s in sources if not nas_files.same_filesystem(s, dest)]
    if not cross:
        try:
            for src in sources:
                nas_files.move_entry(src, dest)
        except nas_files.FileOpError as e:
            return _nas_fail(e.code, str(e), "移动失败")
        return jsonify({"status": "ok"})
    job_id, err = nas_jobs.submit(
        "move", "Move {} item(s)".format(len(sources)),
        "移动 {} 项".format(len(sources)),
        lambda h: nas_files.copy_tree_runner(h, sources, dest,
                                             delete_src=True),
        meta={"dest": dest})
    if err:
        return _nas_fail(err["code"], "Another job is running", "已有任务在运行")
    return jsonify({"status": "ok", "job_id": job_id})


@flask_app.route("/api/v1/nas/files/copy.json", methods=['POST'])
@api_auth_required
def nas_files_copy_api():
    p = request.get_json() or {}
    sources = _nas_collect_sources(p)
    dest = _nas_jail(p.get("dest_dir", ""))
    if sources is None or dest is None:
        return _nas_fail("bad_path", "Path not allowed", "路径不允许")
    job_id, err = nas_jobs.submit(
        "copy", "Copy {} item(s)".format(len(sources)),
        "复制 {} 项".format(len(sources)),
        lambda h: nas_files.copy_tree_runner(h, sources, dest),
        meta={"dest": dest})
    if err:
        return _nas_fail(err["code"], "Another job is running", "已有任务在运行")
    return jsonify({"status": "ok", "job_id": job_id})


@flask_app.route("/api/v1/nas/files/delete.json", methods=['POST'])
@api_session_strict
def nas_files_delete_api():
    p = request.get_json() or {}
    sources = _nas_collect_sources(p)
    if sources is None:
        return _nas_fail("bad_path", "Path not allowed", "路径不允许")
    total_items = 0
    for src in sources:
        items, _size = nas_files.tree_measure(src)
        total_items += items
        if total_items > nas_files.INLINE_DELETE_LIMIT:
            break
    if total_items <= nas_files.INLINE_DELETE_LIMIT:
        try:
            for src in sources:
                nas_files.delete_tree(src)
        except OSError as e:
            return _nas_fail("io_error", str(e), "删除失败")
        pcat_logger.logger.info("NAS: deleted %s item(s) by %s",
                                len(sources), request.remote_addr)
        return jsonify({"status": "ok"})

    def _runner(h):
        h.set_progress(items_total=total_items, message="deleting")
        done = [0]

        def _cb(count):
            done[0] = count
            h.set_progress(items_done=count)
            h.check_cancel()
        for src in sources:
            nas_files.delete_tree(src, progress_cb=_cb)
    job_id, err = nas_jobs.submit(
        "delete", "Delete {} item(s)".format(len(sources)),
        "删除 {} 项".format(len(sources)), _runner)
    if err:
        return _nas_fail(err["code"], "Another job is running", "已有任务在运行")
    pcat_logger.logger.info("NAS: delete job %s (%s sources) by %s", job_id,
                            len(sources), request.remote_addr)
    return jsonify({"status": "ok", "job_id": job_id})


@flask_app.route("/api/v1/nas/files/upload", methods=['POST'])
@api_auth_required
def nas_files_upload_api():
    """Streaming upload: metadata rides the QUERY STRING and the file is the
    RAW request body (not multipart). Deliberate: werkzeug's multipart parser
    spools large parts to /tmp, which is RAM on this device — the raw body
    streams straight into a .part file on the destination filesystem."""
    dest = _nas_jail(request.args.get("dest", ""))
    if dest is None:
        return _nas_fail("bad_path", "Path not allowed", "路径不允许")
    try:
        parts = nas_files.safe_relpath(request.args.get("relpath", ""))
    except nas_files.PathJailError:
        return _nas_fail("bad_path", "Invalid file path", "无效文件路径")
    conflict = request.args.get("conflict", "fail")
    if conflict not in ("fail", "overwrite", "rename"):
        return _nas_fail("bad_action", "Invalid conflict mode", "无效参数")
    directory = dest
    if len(parts) > 1:
        directory = os.path.join(dest, *parts[:-1])
        # Jail BEFORE makedirs: realpath resolves any existing symlink in the
        # middle of the relpath, so a link pointing out of the jail fails
        # here and never causes directory creation outside it.
        directory = _nas_jail(directory)
        if directory is None:
            return _nas_fail("bad_path", "Path not allowed", "路径不允许")
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as e:
            return _nas_fail("io_error", str(e), "创建目录失败")
    max_bytes = int(pcat_config.get_user_config_key(
        "nas_upload_max_bytes", 16 * 1024 * 1024 * 1024))
    try:
        out = nas_files.write_stream(
            directory, parts[-1], request.stream, max_bytes,
            declared_size=request.content_length, conflict=conflict)
    except nas_files.FileOpError as e:
        return _nas_fail(e.code, str(e), "上传失败")
    except OSError as e:
        return _nas_fail("io_error", str(e), "上传失败")
    out["status"] = "ok"
    return jsonify(out)


@flask_app.route("/api/v1/nas/files/download", methods=['GET'])
@api_auth_required
def nas_files_download_api():
    path = _nas_jail(request.args.get("path", ""))
    if path is None or not os.path.isfile(path):
        return _nas_fail("bad_path", "Path not allowed", "路径不允许", 404)
    # inline=1: browser can render the body (image/video/pdf preview). Default
    # is attachment so a bare link still downloads. conditional=True gives
    # Range support (video scrubbing over SMB-less HTTP).
    inline = str(request.args.get("inline", "")).lower() in ("1", "true", "yes")
    return send_file(path, as_attachment=not inline,
                     download_name=os.path.basename(path), conditional=True)


@flask_app.route("/api/v1/nas/files/archive", methods=['GET'])
@api_auth_required
def nas_files_archive_api():
    paths = []
    for raw in request.args.getlist("path")[:200]:
        p = _nas_jail(raw)
        if p is None:
            return _nas_fail("bad_path", "Path not allowed", "路径不允许", 404)
        paths.append(p)
    if not paths:
        return _nas_fail("bad_path", "No paths", "缺少路径", 404)
    name = request.args.get("name") or \
        os.path.basename(paths[0].rstrip("/")) or "download"
    name = re.sub(r"[^A-Za-z0-9 ._一-鿿-]", "_", name)[:64]
    rsp = Response(stream_with_context(nas_files.zip_stream(paths)),
                   mimetype="application/zip", direct_passthrough=True)
    rsp.headers['Content-Disposition'] = \
        "attachment; filename*=UTF-8''{}.zip".format(
            urllib.parse.quote(name))
    rsp.headers['Cache-Control'] = 'no-store'
    return rsp


@flask_app.route("/api/v1/nas/jobs.json", methods=['GET'])
@api_auth_required
def nas_jobs_api():
    return jsonify({"status": "ok", "jobs": nas_jobs.list_jobs()})


@flask_app.route("/api/v1/nas/jobs.json", methods=['POST'])
@api_auth_required
def nas_jobs_action_api():
    p = request.get_json() or {}
    action, job_id = p.get("action"), p.get("id", "")
    if action == "cancel":
        out = nas_jobs.cancel(job_id)
    elif action == "dismiss":
        out = nas_jobs.dismiss(job_id)
    else:
        return _nas_fail("bad_action", "Invalid action", "无效操作")
    if out["code"] != "ok":
        return _nas_fail(out["code"], "Job operation failed", "任务操作失败")
    return nas_jobs_api()


@flask_app.route("/api/v1/nas/shares.json", methods=['GET'])
@api_auth_required
def nas_shares_api():
    return jsonify({"status": "ok",
                    "samba": nas_manager.samba_state(),
                    "users": nas_manager.list_smb_users(),
                    "shares": nas_manager.list_shares()})


@flask_app.route("/api/v1/nas/shares.json", methods=['POST'])
@api_auth_required
def nas_shares_save_api():
    p = request.get_json() or {}
    action = p.get("action")
    if action == "delete":
        # Deletion is strict-tier: enforce a real session inline (this route
        # is std so the create/update path stays mini-display-compatible).
        if 'username' not in session:
            return jsonify({"status": "failed",
                            "message": "Login required"}), 401
        out = nas_manager.delete_share(p.get("id", ""))
    elif action in ("create", "update"):
        out = nas_manager.upsert_share(
            p.get("id") if action == "update" else None,
            p.get("name", ""), p.get("path", ""),
            mode=p.get("mode", "guest"),
            users=[u for u in (p.get("users") or []) if isinstance(u, str)],
            read_only=bool(p.get("read_only")),
            http_browse=bool(p.get("http_browse")))
    else:
        return _nas_fail("bad_action", "Invalid action", "无效操作")
    if not out.get("ok"):
        return _nas_fail(out.get("code", "error"),
                         out.get("message", "failed"), "共享配置失败")
    pcat_logger.logger.info("NAS: share %s by %s", action,
                            request.remote_addr)
    return nas_shares_api()


@flask_app.route("/api/v1/nas/share_users.json", methods=['POST'])
@api_session_strict
def nas_share_users_api():
    p = request.get_json() or {}
    action = p.get("action")
    name = p.get("name", "")
    if action in ("add", "password"):
        out = nas_manager.set_smb_user(name, p.get("password", ""))
    elif action == "remove":
        out = nas_manager.delete_smb_user(name)
    else:
        return _nas_fail("bad_action", "Invalid action", "无效操作")
    if not out.get("ok"):
        return _nas_fail(out.get("code", "error"),
                         out.get("message", "failed"), "用户操作失败")
    pcat_logger.logger.info("NAS: smb user %s %s by %s", action, name,
                            request.remote_addr)
    return nas_shares_api()


@flask_app.route("/api/v1/nas/samba.json", methods=['POST'])
@api_auth_required
def nas_samba_api():
    p = request.get_json() or {}
    out = nas_manager.samba_service(p.get("action", ""))
    if not out.get("ok"):
        return _nas_fail(out.get("code", "error"),
                         out.get("message", "failed"), "服务操作失败")
    return nas_shares_api()


@flask_app.route("/public/share/<share_name>/", defaults={"subpath": ""})
@flask_app.route("/public/share/<share_name>/<path:subpath>")
def public_share_action(share_name, subpath):
    """Read-only, no-login browse/download of shares the user explicitly
    flagged public (pcat_http_public). GET-only; 404 for everything that is
    not an opted-in share; realpath-jailed to the share root so neither
    "..%2f" nor an in-share symlink can leave it. No zip here — an
    unauthenticated tar-pit would be a CPU DoS surface."""
    roots = nas_manager.public_http_roots()
    root = roots.get(share_name)
    if root is None:
        _flask_abort(404)
    try:
        path = nas_files.resolve_jailed(os.path.join(root, subpath), [root])
    except nas_files.PathJailError:
        _flask_abort(404)
    if os.path.isfile(path):
        rsp = send_file(path, as_attachment=False,
                        download_name=os.path.basename(path),
                        conditional=True)
        rsp.headers['Cache-Control'] = 'no-store'
        return rsp
    if not os.path.isdir(path):
        _flask_abort(404)
    try:
        listing = nas_files.list_dir(path)
    except nas_files.FileOpError:
        _flask_abort(404)
    rel = os.path.relpath(path, root)
    crumbs = [] if rel == "." else rel.split(os.sep)
    rsp = make_response(render_template(
        "public_share.html", share_name=share_name, crumbs=crumbs,
        entries=listing["entries"], truncated=listing["truncated"]))
    rsp.headers['Cache-Control'] = 'no-store'
    return rsp


# ─── NAS phase 2/3: partitions, backup, sync, remotes, share links ───────────

@flask_app.route("/api/v1/nas/partitions.json", methods=['GET'])
@api_auth_required
def nas_partitions_api():
    dev = request.args.get("dev", "")
    if not re.match(r"^/dev/[a-z0-9]+$", dev):
        return _nas_fail("bad_dev", "Invalid device", "无效设备")
    table = nas_manager.partition_table(dev)
    if table is None:
        return _nas_fail("read_failed", "Could not read partition table",
                         "读取分区表失败")
    table["status"] = "ok"
    table["dev"] = dev
    # Whole-disk protected (eMMC) still true; annotate each partition so the
    # UI can format/delete data partitions while locking boot/root.
    table["protected"] = nas_manager.is_protected(os.path.basename(dev))
    for part in table.get("parts") or []:
        pdev = nas_manager.part_device_path(dev, part["num"])
        part["protected"] = nas_manager.is_protected(os.path.basename(pdev))
        part["dev"] = pdev
    return jsonify(table)


@flask_app.route("/api/v1/nas/partitions.json", methods=['POST'])
@api_session_strict
def nas_partitions_save_api():
    p = request.get_json() or {}
    action = p.get("action")
    dev = p.get("dev", "")
    confirm = p.get("confirm", "")
    if not re.match(r"^/dev/[a-z0-9]+$", dev):
        return _nas_fail("bad_dev", "Invalid device", "无效设备")
    try:
        if action == "use_free":
            disk_path = nas_manager.partition_guard(dev, "create_free",
                                                    confirm=confirm)
            table = nas_manager.partition_table(disk_path)
            steps, part_dev = nas_manager.build_use_free_plan(disk_path, table)
            steps += nas_manager.build_format(part_dev, "ext4",
                                              nas_manager.DATA_PART_LABEL)
            runner = lambda h: nas_manager.run_partition_job(
                h, steps, part_dev=part_dev, automount_after=True)
            label, label_cn = "Create data volume on " + disk_path, \
                "在 " + disk_path + " 上创建数据卷"
        elif action == "format":
            disk_path = nas_manager.partition_guard(dev, "format",
                                                    confirm=confirm)
            steps = nas_manager.build_format(dev, p.get("fs", "ext4"),
                                             p.get("label") or None)
            # Automount after format so eMMC data partitions (and USB/NVMe)
            # become usable without a second click.
            runner = lambda h: nas_manager.run_partition_job(
                h, steps, part_dev=dev, automount_after=True)
            label, label_cn = "Format " + dev, "格式化 " + dev
        elif action == "delete":
            disk_path = nas_manager.partition_guard(dev, "delete",
                                                    confirm=confirm)
            num = int(p.get("num", 0))
            nas_manager.disable_automount(dev, unmount_now=False)
            steps = nas_manager.build_delete(disk_path, num)
            runner = lambda h: nas_manager.run_partition_job(h, steps)
            label, label_cn = "Delete partition " + dev, "删除分区 " + dev
        elif action == "create":
            disk_path = nas_manager.partition_guard(dev, "create",
                                                    confirm=confirm)
            table = nas_manager.partition_table(disk_path)
            steps = nas_manager.build_create(
                disk_path, int(p.get("start_b", 0)), int(p.get("end_b", 0)),
                table)
            runner = lambda h: nas_manager.run_partition_job(h, steps)
            label, label_cn = "Create partition on " + disk_path, \
                "在 " + disk_path + " 上创建分区"
        elif action == "mklabel":
            # Wipes the whole disk: guarded as a whole-disk op (nothing
            # mounted, never a protected disk) and gated behind the typed
            # device name like every other destructive action.
            disk_path = nas_manager.partition_guard(dev, "create",
                                                    confirm=confirm)
            steps = nas_manager.build_mklabel(disk_path,
                                              p.get("table", "gpt"))
            runner = lambda h: nas_manager.run_partition_job(h, steps)
            label = "New {} partition table on {}".format(
                p.get("table", "gpt").upper(), disk_path)
            label_cn = "在 {} 上新建 {} 分区表".format(
                disk_path, p.get("table", "gpt").upper())
        elif action == "resize":
            disk_path = nas_manager.partition_guard(dev, "resize",
                                                    confirm=confirm)
            table = nas_manager.partition_table(disk_path)
            num = int(p.get("num", 0))
            part = next((x for x in table["parts"] if x["num"] == num), None)
            if part is None:
                return _nas_fail("bad_dev", "Unknown partition", "未知分区")
            if part["fs"] not in ("ext4", None, ""):
                return _nas_fail("bad_fs",
                                 "Only ext4 can be resized", "仅支持调整 ext4")
            new_end = int(p.get("new_end_b", 0))
            grow = new_end > part["end"]
            if grow:
                region_ok = any(r["start"] <= part["end"] + 1 and
                                new_end <= r["end"] for r in table["free"])
                if not region_ok:
                    return _nas_fail("overlap", "No free space after partition",
                                     "分区后无可用空间")
                steps, stdin = nas_manager.build_resize(
                    disk_path, dev, num, new_end, grow=True)
            else:
                new_size = new_end - part["start"]
                if new_size < nas_manager.MIN_PART_BYTES:
                    return _nas_fail("too_small", "Too small", "太小")
                steps, stdin = nas_manager.build_resize(
                    disk_path, dev, num, new_end, grow=False)
                steps[1][2] = "{}K".format(max(1, new_size // 1024))
            runner = lambda h: nas_manager.run_partition_job(
                h, steps, stdin_for_parted=stdin, part_dev=dev)
            label, label_cn = "Resize " + dev, "调整 " + dev
        else:
            return _nas_fail("bad_action", "Invalid action", "无效操作")
    except nas_manager.GuardError as e:
        return _nas_fail(e.code, str(e), "已被安全检查拒绝")
    except (ValueError, TypeError):
        return _nas_fail("bad_args", "Invalid arguments", "参数错误")
    job_id, err = nas_jobs.submit(
        "partition" if action in ("use_free", "create", "delete") else action,
        label, label_cn, runner, meta={"dev": dev},
        exclusive_key="disk:" + os.path.basename(
            nas_manager.parent_disk(os.path.basename(dev))))
    if err:
        return _nas_fail(err["code"], "Another disk job is running",
                         "已有磁盘任务在运行")
    pcat_logger.logger.info("NAS: partition %s %s job=%s by %s", action, dev,
                            job_id, request.remote_addr)
    return jsonify({"status": "ok", "job_id": job_id})


@flask_app.route("/api/v1/nas/backup.json", methods=['GET'])
@api_auth_required
def nas_backup_api():
    data = nas_manager.backup_status()
    data["status"] = "ok"
    return jsonify(data)


@flask_app.route("/api/v1/nas/backup.json", methods=['POST'])
@api_session_strict
def nas_backup_run_api():
    st = nas_manager.backup_status()
    if not st["ready"]:
        return _nas_fail("not_ready", "Need a mounted SD card and NVMe",
                         "需要已挂载的 SD 卡和 NVMe")
    src, dst = st["sd"]["mountpoint"], st["dest"]
    try:
        os.makedirs(dst, exist_ok=True)
    except OSError as e:
        return _nas_fail("io_error", str(e), "创建目录失败")
    job = {"id": "sbackup00", "src": src, "dst": dst, "delete": False}
    job_id, err = nas_jobs.submit(
        "backup", "SD card → NVMe backup", "SD 卡 → NVMe 备份",
        lambda h: nas_manager.run_rsync_now(h, job),
        meta={"dest": dst})
    if err:
        return _nas_fail(err["code"], "Another job is running", "已有任务在运行")
    return jsonify({"status": "ok", "job_id": job_id})


@flask_app.route("/api/v1/nas/sync.json", methods=['GET'])
@api_auth_required
def nas_sync_api():
    return jsonify({"status": "ok", "jobs": nas_manager.list_sync_jobs()})


@flask_app.route("/api/v1/nas/sync.json", methods=['POST'])
@api_auth_required
def nas_sync_save_api():
    p = request.get_json() or {}
    action = p.get("action")
    if action == "delete":
        if 'username' not in session:
            return jsonify({"status": "failed",
                            "message": "Login required"}), 401
        out = nas_manager.delete_sync_job(p.get("id", ""))
    elif action in ("create", "update"):
        out = nas_manager.save_sync_job(
            p.get("id") if action == "update" else None, p)
    elif action in ("enable", "disable"):
        out = nas_manager.save_sync_job(p.get("id", ""), dict(
            next((j for j in nas_manager.list_sync_jobs()
                  if j.get("id") == p.get("id")), {}),
            enabled=(action == "enable")))
    elif action == "run_now":
        job = next((j for j in nas_manager.list_sync_jobs()
                    if j.get("id") == p.get("id")), None)
        if job is None:
            return _nas_fail("not_found", "Unknown job", "未知任务")
        job_id, err = nas_jobs.submit(
            "rsync", "Sync: " + job.get("name", job["id"]),
            "同步: " + job.get("name", job["id"]),
            lambda h: nas_manager.run_rsync_now(h, job),
            meta={"dest": job["dst"]}, exclusive_key="rsync:" + job["id"])
        if err:
            return _nas_fail(err["code"], "Already running", "任务已在运行")
        return jsonify({"status": "ok", "job_id": job_id,
                        "jobs": nas_manager.list_sync_jobs()})
    else:
        return _nas_fail("bad_action", "Invalid action", "无效操作")
    if not out.get("ok"):
        return _nas_fail(out.get("code", "error"),
                         out.get("message", "failed"), "同步任务配置失败")
    return nas_sync_api()


@flask_app.route("/api/v1/nas/sync_log.json", methods=['GET'])
@api_auth_required
def nas_sync_log_api():
    return jsonify({"status": "ok",
                    "log": nas_manager.sync_log_tail(
                        request.args.get("id", ""))})


@flask_app.route("/api/v1/nas/remotes.json", methods=['GET'])
@api_auth_required
def nas_remotes_api():
    return jsonify({"status": "ok",
                    "remotes": [nas_manager._remote_public(r)
                                for r in nas_manager.list_remotes()]})


@flask_app.route("/api/v1/nas/remotes.json", methods=['POST'])
@api_session_strict
def nas_remotes_save_api():
    p = request.get_json() or {}
    action = p.get("action")
    if action in ("create", "update"):
        out = nas_manager.save_remote(
            p.get("id") if action == "update" else None, p)
    elif action == "delete":
        out = nas_manager.delete_remote(p.get("id", ""))
    elif action in ("mount", "unmount", "test"):
        remote = next((r for r in nas_manager.list_remotes()
                       if r.get("id") == p.get("id")), None)
        if remote is None:
            return _nas_fail("not_found", "Unknown remote", "未知远程")
        if action == "unmount":
            out = nas_manager.unmount_remote(remote)
        else:
            out = nas_manager.mount_remote(remote)
            if action == "test" and out.get("ok"):
                try:
                    os.listdir(remote["mountpoint"])
                except OSError as e:
                    out = {"ok": False, "code": "io_error", "message": str(e)}
    else:
        return _nas_fail("bad_action", "Invalid action", "无效操作")
    if not out.get("ok"):
        return _nas_fail(out.get("code", "error"),
                         out.get("message", "failed"), "远程挂载操作失败")
    pcat_logger.logger.info("NAS: remote %s by %s", action,
                            request.remote_addr)
    return nas_remotes_api()


@flask_app.route("/api/v1/nas/links.json", methods=['GET'])
@api_auth_required
def nas_links_api():
    return jsonify({"status": "ok",
                    "links": [nas_manager.link_public(l)
                              for l in nas_manager.list_links()]})


@flask_app.route("/api/v1/nas/links.json", methods=['POST'])
@api_session_strict
def nas_links_save_api():
    p = request.get_json() or {}
    action = p.get("action")
    if action == "create":
        path = _nas_jail(p.get("path", ""))
        if path is None:
            return _nas_fail("bad_path", "Path not allowed", "路径不允许")
        out = nas_manager.create_link(path, p.get("password") or None)
        if not out.get("ok"):
            return _nas_fail(out.get("code", "error"),
                             out.get("message", "failed"), "创建链接失败")
        pcat_logger.logger.info("NAS: share link created for %s by %s",
                                path, request.remote_addr)
        return jsonify({"status": "ok", "link": out["link"],
                        "links": [nas_manager.link_public(l)
                                  for l in nas_manager.list_links()]})
    if action == "delete":
        nas_manager.delete_link(p.get("token", ""))
        return nas_links_api()
    return _nas_fail("bad_action", "Invalid action", "无效操作")


def _link_session_ok(token):
    return token in (session.get("nas_link_ok") or [])


@flask_app.route("/s/<token>", defaults={"subpath": ""},
                 methods=['GET', 'POST'])
@flask_app.route("/s/<token>/", defaults={"subpath": ""},
                 methods=['GET', 'POST'])
@flask_app.route("/s/<token>/<path:subpath>", methods=['GET'])
def public_link_action(token, subpath):
    """Nextcloud-style public share link: /s/<token> serves a file directly
    or browses a folder (reusing the public_share template — its links are
    all relative). Optional password: POST unlocks it into the visitor's
    (anonymous) session. Read-only, jailed to the linked path."""
    link = nas_manager.find_link(token)
    if link is None:
        _flask_abort(404)
    root = os.path.realpath(link["path"])
    if not os.path.exists(root):
        _flask_abort(404)
    if link.get("password_hash"):
        if request.method == 'POST':
            if nas_manager.check_link_password(link,
                                               request.form.get("password")):
                ok = session.get("nas_link_ok") or []
                session["nas_link_ok"] = ok + [token]
                return redirect(request.path if request.path.endswith(token)
                                else "/s/" + token)
            return render_template("public_link_password.html",
                                   name=link["name"], error=True), 401
        if not _link_session_ok(token):
            return render_template("public_link_password.html",
                                   name=link["name"], error=False)
    if link["type"] == "file":
        rsp = send_file(root, as_attachment=True,
                        download_name=link["name"], conditional=True)
        rsp.headers['Cache-Control'] = 'no-store'
        return rsp
    try:
        path = nas_files.resolve_jailed(os.path.join(root, subpath), [root])
    except nas_files.PathJailError:
        _flask_abort(404)
    if os.path.isfile(path):
        rsp = send_file(path, as_attachment=False,
                        download_name=os.path.basename(path),
                        conditional=True)
        rsp.headers['Cache-Control'] = 'no-store'
        return rsp
    if not os.path.isdir(path):
        _flask_abort(404)
    try:
        listing = nas_files.list_dir(path)
    except nas_files.FileOpError:
        _flask_abort(404)
    rel = os.path.relpath(path, root)
    crumbs = [] if rel == "." else rel.split(os.sep)
    rsp = make_response(render_template(
        "public_share.html", share_name=link["name"], crumbs=crumbs,
        entries=listing["entries"], truncated=listing["truncated"]))
    rsp.headers['Cache-Control'] = 'no-store'
    return rsp


_PUBLIC_ENDPOINTS = _PUBLIC_ENDPOINTS | {"public_link_action"}
_CSRF_EXEMPT_ENDPOINTS = _CSRF_EXEMPT_ENDPOINTS | {"public_link_action"}


# ─── LXC: containers / setup automation / migration (hidden page) ────────────
# Backend: pcat_util/lxc_manager. The /lxc page has NO navbar entry by
# design — it is reachable only by typing the URL. Conventions match NAS:
# POSTs return the fresh GET payload where cheap; anything that mutates the
# system uses @api_session_strict (no localhost bypass); long work runs
# through nas_jobs (kinds lxc_create / lxc_storage / lxc_migrate) so the
# existing jobs endpoint and socket push serve this page too.

from pcat_util import lxc_manager


@flask_app.route("/lxc")
def lxc_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")
    if not is_photonicat2:
        return render_error_page(404)
    c_locale = current_locale()
    locales = _locales.get_locales("lxc", c_locale)
    locales.update(base_locales())
    return render_template("lxc.html", locales=locales)


def _lxc_fail(code, detail=""):
    return jsonify({"status": "failed", "code": code,
                    "message": detail or code})


@flask_app.route("/api/v1/lxc/health.json", methods=['GET'])
@api_auth_required
def lxc_health_api():
    data = lxc_manager.preflight()
    data["status"] = "ok"
    return jsonify(data)


@flask_app.route("/api/v1/lxc/fix.json", methods=['POST'])
@api_session_strict
def lxc_fix_api():
    what = (request.json or {}).get("what", "")
    res = lxc_manager.apply_fix(what)
    if res.get("code") not in ("ok", "partial"):
        return _lxc_fail(res.get("code", "failed"), res.get("detail", ""))
    data = lxc_manager.preflight()
    data["status"] = "ok"
    data["fix_result"] = res
    return jsonify(data)


@flask_app.route("/api/v1/lxc/containers.json", methods=['GET'])
@api_auth_required
def lxc_containers_api():
    with_sizes = request.args.get("sizes") == "1"
    return jsonify({"status": "ok",
                    "containers": lxc_manager.list_containers(with_sizes)})


@flask_app.route("/api/v1/lxc/container.json", methods=['POST'])
@api_session_strict
def lxc_container_api():
    body = request.json or {}
    action = body.get("action", "")
    name = body.get("name", "")
    if action == "autostart":
        res = lxc_manager.set_autostart(name, bool(body.get("enabled")))
    elif action == "network":
        res = lxc_manager.set_network(name, body.get("mode", ""))
    elif action == "ipconfig":
        res = lxc_manager.set_ip_config(name, body)
    else:
        res = lxc_manager.container_action(
            action, name, {"newname": body.get("newname", "")})
    if res["code"] != "ok":
        return _lxc_fail(res["code"], res.get("detail", ""))
    return jsonify({"status": "ok",
                    "containers": lxc_manager.list_containers()})


@flask_app.route("/api/v1/lxc/create.json", methods=['POST'])
@api_session_strict
def lxc_create_api():
    body = request.json or {}
    job_id, err = lxc_manager.create_container(
        body.get("name", ""), body.get("dist", ""),
        body.get("release", ""), body.get("arch") or None,
        storage=body.get("storage") or None,
        vmid=body.get("vmid") or None)
    if err:
        return _lxc_fail(err["code"], err.get("detail", ""))
    return jsonify({"status": "ok", "job_id": job_id})


@flask_app.route("/api/v1/lxc/stats.json", methods=['GET'])
@api_auth_required
def lxc_stats_api():
    res = lxc_manager.container_stats(request.args.get("name", ""))
    if res["code"] != "ok":
        return _lxc_fail(res["code"])
    res["status"] = "ok"
    return jsonify(res)


@flask_app.route("/api/v1/lxc/backups.json", methods=['GET'])
@api_auth_required
def lxc_backups_api():
    return jsonify({"status": "ok", "backups": lxc_manager.backup_list()})


@flask_app.route("/api/v1/lxc/backups.json", methods=['POST'])
@api_session_strict
def lxc_backups_post_api():
    body = request.json or {}
    action = body.get("action", "")
    if action == "backup":
        # Timestamp is stamped here, not in lxc_manager: the module stays
        # clock-free so its jobs are reproducible in tests.
        stamp = time.strftime("%Y%m%d_%H%M%S")
        job_id, err = lxc_manager.backup_container(body.get("name", ""), stamp)
    elif action == "restore":
        job_id, err = lxc_manager.restore_backup(
            body.get("file", ""), body.get("newname") or None)
    elif action == "delete":
        res = lxc_manager.backup_delete(body.get("file", ""))
        if res["code"] != "ok":
            return _lxc_fail(res["code"], res.get("detail", ""))
        return jsonify({"status": "ok", "backups": lxc_manager.backup_list()})
    else:
        return _lxc_fail("bad_action")
    if err:
        return _lxc_fail(err["code"], err.get("detail", ""))
    return jsonify({"status": "ok", "job_id": job_id})


@flask_app.route("/api/v1/lxc/config.json", methods=['GET'])
@api_auth_required
def lxc_config_api():
    res = lxc_manager.get_config(request.args.get("name", ""))
    if res["code"] != "ok":
        return _lxc_fail(res["code"])
    return jsonify({"status": "ok", "text": res["text"]})


@flask_app.route("/api/v1/lxc/config.json", methods=['POST'])
@api_session_strict
def lxc_config_post_api():
    body = request.json or {}
    res = lxc_manager.save_config(body.get("name", ""),
                                  body.get("text", ""))
    if res["code"] != "ok":
        return _lxc_fail(res["code"], res.get("detail", ""))
    return jsonify({"status": "ok"})


@flask_app.route("/api/v1/lxc/limits.json", methods=['POST'])
@api_session_strict
def lxc_limits_api():
    body = request.json or {}
    res = lxc_manager.set_limits(body.get("name", ""),
                                 body.get("memory_mb", 0),
                                 body.get("cpu_cores", 0))
    if res["code"] != "ok":
        return _lxc_fail(res["code"], res.get("detail", ""))
    return jsonify({"status": "ok", "live": res.get("live", False)})


@flask_app.route("/api/v1/lxc/clone.json", methods=['POST'])
@api_session_strict
def lxc_clone_api():
    body = request.json or {}
    job_id, err = lxc_manager.clone_container(
        body.get("name", ""), body.get("newname", ""))
    if err:
        return _lxc_fail(err["code"], err.get("detail", ""))
    return jsonify({"status": "ok", "job_id": job_id})


@flask_app.route("/api/v1/lxc/networks.json", methods=['GET'])
@api_auth_required
def lxc_networks_api():
    return jsonify({"status": "ok",
                    "networks": lxc_manager.list_networks(),
                    "ports": lxc_manager.physical_ports()})


@flask_app.route("/api/v1/lxc/networks.json", methods=['POST'])
@api_session_strict
def lxc_networks_post_api():
    body = request.json or {}
    action = body.get("action", "")
    if action == "create":
        res = lxc_manager.create_network(
            body.get("name", ""), port=body.get("port") or None,
            addressing=body.get("addressing", "l2"),
            subnet=body.get("subnet", ""))
    elif action == "delete":
        res = lxc_manager.delete_network(body.get("name", ""))
    else:
        res = {"code": "bad_action"}
    if res["code"] != "ok":
        return _lxc_fail(res["code"], res.get("detail", ""))
    return jsonify({"status": "ok",
                    "networks": lxc_manager.list_networks(),
                    "ports": lxc_manager.physical_ports()})


@flask_app.route("/api/v1/lxc/move.json", methods=['POST'])
@api_session_strict
def lxc_move_api():
    """Move one container's disk between storage volumes (offline)."""
    body = request.json or {}
    dest = body.get("dest") or lxc_manager.lxcpath()
    job_id, err = lxc_manager.move_container(body.get("name", ""), dest)
    if err:
        return _lxc_fail(err["code"], err.get("detail", ""))
    return jsonify({"status": "ok", "job_id": job_id})


@flask_app.route("/api/v1/lxc/storage.json", methods=['GET'])
@api_auth_required
def lxc_storage_api():
    # `volatile` is the list a firmware upgrade would erase (rootfs on the
    # overlay); `recommended` is where they should go instead, or null on a
    # device whose only writable filesystem is the one a flash replaces.
    # lxcpath_durable is the one to warn on even when `volatile` is empty: a
    # lxcpath on the overlay is the default target for every new container,
    # so the next one created there is lost at the next flash.
    path = lxc_manager.lxcpath()
    volatile = lxc_manager.volatile_containers()
    return jsonify({"status": "ok",
                    "lxcpath": path,
                    "lxcpath_durable": not lxc_manager.on_overlay(path),
                    "candidates": lxc_manager.storage_candidates(),
                    "volatile": volatile,
                    "durable": not volatile,
                    "recommended": lxc_manager.default_storage()})


@flask_app.route("/api/v1/lxc/storage.json", methods=['POST'])
@api_session_strict
def lxc_storage_post_api():
    body = request.json or {}
    job_id, err = lxc_manager.move_storage(
        body.get("path", ""), keep_old=bool(body.get("keep_old")))
    if err:
        return _lxc_fail(err["code"], err.get("detail", ""))
    return jsonify({"status": "ok", "job_id": job_id})


@flask_app.route("/api/v1/lxc/migrate.json", methods=['GET'])
@api_auth_required
def lxc_migrate_info_api():
    return jsonify({"status": "ok",
                    "key_ready": lxc_manager.ssh_key_ready(),
                    "pubkey": lxc_manager.ssh_pubkey()})


@flask_app.route("/api/v1/lxc/migrate.json", methods=['POST'])
@api_session_strict
def lxc_migrate_api():
    body = request.json or {}
    action = body.get("action", "")
    host = body.get("host", "")
    port = body.get("port", 22)
    user = body.get("user", "root")
    if action == "test":
        res = lxc_manager.test_target(host, port, user)
        if res["code"] != "ok":
            return _lxc_fail(res["code"], res.get("detail", ""))
        res["status"] = "ok"
        return jsonify(res)
    if action == "pushkey":
        res = lxc_manager.push_key(host, port, user,
                                   body.get("password", ""))
        if res["code"] != "ok":
            return _lxc_fail(res["code"], res.get("detail", ""))
        return jsonify({"status": "ok"})
    if action == "go":
        job_id, err = lxc_manager.migrate(
            body.get("name", ""), host, port, user,
            remote_path=body.get("remote_path") or None,
            mode=body.get("mode", "copy"),
            start_after=bool(body.get("start_after")))
        if err:
            return _lxc_fail(err["code"], err.get("detail", ""))
        return jsonify({"status": "ok", "job_id": job_id})
    return _lxc_fail("bad_action")
