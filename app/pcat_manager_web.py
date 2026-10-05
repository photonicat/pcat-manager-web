#@Author    : qichunren
# Security enhancements implemented:
# - Secure random SECRET_KEY generation
# - Session cookie security (HttpOnly, SameSite, Secure for HTTPS)
# - Rate limiting for login attempts (5 attempts per 5 minutes)
# - CSRF protection for forms
# - Security headers (X-Frame-Options, CSP, etc.)
# - Reasonable session lifetimes (30 days default, 180 days remember)

from flask import Flask, jsonify, render_template, request, redirect, session, make_response, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from datetime import datetime, date, timedelta
import secrets
import time
import urllib.parse
from pcat_util import user_auth, pcat_logger, command_parser, hardware_util, isp_dict
from pcat_util.openwrt import ubus_call as _openwrt_ubus_call, uci_get as _openwrt_uci_get, uci_set as _openwrt_uci_set, uci_commit as _openwrt_uci_commit, debug_status as _openwrt_debug_status
import urllib.error
import subprocess
import threading
# import pyprctl # patch:set real thread name
import pathlib
import json
import base64
import re
import os
import urllib.request
import urllib.error

import pcat_config
import oem_config
import power_event_log
import battery_history
import nt
import pc_modem_client
import app as pc_app
from cmd_tool import CmdTool
from pc_terminal import terminal_manager

install_thread = None
thread_lock = threading.Lock()

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
PIN_PROTECTED_ROUTES = ['/phone', '/perf']
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

def background_install_thread(file_position, file_name):
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
        socketio.emit('log_response', {'data': 'ERROR: opkg-install script not found\n'}, namespace='/')
        return

    if file_position == "remote":
        file_name = pcat_config.volatile_packages_url + file_name

    cmd_args = ["python3", cmd, "install", file_name]
    p = subprocess.Popen(cmd_args, stdout=subprocess.PIPE, bufsize=1)
    while True:
        socketio.sleep(0.5)
        for line in iter(p.stdout.readline, b''):
            line_str = line.decode("utf-8")
            now = datetime.now()
            time_string = now.strftime("%H:%M:%S")
            socketio.emit('log_response', {'data': line_str, 'ts': time_string})
        p.stdout.close()
        break
    install_thread = None

class FlaskAppThread (threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)

    def run(self):
        print("Start web service on port", pcat_config.web_port)
        try:
            socketio.run(flask_app, host="0.0.0.0", port=pcat_config.web_port, allow_unsafe_werkzeug=True)
        except PermissionError as error:
            print("!!! Failed to start web service", error)

flask_app = Flask(__name__)

# Make csrf_token available in templates
flask_app.jinja_env.globals['csrf_token'] = generate_csrf_token

# Add cache bust version based on startup time
startup_time = int(time.time())
@flask_app.context_processor
def inject_cache_bust():
    terminal_enabled = pcat_config.get_user_config_key("terminal_enabled", True)
    return {'cache_bust_version': startup_time, 'terminal_enabled': terminal_enabled}

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

@flask_app.before_request
def restore_session_lifetime():
    """Restore session lifetime based on stored session data before each request"""
    if 'username' in session and session.permanent:
        # Check if user chose to be remembered
        if session.get('remember_login'):
            flask_app.permanent_session_lifetime = timedelta(days=180)
        else:
            flask_app.permanent_session_lifetime = timedelta(days=30)

if os.getenv('FLASK_TEMPLATE_RELOAD', '0') in ('development','1','true','True'):
    print("SETTING TEMPLATES_AUTO_RELOAD is True")
    flask_app.config['TEMPLATES_AUTO_RELOAD'] = True
    flask_app.jinja_env.auto_reload = True
else:
    flask_app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 3600000
socketio = SocketIO(flask_app)

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
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    # CSP allowing inline scripts for AlpineJS but restricting other sources
    response.headers['Content-Security-Policy'] = ("default-src 'self'; "
                                                   "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                                                   "style-src 'self' 'unsafe-inline'; "
                                                   "img-src 'self' data:; "
                                                   "connect-src 'self' ws: wss: *; "
                                                   "font-src 'self'; "
                                                   "object-src 'none'; "
                                                   "base-uri 'self';")
    # Cache static assets for 3 months; versioned files (?v=) are cache-busted on deploy
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=7776000'
    return response

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

    if locale_name == "CN":
        locales = {
            "current_locale": locale_name,
            "switch_to_locale": "EN",
            "switch_to_locale_tip": "Switch to English / 切换英文",
            "visit_wiki": "更多信息: ",
            "community": "社区",
            "config_title": "仪表盘",

            "common_add": "添加",
            "common_login": "登录",
            "common_save": "保存",
            "common_enable": "启用",
            "common_disable": "停用",
            "common_unknown": "未知",
            "common_refresh": "刷新",
            "common_yes": "是",
            "common_no": "否",
            "common_select_all": "全选",
            "common_unselect_all": "全否",
            "operation_tip": "操作提示",
            "common_reboot": "重启",
            "common_reboot_later": "稍后重启",
            "common_save_success": "保存成功",
            "need_reboot_device_now": "现在重启设备?",
            "reboot_reason_power_settings": "电源策略设置已保存。部分设置需要重启设备后才能生效。",
            "rebooting_tip_message_0": "设备正在准备重启，请稍候 ...",
            "rebooting_tip_message_1": "设备正在重启中，请稍后重连 Wi-Fi 或访问后台",

            "nav_dashboard": "仪表盘",
            "nav_settings": "设置",
            "nav_modem": "短信/蜂窝网络",
            "nav_statistics": "数据统计",
            "nav_update": "更新",
            "nav_reboot_device": "重启设备",
            "nav_logout": "登出",
            "nav_screen": "迷你屏幕",

            "remember_login_status": "记住我",
            "form_login_title": "请输入密码",
            "form_login_title_tip": "系统用户 root 的密码",
            
            "login_error_invalid_credentials": "用户名或密码错误",
            "login_error_too_many_attempts": "登录尝试次数过多，请 5 分钟后再试",
            "login_error_security_token": "安全令牌无效，请重试",
            "openwrt_tooltip": "使用您登录此页面的密码或默认密码登录到 OpenWrt",
            "net_state_disabled": "已禁用",
            "net_state_no_link": "已启用，未连接",
            "net_state_connected": "已连接",
            "net_state_active_wan": "当前上网接口",

            "save_action_success_and_reboot": "设置成功，请重启",
            "save_action_success": "设置成功",
            "save_action_failed": "设置不成功，请稍后再试",

            "tab_timers": "开关机策略",
            "tab_device_control": "设备控制",
            "tab_general_settings": "通用设置",
            "tab_modem": "短信/蜂窝网络",
            "no_wifi_info_config": "没有可以配置的 Wi-Fi 信息",
            "toggle_wifi": "Wi-Fi 开关",
            "i_agree_tos": "我同意",
            "tos_text": "服务条款",
            "coming_soon": "敬请期待",
            "coming_soon_subtext": "此功能正在开发中，请稍后再试！",
            
            "sms_delete_single": "删除",
            "sms_reply": "回复",
            "sms_reply_to": "回复至：",
            "sms_reply_placeholder": "回复内容 ...",
            "sms_number_placeholder": "号码",
            "sms_send_button": "发短信",
            "sms_send": "发送",
            "sms_from": "发件人",
            "sms_to": "收件人",  
            "sms_conversations": "会话",
            "sms_traditional": "传统",
            "sms_clear_all_button": "清空全部",
            "sms_clear_all_confirm_title": "确认要清空所有短信吗？",
            "sms_clear_all_confirm_message": "此操作将删除手机和设备中存储的所有短信，无法恢复。",
            "sms_delete_confirm": "确认删除此条短信",
            "sms_delete_confirm_title": "确认删除与",
            "sms_download_all": "下载全部",
            "sms_backup_all_big": "备份所有短信",
            "sms_messages_count": "条消息",
            "alert_message_too_long": "消息太长！最多70个字符。",
            "alert_send_failed": "发送失败",
            "alert_delete_failed": "删除失败",
            "alert_network_error_send": "发送消息时网络错误",
            "alert_network_error_delete": "删除消息时网络错误",
            "alert_clear_sms_failed": "清空短信失败",
            "alert_network_error_clear": "清空短信时网络错误",
            "alert_delete_thread_failed": "删除会话消息失败",
            "sms_type_message_placeholder": "输入消息...",
            "sms_number_tip": "国内号码可以前面不用加 '+86'; 国际号码前面加国家代码，如: +1123456789",
            "sms_refresh_tooltip": "刷新短信",
            "sms_refreshing_tooltip": "正在刷新...",
            "modem_refresh_tooltip": "刷新调制解调器信息",
            "sim_not_available": "SIM卡不可用",
            "sms_show_earlier": "显示更早消息",
            
            # Hook related alerts
            "alert_hook_save_success": "Hook脚本保存成功",
            "alert_hook_save_failed": "保存失败",
            "alert_hook_network_error": "保存时网络错误",
            "alert_cleanup_failed": "清理失败",
            "alert_cleanup_error": "清理时出错",
            "alert_test_api_error": "测试Hook历史API出错",
            "alert_table_create_success": "数据表创建成功！现在可以再次测试验证",
            "alert_table_create_failed": "创建数据表失败",
            "alert_table_create_error": "创建数据表时出错",
            "alert_test_record_success": "测试记录插入成功！点击刷新查看历史记录",
            "alert_test_record_failed": "插入测试记录失败",
            "alert_test_record_error": "插入测试记录时出错",
            "alert_hook_detail_failed": "加载Hook执行详情失败",
            "alert_hook_detail_error": "加载Hook执行详情时出错",
            
            # SMS alerts
            "alert_number_invalid": "号码无效！",
            "alert_send_failed_simple": "发送失败",
            "alert_check_interval_invalid": "检查间隔无效！",
            "alert_modem_reset_success": "所有短信已清除，调制解调器重置成功！",
            "alert_modem_reset_failed": "调制解调器重置失败，请重试。",
            "alert_cpin_invalid": "CPIN必须是1到4位数字或留空。",
            
            # Generic messages
            "message_unknown_error": "未知错误",
            
            # Missing UI translations
            "canceling": "正在取消",
            "dev_upgrading": "开发版升级中",
            "important": "重要",
            "stage_complete": "完成阶段",
            "stage_update": "更新阶段",
            "stage_verify": "验证阶段",
            "updating": "正在更新",
            "uploading": "正在上传",
            
            # MCU related translations
            "mcu_firmware_matches_dev_server": "MCU固件与开发服务器版本匹配"
        }
    else:  # Default to English
        locales = {
            "current_locale": locale_name,
            "switch_to_locale": "中文",
            "switch_to_locale_tip": "切换简体中文 / Switch to Simplified Chinese",
            "visit_wiki": "More Info: ",
            "community": "Community",
            "config_title": "Dashboard",
            "common_add": "Add",
            "common_login": "Login",
            "common_save": "Save",
            "common_enable": "Enable",
            "common_disable": "Disable",
            "common_unknown": "Unknown",
            "common_refresh": "Refresh",
            "common_yes": "Yes",
            "common_no": "No",
            "common_select_all": "Select All",
            "common_unselect_all": "Unselect All",
            "operation_tip": "Operation tip",
            "common_reboot": "Reboot",
            "common_reboot_later": "Reboot later",
            "common_save_success": "Saved successfully.",
            "need_reboot_device_now": "Reboot device now?",
            "reboot_reason_power_settings": "Power settings have been saved. A restart may be required for some changes to take effect.",
            "rebooting_tip_message_0": "Device is preparing to restart, please wait...",
            "rebooting_tip_message_1": "Device is restarting, please manually reconnect to Wi-Fi later if needed.",

            "nav_dashboard": "Dashboard",
            "nav_settings": "Settings",
            "nav_modem": "SMS/Cellular",
            "nav_statistics": "Statistics",
            "nav_update": "Update",
            "nav_reboot_device": "Reboot",
            "nav_logout": "Logout",
            "nav_screen": "Mini Screen",
            
            "remember_login_status": "Remember me",
            "form_login_title": "Enter your Password",
            "form_login_title_tip": "Password for system root user",
            
            "login_error_invalid_credentials": "Invalid username or password",
            "login_error_too_many_attempts": "Too many login attempts. Please try again in 5 minutes.",
            "login_error_security_token": "Security token invalid. Please try again.",
            "openwrt_tooltip": "Login to OpenWrt using the same password you logged in with or default password",
            "net_state_disabled": "Disabled",
            "net_state_no_link": "Enabled, no link",
            "net_state_connected": "Connected",
            "net_state_active_wan": "Active internet connection",

            "save_action_success": "Saved successfully, please reboot.",
            "save_action_failed": "Save failed, please retry later.",

            "tab_timers": "Startup and Shutdown Policy",
            "tab_device_control": "Device Control",
            "tab_general_settings": "General Settings",
            "tab_modem": "SMS/Cellular",
            "no_wifi_info_config": "No available Wi-Fi configuration.",
            "toggle_wifi_ssid": "Toggle Wi-Fi",
            "i_agree_tos": "I agree to the ",
            "tos_text": "Terms of Service",
            "coming_soon": "Coming Soon",
            "coming_soon_subtext": "This feature is under development. Check back soon for updates!",
            
            "sms_delete_single": "Delete",
            "sms_reply": "Reply",
            "sms_reply_to": "Reply to:",
            "sms_reply_placeholder": "Reply content ...",
            "sms_number_placeholder": "Number",
            "sms_send_button": "Send SMS",
            "sms_send": "Send",
            "sms_from": "From",
            "sms_to": "To",
            "sms_conversations": "Conversations", 
            "sms_traditional": "Traditional",
            "sms_clear_all_button": "Clear All",
            "sms_clear_all_confirm_title": "Are you sure you want to clear all SMS messages?",
            "sms_clear_all_confirm_message": "This will delete all messages stored in both the phone and the device and cannot be undone.",
            "sms_delete_confirm": "Delete this message",
            "sms_delete_confirm_title": "Are you sure you want to delete all messages with",
            "sms_download_all": "Download All",
            "sms_backup_all_big": "Backup All Messages",
            "sms_messages_count": "messages",
            "alert_message_too_long": "Message too long! Maximum 70 characters.",
            "alert_send_failed": "Send failed",
            "alert_delete_failed": "Failed to delete message",
            "alert_network_error_send": "Network error while sending message",
            "alert_network_error_delete": "Network error while deleting message",
            "alert_clear_sms_failed": "Failed to clear SMS messages",
            "alert_network_error_clear": "Network error while clearing SMS messages",
            "alert_delete_thread_failed": "Failed to delete thread messages",
            "sms_type_message_placeholder": "Type a message...",
            "sms_number_tip": "Domestic numbers don't need '+86'; International numbers need country code, e.g: +1123456789",
            "sms_refresh_tooltip": "Refresh SMS Messages",
            "sms_refreshing_tooltip": "Refreshing...",
            "modem_refresh_tooltip": "Refresh Modem Info",
            "sim_not_available": "SIM N/A",
            "sms_show_earlier": "Show Earlier Messages",
            
            # Hook related alerts
            "alert_hook_save_success": "Hook script saved successfully",
            "alert_hook_save_failed": "Save failed",
            "alert_hook_network_error": "Network error while saving",
            "alert_cleanup_failed": "Cleanup failed",
            "alert_cleanup_error": "Error during cleanup",
            "alert_test_api_error": "Error testing hook history API",
            "alert_table_create_success": "Table created successfully! You can now test again to verify",
            "alert_table_create_failed": "Failed to create table",
            "alert_table_create_error": "Error creating table",
            "alert_test_record_success": "Test record inserted! Click Refresh to see it in the history",
            "alert_test_record_failed": "Failed to insert test record",
            "alert_test_record_error": "Error inserting test record",
            "alert_hook_detail_failed": "Failed to load hook execution details",
            "alert_hook_detail_error": "Error loading hook execution details",
            
            # SMS alerts
            "alert_number_invalid": "Number invalid!",
            "alert_send_failed_simple": "Send failed",
            "alert_check_interval_invalid": "Check interval invalid!",
            "alert_modem_reset_success": "All SMS messages cleared and modem reset successfully!",
            "alert_modem_reset_failed": "Modem reset failed, please try again.",
            "alert_cpin_invalid": "CPIN must be 1 to 4 digits or left blank.",
            
            # Generic messages
            "message_unknown_error": "Unknown error",
            
            # Missing UI translations
            "canceling": "Canceling",
            "dev_upgrading": "Dev Upgrading",
            "important": "Important",
            "stage_complete": "Complete Stage",
            "stage_update": "Update Stage",
            "stage_verify": "Verify Stage",
            "updating": "Updating",
            "uploading": "Uploading",
            
            # MCU related translations
            "mcu_firmware_matches_dev_server": "MCU firmware matches dev server version"
        }
    locales.update(oem_locales)
    
    # Cache the result
    with device_cache['lock']:
        device_cache[cache_key] = locales
        device_cache['base_locales_timestamp'] = time.time()
    
    return locales

def current_locale():
    # Check if locale is set in cookie first, then session
    if 'locale' not in session:
        # Check for saved language preference in browser cookie
        saved_locale = request.cookies.get('pcat_lang')
        if saved_locale in ['EN', 'CN', 'zh_CN']:
            c_locale = saved_locale
        else:
            # Detect browser language preference
            browser_lang = request.headers.get('Accept-Language', '').lower()
            # Default to English unless Chinese is explicitly preferred
            if 'zh' in browser_lang:
                c_locale = "CN"  # Simplified Chinese
            else:
                c_locale = "EN"  # Default to English
        # Cache the detected locale in session for subsequent requests
        session["locale"] = c_locale
    else:
        c_locale = session["locale"]
    return c_locale

def is_chinese_locale():
    """Helper function to check if current locale is any Chinese variant"""
    locale = current_locale()
    return locale in ["CN", "zh_CN"]

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
    return decorated_function

@socketio.event
def client_connected(message):
    emit('connected_response', {'data': 'Welcome\n'})

@socketio.event
def request_install(message):
    filename = message.get("filename")
    global install_thread
    with thread_lock:
        if install_thread is None:
            install_thread = socketio.start_background_task(background_install_thread, "local", filename)
    emit('log_response', {'data': "Start install {filename}\n".format(filename = filename)})

@socketio.event
def request_install_package(message):
    package = message.get("package")
    global install_thread
    with thread_lock:
        if install_thread is None:
            install_thread = socketio.start_background_task(background_install_thread, "remote", package)
    emit('log_response', {'data': "Start download {package}\n".format(package = package)})

@flask_app.route("/tos")
def tos_action():
    return render_template(nt.tos_file_name)

@flask_app.route("/")
def home_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    c_locale = current_locale()
    if c_locale == "CN":
        locales = {
            "card_main_status": "主要状态",
            "card_system": "系统",
            "card_connection": "连接",
            "card_hardware": "硬件",

            "connection": "连接",
            "connection_unknown": "未知",
            "connection_mobile": "蜂窝网络",
            "connection_wired": "有线",
            "connection_none": "无",
            "connected": "已连接",
            "disconnected": "未连接",
            "disabled": "已禁用",
            "unknown": "未知",
            "wan_ethernet": "WAN (以太网)",
            "wan_cellular": "WAN (蜂窝网络)",
            "wwan_powered_off": "已关闭",
            "lan_ethernet": "LAN (以太网)",
            "signal_strength_tooltip": "信号强度百分比",
            "dl_up_speed": "上传/下载",
            "uptime": "开机时长",
            "uptime_day": "天",
            "uptime_days": "天",
            "uptime_hour": "小时",
            "uptime_hours": "小时", 
            "uptime_minute": "分钟",
            "uptime_minutes": "分钟",
            "uptime_second": "秒",
            "uptime_seconds": "秒",
            "uptime_up": "运行",

            "hostname": "主机名",
            "model": "型号",
            "firmware_version": "MCU 版本",
            "kernel_version": "内核版本",

            "ip_address": "公网 IP 地址",
            "ipv6_address": "公网 IPv6",
            "wan_ip": "WAN IP 地址",
            "dhcp_clients": "DHCP 客户端",
            "wifi_clients": "Wi-Fi 客户端",
            "modem_mode": "网络模式",

            "battery_status": "电池电量",
            "battery_voltage": "电池电压",
            "battery_vcw": "电池状态",
            "battery_current": "电池电流",
            "battery_wattage": "电池功率",
            "battery_charging": "充电中",
            "battery_discharging": "电池供电",
            "battery_full": "电池充满",
            "battery_remaining_discharge_time": "剩余放电时间",
            "battery_remaining_charge_time": "充至90%剩余时间",
            "charge_voltage": "输入电压",
            "board_temperature": "主板温度",
            "sd_card": "SD 卡",
            "sim_card": "SIM 卡",
            "sd_not_exist": "不存在",
            "sd_exist": "已插入",
            "openwrt_version": "OpenWRT 版本",
            "machine_sn": "设备序列号",
            "device_type_onboard": "板载",
            "device_type_pcie": "PCIE",
            "device_type_builtin": "内置",
            "device_type_unknown": "未知",
            "device_edition_home": "家庭版",
            "device_edition_cellular": "移动版",
            "wifi": "Wi-Fi",
            "na": "不适用"
        }
    else:
        locales = {
            "card_main_status": "Main Status",
            "card_system": "System",
            "card_connection": "Connection",
            "card_hardware": "Hardware",

            "connection": "Connection",
            "connection_unknown": "Unknown",
            "connection_mobile": "Mobile",
            "connection_wired": "Wired",
            "connection_none": "None",
            "connected": "Connected",
            "disconnected": "Disconnected",
            "disabled": "Disabled",
            "unknown": "Unknown",
            "wan_ethernet": "WAN (Ethernet)",
            "wan_cellular": "WAN (Cellular)",
            "wwan_powered_off": "Powered Off",
            "lan_ethernet": "LAN (Ethernet)",
            "signal_strength_tooltip": "Signal strength percentage",
            "dl_up_speed": "Upload/Download",
            "uptime": "Uptime",
            "uptime_day": "day",
            "uptime_days": "days",
            "uptime_hour": "hour",
            "uptime_hours": "hours", 
            "uptime_minute": "minute",
            "uptime_minutes": "minutes",
            "uptime_second": "second",
            "uptime_seconds": "seconds",
            "uptime_up": "up",

            "hostname": "Hostname",
            "model": "Model",
            "firmware_version": "MCU Version",
            "kernel_version": "Kernel Version",

            "ip_address": "Public IP Address",
            "ipv6_address": "Public IPv6",
            "wan_ip": "WAN IP Address",
            "dhcp_clients": "DHCP Clients",
            "wifi_clients": "Wi-Fi Clients",
            "modem_mode": "Modem Mode",

            "battery_status": "Battery SoC",
            "battery_voltage": "Battery Voltage",
            "battery_vcw": "Battery Status",
            "battery_current": "Battery Current",
            "battery_wattage": "Battery Wattage",
            "battery_charging": "Charging",
            "battery_discharging": "On Battery",
            "battery_full": "Full",
            "battery_remaining_discharge_time": "Remaining Battery Time",
            "battery_remaining_charge_time": "Charge to 90%",
            "charge_voltage": "Input Voltage",
            "board_temperature": "Motherboard Temperature",
            "sd_card": "SD Card",
            "sim_card": "SIM Card",
            "sd_not_exist": "Not Detected",
            "sd_exist": "Present",
            "openwrt_version": "OpenWRT Version",
            "machine_sn": "Device S/N",
            "device_type_onboard": "Onboard",
            "device_type_pcie": "PCIE",
            "device_type_builtin": "Builtin",
            "device_type_unknown": "Unknown",
            "device_edition_home": "Home",
            "device_edition_cellular": "Cellular",
            "wifi": "Wi-Fi",
            "na": "N/A"
        }

    locales.update(base_locales())
    
    # Get remembered password for OpenWrt form
    remembered_password = session.get('remembered_password', None)
    if not remembered_password:
        # Try to get from config or use None (will default to 'photonicat' in template)
        remembered_password = getattr(pcat_config, 'default_luci_password', None)
        
    return render_template(nt.index_file_name, locales = locales, board_name = board_name, remembered_password = remembered_password)

@api_auth_required
@flask_app.route("/api/v1/board_name")
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
    
    # Update session
    session["locale"] = new_locale

    # Sync language to OpenWrt LuCI setting
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

# Dashboard data caching for efficient data sharing and performance
dashboard_cache = {
    'data': None,
    'timestamp': 0,
    'lock': threading.Lock(),
    'generation_time': 0  # Track data generation time for performance monitoring
}
DASHBOARD_CACHE_DURATION = 5  # seconds - balance between freshness and performance

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
EXPENSIVE_OPS_CACHE_DURATION = 15  # seconds
WIFI_OPS_CACHE_DURATION = 60  # seconds

# Network interfaces cache for performance optimization
network_interfaces_cache = {
    'data': None,
    'timestamp': 0,
    'generation_time': 0,
    'lock': threading.Lock()
}
NETWORK_INTERFACES_CACHE_DURATION = 15  # seconds - network configs change infrequently

def get_modem_mode():
    """Radio generation for the dashboard and the LCD.

    pcat-manager's mode comes from the dialer (quectel-cm), which only reports
    NR5G-NSA when the LTE cell broadcasts the EN-DC availability flag in SIB2.
    Many networks do not, so an active EN-DC connection is reported as LTE.
    AT+QNWINFO lists the NR leg whenever it is active, so trust cell_tech when it
    says 5G.
    """
    socket_mode = pc_app.socket_client.modem_mode
    cell_tech = pc_app.modem_client.basic.get("cell_tech", "")
    if cell_tech == "5G":
        return cell_tech
    return socket_mode or cell_tech


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

def batch_uci_get(keys):
    """Batch UCI get operations for better performance"""
    import subprocess

    # Use uci export to get all values at once if possible
    try:
        # Build single uci command with multiple gets
        cmd = ["uci", "batch"]
        batch_commands = [f"get {key}" for key in keys]

        result = subprocess.run(cmd, input='\n'.join(batch_commands),
                              capture_output=True, text=True, timeout=5)

        if result.returncode == 0:
            values = result.stdout.strip().split('\n')
            return dict(zip(keys, values))
        else:
            # Fallback to individual calls on batch failure
            results = {}
            for key in keys:
                try:
                    value = pc_app.uci_get(key)
                    results[key] = value
                except:
                    results[key] = ""
            return results

    except Exception as e:
        pcat_logger.logger.debug(f"UCI batch operation failed, falling back to individual calls: {e}")
        # Fallback to individual calls
        results = {}
        for key in keys:
            try:
                value = pc_app.uci_get(key)
                results[key] = value
            except:
                results[key] = ""
        return results

def get_network_interfaces_data_optimized():
    """Optimized version of network interfaces data with batched UCI operations"""

    # Define all UCI keys we need to query at once
    uci_keys = [
        "network.wan.disabled",
        "network.wan.device",
        "network.wwan_lte.disabled",
        "network.wwan_5g.disabled",
        "network.wwan_fm350.disabled",
        "wireless.radio0.disabled",
        "wireless.radio1.disabled",
        "network.@device[0].name",
        "network.@device[0].ports",
        "network.@device[1].name",
        "network.@device[1].ports",
        "network.@device[2].name",
        "network.@device[2].ports"
    ]

    # Process WAN interface status
    lan_enabled = pc_app.is_lan_interface_enabled()
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

    return {
        "wan_enabled": wan_enabled,
        "sim_enabled": wwan_enabled,
        "lan_enabled": lan_enabled,
        "onboard_wifi_enabled": onboard_wifi_enabled,
        "pcie_wifi_enabled": pcie_wifi_enabled,
        "lan_exists": lan_exists,
        "wan_exists": wan_exists,
        "onboard_wifi_exists": onboard_wifi_exists,
        "pcie_wifi_exists": pcie_wifi_exists,
        "wan_carrier": wan_carrier,
        "lan_carrier": lan_carrier,
        "network_route_mode": network_route_mode,
        "active_egress": active_egress
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
                "sim_enabled": interfaces_data["sim_enabled"],
                "lan_enabled": interfaces_data["lan_enabled"],
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
                "active_egress": interfaces_data["active_egress"]
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
                    "active_egress": ""
                }

def get_dashboard_data():
    """Get dashboard data with caching to enable single refresh sharing"""
    with dashboard_cache['lock']:
        current_time = time.time()
        
        # Return cached data if still valid
        if (dashboard_cache['data'] is not None and 
            current_time - dashboard_cache['timestamp'] < DASHBOARD_CACHE_DURATION):
            return dashboard_cache['data']
        
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
            "carrier": pc_app.modem_client.basic.get("cell_tech", "") or pc_app.socket_client.modem_mode,
            "dhcp_clients_count": get_cached_expensive_op('dhcp_clients_count', pc_app.main_app.get_dhcp_clients_count),
            "wifi_clients_count": get_cached_expensive_op('wifi_clients_count', command_parser.get_wifi_clients_count),
            "wifi_interfaces": get_cached_expensive_op('wifi_interfaces', lambda: command_parser.parse_uci_show_wireless(False)),
            "on_charging": pc_app.socket_client.on_charging,
            "charge_percent": pc_app.socket_client.charge_percent,
            "voltage": pc_app.socket_client.battery_voltage,
            "charge_voltage": pc_app.socket_client.charger_voltage,
            "board_temperature": pc_app.socket_client.board_temperature,
            "modem_temperature": pc_app.modem_client.basic.get("qtemp_sensors", ""),
            "modem_signal_strength": signal_strength_pct,
            "isp_name": pc_app.socket_client.isp_name,
            "sd_state": pc_app.sd_card_status(),
            "sim_state": pc_app.modem_client.get_inferred_sim_state(pc_app.socket_client.sim_state),

            # Additional fields for status bar compatibility
            "date": date_string,
            "time": time_string,
            "system_status": getattr(pc_app.main_app, 'system_status', 0),
            "carmode": pc_app.socket_client.charger_on_auto_start_mode,
            "network_route_mode": pc_app.socket_client.network_route_mode,
            "modem_mode": get_modem_mode(),

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
        if saved_power_mode == "direct_power":
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
                # Fallback: calculate from sysfs current if primary method failed
                if remaining_time is None and battery_current_a != 0:
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

@api_auth_required
@flask_app.route("/api/v1/dashboard.json", methods=['GET'])
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

@api_auth_required
@flask_app.route("/api/v1/dashboard/performance.json", methods=['GET'])
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

@api_auth_required
@flask_app.route("/api/v1/dev_server_check.json", methods=['GET'])
def dev_server_check():
    """
    Check if dev server is reachable and get latest firmware info.
    Returns dev server status and firmware information.
    """
    import re
    from urllib.parse import urljoin
    from email.utils import parsedate_to_datetime
    from html.parser import HTMLParser
    
    class FirmwareParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.firmware_files = []
            self.file_dates = {}
            self.current_link = None
            self.in_link = False
            self.in_date_cell = False
            
        def handle_starttag(self, tag, attrs):
            if tag == 'a':
                self.in_link = True
                for attr_name, attr_value in attrs:
                    if attr_name == 'href' and attr_value and attr_value.endswith('.img.gz'):
                        if 'photonicatwrt' in attr_value and 'sysupgrade' in attr_value:
                            self.current_link = attr_value
                            self.firmware_files.append(attr_value)
            elif tag == 'td' and self.current_link:
                # Look for date/time cells in directory listing
                self.in_date_cell = True
                
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
    
    def extract_revision_number(filename):
        """Extract rXXXX revision number from filename"""
        match = re.search(r'r(\d+)', filename)
        return int(match.group(1)) if match else 0
    
    def extract_hash_code(version_string):
        """Extract hash code from version string like r7566-973b5947b"""
        match = re.search(r'r\d+-([a-f0-9]+)', version_string)
        return match.group(1) if match else ""
    
    try:
        dev_server_url = "http://192.168.8.28:81/rk3576-photonicat2-openwrt/"
        
        # Check if dev server is reachable
        with urllib.request.urlopen(dev_server_url, timeout=20) as response:
            html_content = response.read().decode('utf-8')
        
        # Parse HTML to find firmware files
        parser = FirmwareParser()
        parser.feed(html_content)
        
        if not parser.firmware_files:
            return {
                "status": "error",
                "message": "No firmware files found on dev server",
                "reachable": True
            }
        
        # Find latest firmware file (highest revision number)
        latest_file = max(parser.firmware_files, key=extract_revision_number)
        latest_revision = extract_revision_number(latest_file)
        
        # Get current local version for comparison
        local_version = pc_app.main_app.board_info.get("openwrt_version", "")
        local_revision = extract_revision_number(local_version)
        local_hash = extract_hash_code(local_version)
        dev_hash = extract_hash_code(latest_file)
        
        # Get build date for the dev firmware file.
        # Prefer the file's Last-Modified HTTP header (authoritative server modify
        # date, works with Caddy/nginx auto-index), falling back to the date column
        # scraped from the directory listing if the header is unavailable.
        dev_build_date = parser.file_dates.get(latest_file, "")
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
        
        response_data = {
            "status": "ok",
            "reachable": True,
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

@api_auth_required
@flask_app.route("/api/v1/firmware_update_check.json", methods=['GET'])
def firmware_update_check():
    """
    Check for firmware updates by comparing local version with remote version.
    Returns update availability and download information.
    """
    def extract_revision_number(version_string):
        """Extract rXXXX revision number from version string"""
        match = re.search(r'r(\d+)', version_string)
        return int(match.group(1)) if match else 0
    
    try:
        # Get current local version
        local_version = pc_app.main_app.board_info.get("openwrt_version", "")
        local_revision = extract_revision_number(local_version)
        
        # Fetch remote version info
        remote_url = "https://dl.photonicat.com/images/photonicat2/openwrt/latest_image.txt"
        pcat_logger.logger.info(f"Checking firmware updates from: {remote_url}")
        
        try:
            data = robust_urlopen(remote_url, timeout=20)
            remote_filename = data.decode('utf-8').strip()
            pcat_logger.logger.info(f"Successfully fetched remote filename: {remote_filename}")
        except Exception as e:
            pcat_logger.logger.error(f"Failed to fetch firmware info: {str(e)}")
            raise
        
        # Extract remote revision number
        remote_revision = extract_revision_number(remote_filename)
        
        # Compare versions - only show update if remote is newer
        update_available = remote_revision > local_revision
        
        # Try to get SHA256 checksum for production firmware
        sha256_hash = ""
        sha256_available = False
        if update_available:
            try:
                sha256_url = f"https://dl.photonicat.com/images/photonicat2/openwrt/{remote_filename}.sha256"
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
        
        # Get the build date from the firmware file's Last-Modified header.
        remote_build_date = ""
        remote_build_epoch = 0
        if update_available:
            try:
                from email.utils import parsedate_to_datetime
                fw_url = f"https://dl.photonicat.com/images/photonicat2/openwrt/{remote_filename}"
                head_req = urllib.request.Request(fw_url, method="HEAD")
                with urllib.request.urlopen(head_req, timeout=10) as head_resp:
                    last_modified = head_resp.headers.get("Last-Modified", "")
                if last_modified:
                    lm_dt = parsedate_to_datetime(last_modified)
                    remote_build_epoch = int(lm_dt.timestamp())
                    remote_build_date = lm_dt.astimezone().strftime("%Y-%m-%d %H:%M")
            except Exception as e:
                pcat_logger.logger.warning(f"Error fetching production firmware Last-Modified: {e}")

        response_data = {
            "status": "ok",
            "local_version": local_version,
            "local_revision": local_revision,
            "remote_filename": remote_filename,
            "remote_revision": remote_revision,
            "update_available": update_available,
            "download_url": f"https://dl.photonicat.com/images/photonicat2/openwrt/{remote_filename}" if update_available else "",
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

# Global variables for firmware upgrade tracking
firmware_upgrade_thread = None
firmware_upgrade_lock = threading.Lock()

# Global status tracking for firmware upgrade (persists across page reloads)
firmware_upgrade_status = {
    'active': False,
    'stage': '',
    'progress': 0,
    'message': '',
    'firmware_url': '',
    'keep_config': True,
    'keep_config_option': '-c',
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
    
    socketio.emit('firmware_upgrade_status', firmware_upgrade_status.copy())

def background_firmware_upgrade_thread(firmware_url, keep_config, keep_config_option="-c", download_method="wget"):
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
        'keep_config': keep_config,
        'keep_config_option': keep_config_option,
        'start_time': time.time(),
        'error': None
    })
    
    # Debug logging
    pcat_logger.logger.info(f"Starting firmware upgrade process: {firmware_url}")
    pcat_logger.logger.info(f"Keep config: {keep_config}")
    pcat_logger.logger.info(f"Keep config option: {keep_config_option}")
    
    try:
        # Stage 1: Download with improved progress tracking
        update_firmware_upgrade_status('download', 0, 'Initializing firmware download...')
        socketio.sleep(0.3)

        # Use /tmp directory for downloads as requested
        temp_dir = '/tmp'
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir, exist_ok=True)

        filename = firmware_url.split('/')[-1]
        temp_file = os.path.join(temp_dir, f'firmware_upgrade_{filename}')

        pcat_logger.logger.info(f"Download target: {temp_file}")
        pcat_logger.logger.info(f"Temp directory: {temp_dir}")

        update_firmware_upgrade_status('download', 0, f'Target file: {filename}')
        socketio.sleep(0.3)
        update_firmware_upgrade_status('download', 0, f'Download directory: {temp_dir}')
        socketio.sleep(0.3)

        # Check available disk space
        try:
            statvfs = os.statvfs(temp_dir)
            free_mb = (statvfs.f_bavail * statvfs.f_frsize) // (1024 * 1024)
            update_firmware_upgrade_status('download', 0, f'Available disk space: {free_mb}MB')
            socketio.sleep(0.3)
        except Exception:
            pass

        # First, get the file size for accurate progress tracking
        update_firmware_upgrade_status('download', 0, 'Querying remote server for firmware size...')
        try:
            import urllib.request
            pcat_logger.logger.info(f"Fetching file size from: {firmware_url}")
            with urllib.request.urlopen(firmware_url) as response:
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

        socketio.sleep(0.3)
        update_firmware_upgrade_status('download', 1, f'Using download method: {download_method}')
        socketio.sleep(0.3)

        # Clean up previous download if exists
        if os.path.exists(temp_file):
            old_size = os.path.getsize(temp_file)
            update_firmware_upgrade_status('download', 1, f'Removing previous download ({old_size // (1024*1024)}MB)...')
            socketio.sleep(0.3)

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
        download_process = subprocess.Popen(
            cmd_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        pcat_logger.logger.info(f"{download_method} process started with PID: {download_process.pid}")
        update_firmware_upgrade_status('download', 3, f'{download_method} process started (PID: {download_process.pid})')
        socketio.sleep(0.3)
        
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
        
        if download_process.returncode != 0:
            stderr_output = download_process.stderr.read()
            pcat_logger.logger.error(f"{download_method} stderr: {stderr_output}")
            stdout_output = download_process.stdout.read()
            pcat_logger.logger.error(f"{download_method} stdout: {stdout_output}")
            
            # Also log download log file if it exists
            if os.path.exists(download_log):
                try:
                    with open(download_log, 'r') as f:
                        log_content = f.read()
                        pcat_logger.logger.error(f"{download_method} log file content:\n{log_content}")
                except:
                    pcat_logger.logger.warning(f"Could not read {download_method} log file")
                    
            raise Exception(f"Download failed: {stderr_output}")
            
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
        socketio.sleep(0.5)

        # Stage 1.5: SHA256 Verification (if available)
        try:
            sha256_url = firmware_url + '.sha256'
            pcat_logger.logger.info(f"Attempting to download SHA256 checksum from: {sha256_url}")
            update_firmware_upgrade_status('verify', 0, 'Starting integrity verification...')
            socketio.sleep(0.3)
            update_firmware_upgrade_status('verify', 10, 'Downloading SHA256 checksum file...')
            
            try:
                sha256_data = robust_urlopen(sha256_url, timeout=10)
                sha256_content = sha256_data.decode('utf-8').strip()
                pcat_logger.logger.info(f"Downloaded SHA256 content: {sha256_content}")
                
                # Parse SHA256 content (format: "hash filename" or just "hash")
                expected_sha256 = sha256_content.split()[0].lower()
                pcat_logger.logger.info(f"Expected SHA256: {expected_sha256}")
                
                update_firmware_upgrade_status('verify', 30, f'Expected SHA256: {expected_sha256[:16]}...')
                socketio.sleep(0.3)
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
                socketio.sleep(0.3)
                update_firmware_upgrade_status('verify', 90, 'Comparing checksums...')
                socketio.sleep(0.3)

                if calculated_sha256 == expected_sha256:
                    pcat_logger.logger.info("✅ SHA256 verification PASSED - firmware integrity verified")
                    update_firmware_upgrade_status('verify', 100, '✅ SHA256 verification PASSED - firmware integrity verified')
                else:
                    error_msg = f"❌ SHA256 verification FAILED! Expected: {expected_sha256}, Got: {calculated_sha256}"
                    pcat_logger.logger.error(error_msg)
                    raise Exception(error_msg)
                    
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    pcat_logger.logger.warning("SHA256 checksum file not found - skipping verification")
                    update_firmware_upgrade_status('verify', 100, '⚠️ SHA256 checksum not available - skipping verification')
                else:
                    raise e
            except Exception as e:
                pcat_logger.logger.warning(f"SHA256 verification failed: {str(e)} - continuing without verification")
                update_firmware_upgrade_status('verify', 100, f'⚠️ SHA256 verification failed: {str(e)} - continuing')
                
        except Exception as e:
            pcat_logger.logger.warning(f"SHA256 verification process failed: {str(e)} - continuing without verification")
            update_firmware_upgrade_status('verify', 100, '⚠️ SHA256 verification unavailable - continuing without verification')
        
        # Stage 2: Prepare
        pcat_logger.logger.info("Starting firmware preparation stage")
        update_firmware_upgrade_status('prepare', 0, 'Starting firmware preparation...')
        socketio.sleep(0.3)

        # Verify file integrity
        update_firmware_upgrade_status('prepare', 10, 'Checking firmware file exists...')
        pcat_logger.logger.info(f"Verifying downloaded file: {temp_file}")
        if not os.path.exists(temp_file):
            pcat_logger.logger.error(f"File does not exist: {temp_file}")
            raise Exception("Downloaded firmware file is missing")

        socketio.sleep(0.3)
        file_size = os.path.getsize(temp_file)
        pcat_logger.logger.info(f"File size verification: {file_size} bytes")
        update_firmware_upgrade_status('prepare', 30, f'Firmware file size: {file_size // (1024*1024)}MB ({file_size:,} bytes)')
        socketio.sleep(0.3)

        update_firmware_upgrade_status('prepare', 50, 'Validating firmware file size...')
        if file_size < 1024*1024:  # Less than 1MB
            pcat_logger.logger.error(f"File too small: {file_size} bytes")
            raise Exception(f"Downloaded firmware file is too small: {file_size} bytes")

        socketio.sleep(0.3)
        pcat_logger.logger.info("File verification passed")
        update_firmware_upgrade_status('prepare', 70, 'File size validation passed')
        socketio.sleep(0.3)

        # Report config preservation mode
        if keep_config and keep_config_option:
            if keep_config_option == '-c':
                config_desc = 'Preserve changed files in /etc/'
            elif keep_config_option == '-o':
                config_desc = 'Preserve changed files in / (except packages)'
            else:
                config_desc = f'Config option: {keep_config_option}'
            update_firmware_upgrade_status('prepare', 85, f'Configuration mode: {config_desc}')
        else:
            update_firmware_upgrade_status('prepare', 85, 'Configuration mode: Fresh install (no config preservation)')
        socketio.sleep(0.3)

        update_firmware_upgrade_status('prepare', 100, 'Firmware prepared for installation')
        
        # Stage 3: Install
        pcat_logger.logger.info("Starting firmware installation stage")
        pcat_logger.logger.warning("CRITICAL: Firmware installation starting - DO NOT POWER OFF!")
        update_firmware_upgrade_status('install', 0, 'Starting firmware installation...')
        socketio.sleep(0.5)
        update_firmware_upgrade_status('install', 5, '⚠️ DO NOT POWER OFF THE DEVICE!')
        socketio.sleep(0.3)
        update_firmware_upgrade_status('install', 10, 'Building sysupgrade command...')
        socketio.sleep(0.3)

        # Prepare sysupgrade command
        sysupgrade_cmd = ['sysupgrade']
        if keep_config and keep_config_option:
            # Use the specific keep config option (-c or -o)
            sysupgrade_cmd.append(keep_config_option)
            if keep_config_option == '-c':
                pcat_logger.logger.info("sysupgrade will preserve all changed files in /etc/ (-c flag)")
            elif keep_config_option == '-o':
                pcat_logger.logger.info("sysupgrade will preserve all changed files in /, except packages (-o flag)")
            else:
                pcat_logger.logger.info(f"sysupgrade using keep config option: {keep_config_option}")
        else:
            # No configuration preservation (fresh install)
            sysupgrade_cmd.append('-n')  # Do not save configuration over reflash
            pcat_logger.logger.info("sysupgrade will perform fresh install with no configuration preservation (-n flag)")
            
        sysupgrade_cmd.append(temp_file)
        pcat_logger.logger.info(f"sysupgrade command: {' '.join(sysupgrade_cmd)}")
        update_firmware_upgrade_status('install', 20, f'Command: {" ".join(sysupgrade_cmd)}')
        socketio.sleep(0.3)

        # Execute sysupgrade detached from Flask process
        pcat_logger.logger.info("Starting detached sysupgrade process...")
        update_firmware_upgrade_status('install', 30, 'Setting up detached sysupgrade process...')
        socketio.sleep(0.3)

        # Create status file for progress reporting
        status_file = '/tmp/sysupgrade_status.json'
        pid_file = '/tmp/sysupgrade.pid'
        log_file = '/tmp/sysupgrade.log'

        update_firmware_upgrade_status('install', 40, 'Creating status tracking files...')
        socketio.sleep(0.3)

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
        socketio.sleep(0.3)
        update_firmware_upgrade_status('install', 60, 'Preparing to hand off to sysupgrade process...')

        # Give UI time to update before starting sysupgrade
        socketio.sleep(2)

        # Transition to restart stage
        update_firmware_upgrade_status('restart', 0, 'Launching sysupgrade in detached mode...')
        socketio.sleep(1)  # Allow UI to update
        
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
            
            # Log start
            echo "[$(date)] Starting sysupgrade: {' '.join(sysupgrade_cmd)}" >> {log_file}
            
            # Update status to installing
            echo '{{
                "stage": "installing",
                "progress": 30,
                "message": "Running sysupgrade...",
                "pid": $$,
                "started": {time.time()}
            }}' > {status_file}
            
            # Execute sysupgrade and capture output
            {' '.join(sysupgrade_cmd)} >> {log_file} 2>&1
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
            
            # Clean up PID file
            rm -f {pid_file}
            
            # Schedule Flask termination after brief delay
            sleep 5
            
            # Find and terminate Flask process gracefully
            pkill -f "pcat-manager-web-main.py" || true
            
            # Force reboot if needed
            sleep 2
            /sbin/reboot
            '''
        ]

        # Start detached process
        update_firmware_upgrade_status('restart', 5, 'Starting sysupgrade process with nohup...')
        pcat_logger.logger.info(f"Starting detached sysupgrade command")
        detached_process = subprocess.Popen(
            detached_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid  # Create new process group
        )

        pcat_logger.logger.info(f"Detached sysupgrade process group started with PID: {detached_process.pid}")
        update_firmware_upgrade_status('restart', 10, f'Sysupgrade process launched (PID: {detached_process.pid})')
        socketio.sleep(0.3)
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
            socketio.sleep(1)

        # Final status update
        pcat_logger.logger.info("Sysupgrade process detached successfully - Flask can now safely terminate")
        update_firmware_upgrade_status('restart', 50, 'Firmware flashing in progress - process running independently')
        socketio.sleep(0.3)
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
            
        # Final status update before system restart
        socketio.sleep(0.5)
        update_firmware_upgrade_status('restart', 70, 'Sysupgrade process is flashing firmware to device...')
        socketio.sleep(0.5)
        update_firmware_upgrade_status('restart', 80, 'Waiting for firmware flash to complete...')
        socketio.sleep(0.5)
        update_firmware_upgrade_status('restart', 90, 'Preparing for system restart...')
        socketio.sleep(0.5)
        pcat_logger.logger.info("Firmware upgrade completed successfully - system will restart")
        update_firmware_upgrade_status('completed', 100, 'Firmware upgrade complete! System is restarting...')

        # Brief delay to ensure final status is sent to clients
        socketio.sleep(2)

        # Initiate system restart - the sysupgrade command should handle this automatically
        # but we'll add a fallback
        socketio.sleep(1)
        pcat_logger.logger.info("Initiating system reboot as fallback...")
        subprocess.run(['reboot'], check=False)
        
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
    
    socketio.emit('firmware_checkpoint_status', status)

def background_firmware_checkpoint_thread(firmware_url, keep_config, keep_config_option="-c", download_method="wget"):
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

def background_firmware_install_thread(file_path, sha256, keep_config, keep_config_option="-c"):
    """Background thread for firmware installation from checkpoint file"""
    import subprocess
    import hashlib
    import os
    import time
    
    try:
        pcat_logger.logger.info(f"Starting firmware installation from checkpoint: {file_path}")
        update_firmware_upgrade_status('prepare', 0, 'Starting checkpoint firmware installation...')
        socketio.sleep(0.3)
        update_firmware_upgrade_status('prepare', 5, f'Checkpoint file: {os.path.basename(file_path)}')
        socketio.sleep(0.3)

        # Verify file exists
        update_firmware_upgrade_status('prepare', 10, 'Checking checkpoint file exists...')
        if not os.path.exists(file_path):
            raise Exception(f"Checkpoint file not found: {file_path}")

        file_size = os.path.getsize(file_path)
        update_firmware_upgrade_status('prepare', 15, f'Checkpoint file size: {file_size // (1024*1024)}MB ({file_size:,} bytes)')
        socketio.sleep(0.3)

        # Re-verify SHA256 if provided
        if sha256:
            pcat_logger.logger.info("Re-verifying SHA256 before installation...")
            update_firmware_upgrade_status('verify', 0, 'Re-verifying firmware integrity before installation...')
            socketio.sleep(0.3)
            update_firmware_upgrade_status('verify', 20, f'Expected SHA256: {sha256[:16]}...')
            socketio.sleep(0.3)
            update_firmware_upgrade_status('verify', 40, 'Calculating SHA256 hash of checkpoint file...')

            file_sha256 = hashlib.sha256()
            with open(file_path, 'rb') as f:
                while chunk := f.read(8192):
                    file_sha256.update(chunk)

            calculated_sha256 = file_sha256.hexdigest()
            update_firmware_upgrade_status('verify', 70, f'Calculated SHA256: {calculated_sha256[:16]}...')
            socketio.sleep(0.3)
            update_firmware_upgrade_status('verify', 80, 'Comparing checksums...')
            socketio.sleep(0.3)
            if calculated_sha256.lower() != sha256.lower():
                raise Exception(f"SHA256 verification failed. Expected: {sha256}, Got: {calculated_sha256}")

            pcat_logger.logger.info("SHA256 verification passed")
            update_firmware_upgrade_status('verify', 100, 'SHA256 verification PASSED')
        else:
            update_firmware_upgrade_status('verify', 100, 'No SHA256 provided - skipping verification')

        socketio.sleep(0.3)
        update_firmware_upgrade_status('prepare', 40, 'Integrity verification completed')
        socketio.sleep(0.3)

        # Report config preservation mode
        if keep_config and keep_config_option:
            if keep_config_option == '-c':
                config_desc = 'Preserve changed files in /etc/'
            elif keep_config_option == '-o':
                config_desc = 'Preserve changed files in / (except packages)'
            else:
                config_desc = f'Config option: {keep_config_option}'
            update_firmware_upgrade_status('prepare', 50, f'Configuration mode: {config_desc}')
        else:
            update_firmware_upgrade_status('prepare', 50, 'Configuration mode: Fresh install (no config preservation)')
        socketio.sleep(0.3)

        # Prepare for installation
        pcat_logger.logger.warning("CRITICAL: Firmware installation starting - DO NOT POWER OFF!")
        update_firmware_upgrade_status('install', 0, 'Starting firmware installation...')
        socketio.sleep(0.5)
        update_firmware_upgrade_status('install', 5, 'DO NOT POWER OFF THE DEVICE!')
        socketio.sleep(0.3)
        update_firmware_upgrade_status('install', 10, 'Building sysupgrade command...')
        socketio.sleep(0.3)

        # Prepare sysupgrade command
        sysupgrade_cmd = ['sysupgrade']
        if keep_config and keep_config_option:
            sysupgrade_cmd.append(keep_config_option)
            pcat_logger.logger.info(f"sysupgrade will use config option: {keep_config_option}")
        else:
            sysupgrade_cmd.append('-n')
            pcat_logger.logger.info("sysupgrade will perform fresh install with no configuration preservation (-n flag)")

        sysupgrade_cmd.append(file_path)
        pcat_logger.logger.info(f"sysupgrade command: {' '.join(sysupgrade_cmd)}")
        update_firmware_upgrade_status('install', 20, f'Command: {" ".join(sysupgrade_cmd)}')
        socketio.sleep(0.3)

        # Execute sysupgrade detached from Flask process
        pcat_logger.logger.info("Starting detached sysupgrade process...")
        update_firmware_upgrade_status('install', 30, 'Setting up detached sysupgrade process...')
        socketio.sleep(0.3)

        # Create status file for progress reporting
        import json
        status_file = '/tmp/sysupgrade_status.json'
        pid_file = '/tmp/sysupgrade.pid'
        log_file = '/tmp/sysupgrade.log'

        update_firmware_upgrade_status('install', 40, 'Creating status tracking files...')
        socketio.sleep(0.3)

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
        socketio.sleep(0.3)
        update_firmware_upgrade_status('install', 60, 'Preparing to hand off to sysupgrade process...')

        socketio.sleep(2)

        update_firmware_upgrade_status('restart', 0, 'Launching sysupgrade in detached mode...')
        socketio.sleep(1)
        
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
            
            # Log start
            echo "[$(date)] Starting sysupgrade: {' '.join(sysupgrade_cmd)}" >> {log_file}
            
            # Update status to installing
            echo '{{
                "stage": "installing",
                "progress": 30,
                "message": "Running sysupgrade...",
                "pid": $$,
                "started": {time.time()}
            }}' > {status_file}
            
            # Execute sysupgrade and capture output
            {' '.join(sysupgrade_cmd)} >> {log_file} 2>&1
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
            
            # Clean up PID file
            rm -f {pid_file}
            
            # Schedule Flask termination after brief delay
            sleep 5
            
            # Find and terminate Flask process gracefully
            pkill -f "pcat-manager-web-main.py" || true
            
            # Force reboot if needed
            sleep 2
            /sbin/reboot
            '''
        ]

        # Start detached process
        update_firmware_upgrade_status('restart', 5, 'Starting sysupgrade process with nohup...')
        pcat_logger.logger.info(f"Starting detached sysupgrade command")
        detached_process = subprocess.Popen(
            detached_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid  # Create new process group
        )

        pcat_logger.logger.info(f"Detached sysupgrade process group started with PID: {detached_process.pid}")
        update_firmware_upgrade_status('restart', 10, f'Sysupgrade process launched (PID: {detached_process.pid})')
        socketio.sleep(0.3)
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
            socketio.sleep(1)

        # Final status update
        pcat_logger.logger.info("Sysupgrade process detached successfully - Flask can now safely terminate")
        update_firmware_upgrade_status('restart', 50, 'Firmware flashing in progress - process running independently')
        socketio.sleep(0.3)
        update_firmware_upgrade_status('restart', 55, 'Cleaning up temporary files...')

        # Cleanup temp file
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                # Also try to remove the temp directory if empty
                temp_dir = os.path.dirname(file_path)
                try:
                    os.rmdir(temp_dir)
                except:
                    pass
        except Exception as cleanup_error:
            pcat_logger.logger.warning(f"Failed to cleanup checkpoint file: {cleanup_error}")

        socketio.sleep(0.5)
        update_firmware_upgrade_status('restart', 70, 'Sysupgrade process is flashing firmware to device...')
        socketio.sleep(0.5)
        update_firmware_upgrade_status('restart', 80, 'Waiting for firmware flash to complete...')
        socketio.sleep(0.5)
        update_firmware_upgrade_status('restart', 90, 'Preparing for system restart...')
        socketio.sleep(0.5)

        # Mark as completed
        update_firmware_upgrade_status('completed', 100, 'Firmware upgrade complete! System is restarting...')
        socketio.sleep(2)
        
        # Initiate system restart fallback
        socketio.sleep(1)
        pcat_logger.logger.info("Initiating system reboot as fallback...")
        try:
            subprocess.Popen(['reboot'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as reboot_error:
            pcat_logger.logger.warning(f"Failed to initiate reboot: {reboot_error}")
        
    except Exception as e:
        error_msg = f"Firmware installation failed: {str(e)}"
        pcat_logger.logger.error(error_msg)
        pcat_logger.logger.error(f"Installation error details: {e}", exc_info=True)
        update_firmware_upgrade_status('error', 0, error_msg, str(e))
        
        # Cleanup on error
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                temp_dir = os.path.dirname(file_path)
                try:
                    os.rmdir(temp_dir)
                except:
                    pass
        except:
            pass

@api_auth_required
@flask_app.route("/api/v1/firmware_upgrade_status.json", methods=['GET'])
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
                socketio.emit('firmware_upgrade_status', response_data.copy())
                
        except Exception as e:
            pcat_logger.logger.warning(f"Error reading detached sysupgrade status: {e}")
    
    response_data['status'] = 'ok'
    
    rsp = make_response(response_data)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@socketio.event
def request_firmware_upgrade(message):
    """Socket handler for automatic firmware upgrade"""
    firmware_url = message.get("firmware_url")
    keep_config = message.get("keep_config", True)
    keep_config_option = message.get("keep_config_option", "-c")  # Default to -c
    download_method = message.get("download_method", "wget")  # Default to wget
    
    if not firmware_url:
        update_firmware_upgrade_status('error', 0, 'Invalid firmware URL', error='Invalid firmware URL')
        return
        
    global firmware_upgrade_thread, firmware_upgrade_status
    with firmware_upgrade_lock:
        if firmware_upgrade_thread is None and not firmware_upgrade_status.get('active', False):
            update_firmware_upgrade_status('starting', 0, 'Firmware upgrade request received')
            firmware_upgrade_thread = socketio.start_background_task(
                background_firmware_upgrade_thread, firmware_url, keep_config, keep_config_option, download_method
            )
            update_firmware_upgrade_status('starting', 0, f'Upgrade thread started for: {firmware_url.split("/")[-1]}')
        else:
            update_firmware_upgrade_status('error', 0, 'Firmware upgrade already in progress', 
                                         error='Another upgrade is already running')

@socketio.event
def get_firmware_upgrade_status_socket(message):
    """Socket handler to get current firmware upgrade status"""
    global firmware_upgrade_status
    emit('firmware_upgrade_status', firmware_upgrade_status.copy())

@socketio.event
def request_firmware_checkpoint(message):
    """Socket handler for firmware checkpoint mode - download only"""
    firmware_url = message.get("firmware_url")
    keep_config = message.get("keep_config", True)
    keep_config_option = message.get("keep_config_option", "-c")
    download_method = message.get("download_method", "wget")
    
    if not firmware_url:
        socketio.emit('firmware_checkpoint_status', {
            'stage': 'error',
            'message': 'No firmware URL provided'
        })
        return
        
    pcat_logger.logger.info(f"Starting firmware checkpoint mode: {firmware_url}")
    
    # Start checkpoint thread
    checkpoint_thread = socketio.start_background_task(
        background_firmware_checkpoint_thread,
        firmware_url, keep_config, keep_config_option, download_method
    )

@socketio.event
def request_firmware_install_from_checkpoint(message):
    """Socket handler to install firmware from checkpoint"""
    file_path = message.get("file_path")
    sha256 = message.get("sha256")
    keep_config = message.get("keep_config", True)
    keep_config_option = message.get("keep_config_option", "-c")
    
    if not file_path:
        update_firmware_upgrade_status('error', 0, 'No file path provided', 'No file path provided for installation')
        return
        
    pcat_logger.logger.info(f"Starting firmware installation from checkpoint: {file_path}")
    
    # Start installation thread  
    install_thread = socketio.start_background_task(
        background_firmware_install_thread,
        file_path, sha256, keep_config, keep_config_option
    )

@socketio.event
def cleanup_checkpoint_file(message):
    """Socket handler to cleanup checkpoint file"""
    file_path = message.get("file_path")
    
    if not file_path:
        return
        
    try:
        import os
        if os.path.exists(file_path):
            os.remove(file_path)
            # Also try to remove the temp directory if empty
            temp_dir = os.path.dirname(file_path)
            try:
                os.rmdir(temp_dir)
            except:
                pass
            pcat_logger.logger.info(f"Cleaned up checkpoint file: {file_path}")
    except Exception as e:
        pcat_logger.logger.warning(f"Failed to cleanup checkpoint file: {e}")

@socketio.event
def cancel_firmware_upgrade(message):
    """Socket handler to cancel firmware upgrade (only during download stage)"""
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
                
                emit('firmware_upgrade_status', {
                    'stage': 'cancelled',
                    'progress': 0,
                    'message': 'Download cancelled by user'
                })
    else:
        emit('firmware_upgrade_status', {
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
            # Store the password for OpenWrt form auto-login
            session['remembered_password'] = input_user_password
            
            # Set session lifetimes and store remember preference
            if request.form.get('remember_login'):
                session.permanent = True
                session['remember_login'] = True  # Store remember preference in session
                flask_app.permanent_session_lifetime = timedelta(days=180)  # 180 days for remember login
            else:
                session.permanent = True
                session['remember_login'] = False  # Store remember preference in session
                flask_app.permanent_session_lifetime = timedelta(days=30)  # 30 days for normal login
            
            # Check for redirect URL after login
            next_url = request.form.get('next') or request.args.get('next')
            if next_url and next_url.startswith('/'):  # Security check: only allow relative URLs
                return redirect(next_url)
            return redirect("/")
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

@api_auth_required
@flask_app.route('/pin_verify', methods=['POST'])
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

@flask_app.route('/logout', methods=['GET', 'POST'])
def logout_action():
    # Clear PIN authentication as well
    session.pop(PIN_SESSION_KEY, None)
    
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    session.pop('username', None)
    session.pop('remember_login', None)  # Clear remember preference on logout
    session.pop('remembered_password', None)  # Clear remembered password on logout
    return redirect("/login")

@flask_app.route("/settings")
def settings_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    c_locale = current_locale()
    if c_locale == "CN":
        locales = {
            "wifi_encrp": "Wi-Fi 加密",
            "wifi_password": "Wi-Fi 密码",
            "wifi_frequency": "Wi-Fi 频段",
            "wifi_channel": "Wi-Fi 频道",
            "hide_wifi_ssid": "隐藏网络不被发现",
            "configuring_wifi_message": "设备正在重新加载配置信息，大约需要 <span x-text='configing_timeout'></span> 秒钟 ...",
            "configuring_wifi_message2": "网络连接有可能断开。",

            "wifi_ssid_is_empty": "Wi-Fi SSID 不能为空",
            "wifi_ssid_too_long": "Wi-Fi SSID 长度不能太长",
            "wifi_password_is_empty": "Wi-Fi 密码不能为空",
            "wifi_password_at_least_8": "Wi-Fi 密码长度最少为 8 位",
            

            "no_encryption": "不加密"
        }
    else:
        locales = {
            "wifi_encrp": "Wi-Fi encryption",
            "wifi_password": "Wi-Fi password",
            "wifi_frequency": "Wi-Fi frequency",
            "wifi_channel": "Wi-Fi Channel",
            "hide_wifi_ssid": "Hide Wi-Fi SSID",
            "configuring_wifi_message": "Configuring Wi-Fi, please wait <span x-text='configing_timeout'></span> seconds ...",
            "configuring_wifi_message2": "Wi-Fi connections may be disconnected for a while",

            "wifi_ssid_is_empty": "Wi-Fi SSID is empty",
            "wifi_ssid_too_long": "Wi-Fi SSID too long",
            "wifi_password_is_empty": "Wi-Fi password is empty",
            "wifi_password_at_least_8": "Wi-Fi password requires >= 8 characters",

            "no_encryption": "No encryption"
        }

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
    if c_locale == "CN":
        locales = {
            "wifi_encrp": "Wi-Fi 加密",
            "wifi_password": "Wi-Fi 密码",
            "wifi_frequency": "Wi-Fi 频段",
            "hide_wifi_ssid": "隐藏网络不被发现",
            "configuring_wifi_message": "设备正在重新加载配置信息，大约需要 10 秒钟...",
            "configuring_wifi_message2": "网络连接有可能断开。",

            "wifi_ssid_is_empty": "Wi-Fi SSID 不能为空",
            "wifi_ssid_too_long": "Wi-Fi SSID 长度不能太长",
            "wifi_password_is_empty": "Wi-Fi 密码不能为空",
            "wifi_password_at_least_8": "Wi-Fi 密码长度最少为 8 位",

            "no_configuration_for_swtich_strategy": "手动模式：按电源按钮开机；再次按电源按钮或输入 shutdown 命令安全关机。不受供电状态影响，完全手动控制",
            "when_power_supply_disconnected": "当供电断开",
            "after_minutes": "分钟后，",
            "device_auto_power_off": "设备自动关机",
            "car_mode_tip_1": "车载/UPS模式：当外接供电接通时，设备自动开机；当供电断开时，设备延迟指定时间后自动安全关机",
            "car_mode_tip_2": "说明：设置后需手动关机使配置生效。此后连接充电器自动开机，断开充电器延迟关机。若手动开机则需手动关机（不受此模式控制）",
            "car_timeout_invalid": "设置的值不合理，最大为 600 分钟",
            "save_action_success_carmode_tip": "设置成功，需手动关机使设置生效",
            "manu_mode": "手动模式",
            "car_mode": "车载/UPS模式",
            "timer_mode": "定时模式",
            "force_power_mode": "直连电源",
            "force_power_mode_tip_1": "直连电源模式：外接电源接通时自动开机，断开时立即关机",
            "force_power_mode_tip_2": "有电池时正常安全关机；无电池时立即断电。忽略电池电量检查，适用于无电池场景或需要快速响应的应用",
            "force_power_mode_mcu_warning": "⚠️ 直连电源模式需要 MCU 固件版本 RA2E1260306000 或更高版本，请先升级 MCU 固件",
            "force_power_mode_mcu_current": "当前 MCU 版本",
            "power_policy_title": "开关机策略",
            "shutdown_policy_title": "关机策略",
            "idle_shutdown_enabled": "启用WiFi无客户端自动关机",
            "idle_shutdown_battery_only": "仅在电池供电时生效",
            "idle_shutdown_timeout": "空闲超时时间",
            "idle_shutdown_minutes": "分钟",
            "idle_shutdown_tip": "当没有WiFi客户端连接时，设备将在指定时间后自动关机。此功能与其他电源模式（如车载模式）配合使用，先触发的条件将执行关机操作",
            "idle_status_active": "有客户端连接",
            "idle_status_idle": "空闲",
            "idle_remaining_time": "剩余时间",
            "low_battery_shutdown_enabled": "启用低电量自动关机",
            "low_battery_threshold": "电量阈值",
            "low_battery_percent": "%",
            "low_battery_tip": "当电池电量下降并低于设定阈值时，设备将自动关机以保护电池。此功能与其他电源模式配合使用（OR逻辑），任一条件先触发即执行关机。注意：若开机时电量在阈值附近（阈值+2%以内），则不会立即关机，需等电量明显回升后再下降才触发",
            "power_on": "开机",
            "power_off": "关机",
            "timer_action": "动作",
            "timer_time": "时间",
            "timer_days": "重复",
            "timer_mon": "周一",
            "timer_tue": "周二",
            "timer_wed": "周三",
            "timer_thu": "周四",
            "timer_fri": "周五",
            "timer_sat": "周六",
            "timer_sun": "周日",
            "same_timer_exist": "此配置已存在",
            "please_select_days": "请选择重复的星期！",
            "no_more_timers_support": "最多支持设置 6 个定时项",
            "no_encryption": "不加密",
            "power_event_log_title": "电源事件日志",
            "power_event_log_tip": "记录所有电源相关事件，包括开机、关机、空闲关机、低电量关机、电压波动等",
            "power_event_clear": "清除日志",
            "power_event_clear_confirm": "确定要清除所有电源事件日志吗？",
            "power_event_no_events": "暂无事件记录",
            "power_event_type_system_start": "系统启动",
            "power_event_type_system_shutdown": "系统关机",
            "power_event_type_idle_shutdown_triggered": "空闲关机触发",
            "power_event_type_idle_countdown_started": "空闲倒计时开始",
            "power_event_type_idle_countdown_reset": "空闲倒计时重置",
            "power_event_type_low_battery_shutdown_triggered": "低电量关机触发",
            "power_event_type_power_mode_changed": "电源模式变更",
            "power_event_type_settings_changed": "设置变更",
            "power_event_type_power_dip": "电压波动",
            "power_event_type_dc_plugged_in": "DC电源接入",
            "power_event_type_dc_unplugged": "DC电源断开"
        }
    else:
        locales = {
            "wifi_encrp": "Wi-Fi encryption",
            "wifi_password": "Wi-Fi password",
            "wifi_frequency": "Wi-Fi frequency",
            "hide_wifi_ssid": "Hide Wi-Fi SSID",
            "configuring_wifi_message": "Configuring Wi-Fi, please wait...",
            "configuring_wifi_message2": "Wi-Fi connections may be disconnected for a while.",

            "wifi_ssid_is_empty": "Wi-Fi SSID is empty",
            "wifi_ssid_too_long": "Wi-Fi SSID too long",
            "wifi_password_is_empty": "Wi-Fi password is empty",
            "wifi_password_at_least_8": "Wi-Fi password length is at least 8 bits",

            "no_configuration_for_swtich_strategy": "Manual Mode: Press power button to turn on; press again or enter `shutdown` command for graceful shutdown. Unaffected by power supply status, fully manual control",
            "when_power_supply_disconnected": "When charger disconnects",
            "after_minutes": "minutes, ",
            "device_auto_power_off": "Device will automatically power off.",
            "car_mode_tip_1": "Car/UPS Mode: Auto power-on when external power connects; delayed graceful shutdown after power disconnects (configurable timeout)",
            "car_mode_tip_2": "Note: After enabling, manually shut down the device for settings to take effect. Then charger connection will trigger auto power-on, disconnection triggers delayed shutdown. Manual power-on is not controlled by this mode and requires manual shutdown",
            "car_timeout_invalid": "Settings invalid! Max 600 minutes.",
            "manu_mode": "Manual mode",
            "car_mode": "Car/UPS mode",
            "timer_mode": "Timer mode",
            "force_power_mode": "Direct Power",
            "force_power_mode_tip_1": "Direct Power Mode: Auto power-on when external power connects, instant power-off when disconnected",
            "force_power_mode_tip_2": "With battery: graceful shutdown. Without battery: instant power-off. Ignores battery level checks. Ideal for battery-free setups or quick-response applications",
            "force_power_mode_mcu_warning": "⚠️ Direct Power Mode requires MCU firmware version RA2E1260306000 or higher. Please upgrade your MCU firmware first",
            "force_power_mode_mcu_current": "Current MCU version",
            "power_policy_title": "Startup & Shutdown",
            "shutdown_policy_title": "Shutdown",
            "idle_shutdown_enabled": "Enable No WiFi Client Auto-Shutdown",
            "idle_shutdown_battery_only": "Only when on battery",
            "idle_shutdown_timeout": "Idle Timeout",
            "idle_shutdown_minutes": "minutes",
            "idle_shutdown_tip": "When no WiFi clients are connected, the device will automatically shut down after the specified time. This feature works in OR mode with other power modes (e.g., Car mode). Whichever condition triggers first will initiate shutdown",
            "idle_status_active": "Clients Connected",
            "idle_status_idle": "Idle",
            "idle_remaining_time": "Time Remaining",
            "low_battery_shutdown_enabled": "Enable Low Battery Auto-Shutdown",
            "low_battery_threshold": "Battery Threshold",
            "low_battery_percent": "%",
            "low_battery_tip": "When battery level drops below the set threshold, the device will automatically shut down to protect the battery. Works in OR mode with other power modes - whichever condition triggers first will initiate shutdown. Note: If the device powers on near the threshold (within 2% above), shutdown will not trigger until the battery clearly rises above and then drops below the threshold again",
            "power_on": "Power on",
            "power_off": "Power off",
            "timer_action": "Action",
            "timer_time": "Time",
            "timer_days": "Timer days",
            "timer_mon": "Monday",
            "timer_tue": "Tuesday",
            "timer_wed": "Wednesday",
            "timer_thu": "Thursday",
            "timer_fri": "Friday",
            "timer_sat": "Saturday",
            "timer_sun": "Sunday",
            "same_timer_exist": "Another same timer already exists!",
            "please_select_days": "Please select days!",
            "no_more_timers_support": "No more timers support!",
            "no_encryption": "No encryption",
            "power_event_log_title": "Power Event Log",
            "power_event_log_tip": "Records all power-related events including startup, shutdown, idle shutdown, low battery shutdown, voltage dips, etc.",
            "power_event_clear": "Clear Log",
            "power_event_clear_confirm": "Are you sure you want to clear all power event logs?",
            "power_event_no_events": "No events recorded",
            "power_event_type_system_start": "System Start",
            "power_event_type_system_shutdown": "System Shutdown",
            "power_event_type_idle_shutdown_triggered": "Idle Shutdown Triggered",
            "power_event_type_idle_countdown_started": "Idle Countdown Started",
            "power_event_type_idle_countdown_reset": "Idle Countdown Reset",
            "power_event_type_low_battery_shutdown_triggered": "Low Battery Shutdown",
            "power_event_type_power_mode_changed": "Power Mode Changed",
            "power_event_type_settings_changed": "Settings Changed",
            "power_event_type_power_dip": "Power Dip",
            "power_event_type_dc_plugged_in": "DC Plugged In",
            "power_event_type_dc_unplugged": "DC Unplugged"
        }
    locales.update(base_locales())
    stm32_firmware_exist = pc_app.socket_client.stm32_firmware_exist()
    saved_power_mode = pcat_config.get_power_mode()
    if saved_power_mode == "direct_power":
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
    if c_locale == "CN":
        locales = {
            "modem_4g_only": "仅4G",
            "modem_5g_only": "仅5G",
            "tab_model_basic": "Modem 信息",
            "tab_sms_box": "短信箱",
            "tab_modem_adv": "Modem 设置",
            "tab_modem_hook": "短信转发",
            
            "modem_invalid": "Modem 未连接",
            "modem_wwan_powered_off": "WWAN 模块已关闭。请在设备控制页面开启 WWAN 电源以使用蜂窝网络和短信功能。",
            "modem_checking_status": "检查 Modem 状态...",
            "modem_loading_info": "加载 Modem 信息...",
            "modem_loading_advanced": "加载 Modem 高级设置...",
            "modem_sim_number": "SIM 卡号",
            "custom": "自定义",
            "modem_imei_number": "Modem IMEI号",
            "modem_firmware_version": "Modem 固件版本号",
            "modem_model": "Modem 型号",
            "modem_tech_pref": "Modem 模式",
            "modem_bands_pref": "Modem 频段",
            "modem_roam_pref": "Modem 数据漫游",
            "modem_temperature": "Modem 温度",
            "modem_isp_details": "运营商信息",
            "modem_network_info": "连接信息",
            "modem_cell_signals": "SINR, RSRQ, RSRP, CSQ %",
            "modem_cell_id": "Cell ID",
            "cell_carrier_info": "载波信息",
            "modem_usbnet_mode": "Modem USB 模式",
            "modem_usb_speed": "USB 速度",
            "modem_cell_info": "Cell Information",
            "modem_serving_info": "服务信息",
            "modem_serving_quality": "SINR, RSRQ, RSRP, SRX 数据",
            "modem_reset_success": "Modem 重置成功",
            "modem_factory_reset_button": "Modem 恢复出厂设置",
            "modem_roam_pref_any": "任意运营商",
            "modem_roam_pref_affiliated": "关联运营商",
            "modem_roam_pref_no": "不漫游",
            "modem_tech_pref_auto": "自动",
            "modem_sim_absent": "未检测到 SIM 卡",
            "modem_no_data": "无短信",
            "modem_4g_only": "仅4G",
            "modem_5g_only": "仅5G",
            "modem_cpin": "SIM 卡 PIN 码",
            "are_you_sure_reset_modem": "⚠️注意：将您的调制解调器恢复出厂设置将永久删除所有自定义设置，包括短信、APN和网络配置。是否继续？",
            "modem_hook_enabled": "启用短信转发脚本",
            "modem_hook_warning": "此钩子脚本会在每条短信到达时自动运行，并可能触发后续操作。⚠️警告: 仅限高级用户使用。配置错误可能导致数据泄露、文件被删除或产生额外费用。仅在您有编程基础，完全理解其行为并愿意承担所有风险的前提下使用。$1  →  sender's phone number, $2  →  message content, $3  →  timestamp",
            "modem_hook_responsibility": "我理解风险并承担全部责任。",
            "hook_script_tab": "脚本设置",
            "hook_history_tab": "执行历史",
            "hook_history_title": "短信钩子执行历史",
            "hook_history_description": "显示所有短信钩子脚本执行的详细日志，包括执行时间、输出和错误信息。",
            "hook_filter_status": "状态",
            "hook_filter_all": "全部",
            "hook_filter_success_only": "仅成功",
            "hook_filter_failed_only": "仅失败",
            "hook_btn_refresh": "刷新",
            "hook_btn_test_api": "测试API",
            "hook_btn_create_table": "创建表",
            "hook_btn_insert_test": "插入测试",
            "hook_btn_cleanup": "清理",
            "hook_stats_total": "总执行次数",
            "hook_stats_successful": "成功次数",
            "hook_stats_failed": "失败次数",
            "hook_stats_avg_duration": "平均执行时长",
            "hook_table_time": "时间",
            "hook_table_status": "状态",
            "hook_table_sms_info": "短信信息",
            "hook_table_duration": "执行时长",
            "hook_table_output": "输出",
            "hook_table_actions": "操作",
            "hook_status_success": "成功",
            "hook_status_failed": "失败",
            "hook_btn_view_details": "查看详情",
            "hook_no_records_title": "未找到钩子执行记录",
            "hook_no_records_message": "当短信触发钩子脚本后，执行记录将显示在这里。",
            "hook_modal_title": "钩子执行详情",
            "hook_modal_execution_info": "执行信息",
            "hook_modal_execution_id": "执行ID",
            "hook_modal_return_code": "返回码",
            "hook_modal_duration": "执行时长",
            "hook_modal_start_time": "开始时间",
            "hook_modal_end_time": "结束时间",
            "hook_modal_sms_message": "短信消息",
            "hook_modal_sms_id": "短信ID",
            "hook_modal_sender": "发送方",
            "hook_modal_message_timestamp": "消息时间",
            "hook_modal_message_content": "消息内容",
            "hook_modal_hook_command": "钩子命令",
            "hook_modal_script_content": "脚本内容（执行时）",
            "hook_modal_stdout": "标准输出",
            "hook_modal_stderr": "错误输出",
            "hook_modal_error_message": "错误信息",
            "hook_modal_close": "关闭"
        }
    else:
        locales = {
            "modem_4g_only": "4G Only",
            "modem_5g_only": "5G Only",
            "tab_model_basic": "Modem Info",
            "tab_sms_box": "SMS Inbox",
            "tab_modem_adv": "Modem Settings",
            "tab_modem_hook": "SMS Hook",
            
            "modem_invalid": "Modem Invalid or Unsupported",
            "modem_wwan_powered_off": "WWAN module is powered off. Enable WWAN Power in Device Control to use cellular and SMS.",
            "modem_checking_status": "Checking Modem Status...",
            "modem_loading_info": "Loading Modem Info...",
            "modem_loading_advanced": "Loading Modem Advanced Settings...",
            "modem_sim_number": "SIM Number",
            "custom": "Custom",
            "modem_imei_number": "Modem IMEI",
            "modem_firmware_version": "Modem Firmware Version",
            "modem_model": "Modem Model",
            "modem_tech_pref": "Cellular 5G/LTE Bands",
            "modem_bands_pref": "Cellular Bands Selection",
            "modem_roam_pref": "Data Roaming",
            "modem_temperature": "Modem Temperature",
            "modem_isp_details": "Operator Details",
            "modem_network_info": "Network Information",
            "modem_cell_signals": "SINR, RSRQ, RSRP, CSQ %",
            "modem_cell_id": "Cell ID",
            "cell_carrier_info": "Carrier Information",
            "modem_usbnet_mode": "Modem USB Mode",
            "modem_usb_speed": "USB Speed",
            "modem_cell_info": "Cell Information",
            "modem_serving_info": "Serving Cell Information",
            "modem_serving_quality": "SINR, RSRQ, RSRP, SRX Data",
            "modem_reset_success": "Modem Reset Success",
            "modem_factory_reset_button": "Modem Factory Reset",
            "modem_roam_pref_any": "Any",
            "modem_roam_pref_affiliated": "Affiliated",
            "modem_roam_pref_no": "No Roaming",
            "modem_tech_pref_auto": "Auto",
            "modem_sim_absent": "SIM card not detected",
            "modem_no_data": "No SMS",
            "modem_4g_only": "4G Only",
            "modem_5g_only": "5G Only",
            "modem_cpin": "SIM PIN",
            "are_you_sure_reset_modem": "⚠️ Caution: Resetting your modem to factory settings will permanently erase all custom settings, including SMS, APN, and network configurations. Proceed?",
            "modem_hook_warning": "This hook script runs automatically on every incoming SMS and can trigger further actions. ⚠️ Caution — advanced users only. A misconfiguration could leak data, delete files, or incur costs. Use it only if you fully understand its behavior and accept all risks. $1  →  sender's phone number, $2  →  message content, $3  →  timestamp",
            "modem_hook_enabled": "Enable SMS Hook Script",
            "modem_hook_responsibility": "I understand the risks and take full responsibility.",
            "hook_script_tab": "Hook Script",
            "hook_history_tab": "Execution History",
            "hook_history_title": "SMS Hook Execution History",
            "hook_history_description": "Detailed log of all SMS hook script executions with timing, output, and error information.",
            "hook_filter_status": "Status",
            "hook_filter_all": "All",
            "hook_filter_success_only": "Success Only",
            "hook_filter_failed_only": "Failed Only",
            "hook_btn_refresh": "Refresh",
            "hook_btn_test_api": "Test API",
            "hook_btn_create_table": "Create Table",
            "hook_btn_insert_test": "Insert Test",
            "hook_btn_cleanup": "Clean Up",
            "hook_stats_total": "Total Executions",
            "hook_stats_successful": "Successful",
            "hook_stats_failed": "Failed",
            "hook_stats_avg_duration": "Avg Duration",
            "hook_table_time": "Time",
            "hook_table_status": "Status",
            "hook_table_sms_info": "SMS Info",
            "hook_table_duration": "Duration",
            "hook_table_output": "Output",
            "hook_table_actions": "Actions",
            "hook_status_success": "Success",
            "hook_status_failed": "Failed",
            "hook_btn_view_details": "View Details",
            "hook_no_records_title": "No hook execution records found",
            "hook_no_records_message": "Hook executions will appear here after SMS messages trigger your hook script.",
            "hook_modal_title": "Hook Execution Details",
            "hook_modal_execution_info": "Execution Info",
            "hook_modal_execution_id": "Execution ID",
            "hook_modal_return_code": "Return Code",
            "hook_modal_duration": "Duration",
            "hook_modal_start_time": "Start Time",
            "hook_modal_end_time": "End Time",
            "hook_modal_sms_message": "SMS Message",
            "hook_modal_sms_id": "SMS ID",
            "hook_modal_sender": "Sender",
            "hook_modal_message_timestamp": "Message Timestamp",
            "hook_modal_message_content": "Message Content",
            "hook_modal_hook_command": "Hook Command",
            "hook_modal_script_content": "Hook Script Content (at time of execution)",
            "hook_modal_stdout": "STDOUT Output",
            "hook_modal_stderr": "STDERR Output",
            "hook_modal_error_message": "Error Message",
            "hook_modal_close": "Close"
        }

    locales.update(base_locales())
    stm32_firmware_exist = pc_app.socket_client.stm32_firmware_exist()
    return render_template(nt.modem_file_name, stm32_firmware_exist = stm32_firmware_exist, locales = locales)

@flask_app.route("/phone")
@pin_required
def phone_action():
    c_locale = current_locale()
    if c_locale == "CN":
        locales = {
            "nav_phone": "电话",
            
            # Voice Calls translations
            "voice_call_manager": "语音通话管理器",
            "voice_call_under_development": "语音通话管理器 (开发中)",
            "voice_no_active_calls": "无活跃通话",
            "voice_calling": "正在呼叫...",
            "voice_make_call": "拨打",
            "voice_call_controls": "通话控制",
            "voice_answer": "接听",
            "voice_hangup": "挂断", 
            "voice_audio_path": "音频路径",
            "voice_speaker": "扬声器",
            "voice_earpiece": "听筒",
            "voice_headset": "耳机",
            "voice_bluetooth": "蓝牙",
            "voice_call_history": "通话记录",
            "voice_all_calls": "全部通话",
            "voice_outgoing": "拨出",
            "voice_incoming": "接入", 
            "voice_missed": "未接",
            "voice_number": "号码",
            "voice_type": "类型",
            "voice_duration": "时长",
            "voice_time": "时间",
            "voice_actions": "操作",
            "voice_call_back": "回拨",
            "voice_delete": "删除",
            "voice_refresh": "刷新",
            "voice_clear": "清空",
            "voice_no_history_found": "未找到通话记录",
            "voice_start_making_calls": "开始拨打电话以查看历史记录",
            "voice_no_calls_found": "未找到",
            "voice_calls_found": "通话",
            "voice_loading_history": "正在加载通话记录..."
        }
    else:
        locales = {
            "nav_phone": "Phone",
            
            # Voice Calls translations
            "voice_call_manager": "Voice Call Manager",
            "voice_call_under_development": "Voice Call Manager (Under Development)",
            "voice_no_active_calls": "No active calls",
            "voice_calling": "Calling...",
            "voice_make_call": "Call",
            "voice_call_controls": "Call Controls",
            "voice_answer": "Answer",
            "voice_hangup": "Hang Up", 
            "voice_audio_path": "Audio Path",
            "voice_speaker": "Speaker",
            "voice_earpiece": "Earpiece",
            "voice_headset": "Headset",
            "voice_bluetooth": "Bluetooth",
            "voice_call_history": "Call History",
            "voice_all_calls": "All Calls",
            "voice_outgoing": "Outgoing",
            "voice_incoming": "Incoming", 
            "voice_missed": "Missed",
            "voice_number": "Number",
            "voice_type": "Type",
            "voice_duration": "Duration",
            "voice_time": "Time",
            "voice_actions": "Actions",
            "voice_call_back": "Call Back",
            "voice_delete": "Delete",
            "voice_refresh": "Refresh",
            "voice_clear": "Clear",
            "voice_no_history_found": "No call history found",
            "voice_start_making_calls": "Start making calls to see your history here",
            "voice_no_calls_found": "No",
            "voice_calls_found": "calls found",
            "voice_loading_history": "Loading call history..."
        }

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

    # Get current TCP congestion control algorithm
    current_tcp_congestion = "cubic"
    try:
        result = subprocess.run(["sysctl", "-n", "net.ipv4.tcp_congestion_control"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            current_tcp_congestion = result.stdout.strip()
    except Exception:
        pass

    if c_locale == "CN":
        locales = {
            "general_settings_title": "通用设置",
            "language_label": "系统语言",
            "language_help": "同时影响本管理界面和 OpenWrt LuCI 的显示语言。",
            "timezone_label": "时区",
            "timezone_help": "设置设备的系统时区。",
            "hostname_label": "主机名",
            "hostname_help": "设置设备的主机名，用于网络标识。",
            "hostname_placeholder": "请输入主机名",
            "invalid_hostname": "主机名只能包含字母、数字和连字符，且不能以连字符开头或结尾。",
            "tcp_cc_label": "TCP 拥塞控制算法",
            "tcp_cc_help": "设置 TCP 拥塞控制算法，影响网络传输性能。",
            "terminal_enabled_label": "Web 终端",
            "terminal_enabled_help": "启用或禁用导航栏中的 Web 终端功能。",
            "saved_succeed": "保存成功",
            "saving": "保存中...",
        }
    else:
        locales = {
            "general_settings_title": "General Settings",
            "language_label": "System Language",
            "language_help": "Affects both this management interface and OpenWrt LuCI display language.",
            "timezone_label": "Timezone",
            "timezone_help": "Set the system timezone for the device.",
            "hostname_label": "Hostname",
            "hostname_help": "Set the device hostname for network identification.",
            "hostname_placeholder": "Enter hostname",
            "invalid_hostname": "Hostname can only contain letters, numbers, and hyphens. Cannot start or end with a hyphen.",
            "tcp_cc_label": "TCP Congestion Control",
            "tcp_cc_help": "Set the TCP congestion control algorithm to optimize network performance.",
            "terminal_enabled_label": "Web Terminal",
            "terminal_enabled_help": "Enable or disable the web terminal access in the navigation bar.",
            "saved_succeed": "Saved Successfully",
            "saving": "Saving...",
        }
    locales.update(base_locales())
    stm32_firmware_exist = pc_app.socket_client.stm32_firmware_exist()
    return render_template(nt.general_settings_file_name,
                           stm32_firmware_exist=stm32_firmware_exist,
                           current_hostname=current_hostname,
                           current_timezone=current_timezone,
                           current_zonename=current_zonename,
                           current_tcp_congestion=current_tcp_congestion,
                           current_terminal_enabled=current_terminal_enabled,
                           locales=locales)

@flask_app.route("/api/v1/general_settings.json", methods=["POST"])
@api_auth_required
def save_general_settings_api():
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No data provided"})

    results = []

    # Save language
    language = data.get("language")
    if language in ["EN", "CN"]:
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
    if tcp_congestion in ["bbr", "cubic", "reno"]:
        try:
            subprocess.run(["sysctl", "-w", f"net.ipv4.tcp_congestion_control={tcp_congestion}"],
                           capture_output=True, text=True, timeout=5)
            # Persist across reboots via sysctl.conf
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
                        f.write(f"{sysctl_key}={tcp_congestion}\n")
                        found = True
                    else:
                        f.write(line)
                if not found:
                    f.write(f"{sysctl_key}={tcp_congestion}\n")
            results.append("tcp_congestion")
        except Exception as e:
            pcat_logger.logger.error(f"Failed to set TCP congestion control: {e}")

    # Save terminal enabled setting
    terminal_enabled = data.get("terminal_enabled")
    if terminal_enabled is not None:
        pcat_config.set_user_config_key("terminal_enabled", bool(terminal_enabled))
        results.append("terminal_enabled")

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

@flask_app.route("/device_control")
def device_control_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")
    
    c_locale = current_locale()
    if c_locale == "CN":
        locales = {
            "power_led_control": "电源指示灯控制",
            "power_led_help_text": "控制电源指示灯的开关状态，支持定时功能。",
            "power_led_all_day": "全天开启",
            "power_led_off_start_time": "LED 开启开始时间",
            "power_led_off_end_time": "LED 开启结束时间",
            "power_led_on_start_time": "LED 开启开始时间",
            "power_led_on_end_time": "LED 开启结束时间",
            "led_mode_off": "关闭",
            "led_mode_timed": "定时",
            "led_mode_all_day": "全天",
            "time_on_from": "开启时间从",
            "time_off_to": "关闭时间到",
            "beeper_control": "蜂鸣器控制",
            "beeper_help_text": "控制蜂鸣器的开关状态，支持定时功能。",
            "beeper_all_day": "全天开启",
            "beeper_off_start_time": "蜂鸣器关闭开始时间",
            "beeper_off_end_time": "蜂鸣器关闭结束时间",
            "beeper_on_start_time": "蜂鸣器开启开始时间",
            "beeper_on_end_time": "蜂鸣器开启结束时间",
            "beeper_mode_off": "关闭",
            "beeper_mode_timed": "定时",
            "beeper_mode_all_day": "全天",  
            "network_save": "保存网络设置",
            "section_network_mode": "网络设置",
            "card_network_mode": "网络接入",
            "network_mode_auto": "自动",
            "network_mode_eth_only": "仅网线",
            "network_mode_cell_only": "仅蜂窝",
            "network_mode_auto_help": "根据可用性自动在以太网和蜂窝网络之间切换",
            "network_mode_eth_only_help": "仅使用以太网连接，有线连接时禁用蜂窝网络",
            "network_mode_cell_only_help": "仅使用蜂窝网络连接，以太网被禁用",
            "network_mode_current": "当前",
            "network_mode_auto_short": "自动",
            "network_mode_eth_only_short": "仅以太网",
            "network_mode_cell_only_short": "仅蜂窝",
            "network_mode_auto_short_help": "根据可用性自动在以太网和蜂窝网络之间切换",
            "network_mode_eth_only_short_help": "仅使用以太网连接，有线连接时禁用蜂窝网络",
            "network_mode_cell_only_short_help": "仅使用蜂窝网络连接，以太网被禁用",
            "network_mode_all_off_short": "全关闭",
            "network_mode_all_off_short_help": "关闭所有网络连接，仅保持 Wi-Fi 和 LAN",
            "network_settings_warning": "网络设置保存有警告。WiFi 可能需要重新连接或设备可能需要重启。",
            "card_device_reset": "系统控制",
            "cpu_powersave": "CPU 节能",
            "cpu_powersave_help_text": "关闭大核(A72)，仅使用小核(A53)运行，降低功耗和发热。",
            "reset_restart_shutdown": "重置、重启 & 关机",
            "fan_settings": "智能风扇",
            "fan_settings_help_text": "智能风扇控制：滑动条设置最大转速。系统自动根据温度调节，实际转速不超过用户设定上限。",
            "fan_mode_manual": "手动",
            "fan_mode_smart_quiet": "安静",
            "fan_mode_smart_cool": "冷却",
            "fan_mode_manual_help": "手动控制：直接设置风扇转速",
            "fan_mode_smart_quiet_help": "智能安静：充电时 45°C / 电池时 52°C 启动风扇",
            "fan_mode_smart_cool_help": "智能冷却：充电时 43°C / 电池时 48°C 启动风扇",
            "fan_speed_label": "风扇转速",
            "fan_max_speed_label": "最大转速",
            "fan_mode_saved": "风扇模式已保存",
            "fan_mode_save_failed": "风扇模式保存失败",
            "fan_mode_save_error": "风扇模式保存出错",
            "wwan_power": "WWAN 电源",
            "wwan_power_help_text": "完全关闭 WWAN 模块以节省电量。关闭后将无法发送或接收短信。",
            "lan_interface_toggle_help_text": "关闭 LAN 网卡可以降低功耗增加续航",
            "onboard_wifi_toggle_help_text": "关闭板载 WiFi 可以降低功耗增加续航",
            "pcie_wifi_toggle_help_text": "关闭 PCIE WiFi 可以降低功耗增加续航",
            "charge_limit": "充电限制",
            "charge_limit_help": "限制电池最大充电量以延长电池寿命",
            "beeper_settings": "蜂鸣器",
            "beeper_settings_help_text": "关闭蜂鸣器，机器可以完全静音。",
            "wan_interface": "WAN 网卡",
            "cellular_network": "蜂窝网络",
            "lan_interface": "LAN 网卡",
            "onboard_wifi": "板载 WiFi",
            "pcie_wifi": "PCIE WiFi",
            "configuring_network": "正在配置网络，请稍候...",
            "apn_auto_config": "自动配置",
            "apn_manu_config": "手动配置",
            "apn_setup": "APN 配置",
            "apn_address": "APN 地址",
            "apn_user_name": "用户名",
            "apn_password": "密码",
            "apn_auth_way": "验证方式",
            "please_input_apn_addr": "请输入 APN 地址！",
            "device_reset": "出厂重置",
            "device_restart": "设备重启",
            "device_shutdown": "设备关机",
            "are_you_sure_reset": "注意：将您的调制解调器重置为出厂设置将永久删除所有自定义设置，包括短信、APN 和网络配置。是否继续？",
            "resetting_device": "设备正在恢复出厂设置，请稍候...",
            "reset_help_text": "恢复出厂设置后，所有配置信息、插件、或数据全部重置到出厂时的状态。",
            "restart_help_text": "重启设备, 目前的网络会断开，请稍后重新连接。",
            "shutdown_help_text": "关机后，目前的网络会断开，确认关机？",
            "wan_toggle_help_text": "开启或关闭 WAN 网卡，关闭可以降低功耗增加续航",
            "cellular_network_toggle_help_text": "开启或关闭蜂窝网络 Modem",
            "lan_toggle_help_text": "开启或关闭 LAN 网卡，关闭可以降低功耗增加续航",
            "onboard_wifi_toggle_help_text": "开启或关闭板载 WiFi，关闭可以降低功耗增加续航",
            "pcie_wifi_toggle_help_text": "开启或关闭 PCIE WiFi，关闭可以降低功耗增加续航",
            "saved_succeed": "保存成功",
            "saving": "保存中...",
            
            # Control button labels
            "control_off": "关闭",
            "control_on": "开启", 
            "control_timed": "定时",
            "control_current": "当前"
        }
    else:
        locales = {
            "power_led_control": "Power LED Control",
            "power_led_help_text": "Control power LED on/off with optional timing.",
            "power_led_all_day": "All Day On",
            "power_led_off_start_time": "LED Off Start Time",
            "power_led_off_end_time": "LED Off End Time",
            "power_led_on_start_time": "LED On Start Time",
            "power_led_on_end_time": "LED On End Time",
            "led_mode_off": "Off",
            "led_mode_timed": "Timed",
            "led_mode_all_day": "All Day",
            "time_on_from": "On from",
            "time_off_to": "Off to",
            "beeper_control": "Beeper Control",
            "beeper_help_text": "Control beeper on/off with optional timing.",
            "beeper_all_day": "All Day On",
            "beeper_off_start_time": "Beeper Off Start Time",
            "beeper_off_end_time": "Beeper Off End Time",
            "beeper_on_start_time": "Beeper On Start Time",
            "beeper_on_end_time": "Beeper On End Time",
            "beeper_mode_off": "Off",
            "beeper_mode_timed": "Timed",
            "beeper_mode_all_day": "All Day",
            "network_save": "Save Network Settings",
            "section_network_mode": "Network Settings",
            "card_network_mode": "Internet via",
            "network_mode_auto": "Auto",
            "network_mode_eth_only": "Eth Only",
            "network_mode_cell_only": "Cell Only",
            "network_mode_auto_help": "Automatically switches between Ethernet and Cellular based on availability",
            "network_mode_eth_only_help": "Uses only Ethernet connection, disables cellular when wired",
            "network_mode_cell_only_help": "Uses only Cellular connection, Ethernet is disabled",
            "network_mode_current": "Current",
            "network_mode_auto_short": "Auto",
            "network_mode_eth_only_short": "Eth Only",
            "network_mode_cell_only_short": "Cell Only",
            "network_mode_auto_short_help": "Automatically switches between Ethernet and Cellular based on availability",
            "network_mode_eth_only_short_help": "Uses only Ethernet connection, disables cellular when wired",
            "network_mode_cell_only_short_help": "Uses only Cellular connection, Ethernet is disabled",
            "network_mode_all_off_short": "All Off",
            "network_mode_all_off_short_help": "Disables all network connections, keeps only Wi-Fi and LAN",
            "network_settings_warning": "Network settings saved with warnings. WiFi may need to reconnect or device may need reboot.",
            "card_device_reset": "System Control",
            "cpu_powersave": "CPU Powersave",
            "cpu_powersave_help_text": "Disable big cores (A72), run on LITTLE cores (A53) only to reduce power and heat.",
            "reset_restart_shutdown": "Reset, Restart & Shutdown",
            "fan_settings": "Smart Fan",
            "fan_settings_help_text": "Smart Fan Control: Slider sets maximum speed limit. System automatically adjusts based on temperature. Actual speed never exceeds user-set limit.",
            "fan_mode_manual": "Manual",
            "fan_mode_smart_quiet": "Quiet",
            "fan_mode_smart_cool": "Cool",
            "fan_mode_manual_help": "Manual control: directly sets fan speed",
            "fan_mode_smart_quiet_help": "Smart Quiet: fan starts at 45°C (charging) / 52°C (battery)",
            "fan_mode_smart_cool_help": "Smart Cool: fan starts at 43°C (charging) / 48°C (battery)",
            "fan_speed_label": "Fan Speed",
            "fan_max_speed_label": "Max Speed",
            "fan_mode_saved": "Fan mode saved",
            "fan_mode_save_failed": "Failed to save fan mode",
            "fan_mode_save_error": "Error saving fan mode",
            "wwan_power": "WWAN Power",
            "wwan_power_help_text": "Completely power off the WWAN module to save power. SMS will not be available when powered off.",
            "lan_interface_toggle_help_text": "Disabling the LAN interface can reduce power consumption and increase battery life.",
            "onboard_wifi_toggle_help_text": "Disabling the Onboard WiFi can reduce power consumption and increase battery life.",
            "pcie_wifi_toggle_help_text": "Disabling the PCIE WiFi can reduce power consumption and increase battery life.",
            "charge_limit": "Charge Limit",
            "charge_limit_help": "Limit max battery charge to extend battery lifespan",
            "beeper_settings": "Beeper",
            "beeper_settings_help_text": "Disable beeper, the device will be completely silent.",
            "wan_interface": "WAN NIC",
            "cellular_network": "Cellular Module",
            "lan_interface": "LAN NIC",
            "onboard_wifi": "Onboard WiFi",
            "pcie_wifi": "PCIE WiFi",
            "configuring_network": "Configuring network, please wait...",
            "apn_auto_config": "Auto Config",
            "apn_manu_config": "Manual Config",
            "apn_setup": "APN Setup",
            "apn_address": "APN Address",
            "apn_user_name": "User Name",
            "apn_password": "Password",
            "apn_auth_way": "Auth type",
            "please_input_apn_addr": "Please input APN!",
            "device_reset": "Factory Reset",
            "device_restart": "Device Restart",
            "device_shutdown": "Device Shutdown",
            "are_you_sure_reset": "Caution: Resetting your modem to factory settings will permanently delete all custom settings, including SMS, APN, and network configurations. Proceed?",
            "resetting_device": "Resetting device, please wait ...",
            "reset_help_text": "After device reset, all configurations and data will be reset to manufacturing default.",
            "restart_help_text": "Restart device, the current network will be disconnected, please reconnect later.",
            "shutdown_help_text": "Shutdown device, the current network will be disconnected, confirm shutdown?",
            "wan_toggle_help_text": "Disabling the WAN interface can reduce power consumption and increase battery life.",
            "cellular_network_toggle_help_text": "Enable / Disable Cellular Network",
            "lan_toggle_help_text": "Disabling the LAN interface can reduce power consumption and increase battery life.",
            "onboard_wifi_toggle_help_text": "Enable / Disable Onboard WiFi, disable can reduce power consumption and increase battery life.",
            "pcie_wifi_toggle_help_text": "Enable / Disable PCIE WiFi, disable can reduce power consumption and increase battery life.",
            "saved_succeed": "Saved Successfully",
            "saving": "Saving...",
            
            # Control button labels
            "control_off": "Off",
            "control_on": "On", 
            "control_timed": "Timed",
            "control_current": "Current"
        }
    locales.update(base_locales())
    stm32_firmware_exist = pc_app.socket_client.stm32_firmware_exist()
    is_em05_modem = pc_app.modem_client.is_em05()
    return render_template(nt.device_control_file_name, stm32_firmware_exist = stm32_firmware_exist, is_em05_modem = is_em05_modem, locales = locales)

@flask_app.route("/stats")
def statistics():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    c_locale = current_locale()
    if c_locale == "CN":
        locales = {
            "card_device_list": "设备列表",
            "card_traffic_stats": "流量统计",

            "mac_address": "MAC 地址",
            "ip_address": "IP 地址",
            "device_name": "设备名",

            "statistics_reset_confirm": "你确定要清空当前统计数据吗？",
            "statistics_reset_success": "统计数据已清空",
            "network_type_wired": "有线网络",
            "network_type_mobile": "蜂窝网络",
            "today_used": "今天使用",
            "week_used": "本周使用",
            "month_used": "本月使用",
            "last_month_used": "上月使用",
            "battery_chart_title": "电池历史",
            "battery_chart_soc": "电量 %",
            "battery_chart_voltage": "电压 V",
            "battery_chart_current": "功率 W",
            "battery_chart_temp": "主板温度",
            "battery_chart_24h": "24小时",
            "battery_chart_7d": "7天",
            "battery_chart_30d": "30天",
            "battery_chart_no_data": "暂无电池数据",
            "battery_chart_power_on": "开机",
            "battery_chart_power_off": "关机",
            "battery_chart_dc_plug": "DC接入",
            "battery_chart_dc_unplug": "DC断开"
        }
    else:
        locales = {
            "card_device_list": "Device List",
            "card_traffic_stats": "Mobile Traffic Statistics",

            "mac_address": "MAC Address",
            "ip_address": "IP Address",
            "device_name": "Device Name",

            "statistics_reset_confirm": "Are you sure to reset the statistics data?",
            "statistics_reset_success": "Statistics Data Reset",
            "network_type_wired": "Wired Network",
            "network_type_mobile": "Cellular Network",
            "today_used": "Today Usage",
            "week_used": "This Week Usage",
            "month_used": "This Month Usage",
            "last_month_used": "Last Month Usage",
            "battery_chart_title": "Battery History",
            "battery_chart_soc": "SOC %",
            "battery_chart_voltage": "Voltage V",
            "battery_chart_current": "Power W",
            "battery_chart_temp": "MB Temp",
            "battery_chart_24h": "24h",
            "battery_chart_7d": "7 Days",
            "battery_chart_30d": "30 Days",
            "battery_chart_no_data": "No battery data available",
            "battery_chart_power_on": "Power On",
            "battery_chart_power_off": "Power Off",
            "battery_chart_dc_plug": "DC In",
            "battery_chart_dc_unplug": "DC Out"
        }
    locales.update(base_locales())
    return render_template(nt.statistics_file_name, locales = locales)

@api_auth_required
@flask_app.route("/stats/reset", methods=['POST'])
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
    if c_locale == "CN":
        locales = {
            "card_title_ipk_upload": "第三方 IPK/插件 安装",
            "card_title_ipk_update": "系统 IPK/插件 更新",
            "please_select_a_update_pkg": "请选择更新包。",
            "upload": "上传",
            "upload_ipks": "上传 IPK(s)",
            "choose_file": "选择文件",
            "install_custom_ipk_title": "安装自定义 IPK/插件包",
            "install_custom_ipk_description": "上传并安装第三方 IPK 包或插件，以扩展您的设备功能。",
            "choose_your_file": "选择您的文件",
            "card_title_ipk_manager": "IPK 包管理",
            "download_from_url": "从链接下载",
            "paste_ipk_url": "粘贴 IPK 包链接",
            "download_url_placeholder": "请输入 IPK 包下载链接 (支持 .ipk, .gz, .xz, .bz2, .tar, 最大300MB)",
            "download_button": "下载并安装",
            "downloading": "正在下载",
            "download_logs": "下载安装日志",
            "download_progress": "下载进度",
            "file_too_large_300mb": "文件过大 (最大300MB)",
            "no_ipk_in_archive": "压缩包中未找到IPK文件",
            "extraction_failed": "解压失败",
            "or_separator": "或",
            "ipk_package_name": "软件包名称",
            "ipk_package_current_version": "当前版本",
            "ipk_package_latest_version": "最新版本",
            "not_need_update": "已最新",
            "need_update": "可以更新",
            "upload_operation_tip": "点击或拖拽文件到此处上传",
            "help_text": "支持 IPK 格式的包，或者 gz、bz2、xz 和 tar 格式的压缩包。压缩包将自动解压并安装其中的所有IPK文件。",
            "check_update": "检查更新", 
            "no_package_info": "没有版本信息", 
            "update": "更新",
            "upgrade_help_text1": "操作提示：更新 IPK 包后，请重启设备，以保证更新生效。",
            "upgrade_help_text2": "在更新某些 IPK 包后，网络有可能断开片刻。",
            "card_title_firmware_manager": "Photonicat 系统镜像管理",
            "firmware_checking": "检查中",
            "firmware_new_version_available": "🎉 新版本可用！",
            "firmware_up_to_date": "固件已是最新版本！",
            "firmware_current_version": "当前版本",
            "firmware_latest_version": "最新版本",
            "firmware_download_button": "下载固件",
            "firmware_flash_button": "刷写固件",
            "firmware_auto_upgrade_button": "自动升级",
            "firmware_manual_update": "手动更新",
            "firmware_manual_update_text": "如需手动刷写系统镜像，请访问：",
            "firmware_luci_flash_button": "打开 LuCI 刷机界面",
            
            # New automatic upgrade locales
            "firmware_upgrading": "正在升级固件",
            "confirm_firmware_upgrade": "确认固件升级",
            "firmware_upgrade_warning": "此操作将自动下载并安装最新固件。升级过程中设备将重启，请确保电源稳定。",
            "upgrade_process": "升级流程",
            "stage_download": "下载",
            "stage_prepare": "准备",
            "stage_install": "安装",
            "stage_restart": "重启",
            "keep_current_settings": "保留当前配置",
            "cancel": "取消",
            "start_upgrade": "开始升级",
            "confirm_cancel_download": "确定要取消下载吗？已下载的文件将被删除。",
            "download_method": "下载方式",
            "recommended": "推荐",
            "faster_but_less_reliable": "更快但不太稳定",
            "download_method_help": "wget 更稳定，aria2c 更快但在某些网络环境下可能不稳定",
            "data_loss_warning": "数据丢失警告",
            "firmware_upgrade_data_loss_warning": "固件升级可能会导致数据丢失。请确保已备份重要数据。如未选择保留配置，所有设置将被重置。",
            
            # Dev server locales
            "dev_server_title": "开发服务器",
            "dev_only": "仅限开发",
            "production_server_title": "固件升级",
            "stable": "稳定版 - dl.photonicat.com",
            "dev_firmware_available": "⚡ 开发版本可用！",
            "dev_firmware_up_to_date": "✅ 开发固件已是最新！",
            "dev_latest_version": "开发最新版本",
            "dev_current_version": "开发当前版本",
            "dev_download_button": "下载开发版固件",
            "dev_auto_upgrade_button": "升级开发版",
            
            # MCU Update Locales - Chinese
            "card_title_mcu_manager": "MCU 固件管理",
            "mcu_checking": "检查 MCU 中...",
            "mcu_dev_server_title": "MCU 服务器 (DEV)",
            "mcu_production_server_title": "MCU 服务器",
            "mcu_dev_update_available": "开发 MCU 更新可用!",
            "mcu_new_version_available": "MCU 更新可用!",
            "mcu_dev_up_to_date": "开发 MCU 已是最新!",
            "mcu_up_to_date": "MCU 已是最新!",
            "mcu_current_version": "当前 MCU 版本",
            "mcu_latest_version": "最新 MCU 版本",
            "mcu_prod_latest_version": "MCU最新版本",
            "mcu_dev_latest_version": "MCU开发最新版本",
            "mcu_dev_update_button": "更新 MCU (开发)",
            "mcu_dev_reinstall_button": "重新安装 MCU",
            "mcu_auto_update_button": "更新 MCU",
            "mcu_updating": "更新中...",
            "confirm_mcu_upgrade": "确认 MCU 更新",
            "mcu_upgrade_warning": "这将更新您的 MCU 固件。过程是安全的，但不应被中断。",
            "mcu_upgrade_process": "更新过程",
            "mcu_upgrade_power_warning": "更新过程中请勿断电！",
            "start_mcu_update": "开始更新",
            "starting": "启动中...",
            "debug_logs": "调试日志",
            
            # Configuration preservation options
            "config_preservation_options": "配置保留选项",
            "config_preserve_etc": "-c: 保留 /etc/ 中所有更改的文件（默认，推荐）",
            "config_preserve_all": "-o: 保留 / 中所有更改的文件，除了来自软件包的文件但包括更改的配置",
            "config_fresh_install": "-n: 全新安装，删除所有配置文件",
            "config_preservation_help": "选择升级期间如何保留您的配置。",
            
            # Download methods with dual language
            "download_method_wget": "wget（单线程下载）",
            "download_method_aria2c": "aria2c（多线程下载）"
            }
    else:
        locales = {
            "card_title_ipk_upload": "Third-party IPK installations",
            "card_title_ipk_update": "System IPK/plugins updates",
            "please_select_a_update_pkg": "Please select a package.",
            "upload": "Upload",
            "upload_ipks": "Upload IPK(s)",
            "choose_file": "Choose File",
            "install_custom_ipk_title": "Install Custom IPK/Plugin Packages",
            "install_custom_ipk_description": "Upload and install third-party IPK packages or plugins to extend your device functionality.",
            "choose_your_file": "Choose Your File",
            "card_title_ipk_manager": "IPK Package Management",
            "download_from_url": "Download from URL",
            "paste_ipk_url": "Paste IPK Package Link",
            "download_url_placeholder": "Enter IPK package download URL (supports .ipk, .gz, .xz, .bz2, .tar, max 300MB)",
            "download_button": "Auto Install",
            "downloading": "Downloading",
            "download_logs": "Download and Install Logs",
            "download_progress": "Download Progress",
            "file_too_large_300mb": "File too large (max 300MB)",
            "no_ipk_in_archive": "No IPK files found in archive",
            "extraction_failed": "Extraction failed",
            "or_separator": "OR",
            "ipk_package_name": "Package Name",
            "ipk_package_current_version": "Current Version",
            "ipk_package_latest_version": "Latest Version",
            "not_need_update": "Already Up-to-date",
            "need_update": "Update",
            "upload_operation_tip": "Click or drag files to upload...",
            "help_text": "Support IPK packages, or compressed archives (gz/bz2/xz/tar). Archives will be automatically extracted and all IPK files installed.",
            "check_update": "Checking for update",
            "no_package_info": "No package info", 
            "update": "Update",
            "upgrade_help_text1": "Tip: After the update, some IPKs may require a reboot to take effect.",
            "upgrade_help_text2": "Updating some IPK packages may cause network disconnection for a while.",
            "card_title_firmware_manager": "Photonicat System Image Manager",
            "firmware_checking": "Checking",
            "firmware_new_version_available": "🎉 New Version Available!",
            "firmware_up_to_date": "Firmware Up to Date!",
            "firmware_current_version": "Current Version",
            "firmware_latest_version": "Latest Version",
            "firmware_download_button": "Download Firmware",
            "firmware_flash_button": "Flash Firmware", 
            "firmware_auto_upgrade_button": "Auto Upgrade",
            "firmware_manual_update": "Manual Update",
            "firmware_manual_update_text": "For manual system image flashing, please visit:",
            "firmware_luci_flash_button": "Open LuCI Flash Interface",
            
            # New automatic upgrade locales
            "firmware_upgrading": "Upgrading Firmware",
            "confirm_firmware_upgrade": "Confirm Firmware Upgrade",
            "firmware_upgrade_warning": "This will automatically download and install the latest firmware. The device will restart during the upgrade process. Please ensure stable power supply.",
            "upgrade_process": "Upgrade Process",
            "stage_download": "Download",
            "stage_prepare": "Prepare", 
            "stage_install": "Install",
            "stage_restart": "Restart",
            "keep_current_settings": "Keep current settings",
            "cancel": "Cancel",
            "start_upgrade": "Start Upgrade",
            "confirm_cancel_download": "Are you sure you want to cancel the download? Downloaded files will be deleted.",
            "download_method": "Download Method",
            "recommended": "recommended",
            "faster_but_less_reliable": "faster but less reliable",
            "download_method_help": "wget is more stable, aria2c is faster but may be unstable in some network environments",
            "data_loss_warning": "Data Loss Warning",
            "firmware_upgrade_data_loss_warning": "Firmware upgrade may cause data loss. Please ensure important data is backed up. If settings are not preserved, all configurations will be reset.",
            
            # Dev server locales
            "dev_server_title": "Photonicat Image Server (DEV)",
            "dev_only": "dev only",
            "production_server_title": "Photonicat Image Server",
            "stable": "stable - dl.photonicat.com",
            "dev_firmware_available": "⚡ Dev Version Available!",
            "dev_firmware_up_to_date": "✅ Dev Firmware Up to Date!",
            "dev_latest_version": "Dev Latest Version",
            "dev_current_version": "Dev Current Version",
            "dev_download_button": "Download Dev Firmware",
            "dev_auto_upgrade_button": "Upgrade Dev Version",
            
            # MCU Update Locales - English
            "card_title_mcu_manager": "MCU Firmware Manager",
            "mcu_checking": "Checking MCU...",
            "mcu_dev_server_title": "MCU Server (DEV)", 
            "mcu_production_server_title": "MCU Firmware",
            "mcu_dev_update_available": "Dev MCU Update Available!",
            "mcu_new_version_available": "MCU Update Available!",
            "mcu_dev_up_to_date": "Dev MCU Up to Date!",
            "mcu_up_to_date": "MCU Up to Date!",
            "mcu_current_version": "Current MCU Version",
            "mcu_latest_version": "Latest MCU Version", 
            "mcu_prod_latest_version": "MCU Latest Version",
            "mcu_dev_latest_version": "MCU Dev Latest Version",
            "mcu_dev_update_button": "Update MCU (Dev)",
            "mcu_dev_reinstall_button": "Reinstall MCU",
            "mcu_auto_update_button": "Update MCU",
            "mcu_updating": "Updating...",
            "confirm_mcu_upgrade": "Confirm MCU Update",
            "mcu_upgrade_warning": "This will update your MCU firmware. The process is safe but should not be interrupted.",
            "mcu_upgrade_process": "Update Process",
            "mcu_upgrade_power_warning": "Do not power off during the update process!",
            "start_mcu_update": "Start Update",
            "starting": "Starting...",
            "debug_logs": "Debug Logs",
            
            # Configuration preservation options  
            "config_preservation_options": "Configuration Preservation Options",
            "config_preserve_etc": "-c: Preserve all changed files in /etc/ (default, recommended)",
            "config_preserve_all": "-o: Preserve all changed files in /, except those from packages but including changed configs",
            "config_fresh_install": "-n: Fresh install, remove all configuration files",
            "config_preservation_help": "Choose how to preserve your configuration during upgrade.",
            
            # Download methods with dual language
            "download_method_wget": "wget (single-threaded download)",
            "download_method_aria2c": "aria2c (multi-threaded download)"
             }
    locales.update(base_locales())

    return render_template(nt.update_file_name, locales = locales, show_debug_logs = pcat_config.get_show_debug_logs())

@flask_app.route("/about")
def about_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    locales = {}
    locales.update(base_locales())
    return render_template(nt.about_file_name, locales = locales)

@flask_app.route("/advanced_admin")
def advanced_admin_action():
    host_ip = request.host.split(':')[0]
    advanced_url = "http://" + host_ip + ":" + str(pcat_config.luci_port)
    return redirect(advanced_url)

@api_auth_required
@flask_app.route("/pkg_upload", methods=['POST'])
def pkg_upload_action():
    if 'username' not in session:
        rsp = make_response("{\"status\": \"failed\", \"message\": \"Login required\"}")
        rsp.headers['Content-Type'] = 'application/json'
        return rsp
    
    uploaded_file = request.files['package']
    filename = uploaded_file.filename
    
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

@api_auth_required
@flask_app.route("/pkg_versions", methods=['GET'])
def pkg_versions_action():
    info = pc_app.pcat_package_version_info()
    rsp = make_response(jsonify({"versions": info}))
    # rsp = make_response("{\"versions\":[{\"name\":\"pcat-manager\",\"version\":\"1.0.10-0\"},{\"name\":\"pcat-manager-web\",\"version\":\"1.0.13-0\"},{\"name\":\"quectel-cm\",\"version\":\"1.0.1-1\"}]}")
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/pkg_update", methods=['GET'])
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

@api_auth_required
@flask_app.route("/api/v1/download_ipk", methods=['POST'])
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

        # Validate file extension
        supported_extensions = ['.ipk', '.gz', '.xz', '.bz2', '.tar']
        url_lower = download_url.lower()
        if not any(url_lower.endswith(ext) for ext in supported_extensions):
            rsp = make_response("{\"status\": \"failed\", \"message\": \"Unsupported file type. Supported: .ipk, .gz, .xz, .bz2, .tar\"}")
            rsp.headers['Content-Type'] = 'application/json'
            return rsp

        # Extract filename from URL
        import os
        url_path = parsed.path
        filename = os.path.basename(url_path)
        if not filename:
            # If no filename in URL, create one based on URL
            filename = f"downloaded_package_{int(time.time())}.ipk"

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
        response = requests.get(download_url, stream=True, timeout=30)
        response.raise_for_status()
        
        # Get filename from Content-Disposition header if available
        content_disposition = response.headers.get('content-disposition', '')
        if content_disposition:
            import re
            matches = re.findall("filename=([^;]+)", content_disposition)
            if matches:
                server_filename = matches[-1].strip('\'"')
                if server_filename and any(server_filename.lower().endswith(ext) for ext in supported_extensions):
                    filename = server_filename

        # Check content length if available
        content_length = response.headers.get('content-length')
        if content_length and int(content_length) > 300 * 1024 * 1024:  # 300MB limit
            rsp = make_response("{\"status\": \"failed\", \"message\": \"File too large (max 300MB)\"}")
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
                    if total_size > 300 * 1024 * 1024:  # 300MB limit
                        f.close()
                        os.remove(local_path)
                        rsp = make_response("{\"status\": \"failed\", \"message\": \"File too large (max 300MB)\"}")
                        rsp.headers['Content-Type'] = 'application/json'
                        return rsp

        # For archive files, validate they contain IPK files but don't extract
        # The opkg-install.py script will handle extraction and install all IPKs
        final_filename = filename
        
        if filename.lower().endswith(('.tar', '.gz', '.bz2', '.xz')):
            try:
                import tarfile
                # Just validate the archive contains IPK files
                with tarfile.open(local_path, 'r:*') as tar:
                    ipk_members = [m for m in tar.getmembers() if m.name.endswith('.ipk') and m.isfile()]
                    
                    if not ipk_members:
                        # Clean up and return error
                        local_path.unlink(missing_ok=True)
                        rsp = make_response("{\"status\": \"failed\", \"message\": \"No IPK files found in archive\"}")
                        rsp.headers['Content-Type'] = 'application/json'
                        return rsp
                    
                    # Log how many IPK files were found
                    print(f"Archive contains {len(ipk_members)} IPK files: {[m.name for m in ipk_members[:5]]}{'...' if len(ipk_members) > 5 else ''}")
                        
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

@api_auth_required
@flask_app.route("/system/reboot", methods=['POST'])
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

#factory reset modem, reset openwrt
@api_auth_required
@flask_app.route("/system/reset", methods=['POST'])
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

@api_auth_required
@flask_app.route("/system/shutdown", methods=['POST'])
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
        "modem_mode": get_modem_mode(),
        "modem_signal_strength": pc_app.socket_client.modem_signal_strength,
        "isp_name": pc_app.socket_client.isp_name,
        "sim_state": pc_app.modem_client.get_inferred_sim_state(pc_app.socket_client.sim_state)
    }

@api_auth_required
@flask_app.route("/api/v1/system/board.json")
def system_board_json_action():
    rsp = make_response(_openwrt_ubus_call("system", "board"))
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@flask_app.route("/api/v1/system/debug_openwrt.json")
def system_debug_openwrt_action():
    return jsonify(_openwrt_debug_status())

@api_auth_required
@flask_app.route("/api/v1/system/info.json")
def system_info_json_action():
    rsp = make_response(_openwrt_ubus_call("system", "info"))
    rsp.headers['Content-Type'] = 'application/json'
    return rsp


@api_auth_required
@flask_app.route("/api/v2/wireless.json", methods=['GET'])
def wireless_v2_action():
    wireless_list = {}
    mock_data = request.args.get('mock') == 'true'
    wireless_list["interfaces"] = command_parser.parse_uci_show_wireless(mock_data)
    json_str = json.dumps(wireless_list)  
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/wireless.json", methods=['POST'])
def save_wireless_action():
    if 'username' not in session:
        json_dict = {"status": "failed", "message":"Login required", "redirect": f"/login?next={request.path}"}
        rsp = make_response(json.dumps(json_dict))
        rsp.headers['Content-Type'] = 'application/json'
        return rsp
    
    json_dict = {"status": "ok"}
    p_data = request.get_json()
    pcat_logger.logger.info("Get user {} posted wireless info:{}".format(request.remote_addr, str(p_data)))

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

        cmdtool = CmdTool()
        
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
        cmdtool.prepare(["uci", "set", "wireless.{}.band={}".format(device, fdef["band"])])
        cmdtool.prepare(["uci", "set", "wireless.{}.htmode={}".format(device, target_htmode)])
        cmdtool.prepare(["uci", "set", "wireless.{}.channel={}".format(device, current_channel)])
        cmdtool.prepare(["uci", "set", "wireless.{}.country=US".format(device)])
        cmdtool.prepare(["uci", "set", "wireless.default_{}.ssid={}".format(device, ssid) ])
        # 6 GHz mandates WPA3-SAE with PMF; force it regardless of user selection.
        if fdef["band"] == "6g":
            encryption = "sae"
        cmdtool.prepare(["uci", "set", "wireless.default_{}.encryption={}".format(device, encryption) ])
        if p_data.get("password") != "":
            cmdtool.prepare(["uci", "set", "wireless.default_{}.key={}".format(device, ssid_key)])
        if p_data.get("hide_ssid") == True:
            cmdtool.prepare(["uci", "set", "wireless.default_{}.hidden=1".format(device)])
        else:
            cmdtool.prepare(["uci", "delete", "wireless.default_{}.hidden".format(device)])

        cmdtool.prepare(["uci", "commit", "wireless"])
        cmdtool.prepare(["luci-reload"])
        cmdtool.prepare(["/sbin/wifi", "reload"])
        threading.Timer(2.0, lambda: cmdtool.start()).start()
    
    json_str = json.dumps(json_dict)  
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp  

_wifi_clients_cache = {'data': None, 'timestamp': 0}
_WIFI_CLIENTS_CACHE_DURATION = 60  # seconds

@api_auth_required
@flask_app.route("/api/v1/wifi_clients.json", methods=['GET'])
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

@api_auth_required
@flask_app.route("/api/v1/time.json", methods=['GET'])
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

@api_auth_required
@flask_app.route("/api/v1/timers.json", methods=['GET'])
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
@api_auth_required
@flask_app.route("/api/v1/timers.json", methods=['POST'])
def save_timers_json_action():
    json_data = request.get_json()
    # [{'hour': 0, 'minute': 0, 'actiont': 'shutdown', 'days': [3, 5]}, 
    #  {'hour': 5, 'minute': 0, 'actiont': 'startup', 'days': [1, 2, 3, 4, 5, 6, 7]}] <class 'list'>
    pcat_logger.logger.info("Get user {} posted timer info:{}".format(request.remote_addr, str(json_data)))

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

@api_auth_required
@flask_app.route("/api/v1/carbootmode.json", methods=['POST'])
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

@api_auth_required
@flask_app.route("/api/v1/forcepowermode.json", methods=['POST'])
def save_force_power_mode_action():
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

@api_auth_required
@flask_app.route("/api/v1/idle_shutdown.json", methods=['GET'])
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

@api_auth_required
@flask_app.route("/api/v1/idle_shutdown.json", methods=['POST'])
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

@api_auth_required
@flask_app.route("/api/v1/low_battery_shutdown.json", methods=['POST'])
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

@api_auth_required
@flask_app.route("/api/v1/power_events.json", methods=['GET'])
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

@api_auth_required
@flask_app.route("/api/v1/power_events.json", methods=['DELETE'])
def clear_power_events():
    """Clear all power event log entries"""
    power_event_log.clear_events()
    response_json = {"status": "ok"}
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/battery_history.json", methods=['GET'])
def get_battery_history():
    """Get battery history data for charting"""
    hours = request.args.get('hours', 24, type=int)
    hours = min(hours, 720)  # Max 30 days
    data = battery_history.get_history(hours=hours)

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

@api_auth_required
@flask_app.route("/api/v1/network_interfaces.json", methods=['GET'])
def network_interfaces_api():
    response_json = get_network_interfaces_data()
    
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/network_interfaces.json", methods=['POST'])
def save_network_interfaces_api():
    
    json_data = request.get_json()
    pcat_logger.logger.info("Get user {} posted network interfaces toggle info:{}".format(request.remote_addr, str(json_data)))

    # Handle new network_mode parameter
    network_mode = json_data.get("network_mode", "auto")
    
    # Save network mode to persistent config
    pcat_config.set_network_mode(network_mode)
    
    wan_flag = json_data.get("wan", False)
    sim_flag = json_data.get("sim", False)
    lan_flag = json_data.get("lan", False)
    onboard_wifi_flag = json_data.get("onboard_wifi", False)
    pcie_wifi_flag = json_data.get("pcie_wifi", False)
    
    pc_app.config_network_toggle(network_mode, wan_flag, sim_flag, lan_flag, onboard_wifi_flag, pcie_wifi_flag)

    response_json = {}
    response_json["status"] = "ok"
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/wwan_power.json", methods=['GET'])
def wwan_power_status_api():
    powered = pc_app.is_wwan_powered()
    return jsonify({"status": "ok", "wwan_powered": powered})

@api_auth_required
@flask_app.route("/api/v1/wwan_power.json", methods=['POST'])
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

@api_auth_required
@flask_app.route("/api/v1/apn_settings.json", methods=['GET'])
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

@api_auth_required
@flask_app.route("/api/v1/apn_settings.json", methods=['POST'])
def save_apn_settings_api():
    p_data = request.get_json()
    pcat_logger.logger.info("Get user {} posted APN info:{}".format(request.remote_addr, str(p_data)))

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

@api_auth_required
@flask_app.route("/api/v1/settings.json", methods=['POST'])
def save_settings_api():
    p_data = request.get_json()
    pcat_logger.logger.info("Get user {} posted settings info:{}".format(request.remote_addr, str(p_data)))

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

@api_auth_required
@flask_app.route("/api/v1/power_led_settings.json", methods=['GET'])
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

@api_auth_required
@flask_app.route("/api/v1/power_led_settings.json", methods=['POST'])
def save_power_led_settings_api():
    """Save Power LED settings"""
    p_data = request.get_json()
    pcat_logger.logger.info("Get user {} posted Power LED settings info:{}".format(request.remote_addr, str(p_data)))
    
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

@api_auth_required
@flask_app.route("/api/v1/beeper_settings.json", methods=['GET'])
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

@api_auth_required
@flask_app.route("/api/v1/beeper_settings.json", methods=['POST'])
def save_beeper_settings_api():
    """Save Beeper settings"""
    p_data = request.get_json()
    pcat_logger.logger.info("Get user {} posted Beeper settings info:{}".format(request.remote_addr, str(p_data)))
    
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

@api_auth_required
@flask_app.route("/api/v1/power_led_status.json", methods=['GET'])
def power_led_status_api():
    """Get current Power LED runtime status"""
    settings = pc_app.timer_manager.settings.get('power_led', {})
    current_status = pc_app.socket_client.status_led_v2_enabled
    mode = settings.get('mode', 'on')
    
    # If current status is None, determine based on mode
    if current_status is None:
        if mode == 'off':
            current_status = False
        elif mode == 'on':
            current_status = True
        elif mode == 'timed':
            # For timed mode, check if we're in the scheduled time range
            start_time = settings.get('start_time', '08:00')
            end_time = settings.get('end_time', '22:00')
            current_status = pc_app.timer_manager.is_time_in_range(start_time, end_time)
    
    json_dict = {
        "status": "ok",
        "current_enabled": current_status,
        "mode": mode,
        "start_time": settings.get('start_time', '08:00'),
        "end_time": settings.get('end_time', '22:00'),
        # Legacy fields for backward compatibility
        "enabled": settings.get('enabled', True),
        "all_day": settings.get('all_day', True)
    }
    json_str = json.dumps(json_dict)
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/beeper_status.json", methods=['GET'])
def beeper_status_api():
    """Get current Beeper runtime status"""
    settings = pc_app.timer_manager.settings.get('beeper', {})
    current_status = pc_app.socket_client.beeper_enabled
    mode = settings.get('mode', 'on')
    
    # If current status is None, determine based on mode
    if current_status is None:
        if mode == 'off':
            current_status = False
        elif mode == 'on':
            current_status = True
        elif mode == 'timed':
            # For timed mode, check if we're in the scheduled time range
            start_time = settings.get('start_time', '08:00')
            end_time = settings.get('end_time', '22:00')
            current_status = pc_app.timer_manager.is_time_in_range(start_time, end_time)
    
    json_dict = {
        "status": "ok",
        "current_enabled": current_status,
        "mode": mode,
        "start_time": settings.get('start_time', '08:00'),
        "end_time": settings.get('end_time', '22:00'),
        # Legacy fields for backward compatibility
        "enabled": settings.get('enabled', True),
        "all_day": settings.get('all_day', True)
    }
    json_str = json.dumps(json_dict)
    rsp = make_response(json_str)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/charge_threshold.json", methods=['GET'])
def charge_threshold_get_api():
    """Get battery charge threshold.

    Defaults to 100 (charge fully, no limit) whenever the limit has not been
    set before. The daemon reports an unset limit as None or 0 (ChargerLimitVoltage=0
    in /etc/pcat-manager.conf), and any value outside the valid 80-100 range is
    treated as unset too, so the UI always starts at 100%.
    """
    value = pc_app.socket_client.charge_threshold
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

@api_auth_required
@flask_app.route("/api/v1/charge_threshold.json", methods=['POST'])
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

@api_auth_required
@flask_app.route("/api/v1/timer_debug.json", methods=['POST'])
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

@api_auth_required
@flask_app.route("/api/v1/data_stats.json", methods=['GET'])
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

    response_json = {}
    response_json["today_used"] = pc_app.main_app.network_stats.today_bytes(network_type) or 0
    response_json["week_used"]  = pc_app.main_app.network_stats.this_week_bytes(network_type) or 0
    response_json["month_used"] = pc_app.main_app.network_stats.month_bytes(date.today(), network_type) or 0

    today = date.today()
    first = today.replace(day=1)
    last_month = first - timedelta(days=1)
    response_json["last_month_used"] = pc_app.main_app.network_stats.month_bytes(last_month, network_type) or 0
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp  

@api_auth_required
@flask_app.route("/api/v1/modem/basic.json", methods=['GET'])
def api_model_basic():
    response_json = {}
    # Thread-safe access to modem_valid
    with pc_app.modem_client.mutex:
        response_json["modem_valid"] = pc_app.modem_client.modem_valid
    response_json["wwan_powered"] = pc_app.is_wwan_powered()
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
    response_json["modem_tech_pref"] = pc_app.modem_client.basic.get("modem_tech_pref", "")
    response_json["modem_roam_pref"] = pc_app.modem_client.basic.get("roam_pref", "")
    response_json["policy_lte_bands"] = pc_app.modem_client.basic.get("policy_lte_bands", [])
    response_json["policy_nr5g_bands"] = pc_app.modem_client.basic.get("policy_nr5g_bands", [])
    response_json["selected_lte_bands"] = pc_app.modem_client.basic.get("setting_lte_bands", [])
    response_json["selected_nr5g_bands"] = pc_app.modem_client.basic.get("setting_nr5g_bands", [])

    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp  

@api_auth_required
@flask_app.route("/api/v1/modem/basic.json", methods=['POST'])
def api_modem_refresh():
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

@api_auth_required
@flask_app.route("/api/v2/modem_advanced.json", methods=['POST'])
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
    # if the modem client is already busy, skip issuing a new command
    if not pc_app.modem_client.querying:
        data = request.get_json() or {}

        # kick off the async config call on your modem client
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

@api_auth_required
@flask_app.route("/api/v1/modem/sim_number.json", methods=['POST'])
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

@api_auth_required
@flask_app.route("/api/v2/modem/reset.json", methods=['POST'])
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

@api_auth_required
@flask_app.route("/api/v1/modem/config.json", methods=['GET'])
def api_model_config():
    response_json = {}
    response_json["sms_check_interval"] = pcat_config.config.getint('modem', 'sms_check_interval', fallback = 30)
    response_json["sms_forward"] = pcat_config.config.getboolean('modem', 'sms_forward', fallback = False)
    response_json["sms_forward_to"] = pcat_config.config.get('modem', 'sms_forward_to', fallback = '')
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp  

@api_auth_required
@flask_app.route("/api/v1/modem/config.json", methods=['POST'])
def api_modem_save_config():
    response_json = {}
    p_data = request.get_json()
    print("p_data", p_data)
    pcat_logger.logger.info("Get config data: {}".format(p_data))

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

# curl -X POST http://172.16.0.1/api/v1/modem/sms/send.json -H 'Content-Type: application/json' -d '{"send_to":"19151790085","msg":"Test from API"}'  
@api_auth_required
@flask_app.route("/api/v1/modem/sms/send.json", methods=['POST'])
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

# curl -X POST http://172.16.0.1/api/v1/modem/sms/send.json -H 'Content-Type: application/json' -d '{"send_to":"19151790085","msg":"Test from API"}'  
@api_auth_required
@flask_app.route("/api/v2/modem/sms/send.json", methods=['POST'])
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
@api_auth_required
@flask_app.route("/api/v1/modem/sms/read.json", methods=['POST'])
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
@api_auth_required
@flask_app.route("/api/v1/sms/delete.json", methods=['POST'])
def api_model_sms_delete():
    p_data = request.get_json()
    pcat_logger.logger.info("User {} request delete message:{}".format(request.remote_addr, str(p_data)))
    
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

@api_auth_required
@flask_app.route("/api/v2/sms/list.json", methods=['GET'])
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
                print(f"SMS list served from cache (age: {current_time - sms_cache['timestamp']:.1f}s)")
                cached_result = sms_cache['data']

            # Cache expired - check if we should fetch or serve old cache
            # If another fetch is in progress or last fetch was < 10 seconds ago, serve old cache
            elif (sms_cache['fetch_in_progress'] or
                  (sms_cache['data'] is not None and
                   current_time - sms_cache['last_fetch_time'] < SMS_FETCH_THROTTLE)):
                if sms_cache['data'] is not None:
                    print(f"SMS list served from old cache due to throttle (last fetch: {current_time - sms_cache['last_fetch_time']:.1f}s ago)")
                    cached_result = sms_cache['data']

    if cached_result:
        # Apply limit to cached result if requested
        if requested_limit and isinstance(cached_result, dict) and 'msg' in cached_result:
            limited_result = dict(cached_result)  # Create a copy
            if len(limited_result["msg"]) > requested_limit:
                limited_result["msg"] = limited_result["msg"][:requested_limit]
                print(f"SMS list limited to {requested_limit} messages from cache")
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

@api_auth_required
@flask_app.route("/api/v2/sms/clear_all.json", methods=['POST'])
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
@api_auth_required  
@flask_app.route("/api/v1/mcu_dev_check.json", methods=['GET'])
def mcu_dev_check():
    """
    Check development server for MCU firmware updates.
    Returns dev server status and MCU firmware information.
    """
    try:
        # Get current local MCU version
        local_version = pc_app.socket_client.stm32_firmware_version or "Unknown"
        
        # Dev server URL - should contain JSON with latest firmware info
        dev_url = "http://192.168.8.28:81/photonicat2_mcu_firmware/latest_img.json"
        
        with urllib.request.urlopen(dev_url, timeout=20) as response:
            remote_data = json.loads(response.read().decode('utf-8'))
        
        # Extract information from remote JSON
        # Expected format: {"version": "1.2.3", "url": "relative/or/absolute/path.bin", "sha256": "hash", "build_date": "2025-01-01"}
        remote_version = remote_data.get("version", "")
        firmware_url = remote_data.get("url", "")
        sha256_hash = remote_data.get("sha256", "")
        build_date = remote_data.get("build_date", "")
        
        # Make URL absolute if it's relative
        if firmware_url and not firmware_url.startswith("http"):
            firmware_url = f"http://192.168.8.28:81/photonicat2_mcu_firmware/{firmware_url}"
            
        # Simple version comparison (assuming semantic versioning or string comparison)
        update_available = local_version != remote_version and remote_version != ""
        newer_available = False
        
        # Try to do numeric comparison if versions are numeric
        try:
            local_parts = [int(x) for x in local_version.replace('v', '').split('.')]
            remote_parts = [int(x) for x in remote_version.replace('v', '').split('.')]
            
            # Pad to same length
            max_len = max(len(local_parts), len(remote_parts))
            local_parts.extend([0] * (max_len - len(local_parts)))
            remote_parts.extend([0] * (max_len - len(remote_parts)))
            
            newer_available = remote_parts > local_parts
            update_available = newer_available
        except (ValueError, AttributeError):
            # Fall back to string comparison
            newer_available = remote_version > local_version if remote_version else False
            update_available = local_version != remote_version and remote_version != ""

        response_data = {
            "status": "ok",
            "reachable": True,
            "local_version": local_version,
            "dev_version": remote_version,
            "dev_firmware_url": firmware_url,
            "dev_sha256": sha256_hash,
            "dev_build_date": build_date,
            "update_available": update_available,
            "newer_available": newer_available,
            "same_version": local_version == remote_version
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


@api_auth_required
@flask_app.route("/api/v1/mcu_prod_check.json", methods=['GET'])
def mcu_prod_check():
    """
    Check production server for MCU firmware updates.
    Returns production server status and MCU firmware information.
    """
    try:
        # Get current local MCU version
        local_version = pc_app.socket_client.stm32_firmware_version or "Unknown"
        
        # Production server URL
        prod_url = "https://dl.photonicat.com/firmware/pcat2_mcu/latest_img.json"
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
        sha256_hash = remote_data.get("sha256", "")
        build_date = remote_data.get("build_date", "")
        
        # Make URL absolute if it's relative
        if firmware_url and not firmware_url.startswith("http"):
            firmware_url = f"https://dl.photonicat.com/firmware/pcat2_mcu/{firmware_url}"
            
        # Simple version comparison
        update_available = local_version != remote_version and remote_version != ""
        newer_available = False
        
        # Try to do numeric comparison if versions are numeric
        try:
            local_parts = [int(x) for x in local_version.replace('v', '').split('.')]
            remote_parts = [int(x) for x in remote_version.replace('v', '').split('.')]
            
            # Pad to same length  
            max_len = max(len(local_parts), len(remote_parts))
            local_parts.extend([0] * (max_len - len(local_parts)))
            remote_parts.extend([0] * (max_len - len(remote_parts)))
            
            newer_available = remote_parts > local_parts
            update_available = newer_available
        except (ValueError, AttributeError):
            # Fall back to string comparison
            newer_available = remote_version > local_version if remote_version else False
            update_available = local_version != remote_version and remote_version != ""

        response_data = {
            "status": "ok",
            "reachable": True,
            "local_version": local_version,
            "prod_version": remote_version,
            "prod_firmware_url": firmware_url,
            "prod_sha256": sha256_hash,
            "prod_build_date": build_date,
            "update_available": update_available,
            "newer_available": newer_available,
            "same_version": local_version == remote_version
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

@api_auth_required
@flask_app.route("/api/v1/mcu_update_status.json", methods=['GET'])
def mcu_update_status_api():
    """Get current MCU update status"""
    response_data = dict(mcu_update_status)
    response_data["status"] = "ok"
    
    rsp = make_response(response_data)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp


@api_auth_required
@flask_app.route("/api/v1/mcu_update.json", methods=['POST'])
def mcu_update_start():
    """Start MCU firmware update process"""
    global mcu_update_status
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No data provided"}), 400
            
        firmware_url = data.get("firmware_url")
        if not firmware_url:
            return jsonify({"status": "error", "message": "Firmware URL required"}), 400
            
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
                    # Check if this is a dev server URL (local network)
                    is_dev_server = "192.168.8.28" in firmware_url or "localhost" in firmware_url or "127.0.0.1" in firmware_url
                    
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
    if c_locale == "CN":
        locales = {
            "screen_edit_title": "编辑屏幕",
            "tab_screen_settings": "屏幕 - 设置",
            "tab_screen_editor": "屏幕 - 编辑",
            "save_settings": "保存设置",
            "load_default": "加载默认",
            "save_json": "保存JSON",
            "show_sms_feature": "显示短信功能",
            "screen_settings_title": "屏幕设置",
            "screen_off_from": "充电时：屏幕关闭时间：从",
            "screen_off_to": "充电时：屏幕关闭时间：到",
            "on_battery_auto_dim_time": "电池模式下自动熄屏时间(秒)",
            "on_charger_auto_dim_time": "充电模式下自动熄屏时间(秒)",
            "screen_live_title": "屏幕实时图片",
            "user_config_json_title": "用户配置JSON",
            "screen_json_help_text": "屏幕配置JSON文件，用于配置屏幕的显示内容和样式。此处用户JSON配置，会被融合到默认配置中。",
            "restart_screen": "重启屏幕",
            "short_click": "短按",
            "screen_max_brightness": "最大亮度",
            "screen_min_brightness": "最小亮度",
            "ping_site0": "Ping 的网站 1",
            "ping_site1": "Ping 的网站 2",
            "sms_limit_for_screen": "屏幕显示的短信数量限制",
            "config_saved": "配置已保存",
            "reference_config": "参考配置",
            "reference_config_description": "查看完整的示例配置，包含所有可用选项和数据键。",
            "programming_guide": "编程指南",
            "programming_guide_description": "完整的编程指南，包含示例和数据键参考。",
            "tab_screen_visual": "屏幕 - 可视化编辑",
            "visual_canvas_title": "页面布局 (172×320)",
            "visual_page": "页",
            "visual_page_add": "添加页",
            "visual_page_remove": "删除此页",
            "visual_drag_hint": "拖动元素移动，拖动右下角手柄调整大小",
            "visual_zones_hint": "顶栏与底栏由系统绘制（仅供预览），只能编辑中间区域",
            "data_sources_title": "数据源",
            "data_sources_refresh_all": "刷新状态",
            "data_sources_help": "数据源会获取一个值（来自网址、命令或 UCI），并将其保存在你设定的 data_key 名称下。要显示它，请在画布上添加一个文本元素，并把它的 data_key 设为相同的名称——这样两者就关联起来了。",
            "data_sources_how_title": "数据源如何显示到屏幕上",
            "data_sources_flow": "数据源 → data_key（例如 BtcUsd）→ data_key = BtcUsd 的文本元素",
            "data_sources_casenote": "两处的 data_key 名称必须完全一致（区分大小写）。新增或修改的数据源需“保存并重启”后才生效。",
            "ds_data_key_tip": "该值保存时使用的名称。把画布上的文本元素的 data_key 设为相同名称即可显示它。",
            "dk_help_tip": "此处要显示的值的名称。可选择你在下方定义的自定义数据源，或内置的设备指标。留空则只显示静态文字。",
            "dk_placeholder": "例如 BtcUsd",
            "dk_now": "当前：",
            "dk_status_custom": "已关联到你的数据源。",
            "dk_status_builtin": "内置设备指标。",
            "dk_status_unknown": "暂无数据源使用此键。请在下方添加一个 data_key 完全相同的数据源，然后“保存并重启”。（区分大小写。）",
            "dk_status_empty": "留空＝无实时值（仅按标签显示静态文字）。输入或选择一个键以显示实时值。",
            "ds_type_http_poll": "HTTP/HTTPS",
            "ds_type_command": "命令",
            "ds_type_uci": "UCI 值",
            "ds_type_env": "环境变量",
            "ds_type_json_file": "JSON 文件",
            "ds_name": "源名称",
            "ds_command": "命令",
            "ds_uci_path": "UCI 路径",
            "ds_parser": "解析器",
            "ds_interval": "间隔（秒）",
            "ds_refresh_now": "立即刷新",
            "ds_shell_warning": "将以显示进程的权限在设备上执行 Shell 命令，请仅使用可信命令。",
            "ds_empty": "暂无自定义数据源。",
            "ds_save_hint": "新增或修改的数据源需点击上方“保存并重启”后才生效（会保存数据源并重启屏幕）。“立即刷新”仅能测试已保存的数据源。",
            "ds_save_restart": "保存并重启",
            "ds_saving_restarting": "保存中…",
            "ds_save_restart_tip": "保存这些数据源并重启屏幕使其生效。",
            "ds_examples_label": "示例（点击即可添加一个现成的数据源）：",
            "ds_examples_internet_note": "HTTP/HTTPS 数据源需要设备能访问互联网。若设备离线，显示的值将为 ERROR 或 TIMEOUT。",
            "ds_local_examples_label": "设备本地示例（无需联网）：",
            "ds_local_examples_note": "command / uci 会以显示进程的权限从设备读取数据；env 读取一个进程环境变量；json_file 从设备上的文件中读取一个值。",
            "ds_expand": "展开",
            "ds_collapse": "收起",
            "ds_expand_all": "全部展开",
            "ds_collapse_all": "全部收起",
            "ds_dup_key": "键重复",
            "ds_dup_key_tip": "另一个已启用的数据源使用了相同的 data_key，它们会在屏幕上互相覆盖。",
            "ds_dup_banner": "以下 data_key 被多个已启用的数据源使用，会在屏幕上互相覆盖：",
            "ds_fetching": "获取中…",
            "ds_value_error_tip": "获取失败。请检查网址、解析器，并确认设备能访问互联网。",
            "visual_add_element": "添加元素",
            "visual_reload": "重新加载",
            "visual_save": "保存布局",
            "visual_props_title": "元素属性",
            "visual_no_selection": "请在画布上选择一个元素进行编辑。",
            "visual_prop_type": "类型",
            "visual_prop_label": "标签",
            "visual_prop_text": "文本",
            "visual_prop_units": "单位",
            "visual_prop_font": "字体",
            "visual_prop_color": "颜色",
            "visual_prop_position": "位置 (x, y)",
            "visual_prop_size": "大小 (宽, 高)",
            "graph_type": "图表类型",
            "graph_time_frame": "时间范围 (分钟)",
            "visual_reset_pos": "重置位置",
            "visual_reset_pos_tip": "重置为加载此布局时该元素所在的位置",
            "visual_reset_pos_default": "默认位置",
            "visual_reset_pos_default_tip": "重置为该元素在默认配置中的位置",
            "visual_center_h": "居中",
            "visual_center_h_tip": "水平居中",
            "visual_snap": "对齐",
            "visual_snap_tip": "将位置对齐到最近的 4 像素网格",
            "visual_pos_moved": "已从原始位置移动",
            "visual_size_changed": "已从原始大小调整",
            "visual_prop_enable": "启用",
            "visual_delete_element": "删除元素",
            "visual_reset_default": "重置为默认",
            "visual_revert_saved": "撤销更改",
            "visual_reset_confirm": "确定要将所有页面的布局重置为默认配置吗？",
            "visual_revert_confirm": "确定要放弃所有未保存的更改吗？",
            "editor_diff_title": "更改对比 (相对已保存)",
            "editor_no_changes": "无更改",
            "editor_show_diff": "显示更改",
            "editor_hide_diff": "隐藏更改",
            "editor_mode_diff": "差异",
            "editor_mode_full": "完整",
            "visual_show_label": "显示标签",
            "visual_show_key": "显示数据键",
            "visual_undo": "撤销",
            "visual_redo": "重做",
            "visual_shortcuts_hint": "快捷键：方向键微调 · Shift+方向键 ×10 · Ctrl+Z 撤销 · Ctrl+Shift+Z 重做 · Ctrl+D 复制 · Del 删除 · Ctrl+S 保存"
            }
    else:
        locales = {
            "screen_edit_title": "Edit Screen",
            "tab_screen_settings": "Screen - Settings",
            "tab_screen_editor": "Screen - Editor",
            "save_settings": "Save Settings",
            "load_default": "Load Default",
            "save_json": "Save JSON",
            "show_sms_feature": "Show SMS Feature",
            "screen_settings_title": "Screen Settings",
            "screen_off_from": "On Charger: Screen Off: From",
            "screen_off_to": "On Charger: Screen Off: To",
            "on_battery_auto_dim_time": "On Battery： Auto Dim Time(seconds)",
            "on_charger_auto_dim_time": "On Charger： Auto Dim Time(seconds)",
            "screen_live_title": "Screen Live Image",
            "user_config_json_title": "User Config JSON",
            "screen_json_help_text": "Screen configuration JSON file, used to configure the display content and style of the screen. User JSON config will be merged into the default config.",
            "restart_screen": "Restart Screen",
            "short_click": "Short Click",
            "screen_max_brightness": "Max Brightness",
            "screen_min_brightness": "Min Brightness",
            "ping_site0": "Ping Site 1",
            "ping_site1": "Ping Site 2",
            "sms_limit_for_screen": "SMS Limit for Screen Display",
            "config_saved": "Config Saved",
            "reference_config": "Reference Config",
            "reference_config_description": "View the complete example configuration with all available options and data keys.",
            "programming_guide": "Programming Guide",
            "programming_guide_description": "Complete programming guide with examples and data key reference.",
            "tab_screen_visual": "Screen - Visual Editor",
            "visual_canvas_title": "Page Layout (172×320)",
            "visual_page": "Page",
            "visual_page_add": "Add page",
            "visual_page_remove": "Remove this page",
            "visual_drag_hint": "Drag elements to move, drag the corner handle to resize",
            "visual_zones_hint": "Top & bottom bars are drawn by the system (preview only); you can only edit the middle area",
            "data_sources_title": "Data Sources",
            "data_sources_refresh_all": "Refresh status",
            "data_sources_help": "A source fetches a value (from a URL, command, or UCI) and stores it under a data_key you choose. To show it, add a text element on the canvas and set its data_key to the SAME name — that links them.",
            "data_sources_how_title": "How a data source reaches the screen",
            "data_sources_flow": "source → data_key (e.g. BtcUsd) → text element with data_key = BtcUsd",
            "data_sources_casenote": "The two data_key names must match exactly (case-sensitive). New/edited sources need Save and Restart to take effect.",
            "ds_data_key_tip": "The name this value is stored under. Set a text element on the canvas to this same data_key to display it.",
            "dk_help_tip": "The name of the value to display here. Pick a custom source you defined below, or a built-in device metric. Leave empty to show static text only.",
            "dk_placeholder": "e.g. BtcUsd",
            "dk_now": "now:",
            "dk_status_custom": "Linked to your data source.",
            "dk_status_builtin": "Built-in device metric.",
            "dk_status_unknown": "No data source has this key yet. Add a source below with this exact data_key, then Save and Restart. (Names are case-sensitive.)",
            "dk_status_empty": "Empty = no live value (shows the Label as static text). Type or pick a key to show a live value.",
            "ds_type_http_poll": "HTTP/HTTPS",
            "ds_type_command": "Command",
            "ds_type_uci": "UCI value",
            "ds_type_env": "Env var",
            "ds_type_json_file": "JSON file",
            "ds_name": "source name",
            "ds_command": "Command",
            "ds_uci_path": "UCI path",
            "ds_parser": "Parser",
            "ds_interval": "Interval (s)",
            "ds_refresh_now": "Refresh now",
            "ds_shell_warning": "Runs a shell command on the device with the display process's privileges. Only use trusted commands.",
            "ds_empty": "No custom data sources yet.",
            "ds_save_hint": "New or edited sources take effect only after you click “Save and Restart” above (this saves them and restarts the screen). “Refresh now” only tests a source that is already saved.",
            "ds_save_restart": "Save and Restart",
            "ds_saving_restarting": "Saving…",
            "ds_save_restart_tip": "Save these sources and restart the screen so they take effect.",
            "ds_examples_label": "Examples (click to add a ready-made one):",
            "ds_examples_internet_note": "HTTP/HTTPS sources need the device to have internet access. If it is offline, the value shows ERROR or TIMEOUT.",
            "ds_local_examples_label": "On-device examples (no internet needed):",
            "ds_local_examples_note": "command / uci read from the device with the display's privileges. env reads a process variable; json_file reads a value from a file on the device.",
            "ds_expand": "Expand",
            "ds_collapse": "Collapse",
            "ds_expand_all": "Expand all",
            "ds_collapse_all": "Collapse all",
            "ds_dup_key": "duplicate key",
            "ds_dup_key_tip": "Another enabled source uses the same data_key; they will overwrite each other on screen.",
            "ds_dup_banner": "These data_keys are used by more than one enabled source and will overwrite each other on screen:",
            "ds_fetching": "fetching…",
            "ds_value_error_tip": "The fetch failed. Check the URL, the parser, and that the device has internet access.",
            "visual_add_element": "Add Element",
            "visual_reload": "Reload",
            "visual_save": "Save Layout",
            "visual_props_title": "Element Properties",
            "visual_no_selection": "Select an element on the canvas to edit it.",
            "visual_prop_type": "Type",
            "visual_prop_label": "Label",
            "visual_prop_text": "Text",
            "visual_prop_units": "Units",
            "visual_prop_font": "Font",
            "visual_prop_color": "Color",
            "visual_prop_position": "Position (x, y)",
            "visual_prop_size": "Size (w, h)",
            "graph_type": "Graph type",
            "graph_time_frame": "Time frame (min)",
            "visual_reset_pos": "Reset position",
            "visual_reset_pos_tip": "Reset to the position it had when this layout was loaded",
            "visual_reset_pos_default": "Default position",
            "visual_reset_pos_default_tip": "Reset to this element’s position in the default configuration",
            "visual_center_h": "Center",
            "visual_center_h_tip": "Center horizontally",
            "visual_snap": "Snap",
            "visual_snap_tip": "Snap position to the nearest 4px grid",
            "visual_pos_moved": "Moved from its original position",
            "visual_size_changed": "Resized from its original size",
            "visual_prop_enable": "Enable",
            "visual_delete_element": "Delete Element",
            "visual_reset_default": "Reset to Default",
            "visual_revert_saved": "Discard Changes",
            "visual_reset_confirm": "Reset the layout of ALL pages back to the default configuration?",
            "visual_revert_confirm": "Discard all unsaved layout changes?",
            "editor_diff_title": "Changes (vs saved)",
            "editor_no_changes": "No changes",
            "editor_show_diff": "Show changes",
            "editor_hide_diff": "Hide changes",
            "editor_mode_diff": "Diff",
            "editor_mode_full": "Full",
            "visual_show_label": "Show Label",
            "visual_show_key": "Show data_key",
            "visual_undo": "Undo",
            "visual_redo": "Redo",
            "visual_shortcuts_hint": "Shortcuts: arrows nudge · Shift+arrows ×10 · Ctrl+Z undo · Ctrl+Shift+Z redo · Ctrl+D duplicate · Del delete · Ctrl+S save"
             }
    locales.update(base_locales())

    return render_template(nt.screen_file_name, locales = locales)


@flask_app.route("/sms")
def sms_action():
    if 'username' not in session:
        return redirect(f"/login?next={request.path}")

    c_locale = current_locale()
    if c_locale == "CN":
        locales = {
            "sms_title": "短信"
            }
    else:
        locales = {
            "sms_title": "SMS"
             }
    locales.update(base_locales())

    return render_template(nt.sms_file_name, locales = locales)

@api_auth_required
@flask_app.route("/api/v1/screen/user_config.json", methods=['GET'])
def api_screen_user_config():
    screen_config = pc_app.screen_client.get_user_config()
    rsp = make_response(screen_config)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/screen/user_config.json", methods=['POST'])
def api_screen_user_config_post():
    p_data = request.get_json()
    pc_app.screen_client.save_user_config(p_data)
    response_json = {}
    response_json["status"] = "ok"
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/screen/default_config.json", methods=['GET'])
def api_screen_default_config():
    screen_config = pc_app.screen_client.get_default_config()
    rsp = make_response(screen_config)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/screen/custom_metrics/status", methods=['GET'])
def api_screen_custom_metrics_status():
    status = pc_app.screen_client.get_custom_metrics_status()
    if status is None:
        status = {"sources": []}
    rsp = make_response(status)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/screen/custom_metrics/<source_name>/execute", methods=['POST'])
def api_screen_custom_metrics_execute(source_name):
    result = pc_app.screen_client.execute_custom_metrics_source(source_name)
    if result is None:
        result = {"status": "error"}
    rsp = make_response(result)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/screen/data.json", methods=['GET'])
def api_screen_data():
    """Live globalData values (data_key -> current value) from the display."""
    data = pc_app.screen_client.get_data()
    if data is None:
        data = {}
    rsp = make_response(data)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/screen/frame.png", methods=['GET'])
def api_screen_frame():
    screen_frame = pc_app.screen_client.get_frame()
    rsp = make_response(screen_frame)
    rsp.headers['Content-Type'] = 'image/png'
    return rsp


@api_auth_required
@flask_app.route("/api/v1/modem/hook_script.json", methods=['GET'])
def api_modem_hook_script():
    script = pcat_config.load_sms_hook_script()
    response_json = {}
    response_json["status"] = "ok"
    response_json["script"] = script
    response_json["enabled"] = pcat_config.get_sms_hook_enabled()
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/modem/hook_script.json", methods=['POST'])
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

@api_auth_required
@flask_app.route("/api/v1/modem/hook_history_insert_test.json", methods=['POST'])
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

@api_auth_required
@flask_app.route("/api/v1/modem/hook_history_create_table.json", methods=['POST'])
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

@api_auth_required
@flask_app.route("/api/v1/modem/hook_history_test.json", methods=['GET'])
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

@api_auth_required
@flask_app.route("/api/v1/modem/hook_history.json", methods=['GET'])
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

@api_auth_required
@flask_app.route("/api/v1/modem/hook_history/<int:history_id>.json", methods=['GET'])
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

@api_auth_required  
@flask_app.route("/api/v1/modem/hook_history.json", methods=['DELETE'])
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

@api_auth_required
@flask_app.route("/api/v1/cpu_scheduling.json", methods=['GET'])
def api_cpu_scheduling():
    response_json = {}
    response_json["status"] = "ok"
    response_json["cpu_scheduling_enabled"] = pcat_config.get_cpu_scheduling_enabled()
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/cpu_scheduling.json", methods=['POST'])
def api_cpu_scheduling_post():
    p_data = request.get_json()
    cpu_scheduling_enabled = p_data.get("cpu_scheduling_enabled", False)
    pcat_config.set_cpu_scheduling_enabled(cpu_scheduling_enabled)
    response_json = {}
    response_json["status"] = "ok"
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/fan_speed.json", methods=['GET'])
def api_fan_speed():
    response_json = {}
    response_json["status"] = "ok"
    response_json["fan_speed"] = pcat_config.get_fan_speed()
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/fan_speed_actual.json", methods=['GET'])
def api_fan_speed_actual():
    response_json = {}
    response_json["status"] = "ok"
    response_json["fan_speed_rpm"] = pcat_config.get_actual_fan_speed()
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/sms_limit_for_screen.json", methods=['GET'])
def api_sms_limit_for_screen():
    response_json = {}
    response_json["status"] = "ok"
    response_json["sms_limit_for_screen"] = pcat_config.get_sms_limit_for_screen()
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/sms_limit_for_screen.json", methods=['POST'])
def api_sms_limit_for_screen_post():
    p_data = request.get_json()
    limit = p_data.get("sms_limit_for_screen", 10)
    pcat_config.set_sms_limit_for_screen(limit)
    response_json = {}
    response_json["status"] = "ok"
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/fan_speed.json", methods=['POST'])
def api_fan_speed_post():
    p_data = request.get_json()
    user_max_speed = p_data.get("fan_speed", 5)

    # Save user's max speed preference
    pcat_config.set_fan_speed(user_max_speed)

    # Get current fan mode
    fan_mode = pcat_config.get_fan_mode()

    # If in manual mode, directly set fan speed
    # If in smart mode, apply smart algorithm with user_max_speed as cap
    if fan_mode == "manual":
        pcat_config.set_fan_speed_direct(user_max_speed)
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
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/fan_mode.json", methods=['GET'])
def api_fan_mode():
    response_json = {}
    response_json["status"] = "ok"
    response_json["fan_mode"] = pcat_config.get_fan_mode()
    rsp = make_response(response_json)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/fan_mode.json", methods=['POST'])
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

@api_auth_required
@flask_app.route("/api/v1/screen/restart", methods=['GET'])
def api_screen_restart():
    res = pc_app.screen_client.restart_screen()
    rsp = make_response(res)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

@api_auth_required
@flask_app.route("/api/v1/screen/short_click", methods=['GET'])
def api_screen_short_click():
    res = pc_app.screen_client.short_click()
    rsp = make_response(res)
    rsp.headers['Content-Type'] = 'application/json'
    return rsp

# Voice Call API Endpoints
@api_auth_required  
@flask_app.route("/api/v1/voice_call/make", methods=['POST'])
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
            # Log the call attempt
            pc_app.sms_client.log_call_history("outgoing", number, "initiated")
            
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

@api_auth_required
@flask_app.route("/api/v1/voice_call/answer", methods=['POST'])
def api_voice_call_answer():
    try:
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

@api_auth_required
@flask_app.route("/api/v1/voice_call/hangup", methods=['POST'])
def api_voice_call_hangup():
    try:
        # Use pysms_tool to hang up the call
        success = pc_app.sms_client.hangup_call()
        
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

@api_auth_required
@flask_app.route("/api/v1/voice_call/status", methods=['GET'])
def api_voice_call_status():
    try:
        # Use pysms_tool to get call status
        call_status = pc_app.sms_client.get_call_status()
        
        response_json = {
            "success": True,
            "calls": call_status.get("calls", []),
            "has_active_calls": len(call_status.get("calls", [])) > 0
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

@api_auth_required
@flask_app.route("/api/v1/voice_call/audio_path", methods=['POST'])
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

@api_auth_required
@flask_app.route("/api/v1/voice_call/history", methods=['GET'])
def api_voice_call_history():
    try:
        # Get call history from SMS client
        history = pc_app.sms_client.get_call_history()
        
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

@api_auth_required
@flask_app.route("/api/v1/voice_call/history", methods=['DELETE'])
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

@api_auth_required
@flask_app.route("/api/v1/voice_call/history/<call_id>", methods=['DELETE'])
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

@api_auth_required
@flask_app.route("/api/v1/voice_call/incoming", methods=['GET'])
def api_voice_call_incoming():
    try:
        # Get incoming call status using pysms_tool
        incoming_call = pc_app.sms_client.get_incoming_call_status()
        
        if incoming_call:
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
    if c_locale == "CN":
        locales = {
            "terminal_title": "终端",
            "terminal_reconnect": "重新连接",
            "terminal_disconnect": "断开",
            "terminal_connected": "已连接",
            "terminal_connecting": "连接中...",
            "terminal_disconnected": "已断开",
        }
    else:
        locales = {
            "terminal_title": "Terminal",
            "terminal_reconnect": "Reconnect",
            "terminal_disconnect": "Disconnect",
            "terminal_connected": "Connected",
            "terminal_connecting": "Connecting...",
            "terminal_disconnected": "Disconnected",
        }

    locales.update(base_locales())
    return render_template(nt.terminal_file_name, locales=locales)


# Terminal: use Socket.IO rooms for broadcasting PTY output.
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
                    socketio.emit('terminal_output',
                                  {'data': data.decode('utf-8', errors='replace')},
                                  to=room)
        except Exception as e:
            pcat_logger.logger.error(f"Terminal reader error for {session_id}: {e}")
        finally:
            with _terminal_lock:
                _terminal_readers.pop(session_id, None)
            socketio.emit('terminal_exit', {'session_id': session_id}, to=room)
            pcat_logger.logger.info(f"Terminal reader EXIT for {session_id}")


@socketio.event
def terminal_open(message):
    """Client requests to open/attach a terminal session."""
    if not pcat_config.skip_auth and 'username' not in session:
        emit('terminal_exit', {'error': 'Not authenticated'})
        return

    session_id = message.get('session_id', 'default')
    cols = message.get('cols', 80)
    rows = message.get('rows', 24)
    room = _terminal_room(session_id)
    pcat_logger.logger.info(f"Terminal open: session_id={session_id}, sid={request.sid}, cols={cols}, rows={rows}")

    # Join the room so this socket receives PTY output
    join_room(room)

    is_new = terminal_manager.get(session_id) is None
    sess = terminal_manager.get_or_create(session_id, cols, rows)
    sess.resize(cols, rows)

    if not is_new:
        # Replay saved output so client sees previous session content
        scrollback = sess.get_scrollback()
        if scrollback:
            emit('terminal_output', {'data': scrollback.decode('utf-8', errors='replace')})

    # Start reader thread if not already running
    with _terminal_lock:
        if session_id not in _terminal_readers:
            _terminal_readers[session_id] = True
            socketio.start_background_task(
                _terminal_reader_thread,
                flask_app.app_context(),
                session_id
            )
            pcat_logger.logger.info(f"Terminal reader started for {session_id}")


@socketio.event
def terminal_input(message):
    """Client sends input to the terminal."""
    session_id = message.get('session_id', 'default')
    data = message.get('data', '')
    sess = terminal_manager.get(session_id)
    if sess:
        sess.write(data)


@socketio.event
def terminal_resize(message):
    """Client resizes the terminal."""
    session_id = message.get('session_id', 'default')
    cols = message.get('cols', 80)
    rows = message.get('rows', 24)
    sess = terminal_manager.get(session_id)
    if sess:
        sess.resize(cols, rows)


@socketio.event
def terminal_kill(message):
    """Client requests to kill a terminal session (for reconnect with fresh shell)."""
    session_id = message.get('session_id', 'default')
    pcat_logger.logger.info(f"Terminal kill request: session_id={session_id}")
    terminal_manager.kill(session_id)
    with _terminal_lock:
        _terminal_readers.pop(session_id, None)
