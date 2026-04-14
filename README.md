# 雨课堂自动签到助手
长江雨课堂全自动签到工具，支持`微信扫码登录`和账号密码自动登录（含验证码破解），适配 Cron `定时任务`，包含`多种调度策略`以及`签到成功推送`实现 24/7 无人值守。

本项目包含两个可独立运行的签到脚本：

- `yuketang_helper.py`：桌面端登录态（扫码）版本
- `yuketang_helper_web.py`：Web 登录版本（仅账密自动登录，不推荐）

## 安装依赖

桌面版脚本：

```bash
pip install requests qrcode
```

Web 版脚本：

```bash
pip install requests qrcode ddddocr playwright Pillow
playwright install chromium
```

## 快速使用

如果你看不懂下面的东西的话，直接运行脚本就行，但你用的不会很爽，可能以后会抽空做个UI。

桌面版：

```bash
python yuketang_helper.py --qr              # 首次扫码登录（或者直接运行一次）
python yuketang_helper.py -a                # 自动扫描当前课堂并签到
python yuketang_helper.py -k                # 会话保活
```

Web 版：

```bash
python yuketang_helper_web.py -p your_phone -pw your_password -a
python yuketang_helper_web.py -k
```

## 调度模式（已内置在两个脚本中）


```bash
python yuketang_helper.py --run-next   #计算最近一轮调度任务，等待并执行后退出（适合配合 cron）
python yuketang_helper.py --daemon     #持续检查周计划/ICS，命中窗口就自动签到，直到 Ctrl+C
```

## 用户配置

### 0) 基础配置

- `BASE_DOMAIN`：雨课堂域名，默认 `changjiang.yuketang.cn`。
- `CHECKIN_COOLDOWN_MINUTES`：重复签到冷却时间（分钟）。
- `ENABLE_RUNTIME_LOG`：是否输出运行时控制台日志；设为 `False` 可关闭非必要日志。
- `AUTO_LOGIN_PHONE`：仅 `yuketang_helper_web.py` 使用，账密自动登录手机号。
- `AUTO_LOGIN_PSWD`：仅 `yuketang_helper_web.py` 使用，账密自动登录密码。

### 1) 每周固定时间调度

```python
WEEKLY_TASKS = [
    {"days": [1, 3, 5], "time": "08:00"},
    {"days": [2], "start": "14:00", "end": "14:20"},
]
```

- `days`：周一到周日为 `1..7`
- `time`：时间点（固定按 10 分钟签到窗口处理）
- `start + end`：时间段窗口
- 每周任务会在命中时间窗口时自动扫描“当前正在进行的课堂”并签到，不需要填写 `course_id`

### 2) ICS 课表调度

```python
ICS_ENABLED = True
ICS_FILENAME = "xxxx.ics"
ICS_FILE = Path(__file__).resolve().with_name(ICS_FILENAME)
ICS_LOOKAHEAD_COUNT = 2
ICS_WINDOW_MINUTES = 10
SCHEDULER_EXTENSION_MINUTES = 15
```

行为说明：

- 脚本只读取 ICS 里的上课开始时间（`DTSTART`）和课程名（`SUMMARY`）
- 命中 ICS 时间窗口后会自动扫描“当前正在进行的课堂”并签到
- `ICS_LOOKAHEAD_COUNT`：保留多少个未来时间点
- `ICS_WINDOW_MINUTES`：每个 ICS 时间点的默认签到窗口长度
- `SCHEDULER_EXTENSION_MINUTES`：统一追加重试分钟数（每周调度与 ICS 调度共用，默认 15）

### 3) PushPlus 推送

```python
PUSHPLUS_TOKEN = ""       #请去PushPlus官网注册，获取用户token
PUSHPLUS_CHANNEL = "wechat"
PUSHPLUS_TEMPLATE = "txt"
PUSHPLUS_TITLE_TEMPLATE = "雨课堂签到成功 - {course_id}"
PUSHPLUS_CONTENT_TEMPLATE = (
    "签到成功\n"
    "模式：{backend_name}\n"
    "课程编号：{course_id}\n"
    "日期：{target_date}\n"
    "时间：{success_time}\n"
    "规则：{rule_label}"
)
```

`PUSHPLUS_CHANNEL` 常见取值：

- `wechat`：微信服务号
- `mail`：邮件
- `webhook`：Webhook
- `cp`：企业微信应用
- `sms`：短信（需开通）

模板占位符支持：`{backend_name}`、`{course_id}`、`{target_date}`、`{success_time}`、`{rule_label}`、`{source}`、`{summary}`。

### 4) 调度状态与重试参数

- `SCHEDULER_STATE_FILE`：调度状态与成功事件合并文件。
- `SCHEDULER_EXTENSION_MINUTES`：统一追加重试分钟数（默认 15）。
- `SCHEDULER_STATE_RETENTION_DAYS`：状态保留天数，超期会自动清理。
- `SCHEDULER_RETRY_INTERVAL_SECONDS`：同一任务在窗口内失败后，最小重试间隔秒数。
- `SCHEDULER_LOOP_INTERVAL_SECONDS`：守护循环扫描间隔秒数。

### 5) 需要改哪些配置

- 必改：`WEEKLY_TASKS` 或 `ICS_FILENAME`（至少配一个调度来源）。
- 常改：`BASE_DOMAIN`、`PUSHPLUS_TOKEN`、`PUSHPLUS_CHANNEL`。
- 调度配置不完整只会影响 `--run-next/--daemon`；`-a`、`-k` 和直接运行菜单模式不依赖调度配置。

## 命令行参数

两个主脚本通用参数：

- `-a, --auto` 自动扫描当前课堂并签到
- `-k, --keepalive` 仅保活
- `--run-next` 只处理最近一次调度窗口，然后退出
- `--daemon` 持续检查周计划/ICS，命中窗口就自动签到，直到手动停止
- `--cooldown N` 重复签到冷却分钟数
- `-s N [S], --schedule N [S]` 延迟 N 分钟后开始，检测间隔可选 S 秒（默认 60）

Web 版附加参数：

- `-p, --phone`
- `-pw, --password`

## 文件说明

- `yuketang_helper.py`：桌面版主脚本（签到、保活、调度）
- `yuketang_helper_web.py`：Web 版主脚本（自动登录、签到、保活、调度）
- `yuketang_session.json`：桌面版登录态与签到状态
- `yuketang_session_web.json`：Web 版登录态与签到状态
- `state.log` / `state_web.log`：调度状态与成功事件合并记录

---

仅用于学习和技术研究，请遵守学校和平台相关规定。
