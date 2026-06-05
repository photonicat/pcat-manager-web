#!/usr/bin/env python3
"""
SMS Tool for 3G/4G/5G Modem - Python Version
===========================================

Based on the C implementation by Cezary Jackiewicz and lovewilliam

DESCRIPTION:
    This tool provides comprehensive SMS and modem management functionality:
    • Send SMS messages via 3G/4G/5G modems
    • Receive and decode SMS messages  
    • Delete SMS messages from storage
    • Check modem and storage status
    • Send USSD codes for service queries
    • Send raw AT commands for debugging

USAGE:
    python3 sms_tool_python.py [OPTIONS] COMMAND [ARGUMENTS]

COMMANDS:
    send <phone> <message>  Send SMS to specified phone number
    recv                    Receive and display SMS messages
    delete <index|all>      Delete SMS by index or all messages
    status                  Show modem storage status
    ussd <code>            Send USSD code (e.g., *100# for balance)
    at <command>           Send raw AT command to modem
    call <phone>           Make voice call to phone number
    answer                 Answer incoming voice call
    hangup                 Hang up current voice call  
    status-call            Get current call status (AT+CLCC)
    audio-path <number>    Set audio path for voice calls
    no-echo               Disable command echo (ATE0)
    reset                  Factory reset RM520N-GL modem

OPTIONS:
    -b, --baudrate <rate>      Serial baudrate (default: 115200)
    -d, --device <device>      TTY device path (default: /dev/ttyUSB0)
    -D, --debug               Enable debug mode for ussd/at commands
    -f, --dateformat <fmt>    Date/time format string (default: %m/%d/%y %H:%M:%S)
    -j, --json                JSON output format for recv command
    -R, --raw-input           Use raw input for ussd commands
    -r, --raw-output          Use raw output for ussd/recv commands
    -s, --storage <type>      SMS storage type (SM/ME/MT for recv/status)

EXAMPLES:
    # Send SMS
    python3 sms_tool_python.py -d /dev/ttyUSB2 send +1234567890 "Hello World"
    
    # Check balance via USSD
    python3 sms_tool_python.py -d /dev/ttyUSB2 ussd "*100#"
    
    # Make voice call
    python3 sms_tool_python.py -d /dev/ttyUSB2 call +1234567890
    
    # Answer incoming call
    python3 sms_tool_python.py -d /dev/ttyUSB2 answer
    
    # Hang up current call
    python3 sms_tool_python.py -d /dev/ttyUSB2 hangup
    
    # Check call status
    python3 sms_tool_python.py -d /dev/ttyUSB2 status-call
    
    # Set audio path
    python3 sms_tool_python.py -d /dev/ttyUSB2 audio-path 1
    
    # Receive messages in JSON format
    python3 sms_tool_python.py -d /dev/ttyUSB2 -j recv
    
    # Receive and assemble multipart messages
    python3 sms_tool_python.py -d /dev/ttyUSB2 -A recv
    
    # Fetch from both SIM and modem storage, delete after
    python3 sms_tool_python.py -d /dev/ttyUSB2 --all-storage --delete-after recv
    
    # Check storage status
    python3 sms_tool_python.py -d /dev/ttyUSB2 status
    
    # Delete all messages
    python3 sms_tool_python.py -d /dev/ttyUSB2 delete all
    
    # Reset modem to factory settings
    python3 sms_tool_python.py -d /dev/ttyUSB2 reset
"""

import argparse
import serial
import time
import sys
import json
import signal
import re
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any

SMS_MESSAGES_DELAY = 0.5
SHORTNUMBER_LENGTH = 6

class SMSCharset:
    GSM_7BIT = 0
    BINARY_8BIT = 1
    UCS2 = 2


class PDUDecoder:
    """PDU encoding/decoding functionality"""
    
    @staticmethod
    def swap_decimal_nibble(x: int) -> int:
        """Swap decimal digits of a number (e.g. 12 -> 21)"""
        return (x // 16) + ((x % 16) * 10)
    
    @staticmethod
    def encode_7bit(text: str) -> bytes:
        """Encode text using GSM 7-bit encoding"""
        if len(text) > 160:
            raise ValueError("SMS message too long")
        
        output = bytearray()
        bits = 0
        bit_count = 0
        
        for char in text:
            char_code = ord(char) & 0x7F
            bits |= (char_code << bit_count)
            bit_count += 7
            
            while bit_count >= 8:
                output.append(bits & 0xFF)
                bits >>= 8
                bit_count -= 8
        
        # Add remaining bits if any
        if bit_count > 0:
            output.append(bits & 0xFF)
        
        return bytes(output)
    
    @staticmethod
    def decode_7bit(data: bytes, length: int) -> str:
        """Decode GSM 7-bit encoded data"""
        output = []
        carry = 0
        carry_bits = 0
        
        for i, byte_val in enumerate(data):
            if len(output) >= length:
                break
                
            # Extract character from current byte and carry
            char_val = ((byte_val << carry_bits) | carry) & 0x7F
            output.append(chr(char_val))
            
            # Update carry for next iteration
            carry = byte_val >> (7 - carry_bits)
            carry_bits += 1
            
            # If we have enough carry bits for a full character
            if carry_bits == 7:
                if len(output) < length:
                    output.append(chr(carry & 0x7F))
                carry = 0
                carry_bits = 0
                
        return ''.join(output[:length])
    
    @staticmethod
    def encode_phone_number(phone: str) -> bytes:
        """Encode phone number for PDU"""
        if phone.startswith('+'):
            ton_npi = 0x91  # International format
            phone = phone[1:]
        else:
            # Check if it's a short number (<=SHORTNUMBER_LENGTH digits) - use National format
            phone_digits = phone.replace('-', '').replace(' ', '')
            if phone_digits.isdigit() and len(phone_digits) <= SHORTNUMBER_LENGTH:
                ton_npi = 0xA1  # National format for short numbers
            else:
                #ton_npi = 0x81  # Unknown format for other numbers
                ton_npi = 0xA1  #always use national format for other numbers
            
        # Pad with F if odd length
        if len(phone) % 2:
            phone += 'F'
            
        # Swap digit pairs
        encoded = bytearray()
        for i in range(0, len(phone), 2):
            digit1 = phone[i]
            digit2 = phone[i + 1] if i + 1 < len(phone) else 'F'
            encoded.append(int(digit2 + digit1, 16))
            
        return bytes([len(phone.rstrip('F')), ton_npi] + list(encoded))
    
    @staticmethod
    def decode_phone_number(data: bytes, offset: int) -> Tuple[str, int]:
        """Decode phone number from PDU"""
        length = data[offset]
        ton_npi = data[offset + 1]
        
        # Calculate number of bytes needed
        num_bytes = (length + 1) // 2
        phone_bytes = data[offset + 2:offset + 2 + num_bytes]
        
        phone = ''
        for byte_val in phone_bytes:
            digit1 = byte_val & 0x0F
            digit2 = (byte_val & 0xF0) >> 4
            
            if digit1 != 0xF:
                phone += str(digit1)
            if digit2 != 0xF:
                phone += str(digit2)
                
        if len(phone) > length:
            phone = phone[:length]
            
        if ton_npi == 0x91:
            phone = '+' + phone
            
        return phone, offset + 2 + num_bytes
    
    @staticmethod
    def decode_timestamp(data: bytes, offset: int) -> Tuple[str, int]:
        """Decode timestamp from PDU"""
        timestamp_bytes = data[offset:offset + 7]
        
        # Each byte represents two digits in swapped format
        year = PDUDecoder.swap_decimal_nibble(timestamp_bytes[0])
        month = PDUDecoder.swap_decimal_nibble(timestamp_bytes[1])
        day = PDUDecoder.swap_decimal_nibble(timestamp_bytes[2])
        hour = PDUDecoder.swap_decimal_nibble(timestamp_bytes[3])
        minute = PDUDecoder.swap_decimal_nibble(timestamp_bytes[4])
        second = PDUDecoder.swap_decimal_nibble(timestamp_bytes[5])
        
        # Timezone is in quarters of an hour
        tz_byte = timestamp_bytes[6]
        tz_quarters = PDUDecoder.swap_decimal_nibble(tz_byte & 0x7F)
        tz_sign = '-' if (tz_byte & 0x08) else '+'
        
        timestamp = f"20{year:02d}/{month:02d}/{day:02d} {hour:02d}:{minute:02d}:{second:02d} GMT{tz_sign}{tz_quarters//4:02d}:{(tz_quarters%4)*15:02d}"
        
        return timestamp, offset + 7
    
    @staticmethod
    def decode_message_text(data: bytes, offset: int, length: int, dcs: int) -> str:
        """Decode message text based on data coding scheme"""
        if dcs == 0x00:
            # GSM 7-bit encoding
            return PDUDecoder.decode_7bit(data[offset:], length)
        elif dcs == 0x08:
            # UCS2 encoding (UTF-16BE)
            try:
                # Length is in bytes for UCS2
                ucs2_data = data[offset:offset + length]
                return ucs2_data.decode('utf-16be')
            except UnicodeDecodeError:
                return f"[UCS2 decode error: {data[offset:offset + length].hex()}]"
        elif dcs == 0x10:
            # Class 0 SMS - GSM 7-bit encoding (immediate display)
            return PDUDecoder.decode_7bit(data[offset:], length)
        elif dcs == 0x11:
            # Class 1 SMS - GSM 7-bit encoding (stored in ME/SIM)
            return PDUDecoder.decode_7bit(data[offset:], length)
        elif dcs == 0x12:
            # Class 2 SMS - GSM 7-bit encoding (stored in SIM)
            return PDUDecoder.decode_7bit(data[offset:], length)
        elif dcs == 0x13:
            # Class 3 SMS - UCS2 encoding (stored in TE)
            try:
                ucs2_data = data[offset:offset + length]
                return ucs2_data.decode('utf-16be')
            except UnicodeDecodeError:
                return f"[UCS2 decode error: {data[offset:offset + length].hex()}]"
        elif dcs == 0x14:
            # Class 0 SMS - 8-bit encoding
            try:
                return data[offset:offset + length].decode('utf-8')
            except UnicodeDecodeError:
                return f"[8-bit decode error: {data[offset:offset + length].hex()}]"
        elif dcs == 0x15:
            # Class 1 SMS - 8-bit encoding
            try:
                return data[offset:offset + length].decode('utf-8')
            except UnicodeDecodeError:
                return f"[8-bit decode error: {data[offset:offset + length].hex()}]"
        elif dcs == 0x16:
            # Class 2 SMS - 8-bit encoding
            try:
                return data[offset:offset + length].decode('utf-8')
            except UnicodeDecodeError:
                return f"[8-bit decode error: {data[offset:offset + length].hex()}]"
        elif dcs == 0x17:
            # Class 3 SMS - 8-bit encoding
            try:
                return data[offset:offset + length].decode('utf-8')
            except UnicodeDecodeError:
                return f"[8-bit decode error: {data[offset:offset + length].hex()}]"
        elif dcs == 0x18:
            # Class 0 SMS - UCS2 encoding
            try:
                ucs2_data = data[offset:offset + length]
                return ucs2_data.decode('utf-16be')
            except UnicodeDecodeError:
                return f"[UCS2 decode error: {data[offset:offset + length].hex()}]"
        elif dcs == 0x19:
            # Class 1 SMS - UCS2 encoding
            try:
                ucs2_data = data[offset:offset + length]
                return ucs2_data.decode('utf-16be')
            except UnicodeDecodeError:
                return f"[UCS2 decode error: {data[offset:offset + length].hex()}]"
        elif dcs == 0x1A:
            # Class 2 SMS - UCS2 encoding
            try:
                ucs2_data = data[offset:offset + length]
                return ucs2_data.decode('utf-16be')
            except UnicodeDecodeError:
                return f"[UCS2 decode error: {data[offset:offset + length].hex()}]"
        elif dcs == 0x1B:
            # Class 3 SMS - UCS2 encoding
            try:
                ucs2_data = data[offset:offset + length]
                return ucs2_data.decode('utf-16be')
            except UnicodeDecodeError:
                return f"[UCS2 decode error: {data[offset:offset + length].hex()}]"
        else:
            # Unknown encoding
            return f"[Unknown encoding DCS={dcs:02X}: {data[offset:offset + length].hex()}]"
    
    @staticmethod
    def decode_pdu(pdu_hex: str) -> Dict[str, Any]:
        """Decode a complete SMS PDU"""
        try:
            # Convert hex string to bytes
            pdu = bytes.fromhex(pdu_hex)
            offset = 0
            
            # SMSC length and number
            smsc_len = pdu[offset]
            offset += 1 + smsc_len
            
            # PDU type
            pdu_type = pdu[offset]
            offset += 1
            
            # Sender address
            sender, offset = PDUDecoder.decode_phone_number(pdu, offset)
            
            # Protocol identifier
            pid = pdu[offset]
            offset += 1
            
            # Data coding scheme
            dcs = pdu[offset]
            offset += 1
            
            # Service center timestamp
            timestamp, offset = PDUDecoder.decode_timestamp(pdu, offset)
            
            # User data length
            udl = pdu[offset]
            offset += 1
            
            # User data header (if present)
            reference = None
            part = None
            total = None
            
            if pdu_type & 0x40:  # UDHI bit set
                udhl = pdu[offset]
                offset += 1
                udh_start = offset
                udh_end = offset + udhl
                
                # Parse User Data Header
                while offset < udh_end:
                    ie_id = pdu[offset]
                    ie_len = pdu[offset + 1]
                    ie_data = pdu[offset + 2:offset + 2 + ie_len]
                    
                    if ie_id == 0x00 and ie_len == 3:  # Concatenated SMS header
                        reference = ie_data[0]
                        total = ie_data[1]
                        part = ie_data[2]
                    elif ie_id == 0x08 and ie_len == 4:  # Concatenated SMS 16-bit reference
                        reference = (ie_data[0] << 8) | ie_data[1]
                        total = ie_data[2]
                        part = ie_data[3]
                    
                    offset += 2 + ie_len
                
                udl -= udhl + 1
            
            # Message text
            message = PDUDecoder.decode_message_text(pdu, offset, udl, dcs)
            
            result = {
                'sender': sender,
                'timestamp': timestamp,
                'message': message,
                'pdu_type': pdu_type,
                'dcs': dcs
            }
            
            # Add multipart SMS fields if present
            if reference is not None:
                result['reference'] = reference
            if part is not None:
                result['part'] = part
            if total is not None:
                result['total'] = total
            
            return result
            
        except Exception as e:
            return {
                'sender': '[Decode error]',
                'timestamp': '[Decode error]',
                'message': f'[PDU decode failed: {str(e)}]',
                'pdu_type': 0,
                'dcs': 0
            }
    
    @staticmethod
    def encode_pdu(phone: str, message: str) -> str:
        """Encode SMS message to PDU format (GSM 7-bit encoding)"""
        pdu = bytearray()

        # SMSC length (0 for default)
        pdu.append(0x00)

        # PDU type (SMS-SUBMIT with relative validity period)
        # Binary: 00010001 -> 00010001 (keep same as C implementation)
        pdu.append(0x11)

        # Message reference (let network assign)
        pdu.append(0x00)

        # Destination address
        phone_encoded = PDUDecoder.encode_phone_number(phone)
        pdu.extend(phone_encoded)

        # Protocol identifier
        pdu.append(0x00)

        # Data coding scheme (7-bit)
        pdu.append(0x00)

        # Validity period (relative format) - matches C implementation
        pdu.append(0xB0)

        # User data length (in septets for 7-bit encoding)
        pdu.append(len(message))

        # User data (7-bit encoded)
        message_encoded = PDUDecoder.encode_7bit(message)
        pdu.extend(message_encoded)

        return pdu.hex().upper()

    @staticmethod
    def encode_pdu_ucs2(phone: str, message: str) -> str:
        """Encode SMS message to PDU format with UCS2 (UTF-16BE) encoding for non-ASCII text"""
        pdu = bytearray()

        # SMSC length (0 for default)
        pdu.append(0x00)

        # PDU type (SMS-SUBMIT with relative validity period)
        pdu.append(0x11)

        # Message reference (let network assign)
        pdu.append(0x00)

        # Destination address
        phone_encoded = PDUDecoder.encode_phone_number(phone)
        pdu.extend(phone_encoded)

        # Protocol identifier
        pdu.append(0x00)

        # Data coding scheme: UCS2 (0x08)
        pdu.append(0x08)

        # Validity period (relative format)
        pdu.append(0xB0)

        # Encode message as UTF-16BE
        utf16_bytes = message.encode('utf-16be')

        # User data length (in bytes for UCS2)
        pdu.append(len(utf16_bytes))

        # User data (UTF-16BE encoded)
        pdu.extend(utf16_bytes)

        return pdu.hex().upper()


class SMSTool:
    """Main SMS tool class"""
    
    def __init__(self, device: Optional[str] = "/dev/ttyUSB0", baudrate: int = 115200, 
                 debug: bool = False, storage: str = "", dateformat: str = "%m/%d/%y %H:%M:%S",
                 gsm_mode: bool = False, serial_port: Optional[serial.Serial] = None):
        self.device = device
        self.baudrate = baudrate
        self.debug = debug
        self.storage = storage
        self.dateformat = dateformat
        self.gsm_mode = gsm_mode
        
        # If a serial port object is provided, use it directly
        if serial_port is not None:
            print(f"\033[32mUsing provided serial port: {serial_port}\033[0m", file=sys.stderr)
            self.serial_port = serial_port
            self._external_serial = True
            # Override device path since we're using external serial
            self.device = getattr(serial_port, 'port', 'external_serial')
        else:
            self.serial_port = None
            self._external_serial = False
        
        # Set up signal handler for timeout only when running standalone
        # When imported as a module, skip signal setup to avoid thread issues
        if __name__ == '__main__':
            signal.signal(signal.SIGALRM, self._timeout_handler)
    
    def _timeout_handler(self, signum, frame):
        """Handle timeout signal"""
        print("\033[31mNo response from modem.\033[0m", file=sys.stderr)
        sys.exit(2)
    
    def _send_command_with_sync(self, command: str, timeout: int = 5, expect_data_response: bool = False) -> List[str]:
        """Send AT command with proper synchronization and return responses"""
        # Clear buffers to prevent sync issues (skip if external port doesn't support it - FM350-GL)
        try:
            self.serial_port.reset_input_buffer()
            self.serial_port.reset_output_buffer()
        except (OSError, IOError, AttributeError):
            # External serial ports may not support buffer reset - continue anyway
            pass
        
        # Store command for echo tracking
        self._last_command = command
        
        if self.debug:
            print(f"\033[44m[SEND] {command}\033[0m", file=sys.stderr)
        
        self.serial_port.write((command + '\r\n').encode('utf-8'))
        self.serial_port.flush()  # Force send immediately
        
        responses = self._wait_for_response(timeout)
        
        return responses
    
    def _open_serial(self):
        """Open serial connection to modem"""
        # If using external serial object, it should already be managed by the caller
        # Don't attempt to open/configure it as this can cause I/O errors on some modems (FM350-GL)
        if self._external_serial:
            if not self.serial_port:
                print("\033[31mExternal serial port is None\033[0m", file=sys.stderr)
                sys.exit(1)
            if not self.serial_port.is_open:
                print("\033[33mWarning: External serial port is not open\033[0m", file=sys.stderr)
            # For external serial ports, trust they're already configured - do nothing
            return
        # Original behavior for device path strings
        try:
            print(f"\033[32mEstablishing serial port: {self.device}\033[0m", file=sys.stderr)
            self.serial_port = serial.Serial(
                port=self.device,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=15,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False
            )
        except Exception as e:
            print(f"\033[31mFailed to open {self.device}: {e}\033[0m", file=sys.stderr)
            sys.exit(1)
    
    def _close_serial(self):
        """Close serial connection"""
        # Don't close external serial objects - let the caller manage them
        if self._external_serial:
            return
        
        # Original behavior for internally created serial connections
        if self.serial_port and self.serial_port.is_open:
            print("\033[32mClosing serial port...\033[0m", file=sys.stderr)
            self.serial_port.close()
    
    def _send_command(self, command: str) -> str:
        """Send AT command and return response"""
        if not self.serial_port:
            raise RuntimeError("Serial port not open")

        # Clear buffers to prevent sync issues (skip if external port doesn't support it - FM350-GL)
        try:
            self.serial_port.reset_input_buffer()
            self.serial_port.reset_output_buffer()
        except (OSError, IOError, AttributeError):
            # External serial ports may not support buffer reset - continue anyway
            pass
        
        # Debug output for sent command with blue background
        if self.debug:
            print(f"\033[44m[SEND] {command}\033[0m", file=sys.stderr)
        
        self.serial_port.write((command + '\r\n').encode('utf-8'))
        self.serial_port.flush()  # Force send immediately

        response_lines = []
        while True:
            line = self.serial_port.readline().decode(errors='ignore').strip()
            if not line:
                continue
            
            # Debug output for received response with yellow background
            if self.debug:
                print(f"\033[43m[RECV] {line}\033[0m", file=sys.stderr)
            
            if line in ('OK', 'ERROR') or line.startswith('+CMS ERROR:') or line.startswith('+CME ERROR:'):
                response_lines.append(line)
                break
            response_lines.append(line)
        
        return '\n'.join(response_lines)
    
    def _wait_for_response(self, timeout: int = 5) -> List[str]:
        """Wait for response lines from modem"""
        responses = []
        
        # Use signal timeout only when running standalone
        if __name__ == '__main__':
            signal.alarm(timeout)
            try:
                while True:
                    line = self.serial_port.readline().decode(errors='ignore').strip()
                    if not line:
                        continue
                    
                    # Debug output for received response with yellow background
                    if self.debug:
                        print(f"\033[43m[RECV] {line}\033[0m", file=sys.stderr)
                    
                    responses.append(line)
                    if line in ('OK', 'ERROR') or line.startswith('+CMS ERROR:') or line.startswith('+CME ERROR:'):
                        break
            finally:
                signal.alarm(0)
        else:
            # When imported as module, use serial timeout instead of signals
            original_timeout = self.serial_port.timeout

            # For external serial ports, DON'T modify timeout - it causes I/O errors on FM350-GL
            # External ports are pre-configured with appropriate timeout (15s)
            timeout_was_modified = False
            if not self._external_serial:
                try:
                    self.serial_port.timeout = max(timeout, original_timeout)
                    timeout_was_modified = True
                except (OSError, IOError) as e:
                    if self.debug:
                        print(f"\033[33mWarning: Could not adjust serial timeout: {e}\033[0m", file=sys.stderr)

            try:
                command_echoed = False
                expected_echo = getattr(self, '_last_command', '').strip()
                consecutive_empty = 0
                
                while True:
                    line = self.serial_port.readline().decode(errors='ignore').strip()
                    if not line:
                        consecutive_empty += 1
                        # If we get too many empty lines in a row AND we have received some responses, break
                        # Increase threshold to 10 for modems with many SMS messages (like FM350-GL)
                        if consecutive_empty >= 10 and len(responses) > 0:
                            if self.debug:
                                print(f"\033[33mBreaking after {consecutive_empty} empty lines with {len(responses)} responses\033[0m", file=sys.stderr)
                            break
                        continue

                    consecutive_empty = 0

                    # Debug output for received response with yellow background
                    if self.debug:
                        print(f"\033[43m[RECV] {line}\033[0m", file=sys.stderr)

                    responses.append(line)

                    # Check if this is the command echo
                    if not command_echoed and expected_echo and line == expected_echo:
                        command_echoed = True
                        if self.debug:
                            print(f"Command echo confirmed: {line}", file=sys.stderr)
                        continue

                    # Check for completion responses
                    if line in ('OK', 'ERROR') or line.startswith('+CMS ERROR:') or line.startswith('+CME ERROR:'):
                        if not command_echoed and expected_echo:
                            if self.debug:
                                print(f"WARNING: Got completion '{line}' without echo for '{expected_echo}'", file=sys.stderr)
                        break
            finally:
                # Only restore timeout if we modified it (skip for external serial ports)
                if timeout_was_modified:
                    try:
                        self.serial_port.timeout = original_timeout
                    except (OSError, IOError) as e:
                        if self.debug:
                            print(f"\033[33mWarning: Could not restore serial timeout: {e}\033[0m", file=sys.stderr)
        
        return responses
    
    def send_sms(self, phone: str, message: str, json_output: bool = False) -> bool:
        """Send SMS message using PDU mode for all numbers (supports GSM 7-bit and UCS2)"""
        if self.debug:
            print(f"Using PDU mode for phone: {phone}", file=sys.stderr)

        self._open_serial()
        try:
            # Always use PDU mode - more reliable than text mode on FM350-GL
            success, status_info = self._send_sms_pdu_mode(phone, message, json_output)
            
            # Output JSON status if requested
            if json_output:
                import json
                print(json.dumps(status_info, indent=2))
            
            return success
            
        finally:
            self._close_serial()

    def _send_sms_pdu_mode(self, phone: str, message: str, json_output: bool = False) -> tuple[bool, dict]:
        """Send SMS using PDU mode (CMGF=0), supports both GSM 7-bit and UCS2 encoding"""
        # Auto-detect if UCS2 is needed (non-ASCII characters present)
        needs_ucs2 = any(ord(c) > 127 for c in message)

        if self.debug:
            encoding_str = "UCS2" if needs_ucs2 else "GSM 7-bit"
            print(f"Using PDU mode (CMGF=0) with {encoding_str} encoding", file=sys.stderr)

        # Initialize status info
        status_info = {
            "success": False,
            "method": "PDU",
            "encoding": "UCS2" if needs_ucs2 else "GSM 7-bit",
            "phone": phone,
            "message": message,
            "error": None,
            "message_id": None
        }

        if needs_ucs2:
            # UCS2: max 70 characters (140 bytes in UTF-16BE)
            utf16_bytes = message.encode('utf-16be')
            if len(utf16_bytes) > 140:
                status_info["error"] = f"SMS message too long: {len(utf16_bytes)} bytes (max 140 bytes for UCS2)"
                if not json_output:
                    print(f"\033[31m{status_info['error']}\033[0m", file=sys.stderr)
                return False, status_info
        else:
            # GSM 7-bit: max 160 characters
            if len(message) > 160:
                status_info["error"] = f"SMS message too long: {len(message)} chars (max 160 for GSM 7-bit)"
                if not json_output:
                    print(f"\033[31m{status_info['error']}\033[0m", file=sys.stderr)
                return False, status_info

        # Set PDU mode
        self._send_command("AT+CMGF=0")

        # Generate PDU with appropriate encoding
        try:
            if needs_ucs2:
                pdu_hex = PDUDecoder.encode_pdu_ucs2(phone, message)
            else:
                pdu_hex = PDUDecoder.encode_pdu(phone, message)
            if self.debug:
                print(f"Generated PDU ({status_info['encoding']}): {pdu_hex}", file=sys.stderr)
        except Exception as e:
            status_info["error"] = f"Failed to encode PDU: {e}"
            if not json_output:
                print(f"\033[31mFailed to encode PDU: {e}\033[0m", file=sys.stderr)
            return False, status_info
        
        # Calculate PDU length (excluding SMSC length byte and SMSC data)
        pdu_bytes = bytes.fromhex(pdu_hex)
        smsc_length = pdu_bytes[0]  # First byte is SMSC length
        pdu_length = len(pdu_bytes) - 1 - smsc_length  # Exclude SMSC length byte and SMSC data
        
        # Send CMGS command with PDU length
        cmd = f'AT+CMGS={pdu_length}'
        if self.debug:
            print(f"\033[44m[SEND] {cmd}\033[0m", file=sys.stderr)
        self.serial_port.write((cmd + '\r\n').encode('utf-8'))
        self.serial_port.flush()

        # Wait for '>' prompt from modem before sending PDU data (FM350-GL requirement)
        prompt_received = False
        prompt_timeout = time.time() + 10  # 10 second timeout for prompt
        while time.time() < prompt_timeout:
            line = self.serial_port.readline().decode(errors='ignore').strip()
            if self.debug and line:
                print(f"\033[43m[RECV] {line}\033[0m", file=sys.stderr)
            if line == '>':
                prompt_received = True
                break
            elif '+CMS ERROR:' in line or line == 'ERROR':
                status_info["error"] = f"Modem rejected CMGS command: {line}"
                if not json_output:
                    print(f"\033[31mModem rejected CMGS command: {line}\033[0m", file=sys.stderr)
                return False, status_info

        if not prompt_received:
            status_info["error"] = "Modem did not send '>' prompt for PDU input"
            if not json_output:
                print(f"\033[31mTimeout waiting for '>' prompt from modem\033[0m", file=sys.stderr)
            return False, status_info

        # Send PDU data with Ctrl-Z
        if self.debug:
            print(f"\033[44m[SEND] {pdu_hex}<CTRL-Z>\033[0m", file=sys.stderr)
        self.serial_port.write((pdu_hex + '\x1A').encode('utf-8'))
        self.serial_port.flush()  # Ensure PDU data is transmitted immediately

        # Wait for response with longer timeout for SMS sending (can take time on network)
        responses = self._wait_for_response(timeout=30)
        
        sms_sent = False
        message_id = None
        for response in responses:
            if response.startswith('+CMGS:'):
                message_id = response[7:].strip()
                status_info["message_id"] = message_id
                if not json_output:
                    print(f"\033[32mSMS sent successfully via PDU mode: {message_id}\033[0m", file=sys.stderr)
                sms_sent = True
            elif response.startswith('+CMS ERROR:'):
                status_info["error"] = f"SMS not sent, error code: {response[11:]}"
                if not json_output:
                    print(f"\033[31mSMS not sent, error code: {response[11:]}\033[0m", file=sys.stderr)
                return False, status_info
            elif response == 'ERROR':
                status_info["error"] = "SMS not sent, command error"
                if not json_output:
                    print("\033[31mSMS not sent, command error\033[0m", file=sys.stderr)
                return False, status_info
            elif response == 'OK' and sms_sent:
                # Final OK confirmation after successful SMS
                status_info["success"] = True
                return True, status_info
        
        status_info["success"] = sms_sent
        return sms_sent, status_info

    def _send_sms_text_mode(self, phone: str, message: str, json_output: bool = False) -> tuple[bool, dict]:
        """Send SMS using text mode (CMGF=1) with UCS2 or GSM encoding"""
        # Initialize status info
        status_info = {
            "success": False,
            "method": "Text",
            "encoding": None,
            "phone": phone,
            "message": message,
            "error": None,
            "message_id": None
        }
        
        # Auto-detect encoding based on manual GSM mode setting
        use_gsm = self.gsm_mode
        status_info["encoding"] = "GSM 7-bit" if use_gsm else "UCS2"
        
        if self.debug:
            encoding_reason = "manual GSM mode" if self.gsm_mode else "default UCS2"
            print(f"Using {'GSM 7-bit' if use_gsm else 'UCS2'} encoding ({encoding_reason})", file=sys.stderr)
        
        if use_gsm:
            # GSM mode - ASCII only, 160 character limit
            if len(message) > 160:
                status_info["error"] = f"SMS message too long: {len(message)} chars (max 160 for GSM)"
                if not json_output:
                    print(f"\033[31mSMS message too long: {len(message)} chars (max 160 for GSM)\033[0m", file=sys.stderr)
                return False, status_info
            
            # Check for non-ASCII characters
            if any(ord(c) > 127 for c in message):
                print("\033[33mWarning: Non-ASCII characters detected in GSM mode, may not display correctly\033[0m", file=sys.stderr)
        else:
            # UCS2 mode - check message length for UCS2 encoding
            try:
                utf16_bytes = message.encode('utf-16be')
                # SMS limit is 140 bytes for UCS2 (70 UTF-16 characters)
                if len(utf16_bytes) > 140:
                    status_info["error"] = f"SMS message too long: {len(utf16_bytes)} bytes (max 140 bytes for UCS2)"
                    if not json_output:
                        print(f"\033[31mSMS message too long: {len(utf16_bytes)} bytes (max 140 bytes for UCS2)\033[0m", file=sys.stderr)
                    return False, status_info
            except UnicodeEncodeError:
                status_info["error"] = f"Cannot encode message: {message}"
                if not json_output:
                    print(f"\033[31mCannot encode message: {message}\033[0m", file=sys.stderr)
                return False, status_info
        
        # Use text mode
        self._send_command("AT+CMGF=1")
        
        if use_gsm:
            # Set character set to GSM
            self._send_command('AT+CSCS="GSM"')
            
            # Send CMGS command with phone number
            cmd = f'AT+CMGS="{phone}"'
            if self.debug:
                print(f"\033[44m[SEND] {cmd}\033[0m", file=sys.stderr)
            self.serial_port.write((cmd + '\r\n').encode('utf-8'))
            self.serial_port.flush()

            # Wait for '>' prompt from modem before sending message data (FM350-GL requirement)
            prompt_received = False
            prompt_timeout = time.time() + 10  # 10 second timeout for prompt
            while time.time() < prompt_timeout:
                line = self.serial_port.readline().decode(errors='ignore').strip()
                if self.debug and line:
                    print(f"\033[43m[RECV] {line}\033[0m", file=sys.stderr)
                if line == '>':
                    prompt_received = True
                    break
                elif '+CMS ERROR:' in line or line == 'ERROR':
                    status_info["error"] = f"Modem rejected CMGS command: {line}"
                    if not json_output:
                        print(f"\033[31mModem rejected CMGS command: {line}\033[0m", file=sys.stderr)
                    return False, status_info

            if not prompt_received:
                status_info["error"] = "Modem did not send '>' prompt for message input"
                if not json_output:
                    print(f"\033[31mTimeout waiting for '>' prompt from modem\033[0m", file=sys.stderr)
                return False, status_info

            # Send message with Ctrl-Z
            if self.debug:
                print(f"\033[44m[SEND] {message}<CTRL-Z>\033[0m", file=sys.stderr)
            self.serial_port.write((message + '\x1A').encode('utf-8'))
            self.serial_port.flush()  # Ensure message data is transmitted immediately
        else:
            # Set character set to UCS2
            self._send_command('AT+CSCS="UCS2"')
            
            # Set SMS parameters for UCS2 (DCS=8 means UCS2 encoding per 3GPP TS 23.038)
            self._send_command('AT+CSMP=17,167,0,8')

            # Encode phone number to UCS2
            phone_ucs2 = ''.join(f'{ord(c):04X}' for c in phone)
            
            # Send CMGS command with plain phone number (don't UCS2 encode phone numbers)
            cmd = f'AT+CMGS="{phone_ucs2}"'
            if self.debug:
                print(f"\033[44m[SEND] {cmd}\033[0m", file=sys.stderr)
            self.serial_port.write((cmd + '\r\n').encode('utf-8'))
            self.serial_port.flush()

            # Wait for '>' prompt from modem before sending message data (FM350-GL requirement)
            prompt_received = False
            prompt_timeout = time.time() + 10  # 10 second timeout for prompt
            while time.time() < prompt_timeout:
                line = self.serial_port.readline().decode(errors='ignore').strip()
                if self.debug and line:
                    print(f"\033[43m[RECV] {line}\033[0m", file=sys.stderr)
                if line == '>':
                    prompt_received = True
                    break
                elif '+CMS ERROR:' in line or line == 'ERROR':
                    status_info["error"] = f"Modem rejected CMGS command: {line}"
                    if not json_output:
                        print(f"\033[31mModem rejected CMGS command: {line}\033[0m", file=sys.stderr)
                    return False, status_info

            if not prompt_received:
                status_info["error"] = "Modem did not send '>' prompt for message input"
                if not json_output:
                    print(f"\033[31mTimeout waiting for '>' prompt from modem\033[0m", file=sys.stderr)
                return False, status_info

            # Encode message to UCS2/UTF-16BE (handle emojis with surrogate pairs)
            try:
                # Encode to UTF-16BE and convert to hex string
                utf16_bytes = message.encode('utf-16be')
                message_ucs2 = utf16_bytes.hex().upper()
            except UnicodeEncodeError:
                status_info["error"] = f"Cannot encode message to UCS2: {message}"
                if not json_output:
                    print(f"\033[31mCannot encode message to UCS2: {message}\033[0m", file=sys.stderr)
                return False, status_info
            
            # Send UCS2 encoded message with Ctrl-Z
            if self.debug:
                print(f"\033[44m[SEND] {message_ucs2}<CTRL-Z>\033[0m", file=sys.stderr)
            self.serial_port.write((message_ucs2 + '\x1A').encode('utf-8'))
            self.serial_port.flush()  # Ensure message data is transmitted immediately

        # Wait for response with longer timeout for SMS sending (can take time on network)
        responses = self._wait_for_response(timeout=30)
        
        sms_sent = False
        message_id = None
        for response in responses:
            if response.startswith('+CMGS:'):
                message_id = response[7:].strip()
                status_info["message_id"] = message_id
                if not json_output:
                    print(f"\033[32mSMS sent successfully: {message_id}\033[0m", file=sys.stderr)
                sms_sent = True
            elif response.startswith('+CMS ERROR:'):
                status_info["error"] = f"SMS not sent, error code: {response[11:]}"
                if not json_output:
                    print(f"\033[31mSMS not sent, error code: {response[11:]}\033[0m", file=sys.stderr)
                return False, status_info
            elif response == 'ERROR':
                status_info["error"] = "SMS not sent, command error"
                if not json_output:
                    print("\033[31mSMS not sent, command error\033[0m", file=sys.stderr)
                return False, status_info
            elif response == 'OK' and sms_sent:
                # Final OK confirmation after successful SMS
                status_info["success"] = True
                return True, status_info
        
        # If we got here and SMS was marked as sent, return True
        # Otherwise return False
        status_info["success"] = sms_sent
        return sms_sent, status_info
    
    def _get_messages_from_storage(self, storage_type: str, delete_after: bool = False) -> List[Dict]:
        """Internal method to get messages from specific storage"""
        # Set storage
        self._send_command(f'AT+CPMS="{storage_type}"')

        # Set PDU mode
        self._send_command("AT+CMGF=0")

        # List all messages
        cmd = "AT+CMGL=4"
        if self.debug:
            print(f"\033[44m[SEND] {cmd}\033[0m", file=sys.stderr)
        self.serial_port.write((cmd + "\r\n").encode())

        messages = []
        current_msg = None

        responses = self._wait_for_response(10)

        print(f"\033[33mDEBUG: Processing {len(responses)} responses from {storage_type}\033[0m", file=sys.stderr)

        for i, response in enumerate(responses):
            print(f"\033[33mDEBUG: Response {i}: {response[:80]}{'...' if len(response) > 80 else ''}\033[0m", file=sys.stderr)

            if response.startswith('+CMGL:'):
                # Parse message header
                match = re.match(r'\+CMGL:\s*(\d+),', response)
                if match:
                    if current_msg:
                        print(f"\033[32mDEBUG: Appending message index {current_msg['index']} with PDU: {current_msg.get('pdu', 'NONE')[:40]}...\033[0m", file=sys.stderr)
                        messages.append(current_msg)
                    current_msg = {'index': int(match.group(1)), 'storage': storage_type}
                    print(f"\033[32mDEBUG: Starting new message index {current_msg['index']}\033[0m", file=sys.stderr)
            elif current_msg and response and not response in ('OK', 'ERROR'):
                # This should be the PDU data
                print(f"\033[32mDEBUG: Setting PDU for message index {current_msg['index']}: {response[:40]}...\033[0m", file=sys.stderr)
                current_msg['pdu'] = response

        if current_msg:
            print(f"\033[32mDEBUG: Appending final message index {current_msg['index']} with PDU: {current_msg.get('pdu', 'NONE')[:40]}...\033[0m", file=sys.stderr)
            messages.append(current_msg)

        print(f"\033[33mDEBUG: Found {len(messages)} messages from {storage_type} before decoding\033[0m", file=sys.stderr)

        # Decode all messages
        for msg in messages:
            print(f"\033[33mDEBUG: Decoding message index {msg['index']} with PDU length {len(msg.get('pdu', ''))}\033[0m", file=sys.stderr)
            msg['decoded'] = PDUDecoder.decode_pdu(msg['pdu'])
            print(f"\033[33mDEBUG: Decoded result: sender={msg['decoded'].get('sender')}, message={msg['decoded'].get('message', '')[:40]}...\033[0m", file=sys.stderr)
        
        # Delete messages if requested
        if delete_after:
            print(f"Starting deletion of {len(messages)} messages from {storage_type} storage", file=sys.stderr)
            for msg in messages:
                cmd = f"AT+CMGD={msg['index']}"
                if self.debug:
                    print(f"\033[44m[SEND] {cmd}\033[0m", file=sys.stderr)
                self.serial_port.write((cmd + "\r\n").encode())
                
                # Check deletion response
                responses = self._wait_for_response()
                deletion_success = False
                for response in responses:
                    if response == 'OK':
                        deletion_success = True
                        if self.debug:
                            print(f"\033[32mSuccessfully deleted message {msg['index']} from {storage_type}\033[0m", file=sys.stderr)
                        break
                    elif response.startswith('+CMS ERROR:'):
                        if self.debug:
                            print(f"\033[31mError deleting message {msg['index']}: {response[11:]}\033[0m", file=sys.stderr)
                        break
                    elif response == 'ERROR':
                        if self.debug:
                            print(f"\033[31mError deleting message {msg['index']}: command error\033[0m", file=sys.stderr)
                        break
                
                if not deletion_success and self.debug:
                    print(f"\033[33mWarning: Failed to delete message {msg['index']} from {storage_type}\033[0m", file=sys.stderr)
        
        return messages

    def get_all_messages(self, storage_types: List[str] = None, delete_after: bool = False) -> List[Dict]:
        """Get all messages from specified storage types"""
        if storage_types is None:
            storage_types = ["SM", "ME"]
        
        all_messages = []
        self._open_serial()
        try:
            for storage_type in storage_types:
                try:
                    messages = self._get_messages_from_storage(storage_type, delete_after)
                    all_messages.extend(messages)
                except Exception as e:
                    if self.debug:
                        print(f"\033[31mError accessing {storage_type}: {e}\033[0m", file=sys.stderr)
            return all_messages
        finally:
            self._close_serial()

    def receive_sms(self, json_output: bool = False, raw_output: bool = False, storage_types: List[str] = None, delete_after: bool = False) -> bool:
        """Receive SMS messages"""
        if storage_types is None:
            storage_types = [self.storage] if self.storage else ["SM", "ME"]
        
        all_messages = self.get_all_messages(storage_types, delete_after)
        
        if json_output:
            print('{"msg":[', end='')
        
        for i, msg in enumerate(all_messages):
            if json_output and i > 0:
                print(',', end='')
            
            if json_output:
                print(f'{{"index":{msg["index"]},', end='')
            else:
                print(f"MSG: {msg['index']} ({msg['storage']})")
            
            if raw_output:
                if json_output:
                    print(f'"content":"{msg["pdu"]}"}}', end='')
                else:
                    print(msg['pdu'])
            else:
                # Use decoded data
                decoded = msg['decoded']
                if json_output:
                    sender = decoded['sender'].replace('"', '\\"')
                    timestamp = decoded['timestamp'].replace('"', '\\"')
                    content = decoded['message'].replace('"', '\\"')
                    
                    json_fields = [f'"sender":"{sender}"', f'"timestamp":"{timestamp}"', f'"content":"{content}"']
                    
                    # Add multipart SMS fields if present
                    if 'reference' in decoded:
                        json_fields.append(f'"reference":{decoded["reference"]}')
                    if 'part' in decoded:
                        json_fields.append(f'"part":{decoded["part"]}')
                    if 'total' in decoded:
                        json_fields.append(f'"total":{decoded["total"]}')
                    
                    print(','.join(json_fields) + '}', end='')
                else:
                    print(f"From: {decoded['sender']}")
                    print(f"Date/Time: {decoded['timestamp']}")
                    print(decoded['message'])
                    print()
        
        if json_output:
            print(']}')
        
        return True

    def receive_sms_assembled(self, json_output: bool = False, storage_types: List[str] = None, delete_after: bool = False) -> bool:
        """Receive SMS messages with multipart messages assembled"""
        all_messages = self.get_all_messages(storage_types, delete_after)
        
        # Group multipart messages by sender and reference
        multipart_groups = {}
        single_messages = []
        
        for msg in all_messages:
            decoded = msg['decoded']
            if 'reference' in decoded and 'part' in decoded and 'total' in decoded:
                # This is part of a multipart message
                key = (decoded['sender'], decoded['reference'])
                if key not in multipart_groups:
                    multipart_groups[key] = {}
                multipart_groups[key][decoded['part']] = {
                    'content': decoded['message'],
                    'timestamp': decoded['timestamp'],
                    'total': decoded['total'],
                    'index': msg['index'],
                    'storage': msg['storage']
                }
            else:
                # Single message
                single_messages.append(msg)
        
        # Assemble multipart messages
        assembled_messages = []
        
        # Add single messages
        for msg in single_messages:
            assembled_messages.append({
                'type': 'single',
                'index': msg['index'],
                'storage': msg['storage'],
                'sender': msg['decoded']['sender'],
                'timestamp': msg['decoded']['timestamp'],
                'content': msg['decoded']['message']
            })
        
        # Add assembled multipart messages
        for (sender, reference), parts in multipart_groups.items():
            if not parts:
                continue
                
            # Get total parts expected
            total_parts = next(iter(parts.values()))['total']
            
            # Check if we have all parts
            if len(parts) == total_parts:
                # Assemble message in correct order
                assembled_content = ""
                earliest_timestamp = None
                indices = []
                storages = []
                
                for part_num in range(1, total_parts + 1):
                    if part_num in parts:
                        part_data = parts[part_num]
                        assembled_content += part_data['content']
                        indices.append(part_data['index'])
                        storages.append(part_data['storage'])
                        
                        # Use earliest timestamp
                        if earliest_timestamp is None or part_data['timestamp'] < earliest_timestamp:
                            earliest_timestamp = part_data['timestamp']
                
                assembled_messages.append({
                    'type': 'multipart',
                    'indices': indices,
                    'storages': storages,
                    'sender': sender,
                    'timestamp': earliest_timestamp,
                    'content': assembled_content,
                    'reference': reference,
                    'total_parts': total_parts
                })
            else:
                # Incomplete multipart message - add individual parts
                for part_num, part_data in parts.items():
                    assembled_messages.append({
                        'type': 'incomplete_multipart',
                        'index': part_data['index'],
                        'storage': part_data['storage'],
                        'sender': sender,
                        'timestamp': part_data['timestamp'],
                        'content': part_data['content'],
                        'reference': reference,
                        'part': part_num,
                        'total_parts': part_data['total']
                    })
        
        # Sort by timestamp
        assembled_messages.sort(key=lambda x: x['timestamp'])
        
        # Output results
        if json_output:
            print('{"msg":[', end='')
        
        for i, msg in enumerate(assembled_messages):
            if json_output and i > 0:
                print(',', end='')
            
            if json_output:
                sender = msg['sender'].replace('"', '\\"')
                timestamp = msg['timestamp'].replace('"', '\\"')
                content = msg['content'].replace('"', '\\"')
                
                if msg['type'] == 'single':
                    print(f'{{"type":"single","index":{msg["index"]},"sender":"{sender}","timestamp":"{timestamp}","content":"{content}"}}', end='')
                elif msg['type'] == 'multipart':
                    indices_str = ','.join(map(str, msg['indices']))
                    print(f'{{"type":"multipart","indices":[{indices_str}],"sender":"{sender}","timestamp":"{timestamp}","content":"{content}","reference":{msg["reference"]},"total_parts":{msg["total_parts"]}}}', end='')
                else:  # incomplete_multipart
                    print(f'{{"type":"incomplete","index":{msg["index"]},"sender":"{sender}","timestamp":"{timestamp}","content":"{content}","reference":{msg["reference"]},"part":{msg["part"]},"total_parts":{msg["total_parts"]}}}', end='')
            else:
                if msg['type'] == 'single':
                    print(f"MSG: {msg['index']} ({msg['storage']})")
                elif msg['type'] == 'multipart':
                    indices_str = ','.join(map(str, msg['indices']))
                    print(f"MSG: [{indices_str}] (Assembled from {msg['total_parts']} parts)")
                else:  # incomplete_multipart
                    print(f"MSG: {msg['index']} ({msg['storage']}) [Incomplete: part {msg['part']}/{msg['total_parts']}]")
                
                print(f"From: {msg['sender']}")
                print(f"Date/Time: {msg['timestamp']}")
                print(msg['content'])
                print()
        
        if json_output:
            print(']}')
        
        return True
    
    def delete_sms(self, index: str) -> bool:
        """Delete SMS message(s)"""
        self._open_serial()
        try:
            if index.lower() == 'all':
                # Try efficient bulk delete first (delete all read messages)
                print("Attempting bulk delete of all messages")
                cmd = "AT+CMGD=1,4"  # Delete all messages regardless of status
                if self.debug:
                    print(f"\033[44m[SEND] {cmd}\033[0m", file=sys.stderr)
                self.serial_port.write((cmd + "\r\n").encode())
                responses = self._wait_for_response()
                
                bulk_delete_success = False
                for response in responses:
                    if response == 'OK':
                        print("\033[32mBulk delete successful\033[0m")
                        bulk_delete_success = True
                        break
                    elif response.startswith('+CMS ERROR:'):
                        print(f"\033[33mBulk delete failed: {response[12:]}, falling back to individual delete\033[0m")
                        break
                
                if bulk_delete_success:
                    return True
                    
                # Fallback to individual deletion if bulk delete fails
                print("Falling back to individual message deletion")
                start_idx, end_idx = 0, 49
                print(f"Delete msg from {start_idx} to {end_idx}")
            else:
                start_idx = end_idx = int(index)
                print(f"Delete msg from {start_idx} to {end_idx}")
            
            deleted_count = 0
            for i in range(start_idx, end_idx + 1):
                cmd = f"AT+CMGD={i}"
                if self.debug:
                    print(f"\033[44m[SEND] {cmd}\033[0m", file=sys.stderr)
                self.serial_port.write((cmd + "\r\n").encode())
                responses = self._wait_for_response()
                
                for response in responses:
                    if response == 'OK':
                        deleted_count += 1
                        if self.debug:
                            print(f"Deleted message {i}")
                        break
                    elif response.startswith('+CMS ERROR:'):
                        if self.debug:
                            print(f"Error deleting message {i}: {response[12:]}")
                        break
            
            if index.lower() == 'all':
                print(f"Individual deletion completed: {deleted_count} messages deleted")
            
            return True
            
        finally:
            self._close_serial()
    
    def get_status(self) -> bool:
        """Get SMS storage status"""
        self._open_serial()
        try:
            # Set storage if specified
            if self.storage:
                self._send_command(f'AT+CPMS="{self.storage}"')
            
            # Query storage status
            cmd = "AT+CPMS?"
            if self.debug:
                print(f"\033[44m[SEND] {cmd}\033[0m", file=sys.stderr)
            self.serial_port.write((cmd + "\r\n").encode())
            responses = self._wait_for_response()
            
            for response in responses:
                if response.startswith('+CPMS:'):
                    # Parse storage info
                    match = re.match(r'\+CPMS:\s*"([^"]+)",(\d+),(\d+)', response)
                    if match:
                        storage_type, used, total = match.groups()
                        print(f"Storage type: {storage_type}, used: {used}, total: {total}")
                        return True
            
            return False
            
        finally:
            self._close_serial()
    
    def send_ussd(self, code: str, raw_input: bool = False, raw_output: bool = False) -> bool:
        """Send USSD code"""
        self._open_serial()
        try:
            if raw_input:
                command = f'AT+CUSD=1,"{code}",15'
            else:
                # Encode to PDU (simplified)
                encoded_code = code.encode().hex().upper()
                command = f'AT+CUSD=1,"{encoded_code}",15'
            
            if self.debug:
                print(f"\033[44m[SEND] {command}\033[0m", file=sys.stderr)
            
            self.serial_port.write((command + '\r\n').encode('utf-8'))
            responses = self._wait_for_response(10)
            
            for response in responses:
                if response.startswith('+CME ERROR:'):
                    print(f"\033[31mError: {response[12:]}\033[0m", file=sys.stderr)
                    return False
                elif response.startswith('+CUSD:'):
                    # Parse USSD response (simplified)
                    if raw_output:
                        match = re.search(r'"([^"]*)"', response)
                        if match:
                            print(match.group(1))
                    else:
                        print("[USSD response decoding not fully implemented]")
                    return True
            
            return False
            
        finally:
            self._close_serial()
    
    def send_at_command(self, command: str) -> bool:
        """Send raw AT command"""
        self._open_serial()
        try:
            # Clear buffers to prevent sync issues (skip if external port doesn't support it - FM350-GL)
            try:
                self.serial_port.reset_input_buffer()
                self.serial_port.reset_output_buffer()
            except (OSError, IOError, AttributeError):
                # External serial ports may not support buffer reset - continue anyway
                pass
            
            # Store command for echo tracking
            self._last_command = command
            
            if self.debug:
                print(f"\033[44m[SEND] {command}\033[0m", file=sys.stderr)
            self.serial_port.write((command + '\r\n').encode('utf-8'))
            self.serial_port.flush()  # Force send immediately
            
  
            responses = self._wait_for_response()
            
            for response in responses:
                if response == 'OK':
                    if self.debug:
                        print(response)
                    return True
                elif response in ('ERROR', 'COMMAND NOT SUPPORT') or response.startswith('+CME ERROR:'):
                    if self.debug:
                        print(response)
                    return False
                else:
                    print(response)
            
            return False
            
        finally:
            self._close_serial()
    
    def reset_modem(self) -> bool:
        """Reset RM520N-GL modem to factory settings"""
        self._open_serial()
        try:
            # Send factory reset command for RM520N-GL
            cmd = 'AT+QCFG="ResetFactory"'
            if self.debug:
                print(f"\033[44m[SEND] {cmd}\033[0m", file=sys.stderr)
            self.serial_port.write((cmd + '\r\n').encode('utf-8'))
            responses = self._wait_for_response(30)  # Longer timeout for reset
            
            for response in responses:
                if response == 'OK':
                    print("\033[32mModem reset successful\033[0m")
                    return True
                elif response in ('ERROR', 'COMMAND NOT SUPPORT') or response.startswith('+CME ERROR:'):
                    print(f"\033[31mModem reset failed: {response}\033[0m", file=sys.stderr)
                    return False
                else:
                    if self.debug:
                        print(response)
            
            return False
            
        finally:
            self._close_serial()

    def make_voice_call(self, phone: str, timeout: int = 30) -> bool:
        """Make a voice call to the specified phone number"""
        self._open_serial()
        try:
            if self.debug:
                print(f"Making voice call to {phone}")
            
            # Make voice call using ATD command
            cmd = f'ATD{phone};'
            if self.debug:
                print(f"\033[44m[SEND] {cmd}\033[0m", file=sys.stderr)
            
            self.serial_port.write((cmd + '\r\n').encode('utf-8'))
            responses = self._wait_for_response(timeout)
            
            call_initiated = False
            for response in responses:
                if response == 'OK':
                    print(f"\033[32mCall initiated to {phone}\033[0m")
                    call_initiated = True
                elif response.startswith('+CME ERROR:'):
                    print(f"\033[31mCall failed: {response[12:]}\033[0m", file=sys.stderr)
                    return False
                elif response == 'ERROR':
                    print(f"\033[31mCall failed: command error\033[0m", file=sys.stderr)
                    return False
                elif response in ('BUSY', 'NO CARRIER', 'NO ANSWER'):
                    print(f"\033[33mCall status: {response}\033[0m")
                    return True  # Call was attempted but not answered
                elif response == 'CONNECT':
                    print(f"\033[32mCall connected to {phone}\033[0m")
                    return True
                else:
                    if self.debug:
                        print(f"Call response: {response}")
            
            return call_initiated
            
        finally:
            self._close_serial()
    
    def hangup_call(self) -> bool:
        """Hang up current voice call"""
        self._open_serial()
        try:
            # Send hangup command
            cmd = 'ATH'
            if self.debug:
                print(f"\033[44m[SEND] {cmd}\033[0m", file=sys.stderr)
            
            self.serial_port.write((cmd + '\r\n').encode('utf-8'))
            responses = self._wait_for_response()
            
            for response in responses:
                if response == 'OK':
                    print("\033[32mCall hung up successfully\033[0m")
                    return True
                elif response.startswith('+CME ERROR:'):
                    print(f"\033[31mHangup failed: {response[12:]}\033[0m", file=sys.stderr)
                    return False
                elif response == 'ERROR':
                    print(f"\033[31mHangup failed: command error\033[0m", file=sys.stderr)
                    return False
                else:
                    if self.debug:
                        print(f"Hangup response: {response}")
            
            return False
            
        finally:
            self._close_serial()
    
    def answer_call(self) -> bool:
        """Answer incoming voice call"""
        self._open_serial()
        try:
            # Answer call using ATA command
            cmd = 'ATA'
            if self.debug:
                print(f"\033[44m[SEND] {cmd}\033[0m", file=sys.stderr)
            
            self.serial_port.write((cmd + '\r\n').encode('utf-8'))
            responses = self._wait_for_response()
            
            for response in responses:
                if response == 'OK':
                    print("\033[32mCall answered successfully\033[0m")
                    return True
                elif response == 'CONNECT':
                    print("\033[32mCall connected\033[0m")
                    return True
                elif response.startswith('+CME ERROR:'):
                    print(f"\033[31mAnswer failed: {response[12:]}\033[0m", file=sys.stderr)
                    return False
                elif response == 'ERROR':
                    print(f"\033[31mAnswer failed: command error\033[0m", file=sys.stderr)
                    return False
                elif response == 'NO CARRIER':
                    print(f"\033[33mCall disconnected\033[0m")
                    return True
                else:
                    if self.debug:
                        print(f"Answer response: {response}")
            
            return False
            
        finally:
            self._close_serial()
    
    def get_call_status(self) -> bool:
        """Get current call status using AT+CLCC"""
        self._open_serial()
        try:
            # Query call status
            cmd = 'AT+CLCC'
            if self.debug:
                print(f"\033[44m[SEND] {cmd}\033[0m", file=sys.stderr)
            
            self.serial_port.write((cmd + '\r\n').encode('utf-8'))
            responses = self._wait_for_response()
            
            calls_found = False
            for response in responses:
                if response.startswith('+CLCC:'):
                    # Parse call list: +CLCC: <id>,<dir>,<stat>,<mode>,<mpty>,"<number>",<type>,"<alpha>"
                    print(f"Call status: {response}")
                    calls_found = True
                elif response == 'OK':
                    if not calls_found:
                        print("No active calls")
                    return True
                elif response.startswith('+CME ERROR:'):
                    print(f"\033[31mStatus query failed: {response[12:]}\033[0m", file=sys.stderr)
                    return False
                elif response == 'ERROR':
                    print(f"\033[31mStatus query failed: command error\033[0m", file=sys.stderr)
                    return False
                else:
                    if self.debug:
                        print(f"Status response: {response}")
            
            return True
            
        finally:
            self._close_serial()
    
    def set_audio_path(self, path: int) -> bool:
        """Set audio path for voice calls (device specific)"""
        self._open_serial()
        try:
            # Set audio path - this is device specific, using common AT+QAUDPATH command
            cmd = f'AT+QAUDPATH={path}'
            if self.debug:
                print(f"\033[44m[SEND] {cmd}\033[0m", file=sys.stderr)
            
            self.serial_port.write((cmd + '\r\n').encode('utf-8'))
            responses = self._wait_for_response()
            
            for response in responses:
                if response == 'OK':
                    print(f"\033[32mAudio path set to {path}\033[0m")
                    return True
                elif response.startswith('+CME ERROR:'):
                    print(f"\033[31mAudio path setting failed: {response[12:]}\033[0m", file=sys.stderr)
                    return False
                elif response == 'ERROR':
                    print(f"\033[31mAudio path setting failed: command error\033[0m", file=sys.stderr)
                    return False
                else:
                    if self.debug:
                        print(f"Audio path response: {response}")
            
            return False
            
        finally:
            self._close_serial()
    
    def disable_echo(self) -> bool:
        """Disable command echo (ATE0)"""
        self._open_serial()
        try:
            # Disable echo
            cmd = 'ATE0'
            if self.debug:
                print(f"\033[44m[SEND] {cmd}\033[0m", file=sys.stderr)
            
            self.serial_port.write((cmd + '\r\n').encode('utf-8'))
            responses = self._wait_for_response()
            
            for response in responses:
                if response == 'OK':
                    print("\033[32mCommand echo disabled\033[0m")
                    return True
                elif response.startswith('+CME ERROR:'):
                    print(f"\033[31mEcho disable failed: {response[12:]}\033[0m", file=sys.stderr)
                    return False
                elif response == 'ERROR':
                    print(f"\033[31mEcho disable failed: command error\033[0m", file=sys.stderr)
                    return False
                else:
                    if self.debug:
                        print(f"Echo disable response: {response}")
            
            return False
            
        finally:
            self._close_serial()


def main():
    version = "1.0.1"
    parser = argparse.ArgumentParser(
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        usage=f'''Photonicat SMS CLI Tool v{version}
       %(prog)s [options] send phoneNumber message
       %(prog)s [options] recv
       %(prog)s [options] delete msg_index | all
       %(prog)s [options] status
       %(prog)s [options] ussd code
       %(prog)s [options] at command
       %(prog)s [options] call phoneNumber
       %(prog)s [options] answer
       %(prog)s [options] hangup
       %(prog)s [options] status-call
       %(prog)s [options] audio-path number
       %(prog)s [options] no-echo
       %(prog)s [options] reset
       
options:
        -b <baudrate> (default: 115200)
        -d <tty device> (default: /dev/ttyUSB3)
        -D debug (for ussd and at)
        -f <date/time format> (for sms/recv)
        -j json output (for sms/recv)
        -R use raw input (for ussd)
        -r use raw output (for ussd and sms/recv)
        -s <preferred storage> (for sms/recv/status)
        -A assemble multipart SMS messages
        -G use GSM character set instead of UCS2
        --delete-after delete messages after fetching
        --all-storage fetch from both SIM and modem storage
        -h, --help show this help message''')
    
    parser.add_argument('-b', '--baudrate', type=int, default=115200, help=argparse.SUPPRESS)
    parser.add_argument('-d', '--device', default='/dev/ttyUSB3', help=argparse.SUPPRESS)
    parser.add_argument('-D', '--debug', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('-f', '--dateformat', default='%m/%d/%y %H:%M:%S', help=argparse.SUPPRESS)
    parser.add_argument('-G', '--gsm-mode', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('-j', '--json', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('-R', '--raw-input', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('-r', '--raw-output', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('-s', '--storage', default='', help=argparse.SUPPRESS)
    parser.add_argument('-A', '--assemble', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--delete-after', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--all-storage', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('-h', '--help', action='help', help=argparse.SUPPRESS)
    
    parser.add_argument('command', nargs='?', help=argparse.SUPPRESS)
    parser.add_argument('args', nargs='*', help=argparse.SUPPRESS)
    
    args = parser.parse_args()
    
    # Show help if no command provided
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    # Validate command and arguments
    if args.command == 'send':
        if len(args.args) < 2:
            parser.error('send command requires phone number and message')
    elif args.command == 'delete':
        if len(args.args) < 1:
            parser.error('delete command requires message index or "all"')
    elif args.command in ('ussd', 'at', 'call', 'audio-path'):
        if len(args.args) < 1:
            parser.error(f'{args.command} command requires additional argument')
    
    # Create SMS tool instance
    sms_tool = SMSTool(
        device=args.device,
        baudrate=args.baudrate,
        debug=args.debug,
        storage=args.storage,
        dateformat=args.dateformat,
        gsm_mode=args.gsm_mode
    )
    
    # Execute command
    try:
        if args.command == 'send':
            phone, message = args.args[0], args.args[1]
            success = sms_tool.send_sms(phone, message, args.json)
        elif args.command == 'recv':
            storage_types = None
            if args.all_storage:
                storage_types = ["SM", "ME"]
            elif args.storage:
                storage_types = [args.storage]
            
            if args.assemble:
                success = sms_tool.receive_sms_assembled(args.json, storage_types, args.delete_after)
            else:
                success = sms_tool.receive_sms(args.json, args.raw_output, storage_types, args.delete_after)
        elif args.command == 'delete':
            success = sms_tool.delete_sms(args.args[0])
        elif args.command == 'status':
            success = sms_tool.get_status()
        elif args.command == 'ussd':
            success = sms_tool.send_ussd(args.args[0], args.raw_input, args.raw_output)
        elif args.command == 'at':
            success = sms_tool.send_at_command(args.args[0])
        elif args.command == 'call':
            success = sms_tool.make_voice_call(args.args[0])
        elif args.command == 'answer':
            success = sms_tool.answer_call()
        elif args.command == 'hangup':
            success = sms_tool.hangup_call()
        elif args.command == 'status-call':
            success = sms_tool.get_call_status()
        elif args.command == 'audio-path':
            success = sms_tool.set_audio_path(int(args.args[0]))
        elif args.command == 'no-echo':
            success = sms_tool.disable_echo()
        elif args.command == 'reset':
            success = sms_tool.reset_modem()
        else:
            parser.error(f'Unknown command: {args.command}')
        
        sys.exit(0 if success else 1)
        
    except KeyboardInterrupt:
        print("\033[31m\nOperation cancelled by user\033[0m", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"\033[31mError: {e}\033[0m", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()