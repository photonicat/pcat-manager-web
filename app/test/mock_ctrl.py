import os
import socket
import time

server_address = '/tmp/pcat-manager.sock'

#########
# { "command": "pmu-status", "code": 0, "battery-voltage": 4126, "charger-voltage": 5061, "on-battery": 0, "charge-percentage": 9471 }
# { "command": "modem-status-get", "code": 0, "mode": "lte", "sim-state": "ready", "isp-name": "", "isp-lpmn": "" }
# { "command": "network-route-mode-get", "code": 0, "mode": "mobile" }   # wired, none
# { "command": "schedule-power-event-get", "code": 0, "event-list": [ ] }
#########
 
# 必须先删除
try:
    os.unlink(server_address)
except OSError:
    if os.path.exists(server_address):
        raise

# 指定协议
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(server_address)
# 监听
server.listen(5)
while True:
  print("Waiting unix client.")
  clientsocket, address = server.accept()
  print("A unix client connected.")
  # 接收消息
  while True:
    data = clientsocket.recv(1024)
    print("RECV:")
    print(data.decode())
    clientsocket.send("Heloo".encode())
    time.sleep(3)
