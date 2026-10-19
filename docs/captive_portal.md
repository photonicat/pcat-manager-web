# Captive Portal Gateway (OpenNDS)

Make the photonicat act as a **captive gateway**: clients on its WiFi/LAN must
pass a login/splash page before getting internet. This is the inverse of Smart
WAN (where the device is a captive *client* of an upstream hotspot).

## Decision: OpenNDS (not wifidog / not custom)

- **OpenNDS** is the maintained successor to nodogsplash, native to fw4/nftables,
  and lets our Flask app serve the branded login via **FAS** if/when we go past
  click-through. wifidog is abandoned; a custom build would re-implement the
  fiddly bits (multi-OS captive detection, session lifecycle, token security).
- Firmware: enabled in the OpenWrt tree
  (`configs/photonicat2_base_defconfig`: `CONFIG_PACKAGE_opennds=y` +
  `CONFIG_PACKAGE_libmicrohttpd-no-ssl=y`). Requires the **fw4/nftables** build
  (already the photonicat2 default). The older fw3 image cannot run modern
  OpenNDS.

## Auth model (current scope)

**Click-through** — splash with an "Accept & Connect" button, no identity.
OpenNDS' built-in click-to-continue ThemeSpec provides this out of the box; we
only rebrand it. Later models (shared password, **SMS-OTP real-name** for China
public-WiFi compliance, vouchers) build on top via FAS pointing at Flask.

## On-device bring-up (after flashing the opennds image)

> Run these over SSH on the device. SSH/admin must stay reachable — see lockout
> guard below.

1. Confirm install + fw4:
   ```sh
   opkg list-installed | grep opennds
   nft list tables | grep fw4        # must show 'table inet fw4'
   ```
2. Pick the **client-facing** interface to gate. Today the only client AP is
   `radio1` ("photonicat2-…-5g") on **`br-lan`** (172.16.0.1/21). Gating
   `br-lan` also gates wired LAN clients — acceptable for a test, but a
   dedicated guest SSID/network is preferable for production.
   ```sh
   uci set opennds.@opennds[0].gatewayinterface='br-lan'
   uci set opennds.@opennds[0].enabled='1'
   ```
3. **Lockout guard (critical):** keep the manager UI + SSH reachable pre-auth.
   - Management is at `172.16.0.1` on br-lan; allow it as a walled-garden host
     and allow the LAN gateway itself. Allow OS captive-detection endpoints so
     client devices auto-pop the login sheet.
   ```sh
   # allow the device's own admin UI / DNS / etc. (gateway is auto-allowed)
   uci add_list opennds.@opennds[0].walledgarden_fqdn_list='4.photonicat.com'
   uci add_list opennds.@opennds[0].walledgarden_fqdn_list='dl.photonicat.com'
   uci commit opennds
   /etc/init.d/opennds restart
   ```
   - My SSH comes in via `eth0` (192.168.21.219), a *different* interface than
     the gated `br-lan`, so it is not affected. Do **not** set
     `gatewayinterface` to the eth0/WAN side.
4. Verify the daemon + ruleset:
   ```sh
   ndsctl status
   ndsctl json            # clients + auth state
   nft list table inet nds 2>/dev/null || nft list ruleset | grep -i nds
   ```

## Test procedure

1. Connect a phone/laptop to the gated WiFi ("photonicat2-…-5g").
2. The OS captive-detection probe should make a **login sheet pop up**
   automatically (Apple/Android/Windows). If not, browse to any **http://** site.
3. Tap **Accept** → OpenNDS authorizes the client MAC → internet works.
4. Confirm on device:
   ```sh
   ndsctl json | grep -A3 <client-mac>   # state: authenticated
   ```
5. Negative test: a second un-accepted client must NOT reach the internet
   (https sites fail / time out until accepted).

### Verified failure modes (2026-07-02, OpenNDS 10.3.1) and fixes

Reproduced on-device with a netns test client bridged onto `br-lan`
(`ip netns` + veth + udhcpc; a DHCP lease is mandatory — OpenNDS 403s
static-IP clients with "IP not allocated by dhcp").

1. **`status.client` DNS record install is racy.** OpenNDS installs
   `dhcp.@dnsmasq[0].address='/status.client/<gw-ip>'` (uncommitted) and
   reloads dnsmasq at startup; the reload intermittently fails
   ("Dnsmasq reload failed!" in logread) and the name never resolves. Every
   portal URL used `http://status.client:2050/...`, so clients could load
   nothing — or submit "Agree & Connect" into a DNS error. **Fix:**
   `option gatewayfqdn 'disable'` → all portal URLs are IP-based
   (`http://<gw-ip>:2050/...`). Enforced by `captive._ensure_portal_config()`
   when the portal is enabled from the settings page.
2. **Two-step accept stranded users on stale tokens.** The stock
   click-to-continue flow authenticates only on the second tap
   (thankyou → landing). With a stale `fas` (cached splash, portal restart —
   each restart rotates the token — or pre-auth session timeout) the landing
   step fails into an unbranded "failed to log in" page whose retry button
   targets `http://status.client` (→ HTTP 511 page with a `target=_blank`
   button that does nothing inside captive-portal mini-browsers). **Fix:**
   `theme_photonicat.sh` now authenticates on the first tap of
   "Agree & Connect", and its branded failure page retries via the client's
   `originurl` (a foreign host always gets a fresh splash redirect; requests
   addressed to the gateway itself get the 511 dead end).
3. **~10-15 s blackhole after enable/restart** while nft rules rebuild:
   client HTTP times out, no redirect. Transient; the settings page already
   delays its status refresh. Retrying/re-opening the captive sheet recovers.

### Notes / gotchas
- **HTTPS can't be intercepted** — detection relies on the OS HTTP probe, not on
  redirecting arbitrary HTTPS. That's expected behaviour.
- Once a client MAC is authorized, all its traffic passes (NAT). Sessions expire
  per `sessiontimeout`.
- Coexistence with **Smart WAN**: the device can be captive-client (upstream,
  radio0 STA) and captive-gateway (downstream, radio1 AP) at once — verify the
  nft sets/marks from opennds and the Smart WAN metric/mwan3 changes don't
  collide.

## pcat-manager-web integration (after the on-device config is verified)

- `app/pcat_util/captive.py` — manage `/etc/config/opennds` via UCI
  (enable/disable, gatewayinterface, walled garden incl. admin-allow), mirroring
  the `wifi_wan.py` pattern.
- Branded **click-to-continue** splash (rebrand the OpenNDS ThemeSpec; OEM
  assets).
- Manager UI: a toggle + "connected/authorized clients" status (from
  `ndsctl json`).
- EN/CN translations.

Build the integration from the **verified** on-device config, not from guessed
file paths — opennds' theme/UCI layout varies by version.
