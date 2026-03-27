# 雨课堂自动签到助手

长江雨课堂全自动签到工具。现已全面切换为桌面端二维码登录，单文件运行，配合 Cron 定时任务可以实现长期保活与自动签到。

## 功能和特性

- **桌面端扫码登录**：终端内显示桌面端登录二维码，手机扫码即可认证
- **自动签到**：自动扫描当前正在进行的课堂并完成签到
- **动态二维码签到测试工具**：独立脚本支持 API 越权和 WebSocket 监听两种方案获取动态暗号
- **签到去重**：同一课堂在冷却期内（默认 30 分钟）不会重复签到
- **桌面端登录态持久化**：统一保存在 `yuketang_session.json` 中，自动保存轮转后的 `desktop_auth` 或桌面端 Cookie
- **会话保活**：定期刷新 Session，防止过期掉线
- **单状态文件**：始终只使用一个 `yuketang_session.json`

## 环境要求

### 运行依赖

```bash
pip install requests qrcode
```

### 动态二维码签到（可选）

```bash
pip install paho-mqtt
```

## 使用方法

### 基础用法

```bash
# 自动登录态加载 + 自动签到
python yuketang_helper.py -a

# 强制重新扫码登录
python yuketang_helper.py --qr

# 交互模式
python yuketang_helper.py
```

### 域名配置

支持不同雨课堂服务器，编辑脚本开头的 `BASE_DOMAIN` 常量：
```python
BASE_DOMAIN = "changjiang.yuketang.cn"  # 长江雨课堂（默认）
# BASE_DOMAIN = "huanghe.yuketang.cn"   # 黄河雨课堂
# BASE_DOMAIN = "pro.yuketang.cn"       # 荷花雨课堂
# BASE_DOMAIN = "yuketang.cn"           # 雨课堂
```

### 兼容参数

脚本已不再支持账号密码登录，`-p/-pw` 仅为兼容老命令保留，当前会被忽略。

### 命令行参数

| 参数 | 说明 |
|------|------|
| `-a` / `--auto` | 自动扫描课堂并签到 |
| `-k` / `--keepalive` | 仅执行会话保活 |
| `-p` / `--phone` | 已弃用，仅为兼容保留 |
| `-pw` / `--password` | 已弃用，仅为兼容保留 |
| `--qr` | 强制重新显示桌面端登录二维码 |
| `--cooldown N` | 签到去重冷却时间（分钟，默认 30） |
| `-s N` / `--schedule N` | 延迟 N 分钟后开始，每分钟检测一次课堂并签到 |

### 动态二维码签到

**测试工具**（独立脚本）：
```bash
python yuketang_ws_listener.py
# 选择测试方案：
# 1 - API 越权方案
# 2 - WebSocket 监听方案
# 3 - 两种都测试
```

### Cron 定时任务示例

```bash
# 每4小时保活一次
0 */4 * * * cd /path/to/yuketang && python yuketang_helper.py -k >> cron.log 2>&1

# 工作日上课时段自动签到
50 7,13,18 * * 1-5 cd /path/to/yuketang && python yuketang_helper.py -a >> cron.log 2>&1
0 10,16 * * 1-5 cd /path/to/yuketang && python yuketang_helper.py -a >> cron.log 2>&1
```

## 文件说明

| 文件 | 说明 |
|------|------|
| `yuketang_helper.py` | 主程序，包含桌面端扫码登录、签到、保活 |
| `yuketang_ws_listener.py` | 动态二维码测试工具（独立脚本） |
| `yuketang_session.json` | 持久化状态文件（`desktop_auth` / `desktop_cookies` + 签到记录），自动生成 |
| `yuketang_technical_handover.md` | 技术移交文档，包含逆向分析过程 |

## 签到去重机制

脚本在 `yuketang_session.json` 中记录每次成功签到的课堂号和时间。再次对同一课堂签到时，如果距上次不超过冷却时间（默认 30 分钟），则自动跳过。

```bash
python yuketang_helper.py -a --cooldown 60  # 60 分钟内不重复签到
```

---
*声明：本项目仅供学习自动化协议逆向技术，请遵守各高校考勤管理相关规定。*
