"""LAN client table for /stats: DHCP leases merged with Wi-Fi association.

Pure functions (no I/O) so they are unit-testable; App.get_lan_clients() does
the reading. One row per MAC: every IPv4 DHCP lease, flagged wifi=True when
that MAC is currently associated to one of our APs, plus any associated
station that has no lease (static IP) with an empty ip.
"""
import ipaddress
import re

_MAC_RE = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")


def parse_dhcp_leases(text):
  """dnsmasq /tmp/dhcp.leases -> [{'mac','ip','name','expiry'}], IPv4 only.

  Line format: '<expiry> <mac> <ipv4> <hostname|*> <client-id|*>'. DHCPv6
  leases carry an IAID where the MAC goes and an IPv6 address, and the file
  ends with a 'duid <hex>' line; both are skipped. '*' (no hostname) -> ''.
  """
  leases = []
  for line in text.splitlines():
    parts = line.split()
    if len(parts) < 4:
      continue
    mac = parts[1].lower()
    if not _MAC_RE.match(mac):
      continue
    try:
      ipaddress.IPv4Address(parts[2])
    except ValueError:
      continue
    try:
      expiry = int(parts[0])
    except ValueError:
      expiry = 0
    name = parts[3] if parts[3] != "*" else ""
    leases.append({"mac": mac, "ip": parts[2], "name": name, "expiry": expiry})
  return leases


def merge_clients(leases, wifi_macs):
  """One row per MAC, sorted by IPv4 address; lease-less Wi-Fi stations last.

  A MAC with several leases keeps the newest (highest expiry) one. MACs are
  compared case-insensitively (hostapd reports lower-case, leases too, but a
  mock or another source may not).
  """
  wifi = {m.lower() for m in wifi_macs if m}
  rows = {}
  for lease in leases:
    prev = rows.get(lease["mac"])
    if prev is not None and prev["expiry"] >= lease["expiry"]:
      continue
    rows[lease["mac"]] = {"mac": lease["mac"], "ip": lease["ip"], "name": lease["name"],
                          "expiry": lease["expiry"], "wifi": lease["mac"] in wifi}
  for mac in wifi:
    if mac not in rows:
      rows[mac] = {"mac": mac, "ip": "", "name": "", "expiry": 0, "wifi": True}

  def sort_key(row):
    if row["ip"]:
      return (0, int(ipaddress.IPv4Address(row["ip"])), row["mac"])
    return (1, 0, row["mac"])
  return sorted(rows.values(), key=sort_key)
