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
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        # OSError (which subsumes the old FileNotFoundError/PermissionError)
        # also catches EIO from a degraded eMMC: os.path.exists() succeeds but
        # the read itself fails. Letting that escape turned every page render
        # into a 500 via get_oem_name() instead of falling back to defaults.
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


def _verify_ed25519_cryptography(pub_bytes, sig_bytes, message):
    """Verify using the `cryptography` package. Raises ImportError if absent."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    public_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
    public_key.verify(sig_bytes, message)  # raises on bad signature
    return True


# --- Pure-Python Ed25519 verification (RFC 8032) ---------------------------
# The target OpenWrt devices ship Python without the `cryptography` module, so
# the C-backed path above is unavailable there and every serial fell back to
# "(invalid)". This dependency-free verifier (hashlib only) is the fallback.
# Uses extended twisted-Edwards coordinates so a full verify is ~0.15s on the
# embedded ARM core, and the result is cached upstream.
_ED_Q = 2**255 - 19


def _ed_inv(x):
    return pow(x, _ED_Q - 2, _ED_Q)


_ED_D = (-121665 * _ed_inv(121666)) % _ED_Q
_ED_D2 = (2 * _ED_D) % _ED_Q
_ED_I = pow(2, (_ED_Q - 1) // 4, _ED_Q)


def _ed_xrecover(y):
    xx = (y * y - 1) * _ed_inv(_ED_D * y * y + 1)
    x = pow(xx, (_ED_Q + 3) // 8, _ED_Q)
    if (x * x - xx) % _ED_Q != 0:
        x = (x * _ED_I) % _ED_Q
    if x % 2 != 0:
        x = _ED_Q - x
    return x


_ED_BY = (4 * _ed_inv(5)) % _ED_Q
_ED_BX = _ed_xrecover(_ED_BY)


def _ed_point(x, y):
    return (x % _ED_Q, y % _ED_Q, 1, (x * y) % _ED_Q)


_ED_B = _ed_point(_ED_BX, _ED_BY)


def _ed_add(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = ((y1 - x1) * (y2 - x2)) % _ED_Q
    b = ((y1 + x1) * (y2 + x2)) % _ED_Q
    c = (t1 * _ED_D2 * t2) % _ED_Q
    dd = (2 * z1 * z2) % _ED_Q
    e = (b - a) % _ED_Q
    f = (dd - c) % _ED_Q
    g = (dd + c) % _ED_Q
    h = (b + a) % _ED_Q
    return ((e * f) % _ED_Q, (g * h) % _ED_Q, (f * g) % _ED_Q, (e * h) % _ED_Q)


def _ed_scalarmult(P, e):
    Q = (0, 1, 1, 0)
    while e > 0:
        if e & 1:
            Q = _ed_add(Q, P)
        P = _ed_add(P, P)
        e >>= 1
    return Q


def _ed_isoncurve(x, y):
    return (-x * x + y * y - 1 - _ED_D * x * x * y * y) % _ED_Q == 0


def _ed_decodepoint(s):
    i = int.from_bytes(s, "little")
    y = i & ((1 << 255) - 1)
    x = _ed_xrecover(y)
    if (x & 1) != ((i >> 255) & 1):
        x = _ED_Q - x
    if not _ed_isoncurve(x, y):
        raise ValueError("point not on curve")
    return _ed_point(x, y)


def _ed_encodepoint(P):
    x, y, z, _t = P
    zi = _ed_inv(z)
    ax = (x * zi) % _ED_Q
    ay = (y * zi) % _ED_Q
    return (ay | ((ax & 1) << 255)).to_bytes(32, "little")


def _ed_equal(P, Q):
    x1, y1, z1, _ = P
    x2, y2, z2, _ = Q
    return (x1 * z2 - x2 * z1) % _ED_Q == 0 and (y1 * z2 - y2 * z1) % _ED_Q == 0


def _verify_ed25519_pure(pub_bytes, sig_bytes, message):
    """Dependency-free Ed25519 verify. Returns True/False."""
    import hashlib
    if len(sig_bytes) != 64 or len(pub_bytes) != 32:
        return False
    R = _ed_decodepoint(sig_bytes[:32])
    A = _ed_decodepoint(pub_bytes)
    S = int.from_bytes(sig_bytes[32:], "little")
    digest = hashlib.sha512(_ed_encodepoint(R) + pub_bytes + message).digest()
    h = int.from_bytes(digest, "little")
    return _ed_equal(_ed_scalarmult(_ED_B, S), _ed_add(R, _ed_scalarmult(A, h)))


def verify_sn_signature(emmc_sn, machine_sn, signature_b64):
    """Verify the Ed25519 signature over 'emmc_sn|machine_sn'.
    Returns True if valid, False otherwise."""
    try:
        pub_bytes = base64.b64decode(MANUFACTURER_PUBLIC_KEY_B64)
        sig_bytes = base64.b64decode(signature_b64)
        message = f"{emmc_sn}|{machine_sn}".encode("utf-8")
        try:
            return _verify_ed25519_cryptography(pub_bytes, sig_bytes, message)
        except ImportError:
            # No `cryptography` on the device — use the pure-Python fallback.
            return _verify_ed25519_pure(pub_bytes, sig_bytes, message)
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
    """Get OEM display name. Checks device-tree first for hw detection.

    Both Photonicat generations carry the brand in their compatible string
    ("ariaboard,photonicat" for the rk3568 Photonicat 1, "...photonicat2" for
    the rk3576 Photonicat 2). Without a boot1 rebrand they show their own
    product name rather than the brand-neutral "Router" default.
    """
    board = _board_compatible()
    if "photonicat" in board:
        # Check if boot1 overrides the name
        oem = get_oem()
        if oem.get("name") and oem["name"] != DEFAULT_OEM["name"]:
            return oem["name"]
        return "Photonicat 2" if "photonicat2" in board else "Photonicat"
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


# Factory defaults for the Photonicat brand (both generations). Used when
# running on a photonicat board with no boot1 OEM override, so the original factory wiki /
# forum links stay present even without an OEM config (the public DEFAULT_OEM
# is intentionally brand-neutral). Mirrors get_oem_name()'s board detection.
PHOTONICAT_WIKI_URL = "https://photonicat.com/wiki/Main_Page"
PHOTONICAT_FORUM_URL = "https://photonicat.com/forums"


def _board_compatible():
    """The device-tree compatible string, or "" off-device / unreadable."""
    try:
        return open("/proc/device-tree/compatible", "r").read().strip()
    except (FileNotFoundError, PermissionError):
        return ""


def _is_photonicat2_board():
    return "photonicat2" in _board_compatible()


def _is_photonicat_board():
    """Either Photonicat generation: the factory wiki/forum links apply to
    both, only the product name differs."""
    return "photonicat" in _board_compatible()


def get_vendor_wiki_url():
    oem = get_oem()
    url = oem.get("wiki_url") or oem.get("company_url") or ""
    if not url and _is_photonicat_board():
        return PHOTONICAT_WIKI_URL
    return url


def get_vendor_site_domain():
    oem = get_oem()
    url = oem.get("company_url", "")
    if url.startswith("https://") or url.startswith("http://"):
        url = url.split("://")[1]
    return url


def get_forum_url():
    oem = get_oem()
    url = oem.get("forum_url", "")
    if not url and _is_photonicat_board():
        return PHOTONICAT_FORUM_URL
    return url


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
