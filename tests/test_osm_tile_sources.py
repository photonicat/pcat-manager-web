#!/usr/bin/env python3
"""Upstream failover for the /gps map tiles.

The map went blank whenever egress moved to the modem ("5G mode"). Measured
on the device: connections to tile.openstreetmap.org are RST-injected within
~0.2s over a Chinese mobile carrier, on port 80 as well as 443, while the
same link fetches every other host normally. fetch_remote() had one hardcoded
upstream, so every tile not already in the local store failed.

These tests pin the behaviour the fix depends on: try the next upstream, stop
retrying a dead one for a while, and do not condemn a healthy mirror over a
tile it simply does not have.

    python3 -m unittest tests.test_osm_tile_sources -v
"""

import os
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app")))
sys.modules.setdefault("pcat_logger", MagicMock())

from pcat_util import osm_tiles  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"tile-bytes"


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def http_error(code):
    return urllib.error.HTTPError("http://x/", code, "err", {}, None)


class TileSourceFailoverTest(unittest.TestCase):
    def setUp(self):
        # Each test starts with every upstream healthy and nothing written to
        # disk (write_back=False keeps the real store out of this).
        osm_tiles._source_penalty.clear()
        self.addCleanup(osm_tiles._source_penalty.clear)

    def _fetch(self, side_effect):
        """fetch_remote with urlopen stubbed; returns (data, hosts_tried)."""
        tried = []

        def fake_urlopen(req, timeout=None):
            tried.append(req.full_url)
            outcome = side_effect(req.full_url)
            if isinstance(outcome, Exception):
                raise outcome
            return FakeResponse(outcome)

        with patch.object(osm_tiles.urllib.request, "urlopen", fake_urlopen):
            data = osm_tiles.fetch_remote(6, 53, 24, write_back=False)
        return data, tried

    def test_more_than_one_upstream_is_configured(self):
        # A single upstream is the bug: one blocked host takes the map with
        # it. The mirrors must also be OSM-scheme (WGS84 z/x/y), not GCJ-02
        # Chinese services, or the marker would sit off the road.
        self.assertGreater(len(osm_tiles.REMOTE_URLS), 1)
        for url in osm_tiles.REMOTE_URLS:
            self.assertIn("{z}", url)
            self.assertIn("{x}", url)
            self.assertIn("{y}", url)
            self.assertIn("openstreetmap", url)

    def test_reset_on_the_primary_falls_through_to_the_mirror(self):
        # The modem case: .org resets, the mirror answers.
        primary = osm_tiles.REMOTE_URLS[0].split("/")[2]

        def outcome(url):
            if primary in url:
                return ConnectionResetError("Connection reset by peer")
            return PNG

        data, tried = self._fetch(outcome)
        self.assertEqual(data, PNG)
        self.assertEqual(len(tried), 2)

    def test_a_dead_upstream_is_not_retried_for_every_later_tile(self):
        # Without the cooldown every tile in the view re-pays the reset on the
        # blocked host, which is most of what made the map hang rather than
        # just look wrong.
        primary = osm_tiles.REMOTE_URLS[0].split("/")[2]

        def outcome(url):
            if primary in url:
                return ConnectionResetError("Connection reset by peer")
            return PNG

        self._fetch(outcome)
        _data, tried = self._fetch(outcome)
        self.assertTrue(all(primary not in u for u in tried),
                        f"blocked host retried immediately: {tried}")

    def test_one_tile_never_pays_more_than_the_attempt_cap(self):
        # Everything dead: the request still has to return on a bounded
        # timeline, because it is holding a granian request thread.
        data, tried = self._fetch(lambda url: TimeoutError("timed out"))
        self.assertIsNone(data)
        self.assertLessEqual(len(tried), osm_tiles.FETCH_ATTEMPTS)

    def test_a_404_does_not_blacklist_a_healthy_mirror(self):
        # Mirrors cover less of the world at high zoom than .org does. A 404
        # is that tile's answer, not a verdict on the source.
        mirror = osm_tiles.REMOTE_URLS[1]
        self._fetch(lambda url: http_error(404) if mirror.split("/")[2] in url
                    else PNG)
        self.assertNotIn(mirror, osm_tiles._source_penalty)

    def test_being_rate_limited_does_blacklist(self):
        # 429/403 is the upstream refusing us specifically, and it will refuse
        # the next tile too.
        primary = osm_tiles.REMOTE_URLS[0]
        self._fetch(lambda url: http_error(429)
                    if primary.split("/")[2] in url else PNG)
        self.assertIn(primary, osm_tiles._source_penalty)

    def test_a_non_png_body_condemns_the_source(self):
        # 200 with an error page or captive-portal splash: serving that as a
        # tile puts garbage on the map.
        primary = osm_tiles.REMOTE_URLS[0]
        data, _tried = self._fetch(
            lambda url: b"<html>nope</html>"
            if primary.split("/")[2] in url else PNG)
        self.assertEqual(data, PNG)
        self.assertIn(primary, osm_tiles._source_penalty)

    def test_all_upstreams_cold_still_retries_rather_than_giving_up(self):
        # Cooldowns must not make the map permanently blank once the uplink
        # recovers before they lapse.
        for url in osm_tiles.REMOTE_URLS:
            osm_tiles._source_penalty[url] = float("inf")
        data, tried = self._fetch(lambda url: PNG)
        self.assertEqual(data, PNG)
        self.assertTrue(tried)


if __name__ == "__main__":
    unittest.main()
