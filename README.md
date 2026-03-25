# 雨课堂自动签到助手

长江雨课堂全自动签到工具。支持账号密码自动登录（含验证码破解）和微信扫码登录，单文件运行，配合 Cron 定时任务可以实现 24/7 无人值守。
注：动态二维码无法自动签到。

## 功能和特性

- **全自动账密登录**：基于 Playwright + ddddocr，无头浏览器自动识别并通过腾讯天御人机验证（汉字点选 / 滑块拼图）
- **微信扫码登录**：终端内显示二维码，手机扫码即可认证
- **智能降级**：未安装 ddddocr/playwright 时自动跳过自动登录，退回到二维码扫码
- **自动签到**：自动扫描当前正在进行的课堂并完成签到
- **签到去重**：同一课堂在冷却期内（默认 30 分钟）不会重复签到
- **Session 持久化**：全量保存 Cookie 属性（含 Domain/Path/Expires），确保 Session 长期有效
- **会话保活**：定期刷新 Session，防止过期掉线
- **凭据优先级**：命令行参数 > 脚本内置配置

## 环境要求

### 最低依赖（仅扫码登录）

```bash
pip install requests qrcode
```

### 完整依赖（含自动登录）

```bash
pip install requests qrcode ddddocr playwright Pillow
playwright install chromium
```

> 未安装完整依赖时，脚本会自动退回到二维码扫码模式，不会报错。

## 使用方法

### 基础用法

```bash
# 自动登录 + 签到（需完整依赖）
python yuketang_helper.py -a

# 强制扫码登录
python yuketang_helper.py --qr

# 交互模式
python yuketang_helper.py
```

### 账号配置

**方式一**：编辑脚本开头的配置区
```python
AUTO_LOGIN_PHONE = "你的手机号"
AUTO_LOGIN_PSWD = "你的密码"
```

**方式二**：命令行参数（优先级更高）
```bash
python yuketang_helper.py -a -p [手机号] -pw [密码]
```

### 命令行参数

| 参数 | 说明 |
|------|------|
| `-a` / `--auto` | 自动扫描课堂并签到 |
| `-k` / `--keepalive` | 仅执行会话保活 |
| `-p` / `--phone` | 手机号 |
| `-pw` / `--password` | 密码 |
| `--qr` | 强制使用二维码扫码登录 |
| `--cooldown N` | 签到去重冷却时间（分钟，默认 30） |

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
| `yuketang_helper.py` | 单文件主程序，包含自动登录、签到、保活全部功能 |
| `yuketang_session.json` | 持久化状态文件（Cookie + 签到记录），自动生成 |

## 签到去重机制

脚本在 `yuketang_session.json` 中记录每次成功签到的课堂号和时间。再次对同一课堂签到时，如果距上次不超过冷却时间（默认 30 分钟），则自动跳过。

```bash
python yuketang_helper.py -a --cooldown 60  # 60 分钟内不重复签到
```

---
*声明：本项目仅供学习自动化协议逆向技术，请遵守各高校考勤管理相关规定。*
