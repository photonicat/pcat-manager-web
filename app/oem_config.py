# -*- coding: utf-8 -*-
# OEM configuration reader for boot1 partition
# Reads branding, serial number, and signature from /dev/mmcblk0boot1

import os
import json
import base64
import logging

logger = logging.getLogger("oem_config")

# Ed25519 public key for verifying machine_sn signatures
# Corresponding private key is kept in the factory signing server (never in this repo)
MANUFACTURER_PUBLIC_KEY_B64 = "dsSEUtJtUy7PtByCPj40EOVF0I8RlMqgIRKaz6toQdo="

BOOT1_PATH = "/dev/mmcblk0boot1"

# Cached parsed data from boot1
_boot1_cache = None

# Default OEM branding (used when boot1 has no oem section)
DEFAULT_OEM = {
    "name": "Router",
    "id": "generic",
    "company_url": "",
    "forum_url": "",
    "wiki_url": "",
    "tips_url": "",
    "firmware_base_url": "",
    "ip_check_urls": [],
    "ipv6_check_url": "",
    "colors": {
        "primary": "#008AD3",
        "accent": "#FDE021",
        "dark": "#00274D"
    },
    "images": {}
}


def _read_boot1_raw():
    """Read and parse the JSON from boot1 partition."""
    try:
        if not os.path.exists(BOOT1_PATH):
            return None
        with open(BOOT1_PATH, "rb") as f:
            raw = f.read()
        null_idx = raw.find(b'\0')
        if null_idx != -1:
            raw = raw[:null_idx]
        text = raw.decode("utf-8").strip()
        if not text:
            return None
        return json.loads(text)
    except (FileNotFoundError, PermissionError, UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to read boot1: {e}")
        return None


def get_boot1_data():
    """Get parsed boot1 JSON, cached after first read."""
    global _boot1_cache
    if _boot1_cache is not None:
        return _boot1_cache

    data = _read_boot1_raw()
    if data is None:
        data = {}
    _boot1_cache = data
    return data


def verify_sn_signature(emmc_sn, machine_sn, signature_b64):
    """Verify the Ed25519 signature over 'emmc_sn|machine_sn'.
    Returns True if valid, False otherwise."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        pub_bytes = base64.b64decode(MANUFACTURER_PUBLIC_KEY_B64)
        public_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
        sig_bytes = base64.b64decode(signature_b64)
        message = f"{emmc_sn}|{machine_sn}".encode("utf-8")
        public_key.verify(sig_bytes, message)
        return True
    except Exception:
        return False


def get_machine_sn():
    """Get machine serial number with signature verification.
    Returns (sn_string, is_verified) tuple."""
    data = get_boot1_data()
    machine_sn = data.get("machine_sn")
    if not machine_sn:
        if not os.path.exists(BOOT1_PATH):
            return ("DEV-UNKNOWN", False)
        return ("N/A", False)

    emmc_sn = data.get("emmc_sn", "")
    signature = data.get("signature", "")
    if emmc_sn and signature:
        verified = verify_sn_signature(emmc_sn, machine_sn, signature)
    else:
        verified = False

    return (machine_sn, verified)


def get_oem():
    """Get OEM branding config. Merges boot1 oem section over defaults.
    Falls back to oem.txt if boot1 has no oem section."""
    data = get_boot1_data()
    oem_from_boot1 = data.get("oem")
    if oem_from_boot1 and isinstance(oem_from_boot1, dict):
        merged = dict(DEFAULT_OEM)
        merged.update(oem_from_boot1)
        return merged

    # Fallback: try oem.txt (legacy)
    return _read_oem_txt()


def _read_oem_txt():
    """Legacy fallback: read oem.txt file."""
    import configparser
    oem_txt_path = "oem.txt"
    if not os.path.exists(oem_txt_path):
        return dict(DEFAULT_OEM)
    try:
        cp = configparser.ConfigParser()
        cp.read_string("[DEFAULT]\n" + open(oem_txt_path).read())

        def _get(key, fallback=""):
            val = cp.get("DEFAULT", key, fallback=fallback)
            if val and val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            return val

        oem_id = _get("OEM_ID", "generic")
        name = _get("OEM_NAME", oem_id)

        return {
            "name": name,
            "id": oem_id,
            "company_url": _get("OEM_COMPANY_URL"),
            "forum_url": "",
            "wiki_url": _get("OEM_COMPANY_URL"),
            "tips_url": "",
            "firmware_base_url": "",
            "ip_check_urls": [],
            "ipv6_check_url": "",
            "colors": DEFAULT_OEM["colors"],
            "images": {}
        }
    except Exception as e:
        logger.warning(f"Failed to read oem.txt: {e}")
        return dict(DEFAULT_OEM)


def get_oem_name():
    """Get OEM display name. Checks device-tree first for hw detection."""
    try:
        board = open("/proc/device-tree/compatible", "r").read().strip()
        if "photonicat2" in board:
            # Check if boot1 overrides the name
            oem = get_oem()
            if oem.get("name") and oem["name"] != DEFAULT_OEM["name"]:
                return oem["name"]
            return "Photonicat 2"
    except (FileNotFoundError, PermissionError):
        pass
    oem = get_oem()
    return oem.get("name", DEFAULT_OEM["name"])


def get_oem_image(key):
    """Get a base64-encoded OEM image by key.
    Returns (data_bytes, mime_type) or (None, None) if not found."""
    oem = get_oem()
    images = oem.get("images", {})
    b64_data = images.get(key)
    if not b64_data:
        return (None, None)

    # Detect mime type from key name
    if key.endswith("_svg"):
        mime = "image/svg+xml"
    elif key.endswith("_png"):
        mime = "image/png"
    elif key.endswith("_ico"):
        mime = "image/x-icon"
    elif key.endswith("_jpg") or key.endswith("_jpeg"):
        mime = "image/jpeg"
    else:
        mime = "application/octet-stream"

    try:
        data = base64.b64decode(b64_data)
        return (data, mime)
    except Exception:
        return (None, None)


def get_logo_uri():
    """Get logo URI - serves from /oem/image/ if available, else static fallback."""
    oem = get_oem()
    images = oem.get("images", {})
    if "logo_svg" in images:
        return "/oem/image/logo_svg"
    if "logo_png" in images:
        return "/oem/image/logo_png"
    # Legacy file-based overrides
    if os.path.exists("static/oem-logo.png"):
        return "/static/oem-logo.png"
    elif os.path.exists("static/oem-logo.svg"):
        return "/static/oem-logo.svg"
    return "/static/logo.svg"


def get_text_logo_uri():
    """Get text logo URI."""
    oem = get_oem()
    images = oem.get("images", {})
    if "logo_text_png" in images:
        return "/oem/image/logo_text_png"
    if os.path.exists("static/oem-logo-text.png"):
        return "/static/oem-logo-text.png"
    return "/static/logo_text.png"


def get_favicon_uri(size):
    """Get favicon URI for a given size (16, 32, 'ico', 'apple')."""
    oem = get_oem()
    images = oem.get("images", {})
    key_map = {
        16: "favicon_16_png",
        32: "favicon_32_png",
        "ico": "favicon_ico",
        "apple": "apple_touch_icon_png"
    }
    key = key_map.get(size)
    if key and key in images:
        return f"/oem/image/{key}"
    # Fallback to static files
    fallback_map = {
        16: "/static/favicon-16x16.png",
        32: "/static/favicon-32x32.png",
        "ico": "/static/favicon.ico",
        "apple": "/static/apple-touch-icon.png"
    }
    return fallback_map.get(size, "/static/favicon.ico")


def get_vendor_wiki_url():
    oem = get_oem()
    return oem.get("wiki_url") or oem.get("company_url") or ""


def get_vendor_site_domain():
    oem = get_oem()
    url = oem.get("company_url", "")
    if url.startswith("https://") or url.startswith("http://"):
        url = url.split("://")[1]
    return url


def get_forum_url():
    oem = get_oem()
    return oem.get("forum_url", "")


def get_tips_url():
    oem = get_oem()
    return oem.get("tips_url", "")


def get_ip_check_urls():
    oem = get_oem()
    return oem.get("ip_check_urls", [])


def get_ipv6_check_url():
    oem = get_oem()
    return oem.get("ipv6_check_url", "")


def get_firmware_base_url():
    oem = get_oem()
    return oem.get("firmware_base_url", "")
