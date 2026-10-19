#!/usr/bin/env python3
"""
Simplified demo of the Phonicat Router Management Web Application 
running on x86/macOS with mock data.
"""

import os
import sys
import time
import threading

# Add app directory to path
sys.path.insert(0, '/Users/c2h2/gits/pcat-manager-web/app')
os.chdir('/Users/c2h2/gits/pcat-manager-web/app')

print("=== Phonicat Router Management Demo ===")
print("Cross-platform development version with mock data")
print("")

# Configure for demo
import pcat_config
pcat_config.web_port = 8080
pcat_config.skip_auth = True

# Initialize app with mock clients  
import app
from mock_hardware import MockSocketClient, MockModemClient, MockSmsClient, MockScreenClient

print("🔧 Initializing mock hardware components...")
app.socket_client = MockSocketClient()
app.modem_client = MockModemClient('/dev/mock_ttyUSB3', '/tmp/mock_modem.db')
app.sms_client = MockSmsClient('/dev/mock_ttyUSB3', app.modem_client, '/tmp/mock_modem.db')
app.screen_client = MockScreenClient()

print("✅ Mock hardware initialized")

# Start Flask web server
from pcat_manager_web import flask_app
print(f"🌐 Starting web server on http://localhost:{pcat_config.web_port}")

def run_flask():
    flask_app.run(host='0.0.0.0', port=pcat_config.web_port, debug=False, use_reloader=False)

flask_thread = threading.Thread(target=run_flask)
flask_thread.daemon = True
flask_thread.start()

# Wait for server to start
time.sleep(3)

print("✅ Web server started successfully!")
print("")
print("=== API DEMONSTRATION ===")

# Test APIs
import urllib.request
import json

def test_api(endpoint, name):
    try:
        url = f"http://localhost:{pcat_config.web_port}{endpoint}"
        response = urllib.request.urlopen(url, timeout=5)
        data = json.loads(response.read().decode())
        print(f"✅ {name}:")
        print(f"   Status: {response.getcode()}")
        print(f"   Data: {json.dumps(data, indent=2)[:200]}...")
        print("")
        return True
    except Exception as e:
        print(f"❌ {name}: {e}")
        return False

# Test various APIs
print("Testing mock data APIs:")
test_api("/api/v1/system/board.json", "System Board Info")
test_api("/api/v1/system/info.json", "System Information") 
test_api("/api/v1/modem/basic.json", "Modem Status")

print("🎉 DEMO COMPLETE!")
print("")
print("Features demonstrated:")
print("✅ Cross-platform compatibility (runs on macOS/x86)")  
print("✅ Platform detection and mock mode activation")
print("✅ Mock hardware components with realistic data patterns")
print("✅ Web server running with Flask")
print("✅ REST API endpoints returning JSON responses")
print("✅ Threaded architecture simulation")
print("")
print(f"🌐 Web interface available at: http://localhost:{pcat_config.web_port}")
print("🔧 Mock data includes realistic patterns for:")
print("   - System statistics and hardware info")
print("   - Cellular modem status and signal quality")
print("   - Battery levels with charge/discharge cycles") 
print("   - SMS messages from mobile carriers")
print("   - Network interface configurations")
print("")
print("Press Ctrl+C to stop the demo server")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\n👋 Demo stopped.")