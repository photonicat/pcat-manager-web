## WiFi 接口

GET /api/v2/wireless.json
```
{
    "interfaces": [
        {
            "exist": true,
            "hidden": "0",
            "device": "radio0",
            "band": "5g",
            "htmode": "VHT40",
            "enabled": false,
            "device_type": "\u677f\u8f7d",
            "ssid": "photonicat-f97da2f0",
            "encryption": "psk2",
            "password": "photonicat",
            "frequency": "5g"
        },
        {
            "exist": true,
            "hidden": "0",
            "device": "radio1",
            "band": "5g",
            "htmode": "HE80",
            "enabled": true,
            "device_type": "PCIE",
            "ssid": "OpenWrt",
            "encryption": "none",
            "frequency": "5g_ax"
        }
    ]
}
```

## 设备配置

获取配置信息的接口：GET /api/v1/network_interfaces.json

保存配置信息时的接口: POST /api/v1/network_interfaces.json

## 手动模式

    POST /api/v1/carbootmode.json with {'carmode': 'false', 'timeout': 0}

## 车载模式

    POST /api/v1/carbootmode.json with {'carmode': 'true', 'timeout': 600}

## 定时模式

    POST /api/v1/timers.json
    [{'id': 1699869698762, 'time': '01:00', 'action': 'startup', 'days': ['1']}, 
     {'id': 1699869709162, 'time': '23:00', 'action': 'shutdown', 'days': ['2', '3']}]