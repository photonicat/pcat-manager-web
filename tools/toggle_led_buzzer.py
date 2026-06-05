#!/usr/bin/env python3

import socket
import json
import sys
import time
import random

# ANSI color codes
class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

# Global socket connection
global_socket = None

def connect_socket():
    """Connect to pcat-manager socket"""
    global global_socket
    try:
        global_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        global_socket.connect('/tmp/pcat-manager.sock')
        print(f"{Colors.GREEN}Socket connected to /tmp/pcat-manager.sock{Colors.RESET}")
        return True
    except Exception as e:
        print(f"{Colors.RED}Socket connection error: {e}{Colors.RESET}")
        return False

def close_socket():
    """Close socket connection"""
    global global_socket
    if global_socket:
        global_socket.close()
        global_socket = None
        print(f"{Colors.BLUE}Socket closed{Colors.RESET}")

def send_socket_command(cmd):
    """Send command to pcat-manager socket using persistent connection"""
    global global_socket
    try:
        if not global_socket:
            if not connect_socket():
                return None
        
        cmd_str = json.dumps(cmd)
        print(f"{Colors.CYAN}{Colors.BOLD}Socket SEND:{Colors.RESET} {Colors.YELLOW}{cmd_str}{Colors.RESET}")
        
        global_socket.send(cmd_str.encode() + b'\0')
        
        # Wait for response
        time.sleep(0.5)
        response = global_socket.recv(1024)
        response_data = None
        if response:
            response_str = response.decode('utf-8', errors='ignore').strip()
            print(f"{Colors.GREEN}{Colors.BOLD}Socket RECV:{Colors.RESET} {Colors.MAGENTA}{response_str}{Colors.RESET}")
            
            # Try to parse JSON, handle extra data by finding first complete JSON
            try:
                response_data = json.loads(response_str)
                print(f"{Colors.BLUE}Parsed JSON:{Colors.RESET} {response_data}")
            except json.JSONDecodeError as parse_error:
                if "Extra data" in str(parse_error):
                    # Extract char position and show in hex
                    char_pos = parse_error.pos
                    print(f"{Colors.YELLOW}Extra data at char {char_pos} (0x{char_pos:02x}), stripping...{Colors.RESET}")
                    
                    # Try to parse just the first JSON object
                    try:
                        # Find the end of the first complete JSON object
                        decoder = json.JSONDecoder()
                        response_data, idx = decoder.raw_decode(response_str)
                        print(f"{Colors.BLUE}Parsed JSON (stripped):{Colors.RESET} {response_data}")
                    except:
                        print(f"{Colors.RED}Failed to strip and parse JSON{Colors.RESET}")
                        response_data = None
                else:
                    print(f"{Colors.RED}JSON parse error: {parse_error}{Colors.RESET}")
                    response_data = None
        else:
            print(f"{Colors.RED}No response received from socket{Colors.RESET}")
        
        return response_data
        
    except Exception as e:
        print(f"{Colors.RED}Socket command error: {e}{Colors.RESET}")
        # Try to reconnect on error
        close_socket()
        return None

def auto_toggle_led():
    """Auto toggle LED 100 times with random delays"""
    print(f"\n{Colors.BLUE}Starting auto toggle LED 100 times...{Colors.RESET}")
    
    # Get initial state
    print(f"{Colors.BLUE}Getting initial LED state...{Colors.RESET}")
    initial_get_cmd = {"command": "pmu-io-get"}
    initial_response = send_socket_command(initial_get_cmd)
    if initial_response:
        current_state = initial_response.get("status-led-v2-enabled", 0)
        print(f"{Colors.BLUE}Initial LED state: {current_state}{Colors.RESET}")
    
    try:
        for i in range(100):
            # Toggle state (alternate between 0 and 1)
            expected_state = i % 2
            
            # Get current state before setting
            print(f"\n{Colors.BLUE}[{i+1}/100] Getting current LED state...{Colors.RESET}")
            pre_get_cmd = {"command": "pmu-io-get"}
            pre_response = send_socket_command(pre_get_cmd)
            
            if pre_response is None:
                print(f"{Colors.RED}❌ Error at iteration {i+1} - No pre-get response received, stopping...{Colors.RESET}")
                break
            
            current_state = pre_response.get("status-led-v2-enabled", -1)
            print(f"{Colors.BLUE}Current state: {current_state}, Setting to: {expected_state}{Colors.RESET}")
            
            # Set new state
            cmd = {"command": "pmu-io-set", "status-led-v2-enabled": expected_state}
            response = send_socket_command(cmd)
            
            if response is None:
                print(f"{Colors.RED}❌ Error at iteration {i+1} - No set response received, stopping...{Colors.RESET}")
                break
            
            # Check if command was successful
            if response.get("code") != 0:
                print(f"{Colors.RED}❌ Error at iteration {i+1} - Set command failed{Colors.RESET}")
                print(f"  Response: {response}")
                break
            
            # Wait and read back to verify
            time.sleep(0.1)
            get_cmd = {"command": "pmu-io-get"}
            verify_response = send_socket_command(get_cmd)
            
            if verify_response is None:
                print(f"{Colors.RED}❌ Error at iteration {i+1} - No verify response received, stopping...{Colors.RESET}")
                break
            
            if verify_response.get("code") != 0:
                print(f"{Colors.RED}❌ Error at iteration {i+1} - Verify command failed{Colors.RESET}")
                print(f"  Response: {verify_response}")
                break
            
            actual_state = verify_response.get("status-led-v2-enabled")
            if actual_state == expected_state:
                print(f"✅ {i+1}/100 - LED state verified: {expected_state}")
            else:
                print(f"❌ {i+1}/100 - LED state mismatch!")
                print(f"  Expected: {expected_state}")
                print(f"  Got: {actual_state}")
                print(f"  Full response JSON: {verify_response}")
                break
            
            # Random sleep between 0.1 and 2 seconds
            sleep_time = random.uniform(0.1, 2.0)
            time.sleep(sleep_time)
            
        print(f"{Colors.GREEN}✅ Completed LED toggle test!{Colors.RESET}")
        
    except Exception as e:
        print(f"{Colors.RED}❌ Exception: {e}{Colors.RESET}")
    finally:
        close_socket()

def auto_toggle_buzzer():
    """Auto toggle buzzer 100 times with random delays"""
    print(f"\n{Colors.BLUE}Starting auto toggle buzzer 100 times...{Colors.RESET}")
    
    # Get initial state
    print(f"{Colors.BLUE}Getting initial buzzer state...{Colors.RESET}")
    initial_get_cmd = {"command": "pmu-io-get"}
    initial_response = send_socket_command(initial_get_cmd)
    if initial_response:
        current_state = initial_response.get("beeper-enabled", 0)
        print(f"{Colors.BLUE}Initial buzzer state: {current_state}{Colors.RESET}")
    
    try:
        for i in range(100):
            # Toggle state (alternate between 0 and 1)
            expected_state = i % 2
            
            # Get current state before setting
            print(f"\n{Colors.BLUE}[{i+1}/100] Getting current buzzer state...{Colors.RESET}")
            pre_get_cmd = {"command": "pmu-io-get"}
            pre_response = send_socket_command(pre_get_cmd)
            
            if pre_response is None:
                print(f"{Colors.RED}❌ Error at iteration {i+1} - No pre-get response received, stopping...{Colors.RESET}")
                break
            
            current_state = pre_response.get("beeper-enabled", -1)
            print(f"{Colors.BLUE}Current state: {current_state}, Setting to: {expected_state}{Colors.RESET}")
            
            # Set new state
            cmd = {"command": "pmu-io-set", "beeper-enabled": expected_state}
            response = send_socket_command(cmd)
            
            if response is None:
                print(f"{Colors.RED}❌ Error at iteration {i+1} - No set response received, stopping...{Colors.RESET}")
                break
            
            # Check if command was successful
            if response.get("code") != 0:
                print(f"{Colors.RED}❌ Error at iteration {i+1} - Set command failed{Colors.RESET}")
                print(f"  Response: {response}")
                break
            
            # Wait and read back to verify
            time.sleep(0.1)
            get_cmd = {"command": "pmu-io-get"}
            verify_response = send_socket_command(get_cmd)
            
            if verify_response is None:
                print(f"{Colors.RED}❌ Error at iteration {i+1} - No verify response received, stopping...{Colors.RESET}")
                break
            
            if verify_response.get("code") != 0:
                print(f"{Colors.RED}❌ Error at iteration {i+1} - Verify command failed{Colors.RESET}")
                print(f"  Response: {verify_response}")
                break
            
            actual_state = verify_response.get("beeper-enabled")
            if actual_state == expected_state:
                print(f"✅ {i+1}/100 - Buzzer state verified: {expected_state}")
            else:
                print(f"❌ {i+1}/100 - Buzzer state mismatch!")
                print(f"  Expected: {expected_state}")
                print(f"  Got: {actual_state}")
                print(f"  Full response JSON: {verify_response}")
                break
            
            # Random sleep between 0.1 and 2 seconds
            sleep_time = random.uniform(0.1, 2.0)
            time.sleep(sleep_time)
            
        print(f"{Colors.GREEN}✅ Completed buzzer toggle test!{Colors.RESET}")
        
    except Exception as e:
        print(f"{Colors.RED}❌ Exception: {e}{Colors.RESET}")
    finally:
        close_socket()

def toggle_led():
    """Single LED toggle"""
    cmd = {"command": "pmu-io-set", "status-led-v2-enabled": 1}
    send_socket_command(cmd)

def toggle_buzzer():
    """Single buzzer toggle"""
    cmd = {"command": "pmu-io-set", "beeper-enabled": 1}
    send_socket_command(cmd)

def main():
    if len(sys.argv) != 2:
        print("Usage: python3 toggle_led_buzzer.py [L|B|L100|B100]")
        print("  L    - Single LED toggle")
        print("  B    - Single buzzer toggle")
        print("  L100 - Auto toggle LED 100 times")
        print("  B100 - Auto toggle buzzer 100 times")
        sys.exit(1)
    
    command = sys.argv[1].upper()
    
    if command == 'L':
        toggle_led()
    elif command == 'B':
        toggle_buzzer()
    elif command == 'L100':
        auto_toggle_led()
    elif command == 'B100':
        auto_toggle_buzzer()
    else:
        print("Invalid command. Use 'L', 'B', 'L100', or 'B100'.")
        sys.exit(1)

if __name__ == "__main__":
    main()