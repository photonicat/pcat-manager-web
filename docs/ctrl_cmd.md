# 控制命令

本程序作为WEB服务，通过 unix socket 与 pcat-manager 进行通信，发送控制命令。

## 通信方式

使用 Unix Domain Socket 进行通信，socket 文件路径为 `/tmp/pcat-manager.sock`。

### 通信示例
```python
#!/usr/bin/env python3
import os, socket
client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.connect("/tmp/pcat-manager.sock")
client.send(b'{"command":"pmu-status"}\0')
data = client.recv(1024)
print(data)
```

## 命令列表

### 1. 电源管理命令

#### 1.1 获取电源状态
- 命令: `pmu-status`
- 请求格式: `{"command":"pmu-status"}`
- 响应格式:
```json
{
  "command": "pmu-status",
  "code": 0,
  "battery-voltage": 4210,    // 电池电压(毫伏)
  "charger-voltage": 5010,    // 充电器电压(毫伏)
  "on-battery": 0,           // 0:充电中, 1:使用电池
  "charge-percentage": 10000, // 充电百分比(0-10000)
  "board-temperature": 35     // 板子温度(可选)
}
```

#### 1.2 获取固件版本
- 命令: `pmu-fw-version-get`
- 请求格式: `{"command":"pmu-fw-version-get"}`
- 响应格式:
```json
{
  "command": "pmu-fw-version-get",
  "code": 0,
  "version": "1.0.0"  // STM32固件版本号
}
```

### 2. 移动网络命令

#### 2.1 获取Modem状态
- 命令: `modem-status-get`
- 请求格式: `{"command":"modem-status-get"}`
- 响应格式:
```json
{
  "command": "modem-status-get",
  "code": 0,
  "mode": "lte",           // 网络模式: none/2g/3g/lte/5g
  "sim-state": "ready",    // SIM卡状态: absent/not-ready/ready/need-pin/need-puk/personalized-network/bad
  "isp-name": "",         // 运营商名称
  "isp-lpmn": "",         // 运营商编号
  "signal-strength": 0    // 信号强度
}
```

#### 2.2 设置飞行模式
- 命令: `modem-rfkill-mode-set`
- 请求格式: `{"command":"modem-rfkill-mode-set", "state": 1}`  // 1:开启, 0:关闭
- 响应格式:
```json
{
  "command": "modem-rfkill-mode-set",
  "code": 0
}
```

#### 2.3 获取APN设置
- 命令: `modem-network-get`
- 请求格式: `{"command":"modem-network-get"}`
- 响应格式:
```json
{
  "command": "modem-network-get",
  "code": 0,
  "device-type": "lte",    // 设备类型: none/lte/5g
  "apn": "",              // APN名称
  "user": "",             // 用户名
  "password": "",         // 密码
  "auth": ""              // 认证方式
}
```

#### 2.4 设置APN
- 命令: `modem-network-setup`
- 请求格式: `{"command":"modem-network-setup", "apn": "apn", "user": "user", "password": "pass", "auth": "auth"}`
- 响应格式:
```json
{
  "command": "modem-network-setup",
  "code": 0
}
```

### 3. 网络路由命令

#### 3.1 获取路由模式
- 命令: `network-route-mode-get`
- 请求格式: `{"command":"network-route-mode-get"}`
- 响应格式:
```json
{
  "command": "network-route-mode-get",
  "code": 0,
  "mode": "none"  // 路由模式: none/wired/mobile
}
```

### 4. 定时任务命令

#### 4.1 获取定时任务
- 命令: `schedule-power-event-get`
- 请求格式: `{"command":"schedule-power-event-get"}`
- 响应格式:
```json
{
  "command": "schedule-power-event-get",
  "code": 0,
  "event-list": [
    {
      "enabled": 1,        // 是否启用: 0/1
      "enable-bits": 56,   // 启用位: 0-7分别对应年/月/日/时/分/星期/动作
      "action": 0,         // 动作: 0=关机, 1=开机
      "year": 0,           // 年(可选)
      "month": 0,          // 月(可选)
      "day": 0,            // 日(可选)
      "hour": 23,          // 时
      "minute": 59,        // 分
      "dow-bits": 1        // 星期重复位: 0-6分别对应周日-周六
    }
  ]
}
```

#### 4.2 设置定时任务
- 命令: `schedule-power-event-set`
- 请求格式: `{"command":"schedule-power-event-set", "event-list": [...]}`
- 响应格式:
```json
{
  "command": "schedule-power-event-set",
  "code": 0
}
```

### 5. 车载模式命令

#### 5.1 获取车载模式状态
- 命令: `charger-on-auto-start-get`
- 请求格式: `{"command":"charger-on-auto-start-get"}`
- 响应格式:
```json
{
  "command": "charger-on-auto-start-get",
  "code": 0,
  "state": 0,           // 状态: 0=禁用, 1=启用
  "timeout": 0,         // 延时时间
  "countdown": 0        // 倒计时
}
```

#### 5.2 设置车载模式
- 命令: `charger-on-auto-start-set`
- 请求格式: `{"command":"charger-on-auto-start-set", "state": 1, "timeout": 30}`
- 响应格式:
```json
{
  "command": "charger-on-auto-start-set",
  "code": 0
}
```

## 注意事项

1. 所有命令都需要以 `\0` 结尾
2. 响应中的 `code` 字段表示命令执行状态，0 表示成功
3. 部分命令可能需要设备支持特定硬件功能
4. 建议在发送命令前检查 socket 连接状态
