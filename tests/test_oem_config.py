#!/usr/bin/env python3
"""Unit tests for app/oem_config.py -- the boot-partition OEM branding reader.

Why this matters: every page render resolves its name, logo, colors and vendor
links through this module, and the serial-number check it performs is the only
thing standing between a genuine factory signature and a spoofed one. A parse
regression bricks the whole UI's branding; a verification regression shows
"verified" for serials the factory never signed.

The boot1 block device is redirected to temp files -- nothing here reads
/dev, /etc or /proc. Ed25519 is exercised with the RFC 8032 test vector and
with fresh keypairs generated in-test (the factory private key never exists
here).

Run: python3 -m unittest tests.test_oem_config  (or via tests/run_tests.py)
"""

import base64
import builtins
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

import oem_config  # noqa: E402  -- stdlib-only imports, safe under run_tests.py

# RFC 8032 section 7.1, TEST 1: empty message.
RFC8032_PUB = bytes.fromhex(
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
RFC8032_SIG = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb882"
    "1590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
RFC8032_MSG = b""


def _make_signed_identity(emmc_sn="EMMC-0123", machine_sn="PCAT2-000042"):
    """Generate a throwaway Ed25519 keypair and sign 'emmc|machine' exactly
    the way the factory signing server does. Returns (pub_b64, sig_b64)."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    priv = ed25519.Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    sig = priv.sign(f"{emmc_sn}|{machine_sn}".encode("utf-8"))
    return (base64.b64encode(pub_raw).decode(),
            base64.b64encode(sig).decode())


def _fake_dt_open(compatible_text):
    """A builtins.open replacement that serves /proc/device-tree/compatible
    from memory and passes everything else to the real open."""
    real_open = builtins.open

    def opener(path, *args, **kwargs):
        if path == "/proc/device-tree/compatible":
            if isinstance(compatible_text, Exception):
                raise compatible_text
            return io.StringIO(compatible_text)
        return real_open(path, *args, **kwargs)

    return opener


class OemConfigTestBase(unittest.TestCase):
    """Redirect boot1 to a temp file, chdir into the temp dir (oem.txt and
    static/ overrides are cwd-relative) and reset the module-level cache so
    no test -- or other suite member -- sees another test's state."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.boot1 = os.path.join(self.tmpdir.name, "boot1.img")
        self._orig_boot1 = oem_config.BOOT1_PATH
        self._orig_cache = oem_config._boot1_cache
        self._orig_cwd = os.getcwd()
        oem_config.BOOT1_PATH = self.boot1
        oem_config._boot1_cache = None
        os.chdir(self.tmpdir.name)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        oem_config.BOOT1_PATH = self._orig_boot1
        oem_config._boot1_cache = self._orig_cache
        self.tmpdir.cleanup()

    def write_boot1(self, data, raw=None):
        """Write boot1 as the factory does: JSON + NUL padding."""
        blob = raw if raw is not None else (
            json.dumps(data).encode("utf-8") + b"\0" * 64)
        with open(self.boot1, "wb") as f:
            f.write(blob)


# ─── boot1 parsing ───────────────────────────────────────────────────────────

class ReadBoot1Tests(OemConfigTestBase):
    """Raw partition parsing. Garbage flash content must degrade to defaults,
    never crash the web service at startup."""

    def test_valid_json_with_nul_padding_parses(self):
        """The factory image is JSON followed by NUL fill to the partition
        end; the padding must be trimmed before json.loads."""
        self.write_boot1({"machine_sn": "PCAT2-1", "oem": {"name": "Acme"}})
        self.assertEqual(oem_config.get_boot1_data()["machine_sn"], "PCAT2-1")

    def test_unpadded_json_parses(self):
        """An exactly-sized image with no NUL terminator still parses."""
        self.write_boot1(None, raw=b'{"machine_sn": "PCAT2-2"}')
        self.assertEqual(oem_config.get_boot1_data()["machine_sn"], "PCAT2-2")

    def test_missing_partition_reads_as_empty(self):
        """Dev boards without the eMMC boot partition boot with defaults."""
        self.assertIsNone(oem_config._read_boot1_raw())
        self.assertEqual(oem_config.get_boot1_data(), {})

    def test_blank_partition_reads_as_empty(self):
        """A factory-erased partition (all NULs) is 'no data', not an error."""
        self.write_boot1(None, raw=b"\0" * 128)
        self.assertEqual(oem_config.get_boot1_data(), {})
        oem_config._boot1_cache = None
        self.write_boot1(None, raw=b"")
        self.assertEqual(oem_config.get_boot1_data(), {})

    def test_corrupt_json_logs_and_reads_as_empty(self):
        """Half-written flash must not take down the UI -- it logs a warning
        and the device runs with default branding."""
        self.write_boot1(None, raw=b'{"machine_sn": "PC\0\0\0')
        with self.assertLogs("oem_config", level="WARNING"):
            self.assertIsNone(oem_config._read_boot1_raw())
        self.assertEqual(oem_config.get_boot1_data(), {})

    def test_undecodable_bytes_log_and_read_as_empty(self):
        """Random binary in the partition (no valid UTF-8) degrades the same
        way instead of raising UnicodeDecodeError into every caller."""
        self.write_boot1(None, raw=b"\xff\xfe\xfa junk")
        with self.assertLogs("oem_config", level="WARNING"):
            self.assertEqual(oem_config.get_boot1_data(), {})

    def test_boot1_is_read_once_then_cached(self):
        """The block device is read a single time per process -- later config
        lookups (every page render) must not hit the eMMC again."""
        self.write_boot1({"machine_sn": "PCAT2-A"})
        self.assertEqual(oem_config.get_boot1_data()["machine_sn"], "PCAT2-A")
        self.write_boot1({"machine_sn": "PCAT2-B"})
        self.assertEqual(oem_config.get_boot1_data()["machine_sn"], "PCAT2-A",
                         "second read should come from the cache")


# ─── Ed25519 verification ────────────────────────────────────────────────────

class PureEd25519Tests(unittest.TestCase):
    """The dependency-free verifier used on devices without `cryptography`.
    If it drifts from RFC 8032, genuine factory serials show '(invalid)'."""

    def test_rfc8032_test_vector_verifies(self):
        """The canonical RFC 8032 TEST-1 signature must verify."""
        self.assertTrue(oem_config._verify_ed25519_pure(
            RFC8032_PUB, RFC8032_SIG, RFC8032_MSG))

    def test_tampered_signature_fails(self):
        """One flipped bit in the signature must flip the verdict."""
        bad = bytearray(RFC8032_SIG)
        bad[0] ^= 0x01
        self.assertFalse(oem_config._verify_ed25519_pure(
            RFC8032_PUB, bytes(bad), RFC8032_MSG))

    def test_tampered_message_fails(self):
        """A signature over one message must not validate another."""
        self.assertFalse(oem_config._verify_ed25519_pure(
            RFC8032_PUB, RFC8032_SIG, b"x"))

    def test_wrong_lengths_are_rejected(self):
        """Truncated key or signature material fails fast, no math attempted."""
        self.assertFalse(oem_config._verify_ed25519_pure(
            RFC8032_PUB[:31], RFC8032_SIG, RFC8032_MSG))
        self.assertFalse(oem_config._verify_ed25519_pure(
            RFC8032_PUB, RFC8032_SIG[:63], RFC8032_MSG))

    def test_point_not_on_curve_raises_value_error(self):
        """An encoding that decodes to a non-curve point (y=2) is rejected at
        decode -- forged 'points' cannot enter the verification equation."""
        bad_point = (2).to_bytes(32, "little")
        with self.assertRaises(ValueError):
            oem_config._ed_decodepoint(bad_point)

    def test_pure_verifier_agrees_with_cryptography_backend(self):
        """Both verifier implementations must agree on a fresh signature, or
        the same device would report a different serial validity depending on
        whether `cryptography` happens to be installed."""
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives import serialization
        priv = ed25519.Ed25519PrivateKey.generate()
        pub = priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        msg = b"EMMC-1|PCAT2-1"
        sig = priv.sign(msg)
        self.assertTrue(oem_config._verify_ed25519_pure(pub, sig, msg))
        self.assertTrue(oem_config._verify_ed25519_cryptography(pub, sig, msg))


class VerifySnSignatureTests(unittest.TestCase):
    """The public verify entry point: it must never raise, and must bind the
    signature to the emmc_sn|machine_sn pair (no serial transplants)."""

    def test_valid_factory_signature_verifies(self):
        """A signature made with the manufacturer key over 'emmc|machine'
        reports the serial as genuine."""
        pub_b64, sig_b64 = _make_signed_identity("EMMC-0123", "PCAT2-000042")
        with patch.object(oem_config, "MANUFACTURER_PUBLIC_KEY_B64", pub_b64):
            self.assertTrue(oem_config.verify_sn_signature(
                "EMMC-0123", "PCAT2-000042", sig_b64))

    def test_signature_is_bound_to_both_serials(self):
        """Moving a signed machine_sn to a different eMMC (cloned config)
        must fail: the eMMC serial is part of the signed message."""
        pub_b64, sig_b64 = _make_signed_identity("EMMC-0123", "PCAT2-000042")
        with patch.object(oem_config, "MANUFACTURER_PUBLIC_KEY_B64", pub_b64):
            self.assertFalse(oem_config.verify_sn_signature(
                "EMMC-9999", "PCAT2-000042", sig_b64))
            self.assertFalse(oem_config.verify_sn_signature(
                "EMMC-0123", "PCAT2-999999", sig_b64))

    def test_pure_python_fallback_path_verifies(self):
        """On the device (no `cryptography` package) the pure verifier must
        accept the very same factory signature."""
        pub_b64, sig_b64 = _make_signed_identity()
        with patch.object(oem_config, "MANUFACTURER_PUBLIC_KEY_B64", pub_b64), \
             patch.object(oem_config, "_verify_ed25519_cryptography",
                          side_effect=ImportError("no cryptography")):
            self.assertTrue(oem_config.verify_sn_signature(
                "EMMC-0123", "PCAT2-000042", sig_b64))
            self.assertFalse(oem_config.verify_sn_signature(
                "EMMC-0123", "PCAT2-other", sig_b64))

    def test_garbage_signature_material_returns_false_not_exception(self):
        """Corrupt base64, undersized blobs, or non-curve points from a bad
        flash must read as 'invalid', never crash the page handler."""
        self.assertFalse(oem_config.verify_sn_signature(
            "E", "M", "!!!not-base64!!!"))
        self.assertFalse(oem_config.verify_sn_signature(
            "E", "M", base64.b64encode(b"short").decode()))
        # 64 bytes whose R encoding (y=2) is not on the curve.
        bad_sig = base64.b64encode(
            (2).to_bytes(32, "little") + bytes(32)).decode()
        with patch.object(oem_config, "_verify_ed25519_cryptography",
                          side_effect=ImportError("no cryptography")):
            self.assertFalse(oem_config.verify_sn_signature("E", "M", bad_sig))


class MachineSnTests(OemConfigTestBase):
    """get_machine_sn feeds the device-info page's serial + verified badge."""

    def test_no_partition_reports_dev_unknown(self):
        """A dev board without boot1 shows DEV-UNKNOWN, unverified."""
        self.assertEqual(oem_config.get_machine_sn(), ("DEV-UNKNOWN", False))

    def test_partition_without_serial_reports_na(self):
        """A present-but-unprovisioned partition shows N/A, unverified --
        distinct from the missing-partition case so the factory can tell
        'never flashed' from 'flashed empty'."""
        self.write_boot1({"oem": {"name": "Acme"}})
        self.assertEqual(oem_config.get_machine_sn(), ("N/A", False))

    def test_serial_without_signature_is_unverified(self):
        """A serial with no signature material shows but never claims
        verification."""
        self.write_boot1({"machine_sn": "PCAT2-7"})
        self.assertEqual(oem_config.get_machine_sn(), ("PCAT2-7", False))
        oem_config._boot1_cache = None
        self.write_boot1({"machine_sn": "PCAT2-7", "emmc_sn": "E7"})
        self.assertEqual(oem_config.get_machine_sn(), ("PCAT2-7", False))

    def test_fully_signed_serial_reports_verified(self):
        """With emmc_sn + signature present, the badge reflects the actual
        cryptographic verdict."""
        pub_b64, sig_b64 = _make_signed_identity("E42", "PCAT2-42")
        self.write_boot1({"machine_sn": "PCAT2-42", "emmc_sn": "E42",
                          "signature": sig_b64})
        with patch.object(oem_config, "MANUFACTURER_PUBLIC_KEY_B64", pub_b64):
            self.assertEqual(oem_config.get_machine_sn(), ("PCAT2-42", True))


# ─── OEM branding resolution ─────────────────────────────────────────────────

class GetOemTests(OemConfigTestBase):
    """Merging boot1 branding over defaults. A miss here rebrands or
    un-brands every page of a customer's device."""

    def test_boot1_oem_overrides_merge_over_defaults(self):
        """OEM-set keys win; unset keys keep factory defaults so a partial
        OEM config never leaves the UI with missing colors/urls."""
        self.write_boot1({"oem": {"name": "AcmeNet", "id": "acme",
                                  "forum_url": "https://forum.acme.example"}})
        oem = oem_config.get_oem()
        self.assertEqual(oem["name"], "AcmeNet")
        self.assertEqual(oem["forum_url"], "https://forum.acme.example")
        self.assertEqual(oem["colors"], oem_config.DEFAULT_OEM["colors"])
        self.assertEqual(oem["ip_check_urls"], [])

    def test_non_dict_oem_section_falls_back(self):
        """A corrupt oem section (wrong type) behaves like no oem section."""
        self.write_boot1({"oem": "not-a-dict"})
        self.assertEqual(oem_config.get_oem()["name"],
                         oem_config.DEFAULT_OEM["name"])

    def test_no_boot1_and_no_oem_txt_yields_defaults(self):
        """Bare dev environment renders with the neutral 'Router' brand."""
        oem = oem_config.get_oem()
        self.assertEqual(oem["name"], "Router")
        self.assertEqual(oem["id"], "generic")


class OemTxtFallbackTests(OemConfigTestBase):
    """The legacy oem.txt path used before boot1 provisioning existed.
    The base class already chdirs into the temp dir, where oem.txt lives."""

    def test_oem_txt_values_and_quote_stripping(self):
        """Quoted values in oem.txt lose their quotes; the company URL doubles
        as the wiki URL exactly as the legacy format intended."""
        with open("oem.txt", "w") as f:
            f.write('OEM_ID="acme"\nOEM_NAME="Acme Router"\n'
                    'OEM_COMPANY_URL="https://acme.example"\n')
        oem = oem_config.get_oem()
        self.assertEqual(oem["id"], "acme")
        self.assertEqual(oem["name"], "Acme Router")
        self.assertEqual(oem["company_url"], "https://acme.example")
        self.assertEqual(oem["wiki_url"], "https://acme.example")

    def test_missing_name_falls_back_to_id(self):
        """OEM_NAME absent: the id doubles as display name, never blank."""
        with open("oem.txt", "w") as f:
            f.write("OEM_ID=acme\n")
        self.assertEqual(oem_config.get_oem()["name"], "acme")

    def test_malformed_oem_txt_logs_and_yields_defaults(self):
        """A broken legacy file must not crash startup."""
        with open("oem.txt", "w") as f:
            f.write("this line has no equals sign at all\n")
        with self.assertLogs("oem_config", level="WARNING"):
            self.assertEqual(oem_config.get_oem()["name"], "Router")


class OemNameTests(OemConfigTestBase):
    """Display-name resolution with hardware detection."""

    def test_photonicat2_board_without_override_shows_factory_name(self):
        """A Photonicat 2 board with generic boot1 branding still shows its
        proper product name, not 'Router'."""
        with patch("builtins.open",
                   side_effect=_fake_dt_open("rockchip,photonicat2-v1")):
            self.assertEqual(oem_config.get_oem_name(), "Photonicat 2")

    def test_photonicat2_board_with_oem_override_shows_oem_name(self):
        """An OEM rebrand on Photonicat 2 hardware wins over the factory
        name -- the customer's brand is what their users must see."""
        self.write_boot1({"oem": {"name": "AcmeNet"}})
        with patch("builtins.open",
                   side_effect=_fake_dt_open("rockchip,photonicat2-v1")):
            self.assertEqual(oem_config.get_oem_name(), "AcmeNet")

    def test_photonicat1_board_without_override_shows_factory_name(self):
        """The rk3568 Photonicat 1 ("ariaboard,photonicat") is Photonicat
        hardware too: it shows its product name, not 'Router'."""
        with patch("builtins.open",
                   side_effect=_fake_dt_open("ariaboard,photonicat\0rockchip,rk3568")):
            self.assertEqual(oem_config.get_oem_name(), "Photonicat")

    def test_photonicat1_board_with_oem_override_shows_oem_name(self):
        self.write_boot1({"oem": {"name": "AcmeNet"}})
        with patch("builtins.open",
                   side_effect=_fake_dt_open("ariaboard,photonicat\0rockchip,rk3568")):
            self.assertEqual(oem_config.get_oem_name(), "AcmeNet")

    def test_other_board_uses_oem_name(self):
        """Non-photonicat hardware falls through to the OEM/default name."""
        self.write_boot1({"oem": {"name": "OtherBrand"}})
        with patch("builtins.open",
                   side_effect=_fake_dt_open("some,other-board")):
            self.assertEqual(oem_config.get_oem_name(), "OtherBrand")

    def test_unreadable_device_tree_uses_oem_name(self):
        """x86 dev hosts (no device-tree) still get a name, no exception."""
        self.write_boot1({"oem": {"name": "DevBrand"}})
        with patch("builtins.open",
                   side_effect=_fake_dt_open(PermissionError("denied"))):
            self.assertEqual(oem_config.get_oem_name(), "DevBrand")


# ─── OEM images and URIs ─────────────────────────────────────────────────────

class OemImageTests(OemConfigTestBase):
    """Serving base64 images from boot1: mime typing and decode safety."""

    def _boot1_with_images(self, images):
        self.write_boot1({"oem": {"images": images}})

    def test_mime_type_follows_key_suffix(self):
        """Each suffix maps to the right Content-Type; the browser only
        renders the logo if this is correct."""
        payload = base64.b64encode(b"imagebytes").decode()
        cases = {"logo_svg": "image/svg+xml",
                 "logo_png": "image/png",
                 "favicon_ico": "image/x-icon",
                 "photo_jpg": "image/jpeg",
                 "photo_jpeg": "image/jpeg",
                 "mystery_bin": "application/octet-stream"}
        self._boot1_with_images({k: payload for k in cases})
        for key, mime in cases.items():
            data, got_mime = oem_config.get_oem_image(key)
            self.assertEqual(data, b"imagebytes", key)
            self.assertEqual(got_mime, mime, key)

    def test_missing_key_returns_none_pair(self):
        """Absent images 404 cleanly via (None, None)."""
        self._boot1_with_images({})
        self.assertEqual(oem_config.get_oem_image("logo_svg"), (None, None))

    def test_corrupt_base64_returns_none_pair(self):
        """Un-decodable image data must not raise into the image route."""
        self._boot1_with_images({"logo_png": "!!!corrupt!!!"})
        self.assertEqual(oem_config.get_oem_image("logo_png"), (None, None))


class UriResolutionTests(OemConfigTestBase):
    """Logo/favicon URI precedence: boot1 images > legacy static overrides >
    stock files. Getting order wrong reverts a customer's rebrand."""

    def setUp(self):
        super().setUp()
        os.mkdir("static")  # cwd is the temp dir (base class chdir)

    def test_logo_prefers_boot1_svg_then_png(self):
        """boot1-provisioned logos always beat files on disk."""
        b64 = base64.b64encode(b"x").decode()
        self.write_boot1({"oem": {"images": {"logo_svg": b64,
                                             "logo_png": b64}}})
        self.assertEqual(oem_config.get_logo_uri(), "/oem/image/logo_svg")
        oem_config._boot1_cache = None
        self.write_boot1({"oem": {"images": {"logo_png": b64}}})
        self.assertEqual(oem_config.get_logo_uri(), "/oem/image/logo_png")

    def test_logo_falls_back_to_static_overrides_then_stock(self):
        """Legacy file-based rebrands (oem-logo.*) still work, png first."""
        open("static/oem-logo.png", "w").close()
        open("static/oem-logo.svg", "w").close()
        self.assertEqual(oem_config.get_logo_uri(), "/static/oem-logo.png")
        os.remove("static/oem-logo.png")
        self.assertEqual(oem_config.get_logo_uri(), "/static/oem-logo.svg")
        os.remove("static/oem-logo.svg")
        self.assertEqual(oem_config.get_logo_uri(), "/static/logo.svg")

    def test_text_logo_precedence(self):
        """Same precedence chain for the wordmark logo."""
        b64 = base64.b64encode(b"x").decode()
        self.write_boot1({"oem": {"images": {"logo_text_png": b64}}})
        self.assertEqual(oem_config.get_text_logo_uri(),
                         "/oem/image/logo_text_png")
        oem_config._boot1_cache = None
        self.write_boot1({})
        open("static/oem-logo-text.png", "w").close()
        self.assertEqual(oem_config.get_text_logo_uri(),
                         "/static/oem-logo-text.png")
        os.remove("static/oem-logo-text.png")
        self.assertEqual(oem_config.get_text_logo_uri(),
                         "/static/logo_text.png")

    def test_favicon_uses_boot1_when_present_else_static(self):
        """Each favicon slot serves the OEM icon when provisioned and the
        stock icon otherwise; unknown sizes get the .ico."""
        b64 = base64.b64encode(b"x").decode()
        self.write_boot1({"oem": {"images": {
            "favicon_16_png": b64, "favicon_32_png": b64,
            "favicon_ico": b64, "apple_touch_icon_png": b64}}})
        self.assertEqual(oem_config.get_favicon_uri(16),
                         "/oem/image/favicon_16_png")
        self.assertEqual(oem_config.get_favicon_uri(32),
                         "/oem/image/favicon_32_png")
        self.assertEqual(oem_config.get_favicon_uri("ico"),
                         "/oem/image/favicon_ico")
        self.assertEqual(oem_config.get_favicon_uri("apple"),
                         "/oem/image/apple_touch_icon_png")
        oem_config._boot1_cache = None
        self.write_boot1({})
        self.assertEqual(oem_config.get_favicon_uri(16),
                         "/static/favicon-16x16.png")
        self.assertEqual(oem_config.get_favicon_uri(32),
                         "/static/favicon-32x32.png")
        self.assertEqual(oem_config.get_favicon_uri("ico"),
                         "/static/favicon.ico")
        self.assertEqual(oem_config.get_favicon_uri("apple"),
                         "/static/apple-touch-icon.png")
        self.assertEqual(oem_config.get_favicon_uri("bogus"),
                         "/static/favicon.ico")


# ─── Vendor URLs ─────────────────────────────────────────────────────────────

class VendorUrlTests(OemConfigTestBase):
    """Help/wiki/forum links in the UI footer and error pages."""

    def test_wiki_url_prefers_oem_wiki_then_company(self):
        """An OEM wiki wins; without one the company site is the next-best
        help destination."""
        self.write_boot1({"oem": {"wiki_url": "https://wiki.acme.example",
                                  "company_url": "https://acme.example"}})
        self.assertEqual(oem_config.get_vendor_wiki_url(),
                         "https://wiki.acme.example")
        oem_config._boot1_cache = None
        self.write_boot1({"oem": {"company_url": "https://acme.example"}})
        self.assertEqual(oem_config.get_vendor_wiki_url(),
                         "https://acme.example")

    def test_bare_photonicat2_board_gets_factory_wiki_and_forum(self):
        """Factory hardware with no OEM override keeps its original help
        links -- users are never left with a dead 'Help' button."""
        with patch("builtins.open",
                   side_effect=_fake_dt_open("rockchip,photonicat2-v1")):
            self.assertEqual(oem_config.get_vendor_wiki_url(),
                             oem_config.PHOTONICAT_WIKI_URL)
            self.assertEqual(oem_config.get_forum_url(),
                             oem_config.PHOTONICAT_FORUM_URL)

    def test_bare_photonicat1_board_gets_factory_wiki_and_forum(self):
        """Same brand, same help links on the first-generation board."""
        with patch("builtins.open",
                   side_effect=_fake_dt_open("ariaboard,photonicat\0rockchip,rk3568")):
            self.assertEqual(oem_config.get_vendor_wiki_url(),
                             oem_config.PHOTONICAT_WIKI_URL)
            self.assertEqual(oem_config.get_forum_url(),
                             oem_config.PHOTONICAT_FORUM_URL)

    def test_bare_generic_board_gets_empty_links(self):
        """Unbranded generic hardware shows no vendor links at all."""
        with patch("builtins.open",
                   side_effect=_fake_dt_open(FileNotFoundError("no dt"))):
            self.assertEqual(oem_config.get_vendor_wiki_url(), "")
            self.assertEqual(oem_config.get_forum_url(), "")

    def test_oem_forum_url_wins_over_factory(self):
        """An OEM forum replaces the factory forum even on factory hardware."""
        self.write_boot1({"oem": {"forum_url": "https://f.acme.example"}})
        with patch("builtins.open",
                   side_effect=_fake_dt_open("rockchip,photonicat2-v1")):
            self.assertEqual(oem_config.get_forum_url(),
                             "https://f.acme.example")

    def test_site_domain_strips_scheme_only(self):
        """The footer shows a bare domain whatever the configured scheme."""
        for url, expected in (("https://acme.example/x", "acme.example/x"),
                              ("http://acme.example", "acme.example"),
                              ("acme.example", "acme.example"),
                              ("", "")):
            oem_config._boot1_cache = None
            self.write_boot1({"oem": {"company_url": url}})
            self.assertEqual(oem_config.get_vendor_site_domain(), expected)

    def test_simple_url_getters_read_oem_values_and_defaults(self):
        """tips/ip-check/ipv6/firmware URLs come straight from the OEM config
        with safe empty defaults."""
        self.write_boot1({"oem": {
            "tips_url": "https://tips.example",
            "ip_check_urls": ["https://ip1.example", "https://ip2.example"],
            "ipv6_check_url": "https://v6.example",
            "firmware_base_url": "https://fw.example"}})
        self.assertEqual(oem_config.get_tips_url(), "https://tips.example")
        self.assertEqual(oem_config.get_ip_check_urls(),
                         ["https://ip1.example", "https://ip2.example"])
        self.assertEqual(oem_config.get_ipv6_check_url(), "https://v6.example")
        self.assertEqual(oem_config.get_firmware_base_url(),
                         "https://fw.example")
        oem_config._boot1_cache = None
        self.write_boot1({})
        self.assertEqual(oem_config.get_tips_url(), "")
        self.assertEqual(oem_config.get_ip_check_urls(), [])
        self.assertEqual(oem_config.get_ipv6_check_url(), "")
        self.assertEqual(oem_config.get_firmware_base_url(), "")



class DegradedFlashTest(OemConfigTestBase):
    """Regression: an EIO reading the boot1 partition (degraded eMMC -- the
    file exists, the read fails) used to escape the except clause and turn
    every page render into a 500 via get_oem_name(). It must fall back to
    default branding instead."""

    def test_eio_during_read_falls_back_to_none(self):
        self.write_boot1({"oem_name": "x"})
        real_open = open

        def eio_open(path, *a, **kw):
            if str(path) == self.boot1:
                raise OSError(5, "Input/output error")
            return real_open(path, *a, **kw)

        with patch("builtins.open", side_effect=eio_open):
            self.assertIsNone(oem_config._read_boot1_raw())


if __name__ == "__main__":
    unittest.main()
