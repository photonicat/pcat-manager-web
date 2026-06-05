#@Author    : qichunren

import fcntl
import socket
import struct
import base64
import os.path
import pathlib
import subprocess
from pcat_util import command_parser
from pcat_util.openwrt import uci_show

def getEMMCSerial():
    try:
        return open("/sys/class/block/mmcblk0/device/serial").read().strip()
    except:
        return ""

def getEMMCSerialEncoded():
    if len(getEMMCSerial()) > 0:
        return base64.urlsafe_b64encode(getEMMCSerial().encode("utf-8")).decode("utf-8").strip("=")
    else:
        return ""

def getmac(interface):
    mac = open('/sys/class/net/'+interface+'/address').readline()
    return mac[0:17]

def getHwAddr(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        info = fcntl.ioctl(s.fileno(), 0x8927,  struct.pack('256s', bytes(ifname, 'utf-8')[:15]))
    except:
        print("Failed to get hwaddr from", ifname)
        return None
    return info[18:24]
    # return ':'.join('%02x' % b for b in info[18:24])

def getHwAddrEncoded(ifname):
    print("Try to get hw from", ifname)
    m = getmac(ifname).replace(":",'')

    if len(m)>1:
        return base64.urlsafe_b64encode(m.encode("utf-8")).decode("utf-8").strip("=")
    else:
        return ""

def get_external_wifi():
    cmd_output = uci_show("wireless")
    
    # For test
    # current_working_path = pathlib.Path(__file__).parent.parent.resolve()
    # fixture_file = current_working_path.joinpath("test", "fixture_wireless.txt")
    # f = open(fixture_file, "rb")
    # cmd_output = f.read()
    # f.close()

    if cmd_output == "":
        return None
    interfaces = command_parser.parse_uci_show_wireless(cmd_output)
    for interface in interfaces:
        if interface["device_type"] == "PCIE":
            return interface
    return None

# For self test
if __name__ == "__main__":
  current_working_path = pathlib.Path(__file__).parent.parent.resolve()
  fixture_file = current_working_path.joinpath("test", "fixture_wireless.txt")
  f = open(fixture_file, "rb")
  cmd_output = f.read()
  f.close()
  wifi_list = get_external_wifi(cmd_output)
  print("Get list:", wifi_list)