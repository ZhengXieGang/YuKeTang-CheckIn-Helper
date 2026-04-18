# 雨课堂自动签到助手

长江雨课堂全自动签到工具，支持微信扫码登录和账号密码自动登录，适配 Cron 定时任务，PushPlus消息推送，实现 24/7 无人值守。

## 功能和特性

- **微信扫码登录**：终端内显示二维码，手机扫码即可完成认证
- **账密登录**（不推荐）：基于 Playwright + ddddocr 实现无头浏览器自动登录，自动识别并通过验证码
- **自动签到**：自动扫描当前正在进行的课堂并完成签到
- **签到去重**：同一课堂在冷却期内（默认 15 分钟）不会重复签到
- **会话保活**：定期刷新 Session，防止过期掉线
- **成功推送**：签到成功后可通过 PushPlus 推送提醒
- **持续重试**：启动后持续运行；到达超时点自动追加等待时间，直到签到成功
- **动态码签到（测试）**：独立测试脚本 `test_checkin_bypass.py` 可自动探测多种签到路径，搭配仓库内附带的安装包，方便快捷的抓取签到日志进行分析。

## 环境要求
`yuketang_helper.py`：
```bash
pip install requests qrcode
```
`yuketang_helper_web.py`：
```bash
pip install requests qrcode ddddocr playwright Pillow
playwright install chromium
```

## 使用方法

### 基础用法（请使用支持 UTF-8 的终端）

```bash
# 首次使用（自动登录 + 签到）
python yuketang_helper.py -a

# 强制扫码登录
python yuketang_helper.py -qr

# 交互模式
python yuketang_helper.py

# 定时签到（延迟 5 分钟后开始，每 60 秒检测一次）
python yuketang_helper.py -s 5

# 会话保活
python yuketang_helper.py -k
```

### 动态码签到测试

```bash
# 自动测试所有签到路径，输出报告
python test_checkin_bypass.py
```

测试脚本会自动从 `yuketang_session.json` 读取 Cookie，测试完成后输出报告。
# 方式1: 提供二维码的照片
python3 test_checkin_bypass.py qr_photo.jpg
# 方式2: 使用二维码里的 URL
python3 test_checkin_bypass.py --url "https://changjiang.yuketang.cn/api/v3/..."
# 方式3: 持续拍摄丢进文件夹（自动识别新图片）
python3 test_checkin_bypass.py --watch ~/Screenshots/

### 域名配置

支持不同雨课堂服务器，编辑脚本开头的 `BASE_DOMAIN` 常量：
```python
BASE_DOMAIN = "changjiang.yuketang.cn"  # 长江雨课堂（默认）
# BASE_DOMAIN = "huanghe.yuketang.cn"   # 黄河雨课堂
# BASE_DOMAIN = "pro.yuketang.cn"       # 荷花雨课堂
# BASE_DOMAIN = "yuketang.cn"           # 雨课堂
```

### 推送与持续重试配置

在 `yuketang_helper.py` / `yuketang_helper_web.py` 顶部可直接修改：


### 命令行参数

| 参数 | 说明 |
|------|------|
| `-a` / `-auto` | 持续扫描课堂并签到，直到成功 |
| `-k` / `-keepalive` | 仅执行会话保活 |
| `-qr` | 强制重新显示桌面端登录二维码 |
| `-cooldown N` | 签到去重冷却时间（分钟，默认 15） |
| `-s N` / `-schedule N` | 延迟 N 分钟后开始，持续签到直到成功 |

### 长驻运行建议

```bash
# 每4小时保活一次
0 */4 * * * cd /path/to/yuketang && python yuketang_helper.py -k >> cron.log 2>&1

# 延迟 5 分钟后启动持续签到（会一直运行到签到成功）
python yuketang_helper.py -s 5
```

`-a` / `-s` 是“持续运行直到签到成功”模式，不建议高频由 Cron 重复拉起。

## 文件说明

| 文件 | 说明 |
|------|------|
| `yuketang_helper.py` | 桌面端登录版主程序（签到、保活、持续重试、推送） |
| `yuketang_helper_web.py` | Web 账密登录版主程序（签到、保活、持续重试、推送） |
| `yktmobile-0.0.2-debug.apk` | 仓库内附带的 Android 测试安装包，可直接安装体验移动端扫码分析工具 |
| `yuketang_session.json` | 桌面端持久化状态文件（Cookie + 签到记录），自动生成 |
| `yuketang_session_web.json` | Web 版持久化状态文件（Cookie + 签到记录），自动生成 |

## 签到去重机制

每次成功签到后会在 `yuketang_session.json` 中记录课堂号和时间。再次对同一课堂签到时，如果距上次不超过冷却时间（默认 15 分钟），则自动跳过。

可通过 `-cooldown` 参数自定义冷却时间：
```bash
python yuketang_helper.py -a -cooldown 60  # 60 分钟内不重复签到
```

---
*声明：本项目仅供学习自动化协议逆向技术，请遵守各高校考勤管理相关规定。*
