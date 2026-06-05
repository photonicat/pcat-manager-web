# OpenWrt 简化WEB界面项目设计文档

## 1. 项目概述
### 1.1 项目背景
为OpenWrt设备开发轻量级WEB管理界面，替代原生Luci的复杂配置流程，主要面向需要快速配置基础网络功能的终端用户。

### 1.2 项目目标
- 提供核心网络配置功能
- 降低用户学习成本
- 支持主流移动端访问

## 2. 技术架构
### 2.1 系统架构图
```
[Browser] <-> [Flask Application] <-> [UCI System]
                   ↑
 [Shell Command Bridge]
```

### 2.2 技术选型
| 组件          | 技术方案               | 说明                          |
|---------------|-----------------------|-------------------------------|
| 前端框架      | Tailwind CSS + AlpineJS | 轻量级响应式布局               |
| 后端框架      | Flask 3.x            | 微框架适合嵌入式环境           |
| 配置管理      | UCI Python Binding   | 直接操作OpenWrt配置            |

## 3. 功能模块设计
### 3.1 核心功能矩阵

| 功能模块 | 子功能 | 说明 |
|---------|--------|------|
| 系统管理 | 设备控制 | 重启、重置等基础操作 |
| | 系统信息 | 显示设备基本信息、运行状态 |
| | 固件更新 | 支持本地和远程包更新 |
| 网络配置 | 无线设置 | WiFi配置、客户端管理 |
| | 网络接口 | 有线网络配置 |
| | 移动网络 | APN设置、Modem管理 |
| 数据统计 | 流量统计 | 网络使用数据统计 |
| | 系统监控 | 资源使用监控 |
| 定时任务 | 定时控制 | 设备定时开关机 |
| 高级管理 | 系统配置 | 高级系统参数设置 |

### 3.2 关键业务流程

1. 用户认证流程
   - 基于Flask Session的用户会话管理
   - 登录状态持久化
   - 权限控制中间件

2. 配置管理流程
   - 基于UCI的配置读写
   - 配置变更实时生效
   - 配置备份与恢复

3. 设备控制流程
   - WebSocket实时通信
   - 异步任务处理
   - 状态实时反馈

4. 系统更新流程
   - 本地包上传安装
   - 远程包更新
   - 安装进度实时反馈

## 4. 接口设计

### 4.1 Web API接口

#### 系统管理接口
- GET /api/v1/system/board.json - 获取设备信息
- GET /api/v1/system/info.json - 获取系统信息
- POST /system/reboot - 系统重启
- POST /system/reset - 系统重置

#### 网络配置接口
- GET /api/v2/wireless.json - 获取无线配置
- POST /api/v1/wireless.json - 保存无线配置
- GET /api/v1/network_interfaces.json - 获取网络接口配置
- POST /api/v1/network_interfaces.json - 保存网络接口配置

#### 移动网络接口
- GET /api/v1/apn_settings.json - 获取APN设置
- POST /api/v1/apn_settings.json - 保存APN设置
- GET /api/v1/modem/basic.json - 获取Modem基本信息
- POST /api/v1/modem/config.json - 保存Modem配置

#### 数据统计接口
- GET /api/v1/data_stats.json - 获取数据统计信息
- POST /statistics/reset - 重置统计数据

### 4.2 WebSocket接口

- client_connected - 客户端连接事件
- request_install - 包安装请求
- log_response - 日志实时推送

## 5. 开发流程

1. 环境准备
```bash
pnpm run build:css  # 构建CSS
cd app
python3 ./pcat-manager-web.py  # 启动开发服务器
```

2. 开发规范
- 遵循PEP 8 Python代码规范
- 使用类型注解提高代码可读性
- 模块化设计，保持代码结构清晰

3. 测试流程
- 单元测试覆盖核心功能
- 接口测试确保API稳定性
- 集成测试验证功能完整性

## 6. 部署方案

### 6.1 环境要求

- Python 3.x
- Flask 3.x
- OpenWrt系统环境
- 必要的系统依赖包

### 6.2 安装流程

1. 系统依赖安装
```bash
opkg update
opkg install python3 python3-flask python3-socketio
```

2. 应用部署
- 复制应用文件到目标目录
- 配置系统服务
- 设置开机自启

3. 配置管理
- 编辑 /etc/pcat-manager-web.ini
- 配置OEM信息（可选）
- 设置系统参数

### 6.3 维护建议

- 定期检查日志文件
- 监控系统资源使用
- 及时更新安全补丁
- 定期备份配置文件

### B. 相关文档链接
- OpenWrt UCI文档: https://openwrt.org/docs/guide-user/base-system/uci
- Flask文档: https://flask.palletsprojects.com/en/stable/
- Tailwind CSS文档: https://v3.tailwindcss.com/
- AlpineJS文档: https://alpinejs.dev/
