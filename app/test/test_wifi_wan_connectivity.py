#!/usr/bin/env python3
"""Unit tests for Smart WAN captive-portal / connectivity detection.

Covers the probe classification, the retry bias toward 'online', and the
flap-damping in _refresh_connectivity that stops a single bad probe from
bouncing egress between WiFi and cellular.

Runs anywhere: curl is mocked, no device / network needed.

    cd app && python3 -m unittest test.test_wifi_wan_connectivity -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pcat_util import wifi_wan


HTTPS = wifi_wan._CONN_CHECK_URL_HTTPS
HTTP = wifi_wan._CONN_CHECK_URL


def fake_curl(responses, hardened=None):
    """Build a _curl_probe replacement.

    `responses` maps a URL to either a single (code, body[, redirect_url])
    tuple or a list of them consumed one per call (to simulate a flaky link
    across retries); 2-tuples are padded with an empty redirect_url. Unmapped
    URLs return ('000', '', ''). `hardened`, when given, is a separate
    response map answering the transparent-proxy-escaping form (bypass_gid
    set); without it the hardened form answers from `responses` too. Every
    call is recorded on `.calls` as (url, doh_url, bypass_gid)."""
    def build(m):
        return {url: list(v) if isinstance(v, list) else [v] for url, v in m.items()}
    state = build(responses)
    hard_state = build(hardened) if hardened is not None else state

    def _probe(url, l3dev, doh_url=None, bypass_gid=None):
        _probe.calls.append((url, doh_url, bypass_gid))
        seq = (hard_state if bypass_gid is not None else state).get(url)
        if not seq:
            return ("000", "", "")
        r = seq.pop(0) if len(seq) > 1 else seq[0]
        return r if len(r) == 3 else r + ("",)

    _probe.calls = []
    return _probe


class _NoPoisonMixin(unittest.TestCase):
    """Stub out _fakeip_poisoned so tests stay network-free and deterministic
    (the real one calls getaddrinfo)."""
    def setUp(self):
        self._orig_poison = wifi_wan._fakeip_poisoned
        self.addCleanup(lambda: setattr(wifi_wan, "_fakeip_poisoned", self._orig_poison))
        wifi_wan._fakeip_poisoned = lambda: False


class ProbeClassificationTest(unittest.TestCase):
    def setUp(self):
        self._orig = wifi_wan._curl_probe
        self.addCleanup(lambda: setattr(wifi_wan, "_curl_probe", self._orig))
        wifi_wan._portal_seen["url"] = ""

    def _set(self, responses):
        wifi_wan._curl_probe = fake_curl(responses)

    def test_https_200_is_online(self):
        self._set({HTTPS: ("200", "203.0.113.7")})
        self.assertEqual(wifi_wan._probe_once("wlan0"), "online")

    def test_dead_uplink_is_offline(self):
        self._set({HTTPS: ("000", ""), HTTP: ("000", "")})
        self.assertEqual(wifi_wan._probe_once("wlan0"), "offline")

    def test_http_ip_echo_is_online_not_portal(self):
        # TLS glitched but plain HTTP returns our own IP echo => really online.
        self._set({HTTPS: ("000", ""), HTTP: ("200", "203.0.113.7\n")})
        self.assertEqual(wifi_wan._probe_once("wlan0"), "online")

    def test_http_redirect_is_portal(self):
        self._set({HTTPS: ("000", ""), HTTP: ("302", "")})
        self.assertEqual(wifi_wan._probe_once("wlan0"), "portal")

    def test_http_login_page_is_portal(self):
        self._set({HTTPS: ("000", ""), HTTP: ("200", "<html>Please sign in</html>")})
        self.assertEqual(wifi_wan._probe_once("wlan0"), "portal")

    def test_ip_echo_helper(self):
        self.assertTrue(wifi_wan._looks_like_ip_echo("203.0.113.7"))
        self.assertTrue(wifi_wan._looks_like_ip_echo("2001:db8::1\n"))
        self.assertFalse(wifi_wan._looks_like_ip_echo("<html>"))
        self.assertFalse(wifi_wan._looks_like_ip_echo(""))
        self.assertFalse(wifi_wan._looks_like_ip_echo("x" * 100))


class ProbeRetryTest(_NoPoisonMixin):
    def setUp(self):
        super().setUp()
        self._orig = wifi_wan._curl_probe
        self.addCleanup(lambda: setattr(wifi_wan, "_curl_probe", self._orig))

    def test_transient_failure_then_online(self):
        # First round: HTTPS fails, HTTP dead (offline). Second round: HTTPS 200.
        wifi_wan._curl_probe = fake_curl({
            HTTPS: [("000", ""), ("200", "203.0.113.7")],
            HTTP: [("000", ""), ("000", "")],
        })
        self.assertEqual(wifi_wan._probe_connectivity("wlan0"), "online")

    def test_sustained_failure_is_offline(self):
        wifi_wan._curl_probe = fake_curl({HTTPS: ("000", ""), HTTP: ("000", "")})
        self.assertEqual(wifi_wan._probe_connectivity("wlan0"), "offline")

    def test_no_l3dev_is_offline(self):
        self.assertEqual(wifi_wan._probe_connectivity(""), "offline")


class TransparentProxyEscapeTest(_NoPoisonMixin):
    """OpenClash-style hijack of router-self traffic: the plain bound probe is
    structurally dead while the gid+DoH hardened form gets through. Verified
    against real OpenClash nft rules on hardware; these tests pin the logic."""

    def setUp(self):
        super().setUp()
        self._orig = wifi_wan._curl_probe
        self.addCleanup(lambda: setattr(wifi_wan, "_curl_probe", self._orig))

    def test_fakeip_skips_straight_to_hardened(self):
        # Poisoned resolver => plain probes would only burn their timeouts, so
        # every call must be the hardened form.
        wifi_wan._fakeip_poisoned = lambda: True
        fake = fake_curl({}, hardened={HTTPS: ("200", "203.0.113.7")})
        wifi_wan._curl_probe = fake
        self.assertEqual(wifi_wan._probe_connectivity("eth0"), "online")
        self.assertTrue(fake.calls)
        self.assertTrue(all(gid is not None for _, _, gid in fake.calls))

    def test_non_fakeip_proxy_rescued_by_arbitration(self):
        # Proxy modes without fake-ip still eat bound probes; the single
        # hardened arbitration round must rescue the verdict.
        fake = fake_curl({HTTPS: ("000", ""), HTTP: ("000", "")},
                         hardened={HTTPS: ("200", "203.0.113.7")})
        wifi_wan._curl_probe = fake
        self.assertEqual(wifi_wan._probe_connectivity("eth0"), "online")

    def test_truly_dead_uplink_still_offline(self):
        # Hardened form failing too must not invent connectivity.
        wifi_wan._fakeip_poisoned = lambda: True
        fake = fake_curl({}, hardened={HTTPS: ("000", ""), HTTP: ("000", "")})
        wifi_wan._curl_probe = fake
        self.assertEqual(wifi_wan._probe_connectivity("eth0"), "offline")

    def test_hardened_rounds_rotate_doh_resolvers(self):
        wifi_wan._fakeip_poisoned = lambda: True
        fake = fake_curl({}, hardened={HTTPS: ("000", ""), HTTP: ("000", "")})
        wifi_wan._curl_probe = fake
        wifi_wan._probe_connectivity("eth0")
        dohs = {doh for _, doh, _ in fake.calls}
        expect = min(wifi_wan._CONN_CHECK_ATTEMPTS, len(wifi_wan._DOH_URLS))
        self.assertEqual(len(dohs), expect)


class FlapDampingTest(_NoPoisonMixin):
    def setUp(self):
        super().setUp()
        self._orig = wifi_wan._curl_probe
        self.addCleanup(lambda: setattr(wifi_wan, "_curl_probe", self._orig))
        # Reset the shared caches to a known good baseline before each test.
        wifi_wan._conn_cache.update({"result": "unknown", "time": 0.0, "bad_streak": 0})
        wifi_wan._portal_seen["url"] = ""

    def _online(self):
        wifi_wan._curl_probe = fake_curl({HTTPS: ("200", "203.0.113.7")})

    def _offline(self):
        wifi_wan._curl_probe = fake_curl({HTTPS: ("000", ""), HTTP: ("000", "")})

    def _portal(self):
        wifi_wan._curl_probe = fake_curl({HTTPS: ("000", ""), HTTP: ("302", "")})

    def test_single_bad_probe_holds_online(self):
        self._online()
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "online")
        # One bad round must NOT demote — this is the anti-flap guarantee.
        self._offline()
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "online")
        self.assertEqual(wifi_wan._conn_cache["bad_streak"], 1)

    def test_sustained_bad_probes_demote(self):
        self._online()
        wifi_wan._refresh_connectivity("wlan0")
        self._offline()
        wifi_wan._refresh_connectivity("wlan0")                       # held (streak 1)
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "offline")  # streak 2 -> demote

    def test_recovery_is_immediate(self):
        self._online()
        wifi_wan._refresh_connectivity("wlan0")
        self._offline()
        wifi_wan._refresh_connectivity("wlan0")
        wifi_wan._refresh_connectivity("wlan0")  # now offline
        self.assertEqual(wifi_wan._conn_cache["result"], "offline")
        # A single good probe restores online and clears the streak.
        self._online()
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "online")
        self.assertEqual(wifi_wan._conn_cache["bad_streak"], 0)

    def test_portal_passes_through_after_threshold(self):
        self._online()
        wifi_wan._refresh_connectivity("wlan0")
        self._portal()
        wifi_wan._refresh_connectivity("wlan0")                      # held online (streak 1)
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "portal")  # streak 2 -> portal

    def test_boot_first_offline_probe_held_unknown(self):
        # Boot / fresh-join grace: the very first probe often races DHCP/DNS
        # settling. From 'unknown', a single failed round must NOT report
        # offline (which would demote wifi under cellular and flip egress to
        # 5G for a tick right after startup).
        self._offline()
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "unknown")
        self.assertEqual(wifi_wan._conn_cache["bad_streak"], 1)
        # Sustained failure is still caught on the next round.
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "offline")

    def test_boot_portal_passes_through_immediately(self):
        # A portal verdict is a positive HTTP fingerprint, not a timeout:
        # no confirmation round needed even from 'unknown', so captive-portal
        # failover/login stays prompt at boot.
        self._portal()
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "portal")

    def test_boot_online_clears_grace(self):
        self._offline()
        wifi_wan._refresh_connectivity("wlan0")  # held unknown (streak 1)
        self._online()
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "online")
        self.assertEqual(wifi_wan._conn_cache["bad_streak"], 0)


PORTAL_URL = "http://gcppty.gw/login?dst=http%3A%2F%2Fbbb.com%2F"


class PortalUrlCaptureTest(unittest.TestCase):
    """The probe must remember where a captive portal points so the access
    pin can hold DNS + routing open for exactly that login chain."""

    def setUp(self):
        self._orig = wifi_wan._curl_probe
        self.addCleanup(lambda: setattr(wifi_wan, "_curl_probe", self._orig))
        wifi_wan._portal_seen["url"] = ""
        self.addCleanup(lambda: wifi_wan._portal_seen.update({"url": ""}))

    def test_redirect_location_is_captured(self):
        wifi_wan._curl_probe = fake_curl({
            HTTPS: ("000", ""), HTTP: ("302", "", PORTAL_URL)})
        self.assertEqual(wifi_wan._probe_once("wlan0"), "portal")
        self.assertEqual(wifi_wan._portal_seen["url"], PORTAL_URL)

    def test_meta_refresh_in_body_is_captured(self):
        body = ('<html><head><meta http-equiv="refresh" '
                'content="0; url=http://gcppty.gw/login"></head></html>')
        wifi_wan._curl_probe = fake_curl({
            HTTPS: ("000", ""), HTTP: ("200", body)})
        self.assertEqual(wifi_wan._probe_once("wlan0"), "portal")
        self.assertEqual(wifi_wan._portal_seen["url"], "http://gcppty.gw/login")

    def test_js_redirect_in_body_is_captured(self):
        body = '<script>window.location.href = "http://10.0.0.1/auth";</script>'
        wifi_wan._curl_probe = fake_curl({
            HTTPS: ("000", ""), HTTP: ("200", body)})
        wifi_wan._probe_once("wlan0")
        self.assertEqual(wifi_wan._portal_seen["url"], "http://10.0.0.1/auth")

    def test_relative_meta_refresh_stays_on_check_host_and_is_dropped(self):
        # A relative refresh resolves against the probe URL — our own check
        # host — and must be dropped: pinning 4.photonicat.com to the
        # hotspot's DNS would poison the online probe itself.
        body = '<meta http-equiv=refresh content="0;url=/portal/login">'
        wifi_wan._curl_probe = fake_curl({
            HTTPS: ("000", ""), HTTP: ("200", body)})
        self.assertEqual(wifi_wan._probe_once("wlan0"), "portal")
        self.assertEqual(wifi_wan._portal_seen["url"], "")

    def test_own_check_host_is_never_a_portal_target(self):
        wifi_wan._curl_probe = fake_curl({
            HTTPS: ("000", ""),
            HTTP: ("302", "", "http://4.photonicat.com/elsewhere")})
        self.assertEqual(wifi_wan._probe_once("wlan0"), "portal")
        self.assertEqual(wifi_wan._portal_seen["url"], "")

    def test_first_capture_of_an_episode_wins(self):
        wifi_wan._curl_probe = fake_curl({
            HTTPS: ("000", ""), HTTP: ("302", "", PORTAL_URL)})
        wifi_wan._probe_once("wlan0")
        wifi_wan._curl_probe = fake_curl({
            HTTPS: ("000", ""), HTTP: ("302", "", "http://other.gw/")})
        wifi_wan._probe_once("wlan0")
        self.assertEqual(wifi_wan._portal_seen["url"], PORTAL_URL)

    def test_cleared_when_verdict_leaves_portal(self):
        wifi_wan._conn_cache.update({"result": "unknown", "time": 0.0,
                                     "bad_streak": 0})
        wifi_wan._curl_probe = fake_curl({
            HTTPS: ("000", ""), HTTP: ("302", "", PORTAL_URL)})
        orig_poison = wifi_wan._fakeip_poisoned
        self.addCleanup(lambda: setattr(wifi_wan, "_fakeip_poisoned", orig_poison))
        wifi_wan._fakeip_poisoned = lambda: False
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "portal")
        self.assertEqual(wifi_wan._portal_seen["url"], PORTAL_URL)
        wifi_wan._curl_probe = fake_curl({HTTPS: ("200", "203.0.113.7")})
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "online")
        self.assertEqual(wifi_wan._portal_seen["url"], "")


def _dns_response(txid, answers, flags=0x8180, qname=b"\x06gcppty\x02gw\x00"):
    """Craft a DNS response: header, one question, then raw `answers` bytes."""
    import struct
    header = struct.pack(">HHHHHH", txid, flags, 1, len(answers), 0, 0)
    question = qname + struct.pack(">HH", 1, 1)
    return header + question + b"".join(answers)


def _rr(name, rtype, rdata):
    import struct
    return name + struct.pack(">HHIH", rtype, 1, 60, len(rdata)) + rdata


class DnsQueryParseTest(unittest.TestCase):
    """The raw resolver behind the portal pin: query building and
    compression-aware answer parsing."""

    def test_query_roundtrip_shape(self):
        q = wifi_wan._build_dns_query("gcppty.gw", 0x1234)
        # header + qname + qtype/qclass
        self.assertEqual(q[:2], b"\x12\x34")
        self.assertIn(b"\x06gcppty\x02gw\x00", q)
        self.assertEqual(q[-4:], b"\x00\x01\x00\x01")

    def test_bad_labels_rejected(self):
        with self.assertRaises(ValueError):
            wifi_wan._build_dns_query("a..b", 1)
        with self.assertRaises((ValueError, UnicodeError)):
            wifi_wan._build_dns_query("héllo.gw", 1)

    def test_a_records_extracted_through_cname_and_compression(self):
        ptr = b"\xc0\x0c"  # compression pointer to the question name
        cname = _rr(ptr, 5, b"\x06portal\x05hotel\x00")
        a1 = _rr(ptr, 1, bytes([10, 1, 2, 3]))
        a2 = _rr(b"\x06portal\x05hotel\x00", 1, bytes([10, 1, 2, 4]))
        resp = _dns_response(0xBEEF, [cname, a1, a2])
        self.assertEqual(wifi_wan._parse_dns_a(resp, 0xBEEF),
                         ["10.1.2.3", "10.1.2.4"])

    def test_txid_mismatch_rejected(self):
        resp = _dns_response(0xBEEF, [_rr(b"\xc0\x0c", 1, bytes([10, 1, 2, 3]))])
        self.assertEqual(wifi_wan._parse_dns_a(resp, 0xDEAD), [])

    def test_query_flags_required(self):
        # A packet without the response bit must be ignored.
        resp = _dns_response(0xBEEF, [_rr(b"\xc0\x0c", 1, bytes([1, 2, 3, 4]))],
                             flags=0x0100)
        self.assertEqual(wifi_wan._parse_dns_a(resp, 0xBEEF), [])

    def test_truncated_garbage_is_safe(self):
        self.assertEqual(wifi_wan._parse_dns_a(b"", 1), [])
        self.assertEqual(wifi_wan._parse_dns_a(b"\x00\x01\x80", 1), [])


class PortalPinHelpersTest(unittest.TestCase):
    """Pure pieces of the access pin: config generation and input filtering
    (a hostile AP controls the Location header these values come from)."""

    def test_conf_text_scopes_and_unrebinds(self):
        text = wifi_wan._portal_conf_text(
            ["gcppty.gw"], ["192.168.8.1", "192.168.8.2"])
        self.assertEqual(text,
                         "server=/gcppty.gw/192.168.8.1\n"
                         "server=/gcppty.gw/192.168.8.2\n"
                         "rebind-domain-ok=/gcppty.gw/\n")

    def test_hostname_filter(self):
        ok = ["gcppty.gw", "portal.hotel-wifi.com", "a1.b2"]
        bad = ["", "bad\nhost", "host/path", "-lead.dot", "trail.", "a b"]
        for h in ok:
            self.assertTrue(wifi_wan._HOSTNAME_RE.match(h), h)
        for h in bad:
            self.assertFalse(wifi_wan._HOSTNAME_RE.match(h), h)

    def test_pinnable_ip(self):
        for ip in ["10.1.2.3", "192.168.8.1", "203.0.113.7", "100.64.0.1"]:
            self.assertTrue(wifi_wan._pinnable_ip(ip), ip)
        for ip in ["127.0.0.1", "0.0.0.0", "224.0.0.1", "169.254.1.1",
                   "2001:db8::1", "not-an-ip", ""]:
            self.assertFalse(wifi_wan._pinnable_ip(ip), ip)

    def test_url_host(self):
        self.assertEqual(wifi_wan._url_host(PORTAL_URL), "gcppty.gw")
        self.assertEqual(wifi_wan._url_host("https://Portal.Hotel:8443/x"),
                         "portal.hotel")
        self.assertEqual(wifi_wan._url_host("ftp://x/"), "")
        self.assertEqual(wifi_wan._url_host("not a url"), "")


if __name__ == "__main__":
    unittest.main()
