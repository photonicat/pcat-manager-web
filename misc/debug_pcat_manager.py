import socket
import json

# Set the path for the Unix socket
socket_path = '/tmp/pcat-manager.sock'

# Create the Unix socket client
client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

# Connect to the server
client.connect(socket_path)

# Send a message to the server
cmd = { "command": "modem-network-get" }
cmd_str = json.dumps(cmd)
client.sendall(cmd_str.encode() + b'\0')

# Receive a response from the server
response = client.recv(1024)
print(f'Received response: {response.decode()}')

# Close the connection
client.close()