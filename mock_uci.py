#!/usr/bin/env python3
"""
Mock UCI (Unified Configuration Interface) command for cross-platform development.
This script mimics the behavior of the UCI command used in OpenWrt.
"""

import sys
import json

def main():
    if len(sys.argv) < 2:
        print("Usage: mock_uci <command> [args...]")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "show":
        if len(sys.argv) > 2 and "wireless" in sys.argv[2]:
            # Mock wireless configuration
            print("""wireless.radio0=wifi-device
wireless.radio0.type='mac80211'
wireless.radio0.path='platform/soc/18000000.wifi'
wireless.radio0.band='2g'
wireless.radio0.channel='auto'
wireless.radio0.htmode='HT20'
wireless.radio0.disabled='0'

wireless.default_radio0=wifi-iface
wireless.default_radio0.device='radio0'
wireless.default_radio0.network='lan'
wireless.default_radio0.mode='ap'
wireless.default_radio0.ssid='Photonicat-Mock-2G'
wireless.default_radio0.encryption='psk2'
wireless.default_radio0.key='mockpassword123'

wireless.radio1=wifi-device
wireless.radio1.type='mac80211'
wireless.radio1.path='pci0000:00/0000:00:1e.0'
wireless.radio1.band='5g'
wireless.radio1.channel='auto'
wireless.radio1.htmode='VHT80'
wireless.radio1.disabled='0'

wireless.default_radio1=wifi-iface
wireless.default_radio1.device='radio1'
wireless.default_radio1.network='lan'
wireless.default_radio1.mode='ap'
wireless.default_radio1.ssid='Photonicat-Mock-5G'
wireless.default_radio1.encryption='psk2'
wireless.default_radio1.key='mockpassword123'
""")
        else:
            # Mock generic config show
            print("mock.config='test_value'")
            print("mock.enabled='1'")
    
    elif command == "set":
        # Mock set command
        if len(sys.argv) > 2:
            print(f"Mock: Setting {sys.argv[2]}")
        sys.exit(0)
    
    elif command == "commit":
        # Mock commit command  
        print("Mock: Configuration committed")
        sys.exit(0)
    
    elif command == "get":
        # Mock get command
        if len(sys.argv) > 2:
            print("mock_value")
        sys.exit(0)
        
    elif command == "--version":
        print("Mock UCI version 1.0.0")
        sys.exit(0)
    
    else:
        print(f"Mock UCI: Unknown command '{command}'")
        sys.exit(1)

if __name__ == "__main__":
    main()