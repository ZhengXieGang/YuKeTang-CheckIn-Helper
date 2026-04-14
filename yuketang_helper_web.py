#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import qrcode
import requests

try:
    from playwright.async_api import async_playwright

    HAS_PLAYWRIGHT = True
except ImportError:
    async_playwright = None
    HAS_PLAYWRIGHT = False

try:
    import ddddocr
    from PIL import Image

    HAS_AUTO_LOGIN = HAS_PLAYWRIGHT
except ImportError:
    HAS_AUTO_LOGIN = False

# ========== 用户配置 ==========
BASE_DOMAIN = "changjiang.yuketang.cn"  # 雨课堂域名
AUTO_LOGIN_PHONE = ""  # Web 自动登录手机号
AUTO_LOGIN_PSWD = ""  # Web 自动登录密码
CHECKIN_COOLDOWN_MINUTES = 15  # 重复签到冷却时间（分钟）
ENABLE_RUNTIME_LOG = True  # 是否输出运行日志（False=尽量静默）

WEEKLY_TASKS = [
    # 只需配置星期+时间点/时间段；不需要 course_id
    # {"days": [1, 3, 5], "time": "08:00"},
    # {"days": [2], "start": "14:00", "end": "14:20"},
]

ICS_ENABLED = True  # 是否启用 ICS 调度
ICS_FILENAME = "校验导出.ics"  # 只写文件名，脚本会自动在当前目录读取
ICS_FILE = Path(__file__).resolve().with_name(ICS_FILENAME)
ICS_LOOKAHEAD_COUNT = 2  # 仅保留未来 N 个 ICS 时间点
ICS_WINDOW_MINUTES = 10  # ICS 每个时间点默认窗口（分钟）
SCHEDULER_EXTENSION_MINUTES = 15  # 统一追加重试时间（分钟）

PUSHPLUS_TOKEN = ""  # PushPlus token（为空=关闭推送）
PUSHPLUS_CHANNEL = "wechat"  # wechat / mail / webhook / cp / sms
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

SCHEDULER_STATE_FILE = Path(__file__).resolve().with_name("state_web.log")  # 调度状态+成功事件合并文件
SCHEDULER_STATE_RETENTION_DAYS = 7  # 状态保留天数
SCHEDULER_RETRY_INTERVAL_SECONDS = 20  # 窗口内失败重试间隔（秒）
SCHEDULER_LOOP_INTERVAL_SECONDS = 10  # 守护循环扫描间隔（秒）
# ==============================

GLOBAL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
BASE_URL = f"https://{BASE_DOMAIN}"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "yuketang_session_web.json")
DESKTOP_STATE_FILE = os.path.join(SCRIPT_DIR, "yuketang_session.json")
BROWSER_SYNC_WAIT_SECONDS = 6


def log(msg):
    if not ENABLE_RUNTIME_LOG:
        return
    print(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}")


def read_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json_file(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def load_state_dict(path=STATE_FILE):
    state = read_json_file(path, {})
    return state if isinstance(state, dict) else {}


def normalize_cookie_record(cookie):
    if not isinstance(cookie, dict) or "name" not in cookie:
        return None
    expires = cookie.get("expires")
    try:
        expires = int(expires) if expires not in (None, "", 0, "0") else None
    except Exception:
        expires = None
    return {
        "name": cookie["name"],
        "value": cookie.get("value", ""),
        "domain": cookie.get("domain") or BASE_DOMAIN,
        "path": cookie.get("path") or "/",
        "expires": expires,
        "secure": bool(cookie.get("secure", False)),
    }


@dataclass(frozen=True)
class SchedulerSettings:
    weekly_tasks: list[dict[str, Any]]
    ics_enabled: bool
    ics_file: Path
    ics_lookahead_count: int
    ics_window_minutes: int
    extension_minutes: int
    state_file: Path
    state_retention_days: int = 7
    retry_interval_seconds: int = 20
    loop_interval_seconds: int = 5
    pushplus_token: str = ""
    pushplus_channel: str = "wechat"
    pushplus_template: str = "txt"
    pushplus_title_template: str = "雨课堂签到成功 - {course_id}"
    pushplus_content_template: str = (
        "签到成功\n"
        "模式：{backend_name}\n"
        "课程编号：{course_id}\n"
        "日期：{target_date}\n"
        "时间：{success_time}\n"
        "规则：{rule_label}"
    )
    backend_name: str = "desktop"


@dataclass(frozen=True)
class TaskWindow:
    course_id: str
    rule_label: str
    target_date: date
    start_at: datetime
    end_at: datetime
    extended_end_at: datetime
    source: str
    summary: str = ""

    @property
    def attempt_key(self) -> str:
        return f"{self.source}:{self.target_date.isoformat()}:{self.course_id}:{self.start_at.isoformat()}"

    @property
    def success_key(self) -> str:
        return f"{self.source}:{self.course_id}:{self.start_at.isoformat()}"


class SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


class CheckinScheduler:
    def __init__(self, settings: SchedulerSettings, sign_func: Callable[[TaskWindow], bool]):
        self.settings = settings
        self.sign_func = sign_func
        self.last_attempts: dict[str, datetime] = {}
        self.local_tz = ZoneInfo("Asia/Shanghai") if ZoneInfo else timezone(timedelta(hours=8))
        self._ics_cache_signature = None
        self._ics_cache_windows: list[TaskWindow] = []
        self.validate_configuration()

    def validate_configuration(self) -> None:
        has_weekly = bool(self.settings.weekly_tasks)
        has_ics = bool(self.settings.ics_enabled and self.settings.ics_file.exists())
        if not has_weekly and not has_ics:
            raise ValueError("请至少配置 WEEKLY_TASKS，或启用 ICS 并提供可用的 ICS_FILE")

        for task in self.settings.weekly_tasks:
            self.validate_weekly_task(task)

        if has_ics:
            if self.settings.ics_lookahead_count < 1:
                raise ValueError("ICS_LOOKAHEAD_COUNT 必须大于等于 1")
            self.load_ics_windows()

    def validate_weekly_task(self, task: dict[str, Any]) -> None:
        self.normalize_days(task.get("days"))
        self.resolve_weekly_time_rule(task)

    def load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.settings.state_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}
        if not isinstance(state.get("successes"), dict):
            state["successes"] = {}
        if not isinstance(state.get("events"), list):
            state["events"] = []
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        self.settings.state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def cleanup_state(self, state: dict[str, Any], today: date) -> dict[str, Any]:
        keep_from = today - timedelta(days=max(0, self.settings.state_retention_days - 1))
        cleaned: dict[str, Any] = {"successes": {}, "events": []}
        for day_text, records in state.get("successes", {}).items():
            try:
                record_day = date.fromisoformat(day_text)
            except ValueError:
                continue
            if record_day < keep_from or record_day > today + timedelta(days=30):
                continue
            if isinstance(records, dict):
                cleaned["successes"][day_text] = records
        for event in state.get("events", []):
            if not isinstance(event, dict):
                continue
            timestamp = str(event.get("timestamp", ""))
            event_day = None
            if len(timestamp) >= 10:
                try:
                    event_day = date.fromisoformat(timestamp[:10])
                except ValueError:
                    event_day = None
            if event_day and keep_from <= event_day <= today + timedelta(days=30):
                cleaned["events"].append(event)
        if len(cleaned["events"]) > 2000:
            cleaned["events"] = cleaned["events"][-2000:]
        return cleaned

    def ensure_state(self) -> dict[str, Any]:
        state = self.cleanup_state(self.load_state(), datetime.now().date())
        self.save_state(state)
        return state

    def has_success_today(self, state: dict[str, Any], success_key: str, target_day: date) -> bool:
        records = state.get("successes", {}).get(target_day.isoformat(), {})
        return str(success_key) in records

    def mark_success(self, state: dict[str, Any], window: TaskWindow, when: datetime) -> None:
        day_text = window.target_date.isoformat()
        state.setdefault("successes", {})
        state["successes"].setdefault(day_text, {})
        state["successes"][day_text][str(window.success_key)] = {
            "timestamp": when.strftime("%Y-%m-%d %H:%M:%S"),
            "source": window.source,
            "rule": window.rule_label,
            "course_id": window.course_id,
        }
        self.save_state(state)

    def append_success_log(self, state: dict[str, Any], window: TaskWindow, when: datetime) -> None:
        event = {
            "timestamp": when.strftime("%Y-%m-%d %H:%M:%S"),
            "backend": self.settings.backend_name,
            "source": window.source,
            "course_id": window.course_id,
            "date": window.target_date.isoformat(),
            "rule": window.rule_label,
        }
        if window.summary:
            event["summary"] = window.summary
        state.setdefault("events", [])
        if not isinstance(state["events"], list):
            state["events"] = []
        state["events"].append(event)
        if len(state["events"]) > 2000:
            state["events"] = state["events"][-2000:]
        self.save_state(state)

    def format_template(self, template: str, context: dict[str, Any]) -> str:
        try:
            return template.format_map(SafeFormatDict(context))
        except Exception:
            return template

    def pushplus_notify(self, window: TaskWindow, when: datetime) -> None:
        token = self.settings.pushplus_token.strip()
        if not token:
            return

        context = {
            "backend_name": self.settings.backend_name,
            "course_id": window.course_id,
            "target_date": window.target_date.isoformat(),
            "success_time": when.strftime("%Y-%m-%d %H:%M:%S"),
            "rule_label": window.rule_label,
            "source": window.source,
            "summary": window.summary,
        }
        payload = {
            "token": token,
            "title": self.format_template(self.settings.pushplus_title_template, context),
            "content": self.format_template(self.settings.pushplus_content_template, context),
            "template": self.settings.pushplus_template.strip() or "txt",
            "channel": self.settings.pushplus_channel.strip() or "wechat",
        }
        try:
            requests.post("https://www.pushplus.plus/send", json=payload, timeout=8)
        except requests.RequestException:
            return

    def parse_time_text(self, text: str) -> dt_time:
        return datetime.strptime(text.strip(), "%H:%M").time()

    def normalize_days(self, days: Any) -> list[int]:
        if not isinstance(days, list) or not days:
            raise ValueError("days 必须是非空列表，周一=1 ... 周日=7")
        result = []
        for item in days:
            value = int(item)
            if value < 1 or value > 7:
                raise ValueError(f"非法星期值: {item}")
            result.append(value)
        return result

    def resolve_weekly_time_rule(self, task: dict[str, Any]) -> tuple[dt_time, dt_time, str]:
        if "time" in task:
            start_time = self.parse_time_text(str(task["time"]))
            end_dt = datetime.combine(date.today(), start_time) + timedelta(minutes=10)
            return start_time, end_dt.time(), f"{task['time']}(+10m)"

        if "start" in task and "end" in task:
            start_time = self.parse_time_text(str(task["start"]))
            end_time = self.parse_time_text(str(task["end"]))
            return start_time, end_time, f"{task['start']}-{task['end']}"

        raise ValueError("WEEKLY_TASKS 任务必须配置 time 或 start/end，且不需要其他时间字段")

    def build_weekly_window(self, task: dict[str, Any], target_day: date) -> TaskWindow | None:
        days = self.normalize_days(task.get("days"))
        if target_day.isoweekday() not in days:
            return None

        course_id = str(task.get("course_id", "AUTO")).strip() or "AUTO"
        start_time, end_time, label = self.resolve_weekly_time_rule(task)
        start_at = datetime.combine(target_day, start_time)
        end_at = datetime.combine(target_day, end_time)
        if end_at <= start_at:
            end_at += timedelta(days=1)

        return TaskWindow(
            course_id=course_id,
            rule_label=f"每周 {label}",
            target_date=target_day,
            start_at=start_at,
            end_at=end_at,
            extended_end_at=end_at + timedelta(minutes=max(0, int(self.settings.extension_minutes))),
            source="weekly",
            summary=str(task.get("summary", "")).strip(),
        )

    def get_tzinfo(self, tzid: str):
        if tzid and ZoneInfo:
            try:
                return ZoneInfo(tzid)
            except Exception:
                pass
        return self.local_tz

    def unfold_ics_lines(self, text: str) -> list[str]:
        lines: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.rstrip("\r\n")
            if line.startswith((" ", "\t")) and lines:
                lines[-1] += line[1:]
            else:
                lines.append(line)
        return lines

    def unescape_ics_text(self, value: str) -> str:
        return (
            value.replace("\\N", "\n")
            .replace("\\n", "\n")
            .replace("\\,", ",")
            .replace("\\;", ";")
            .replace("\\\\", "\\")
            .strip()
        )

    def parse_ics_datetime(self, value: str, params: dict[str, str]) -> datetime:
        value = value.strip()
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(self.local_tz).replace(tzinfo=None)

        for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
            try:
                dt = datetime.strptime(value, fmt)
                return dt.replace(tzinfo=self.get_tzinfo(params.get("TZID", ""))).astimezone(self.local_tz).replace(tzinfo=None)
            except ValueError:
                continue

        raise ValueError(f"无法解析 ICS 时间: {value}")

    def build_ics_window(self, fields: dict[str, list[tuple[dict[str, str], str]]]) -> TaskWindow | None:
        summary_items = fields.get("SUMMARY", [])
        dtstart_items = fields.get("DTSTART", [])
        if not summary_items or not dtstart_items:
            return None

        summary = self.unescape_ics_text(summary_items[0][1])
        course_id = summary or "AUTO"

        start_at = self.parse_ics_datetime(dtstart_items[0][1], dtstart_items[0][0])
        window_minutes = max(1, int(self.settings.ics_window_minutes))
        end_at = start_at + timedelta(minutes=window_minutes)
        extension_minutes = max(0, int(self.settings.extension_minutes))
        return TaskWindow(
            course_id=course_id,
            rule_label=f"ICS {summary} {start_at.strftime('%m-%d %H:%M')}(+{window_minutes}m)",
            target_date=start_at.date(),
            start_at=start_at,
            end_at=end_at,
            extended_end_at=end_at + timedelta(minutes=extension_minutes),
            source="ics",
            summary=summary,
        )

    def parse_ics_file(self) -> list[TaskWindow]:
        text = self.settings.ics_file.read_bytes().decode("utf-8-sig", errors="ignore")
        fields: dict[str, list[tuple[dict[str, str], str]]] = {}
        windows: list[TaskWindow] = []
        in_event = False

        for line in self.unfold_ics_lines(text):
            if line == "BEGIN:VEVENT":
                in_event = True
                fields = {}
                continue
            if line == "END:VEVENT":
                in_event = False
                window = self.build_ics_window(fields)
                if window:
                    windows.append(window)
                fields = {}
                continue
            if not in_event or ":" not in line:
                continue

            key_text, value = line.split(":", 1)
            parts = key_text.split(";")
            key = parts[0].upper()
            params: dict[str, str] = {}
            for part in parts[1:]:
                if "=" in part:
                    param_key, param_value = part.split("=", 1)
                    params[param_key.upper()] = param_value
            fields.setdefault(key, []).append((params, value))

        return sorted(windows, key=lambda item: (item.start_at, item.course_id, item.summary))

    def load_ics_windows(self) -> list[TaskWindow]:
        if not self.settings.ics_enabled or not self.settings.ics_file.exists():
            return []

        stat = self.settings.ics_file.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature == self._ics_cache_signature:
            return self._ics_cache_windows

        try:
            windows = self.parse_ics_file()
        except Exception as exc:
            if self._ics_cache_signature is None:
                raise ValueError(f"解析 ICS 文件失败: {exc}") from exc
            return self._ics_cache_windows

        self._ics_cache_signature = signature
        self._ics_cache_windows = windows
        return self._ics_cache_windows

    def get_candidate_time(self, window: TaskWindow, now: datetime) -> datetime | None:
        if window.extended_end_at < now:
            return None
        if window.start_at <= now <= window.extended_end_at:
            return now
        return window.start_at

    def limit_to_next_times(self, windows: list[TaskWindow], now: datetime, count: int) -> list[TaskWindow]:
        if count < 1:
            return []

        grouped: dict[datetime, list[TaskWindow]] = {}
        for window in windows:
            candidate_time = self.get_candidate_time(window, now)
            if candidate_time is None:
                continue
            grouped.setdefault(candidate_time, []).append(window)

        result: list[TaskWindow] = []
        for candidate_time in sorted(grouped)[:count]:
            result.extend(sorted(grouped[candidate_time], key=lambda item: (item.start_at, item.course_id, item.summary)))
        return result

    def collect_weekly_windows(
        self,
        now: datetime,
        state: dict[str, Any],
        search_days: int = 30,
        include_previous_day: bool = False,
    ) -> list[TaskWindow]:
        windows: list[TaskWindow] = []
        start_offset = -1 if include_previous_day else 0
        for offset in range(start_offset, search_days + 1):
            target_day = now.date() + timedelta(days=offset)
            for task in self.settings.weekly_tasks:
                window = self.build_weekly_window(task, target_day)
                if not window:
                    continue
                if self.has_success_today(state, window.success_key, window.target_date):
                    continue
                if window.extended_end_at < now:
                    continue
                windows.append(window)
        return windows

    def collect_ics_windows(self, now: datetime, state: dict[str, Any]) -> list[TaskWindow]:
        windows = []
        for window in self.load_ics_windows():
            if self.has_success_today(state, window.success_key, window.target_date):
                continue
            if window.extended_end_at < now:
                continue
            windows.append(window)
        return self.limit_to_next_times(windows, now, self.settings.ics_lookahead_count)

    def collect_candidate_windows(
        self,
        now: datetime,
        state: dict[str, Any],
        search_days: int = 30,
        include_previous_day: bool = False,
    ) -> list[TaskWindow]:
        windows = self.collect_weekly_windows(now, state, search_days=search_days, include_previous_day=include_previous_day)
        windows.extend(self.collect_ics_windows(now, state))
        return windows

    def get_active_windows(self, now: datetime, state: dict[str, Any]) -> list[TaskWindow]:
        windows = [
            window
            for window in self.collect_candidate_windows(now, state, include_previous_day=True)
            if window.start_at <= now <= window.extended_end_at
        ]
        return sorted(windows, key=lambda item: (item.start_at, item.course_id, item.summary))

    def get_next_candidate_group(
        self,
        now: datetime,
        state: dict[str, Any],
        search_days: int = 30,
    ) -> tuple[datetime | None, list[TaskWindow]]:
        candidates: list[tuple[datetime, TaskWindow]] = []
        for window in self.collect_candidate_windows(now, state, search_days=search_days):
            candidate_time = self.get_candidate_time(window, now)
            if candidate_time is None:
                continue
            candidates.append((candidate_time, window))

        if not candidates:
            return None, []

        earliest = min(item[0] for item in candidates)
        group = [window for candidate_time, window in candidates if candidate_time == earliest]
        group.sort(key=lambda item: (item.start_at, item.course_id, item.summary))
        return earliest, group

    def process_window(self, window: TaskWindow, state: dict[str, Any]) -> bool:
        now = datetime.now()
        if self.has_success_today(state, window.success_key, window.target_date):
            return False
        if now < window.start_at or now > window.extended_end_at:
            return False

        last_attempt = self.last_attempts.get(window.attempt_key)
        if last_attempt and (now - last_attempt).total_seconds() < self.settings.retry_interval_seconds:
            return False

        self.last_attempts[window.attempt_key] = now
        try:
            success = bool(self.sign_func(window))
        except Exception:
            success = False
        if not success:
            return False

        success_time = datetime.now()
        self.mark_success(state, window, success_time)
        self.append_success_log(state, window, success_time)
        self.pushplus_notify(window, success_time)
        return True

    def run_next(self) -> int:
        state = self.ensure_state()
        next_time, group = self.get_next_candidate_group(datetime.now(), state)
        if not next_time or not group:
            return 0

        sleep_seconds = (next_time - datetime.now()).total_seconds()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

        while True:
            now = datetime.now()
            active = [window for window in group if window.start_at <= now <= window.extended_end_at]
            if not active:
                return 0
            for window in active:
                self.process_window(window, state)
            if all(self.has_success_today(state, window.success_key, window.target_date) for window in group):
                return 0
            time.sleep(self.settings.loop_interval_seconds)

    def run_daemon(self) -> int:
        state = self.ensure_state()
        last_cleanup_day = datetime.now().date()
        self.last_attempts.clear()

        while True:
            now = datetime.now()
            if now.date() != last_cleanup_day:
                state = self.cleanup_state(state, now.date())
                self.save_state(state)
                self.last_attempts.clear()
                last_cleanup_day = now.date()

            for window in self.get_active_windows(now, state):
                self.process_window(window, state)

            time.sleep(self.settings.loop_interval_seconds)

def persist_cookie_records(cookies):
    state = load_state_dict()
    future = int(time.time()) + 86400 * 365
    normalized = []
    for cookie in cookies:
        item = normalize_cookie_record(cookie)
        if not item:
            continue
        item["expires"] = item["expires"] or future
        normalized.append(item)
    state["cookies"] = normalized
    write_json_file(STATE_FILE, state)


def persist_browser_state(storage_state):
    state = load_state_dict()
    state["browser_state"] = storage_state
    write_json_file(STATE_FILE, state)


def load_browser_state():
    state = load_state_dict()
    browser_state = state.get("browser_state")
    if isinstance(browser_state, dict):
        return browser_state
    return None


def extract_cookie_records(raw_state):
    if isinstance(raw_state, list):
        return raw_state
    if not isinstance(raw_state, dict):
        return []

    cookie_list = raw_state.get("cookies", [])
    if cookie_list:
        return cookie_list

    desktop_cookies = raw_state.get("desktop_cookies", [])
    if desktop_cookies:
        return desktop_cookies

    if "sessionid" in raw_state:
        return [
            {"name": k, "value": v, "domain": BASE_DOMAIN, "path": "/"}
            for k, v in raw_state.items()
            if isinstance(v, str)
        ]

    return []


class AutoLogin:
    """基于 Playwright + ddddocr 的全自动账密登录，含验证码破解"""

    def __init__(self, phone, password):
        self.phone = phone
        self.password = password
        self.ocr = ddddocr.DdddOcr(show_ad=False)
        self.det = ddddocr.DdddOcr(det=True, show_ad=False)
        self.slider_ocr = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
        self.error_msg = None

    def _select_spots(self, char_map, instruction, attempt):
        try:
            target = re.sub(r"请依次点击|:|：|\"|'| ", "", instruction)
            clean = []
            for item in char_map:
                if not any(((item["x"] - e["x"]) ** 2 + (item["y"] - e["y"]) ** 2) ** 0.5 < 25 for e in clean):
                    clean.append(item)
            spots = [None] * len(target)
            used = set()
            for i, ch in enumerate(target):
                for item in clean:
                    if id(item) not in used and (ch in item["char"] or item["char"] in ch):
                        spots[i] = item
                        used.add(id(item))
                        break
            avail = [x for x in clean if id(x) not in used]
            mode = attempt % 4
            if mode == 0:
                avail.sort(key=lambda x: x["x"])
            elif mode == 1:
                avail.sort(key=lambda x: x["x"], reverse=True)
            elif mode == 2:
                avail.sort(key=lambda x: x["y"])
            else:
                avail.sort(key=lambda x: x["x"] + x["y"])
            ptr = 0
            for i in range(len(spots)):
                if spots[i] is None and ptr < len(avail):
                    spots[i] = avail[ptr]
                    ptr += 1
            return [x for x in spots if x is not None]
        except Exception:
            return char_map[:3]

    async def run(self):
        log("[*] 正在启动自动化登录...")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(user_agent=GLOBAL_UA)
            page = await ctx.new_page()

            await page.goto(f"{BASE_URL}/v2/web/index")
            await asyncio.sleep(2)
            try:
                await page.locator("img.changeImg, .login-type-img").first.click(timeout=3000)
            except Exception:
                pass
            await page.fill('input[name="loginname"]', self.phone)
            await page.fill('input[type="password"]', self.password)

            async def handle_response(response):
                if "/api/v3/user/login/password" in response.url:
                    try:
                        if "application/json" in response.headers.get("content-type", "").lower():
                            data = await response.json()
                            code = data.get("code")
                            if code is not None and code != 0 and code != -10:
                                self.error_msg = data.get("msg") or data.get("message") or f"错误代码: {code}"
                    except Exception:
                        pass

            page.on("response", handle_response)

            await page.locator(".submit-btn.login-btn").click()

            ok = False
            for attempt in range(15):
                await asyncio.sleep(2.5)

                try:
                    msg_els = await page.locator(".el-message__content, .err-msg, .error-msg, [role=\"alert\"]").all()
                    for el in msg_els:
                        if await el.is_visible():
                            txt = await el.text_content()
                            if txt and len(txt.strip()) > 0:
                                self.error_msg = txt.strip()
                                break
                except Exception:
                    pass

                if self.error_msg:
                    log(f"[-] 登录失败: {self.error_msg}")
                    break

                if any(c["name"] == "sessionid" for c in await ctx.cookies()):
                    ok = True
                    break

                frame = next((f for f in page.frames if "turing.captcha" in f.url), None)
                if not frame:
                    continue

                try:
                    instr = ""
                    for sel in ["#instructionText", ".tc-title-words", ".tc-instruction-text"]:
                        try:
                            text = await frame.locator(sel).first.text_content(timeout=1000)
                            if text:
                                instr = text
                                break
                        except Exception:
                            pass
                    if not instr:
                        await frame.evaluate("document.querySelector('#reload, .tc-action--refresh').click()")
                        continue

                    if "滑" in instr:
                        await self._handle_slider(frame)
                    elif "点击" in instr:
                        await self._handle_click(frame, instr, attempt)

                    await asyncio.sleep(1.5)
                    if await frame.locator("#slideBg").is_visible():
                        await frame.evaluate("document.querySelector('#reload, .tc-action--refresh').click()")
                except Exception:
                    try:
                        await frame.evaluate("document.querySelector('#reload, .tc-action--refresh').click()")
                    except Exception:
                        pass

            if ok:
                try:
                    await page.goto(f"{BASE_URL}/v2/web/index", wait_until="load", timeout=20000)
                except Exception:
                    pass
                await asyncio.sleep(BROWSER_SYNC_WAIT_SECONDS)
            else:
                await asyncio.sleep(2)
            cookies = await ctx.cookies()
            storage_state = await ctx.storage_state()
            await browser.close()

            if ok and any(c["name"] == "sessionid" for c in cookies):
                persist_cookie_records(cookies)
                persist_browser_state(storage_state)
                log("[+] 自动登录成功，Session 已保存")
                return True
            log("[-] 自动登录未能通过验证")
            return False

    async def _handle_slider(self, frame):
        s_bg = await frame.locator("#slideBg").get_attribute("style")
        s_bk = await frame.locator("#slideBlock").get_attribute("style")
        m_bg = re.search(r'url\("?(.+?)"?\)', s_bg.replace("&quot;", '"'))
        m_bk = re.search(r'url\("?(.+?)"?\)', s_bk.replace("&quot;", '"'))
        if m_bg and m_bk:
            def dl(url):
                target = url if url.startswith("http") else "https:" + url
                resp = requests.get(target, headers={"User-Agent": "Mozilla"}, timeout=15)
                resp.raise_for_status()
                return resp.content

            bg_b = dl(m_bg.group(1))
            bk_b = dl(m_bk.group(1))
            res = self.slider_ocr.slide_match(bk_b, bg_b, simple_target=True)
            box = await frame.locator("#slideBg").bounding_box()
            if box:
                scale = box["width"] / Image.open(BytesIO(bg_b)).size[0]
                btn = frame.locator(".tc-action--normal, #tcOperation").first
                await btn.drag_to(
                    btn,
                    source_position={"x": 0, "y": 0},
                    target_position={"x": res["target"][0] * scale, "y": 0},
                )

    async def _handle_click(self, frame, instr, attempt):
        s_bg = await frame.locator("#slideBg").get_attribute("style")
        match = re.search(r'url\("?(.+?)"?\)', s_bg.replace("&quot;", '"'))
        if not match:
            return
        url = match.group(1) if match.group(1).startswith("http") else "https:" + match.group(1)
        resp = requests.get(url, headers={"User-Agent": "Mozilla"}, timeout=15)
        resp.raise_for_status()
        bg_b = resp.content
        poses = self.det.detection(bg_b)
        img = Image.open(BytesIO(bg_b))
        width, height = img.size
        chars = []
        for p in poses:
            if (p[2] - p[0]) * (p[3] - p[1]) < 400:
                continue
            crop = img.crop(p)
            buf = BytesIO()
            crop.save(buf, "PNG")
            ch = self.ocr.classification(buf.getvalue())
            chars.append({"char": ch, "x": (p[0] + p[2]) / 2, "y": (p[1] + p[3]) / 2})
        elem = frame.locator("#slideBg")
        box = await elem.bounding_box()
        if box and chars:
            sx, sy = box["width"] / width, box["height"] / height
            for spot in self._select_spots(chars, instr, attempt):
                await elem.click(position={"x": spot["x"] * sx, "y": spot["y"] * sy}, force=True)
                await asyncio.sleep(0.5)
            try:
                await frame.evaluate("document.querySelector('.verify-btn.show').click()")
            except Exception:
                pass


class YuketangHelper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": GLOBAL_UA, "xtbz": "ykt", "Content-Type": "application/json"})
        self.sessionid = None
        self.csrftoken = None

    def _load_state(self):
        return load_state_dict()

    def _save_state(self, state):
        write_json_file(STATE_FILE, state)

    def _refresh_session_fields(self):
        self.sessionid = self.session.cookies.get("sessionid")
        self.csrftoken = self.session.cookies.get("csrftoken")
        self.session.headers.update({"User-Agent": GLOBAL_UA, "xtbz": "ykt", "Content-Type": "application/json"})
        if self.csrftoken:
            self.session.headers.update({"X-CSRFToken": self.csrftoken})
        else:
            self.session.headers.pop("X-CSRFToken", None)

    def _cookie_records_from_jar(self):
        cookies = []
        for cookie in self.session.cookies:
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "expires": cookie.expires,
                    "secure": bool(cookie.secure),
                }
            )
        return cookies

    def _set_cookie_records(self, cookies, clear=False):
        if clear:
            self.session.cookies.clear()
        for cookie in cookies:
            item = normalize_cookie_record(cookie)
            if not item:
                continue
            kwargs = {"domain": item["domain"], "path": item["path"]}
            if item["expires"]:
                kwargs["expires"] = int(item["expires"])
            self.session.cookies.set(item["name"], item["value"], **kwargs)
        self._refresh_session_fields()

    def _load_cookie_state_sources(self):
        sources = [(STATE_FILE, self._load_state())]
        if os.path.abspath(DESKTOP_STATE_FILE) != os.path.abspath(STATE_FILE):
            sources.append((DESKTOP_STATE_FILE, load_state_dict(DESKTOP_STATE_FILE)))
        return sources

    def _load_cookies_from_state(self):
        for path, raw in self._load_cookie_state_sources():
            cookie_list = extract_cookie_records(raw)
            if not cookie_list:
                continue
            self._set_cookie_records(cookie_list, clear=True)
            if self.session.cookies:
                if os.path.abspath(path) != os.path.abspath(STATE_FILE):
                    log(f"[*] 已从 {os.path.basename(path)} 导入 Cookie")
                return True
        self._set_cookie_records([], clear=True)
        return False

    def _probe_session(self):
        if not self.session.cookies.get("sessionid"):
            return False
        try:
            self.session.get(f"{BASE_URL}/v2/web/index", timeout=10)
            self._refresh_session_fields()
            resp = self.session.get(f"{BASE_URL}/api/v3/user/basic-info", timeout=10)
            if "application/json" not in resp.headers.get("Content-Type", ""):
                return False
            data = resp.json()
            if isinstance(data, dict) and (data.get("code") == 0 or data.get("success")):
                self._refresh_session_fields()
                self.save_session()
                return True
        except Exception:
            return False
        return False

    def _cookies_for_playwright(self):
        result = []
        for cookie in self.session.cookies:
            item = {
                "name": cookie.name,
                "value": cookie.value,
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
            }
            if cookie.expires:
                item["expires"] = int(cookie.expires)
            if cookie.domain:
                item["domain"] = cookie.domain
            else:
                item["url"] = BASE_URL
            result.append(item)
        return result

    async def _sync_with_browser(self, storage_state=None, seed_cookies=None):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context_kwargs = {"user_agent": GLOBAL_UA}
            if storage_state:
                context_kwargs["storage_state"] = storage_state
            ctx = await browser.new_context(**context_kwargs)
            if seed_cookies:
                await ctx.add_cookies(seed_cookies)
            page = await ctx.new_page()
            try:
                await page.goto(f"{BASE_URL}/v2/web/index", wait_until="load", timeout=25000)
            except Exception:
                try:
                    await page.goto(f"{BASE_URL}/v2/web/index", timeout=25000)
                except Exception:
                    pass
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            try:
                await page.evaluate(
                    """async () => {
                        const urls = [
                            '/api/v3/user/basic-info',
                            '/api/v3/classroom/on-lesson-upcoming-exam'
                        ];
                        for (const url of urls) {
                            try {
                                await fetch(url, {credentials: 'include'});
                            } catch (e) {}
                        }
                    }"""
                )
            except Exception:
                pass
            await asyncio.sleep(BROWSER_SYNC_WAIT_SECONDS)
            cookies = await ctx.cookies()
            storage_state = await ctx.storage_state()
            await browser.close()
            return cookies, storage_state

    def _bootstrap_browser_state(self):
        if not HAS_PLAYWRIGHT or not self.session.cookies.get("sessionid"):
            return False
        try:
            cookies, storage_state = asyncio.run(
                self._sync_with_browser(seed_cookies=self._cookies_for_playwright())
            )
        except Exception as e:
            log(f"[!] 浏览器态同步失败: {e}")
            return False
        if not cookies:
            return False
        self._set_cookie_records(cookies, clear=True)
        persist_browser_state(storage_state)
        if self._probe_session():
            log("[+] 已同步浏览器登录态")
            return True
        return False

    def _rehydrate_session_from_browser_state(self):
        browser_state = load_browser_state()
        if not HAS_PLAYWRIGHT or not browser_state:
            return False
        log("[*] Cookie 已失效，尝试用浏览器登录态恢复...")
        try:
            cookies, storage_state = asyncio.run(self._sync_with_browser(storage_state=browser_state))
        except Exception as e:
            log(f"[!] 浏览器登录态恢复失败: {e}")
            return False
        if not any(c.get("name") == "sessionid" for c in cookies):
            return False
        self._set_cookie_records(cookies, clear=True)
        persist_browser_state(storage_state)
        if self._probe_session():
            log("[+] 已从浏览器登录态恢复会话")
            return True
        return False

    def save_session(self):
        self._refresh_session_fields()
        persist_cookie_records(self._cookie_records_from_jar())

    def load_session(self):
        try:
            if self._load_cookies_from_state() and self._probe_session():
                if HAS_PLAYWRIGHT and not load_browser_state():
                    self._bootstrap_browser_state()
                return True
            return self._rehydrate_session_from_browser_state()
        except Exception:
            return False

    def _check_cooldown(self, lesson_id, emit_log=True):
        state = self._load_state()
        last = state.get("last_checkin") if isinstance(state, dict) else None
        if not last or str(last.get("lesson_id")) != str(lesson_id):
            return False
        try:
            elapsed = (
                datetime.now() - datetime.strptime(last["time"], "%Y-%m-%d %H:%M:%S")
            ).total_seconds() / 60
            if elapsed < CHECKIN_COOLDOWN_MINUTES:
                if emit_log:
                    log(f"[*] 课堂 {lesson_id} 在 {int(elapsed)} 分钟前已签到，跳过")
                return True
        except Exception:
            pass
        return False

    def _record_checkin(self, lesson_id):
        state = self._load_state()
        if not isinstance(state, dict):
            state = {}
        state["last_checkin"] = {
            "lesson_id": str(lesson_id),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_state(state)

    def auto_login(self, phone, password):
        if not HAS_AUTO_LOGIN:
            log("[!] 未安装 ddddocr/playwright，无法使用自动登录")
            return False
        bot = AutoLogin(phone, password)
        return asyncio.run(bot.run())

    def get_login_qrcode(self):
        log("[*] 正在请求微信授权参数...")
        try:
            resp = self.session.post(f"{BASE_URL}/api/v3/user/login/wechat-auth-param", json={})
            self._refresh_session_fields()
            self.csrftoken = self.session.cookies.get("csrftoken") or resp.cookies.get("csrftoken")
            data = resp.json()
            if data["code"] != 0:
                return None, None
            auth = data["data"]
            login_url = (
                f"https://open.weixin.qq.com/connect/qrconnect?appid={auth['appId']}"
                f"&redirect_uri={auth['redirectUri']}&response_type=code"
                f"&scope=snsapi_login&state={auth['state']}"
            )
            r_wx = requests.get(login_url + "&login_type=jssdk&self_redirect=true", headers={"User-Agent": GLOBAL_UA})
            uuid = re.search(r'src="/connect/qrcode/([^"]+)"', r_wx.text).group(1)
            qr = qrcode.QRCode()
            qr.add_data(f"https://open.weixin.qq.com/connect/confirm?uuid={uuid}")
            qr.make(fit=True)
            qr.print_ascii(invert=True)
            log("[*] 请使用微信扫描上方二维码登录")
            return auth["state"], uuid
        except Exception:
            return None, None

    def wait_for_login_and_callback(self, state, uuid):
        log("[*] 等待手机端确认...")
        while True:
            try:
                content = requests.get(
                    f"https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid={uuid}&_={int(time.time() * 1000)}",
                    timeout=30,
                ).text
                if "window.wx_errcode=405" in content:
                    code = re.search(r"window.wx_code='([^']+)'", content).group(1)
                    log("[+] 微信授权成功")
                    return self._finalize_login(code, state)
                if "window.wx_errcode=404" in content:
                    log("[*] 已扫码，请在手机上点击确认")
                elif "window.wx_errcode=403" in content:
                    return False
                time.sleep(2)
            except KeyboardInterrupt:
                return False
            except Exception:
                time.sleep(5)

    def _finalize_login(self, code, state):
        try:
            self.session.get(
                f"{BASE_URL}/api/v3/user/login/wechat-web-callback",
                params={"code": code, "state": state},
                allow_redirects=True,
            )
            self._refresh_session_fields()
            if self.session.cookies.get("sessionid"):
                self.save_session()
                if HAS_PLAYWRIGHT:
                    self._bootstrap_browser_state()
                return True
            return False
        except Exception:
            return False

    def get_active_lesson_data(self):
        try:
            self._refresh_session_fields()
            self.session.headers.update({"X-CSRFToken": self.csrftoken, "User-Agent": GLOBAL_UA})
            data = self.session.get(f"{BASE_URL}/api/v3/classroom/on-lesson-upcoming-exam").json()
            active = data.get("data", {}).get("onLessonClassrooms", [])
            self.save_session()
            if not active:
                return None, None
            return active[0].get("lessonId"), active[0].get("classroomId")
        except Exception:
            return None, None

    def sign_in(self, lesson_id, classroom_id=None, source=1, emit_log=True):
        if self._check_cooldown(lesson_id, emit_log=emit_log):
            return False
        self._refresh_session_fields()
        payload = {"lessonId": str(lesson_id), "source": source}
        headers = {
            "X-CSRFToken": self.csrftoken,
            "xtbz": "ykt",
            "User-Agent": GLOBAL_UA,
            "Referer": f"{BASE_URL}/v2/web/index",
        }
        if classroom_id:
            headers["Referer"] = f"{BASE_URL}/v2/web/studentLog/{classroom_id}"
        try:
            res = self.session.post(f"{BASE_URL}/api/v3/lesson/checkin", headers=headers, json=payload).json()
            if res.get("code") == 0:
                if emit_log:
                    log(f"[+] 签到成功 (课堂: {lesson_id})")
                self._record_checkin(lesson_id)
                self.save_session()
                return True
            else:
                if emit_log:
                    log(f"[-] 签到失败: {res.get('msg')}")
                return False
        except Exception as e:
            if emit_log:
                log(f"[!] 签到请求异常: {e}")
            return False

    def keep_alive(self):
        try:
            self.session.get(f"{BASE_URL}/v2/web/index", timeout=10)
            self._refresh_session_fields()
            self.session.headers.update({"X-CSRFToken": self.csrftoken, "User-Agent": GLOBAL_UA})
            resp = self.session.get(f"{BASE_URL}/api/v3/user/basic-info", timeout=10)
            if resp.json().get("code") == 0:
                log("[+] 会话保活成功")
                self.save_session()
                return True
            return False
        except Exception:
            return False


def build_scheduler(helper):
    return CheckinScheduler(
        SchedulerSettings(
            weekly_tasks=WEEKLY_TASKS,
            ics_enabled=ICS_ENABLED,
            ics_file=ICS_FILE,
            ics_lookahead_count=ICS_LOOKAHEAD_COUNT,
            ics_window_minutes=ICS_WINDOW_MINUTES,
            extension_minutes=SCHEDULER_EXTENSION_MINUTES,
            state_file=SCHEDULER_STATE_FILE,
            state_retention_days=SCHEDULER_STATE_RETENTION_DAYS,
            retry_interval_seconds=SCHEDULER_RETRY_INTERVAL_SECONDS,
            loop_interval_seconds=SCHEDULER_LOOP_INTERVAL_SECONDS,
            pushplus_token=PUSHPLUS_TOKEN,
            pushplus_channel=PUSHPLUS_CHANNEL,
            pushplus_template=PUSHPLUS_TEMPLATE,
            pushplus_title_template=PUSHPLUS_TITLE_TEMPLATE,
            pushplus_content_template=PUSHPLUS_CONTENT_TEMPLATE,
            backend_name="web",
        ),
        sign_func=lambda window: (
            (
                lambda lesson: helper.sign_in(lesson[0], classroom_id=lesson[1], emit_log=False)
                if lesson[0]
                else False
            )(helper.get_active_lesson_data())
        ),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="雨课堂自动签到助手（Web 账密登录版）")
    parser.add_argument("-a", "--auto", action="store_true", help="自动扫描课堂并签到")
    parser.add_argument("-k", "--keepalive", action="store_true", help="仅执行会话保活")
    parser.add_argument("-p", "--phone", type=str, help="手机号（覆盖脚本内置配置）")
    parser.add_argument("-pw", "--password", type=str, help="密码（覆盖脚本内置配置）")
    parser.add_argument(
        "--cooldown",
        type=int,
        default=CHECKIN_COOLDOWN_MINUTES,
        help=f"签到去重冷却时间，分钟（默认 {CHECKIN_COOLDOWN_MINUTES}）",
    )
    parser.add_argument(
        "-s",
        "--schedule",
        type=int,
        nargs="+",
        metavar="N",
        help="延迟 N 分钟后开始；可选再给一个数字作为检测间隔秒数（默认 60）",
    )
    parser.add_argument("--run-next", action="store_true", help="调度模式：只等最近一次任务窗口，执行后退出")
    parser.add_argument("--daemon", action="store_true", help="调度模式：持续检查周计划/ICS，命中窗口即自动签到，直到手动停止")
    args = parser.parse_args()

    CHECKIN_COOLDOWN_MINUTES = args.cooldown
    phone = args.phone or AUTO_LOGIN_PHONE
    password = args.password or AUTO_LOGIN_PSWD

    helper = YuketangHelper()
    auth = helper.load_session()

    if not auth:
        if HAS_AUTO_LOGIN:
            auth = helper.auto_login(phone, password)
            if auth:
                auth = helper.load_session()
        else:
            log("[*] 未检测到自动登录依赖 (ddddocr/playwright)，跳过自动登录")

    if not auth:
        log("[!] 登录失败，请检查账号、依赖和网络配置")
        sys.exit(1)

    if args.keepalive:
        sys.exit(0 if helper.keep_alive() else 1)

    if args.run_next or args.daemon:
        try:
            scheduler = build_scheduler(helper)
        except ValueError as e:
            log(f"[!] 调度配置错误: {e}")
            sys.exit(1)
        try:
            sys.exit(scheduler.run_next() if args.run_next else scheduler.run_daemon())
        except KeyboardInterrupt:
            sys.exit(0)

    if args.auto:
        lesson_id, classroom_id = helper.get_active_lesson_data()
        if lesson_id:
            sys.exit(0 if helper.sign_in(lesson_id, classroom_id=classroom_id) else 1)
        else:
            log("[-] 当前没有正在进行的课堂")
        sys.exit(0)

    if args.schedule is not None:
        if not (1 <= len(args.schedule) <= 2):
            log("[!] -s/--schedule 参数格式错误，应为: -s 延迟分钟 [检测秒数]")
            sys.exit(1)
        delay = args.schedule[0]
        interval_seconds = args.schedule[1] if len(args.schedule) == 2 else 60
        if interval_seconds <= 0:
            log("[!] 检测间隔必须大于 0 秒")
            sys.exit(1)
        if delay > 0:
            log(f"[*] 将在 {delay} 分钟后开始自动签到循环...")
            time.sleep(delay * 60)
        log(f"[*] 开始自动签到循环（每 {interval_seconds} 秒检测一次，Ctrl+C 退出）")
        try:
            while True:
                lesson_id, classroom_id = helper.get_active_lesson_data()
                if lesson_id:
                    helper.sign_in(lesson_id, classroom_id=classroom_id)
                else:
                    log(f"[-] 当前没有正在进行的课堂，{interval_seconds} 秒后重试...")
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            log("[*] 已停止定时签到")
        sys.exit(0)

    while True:
        print("\n1. 自动扫描签到\n2. 定时签到\n3. 退出")
        try:
            choice = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.exit(0)
        if choice == "1":
            lesson_id, classroom_id = helper.get_active_lesson_data()
            if lesson_id:
                helper.sign_in(lesson_id, classroom_id=classroom_id)
            else:
                log("[-] 当前没有正在进行的课堂")
        elif choice == "2":
            try:
                delay = int(input("请输入延迟分钟数 (0 = 立即开始): ").strip())
            except (ValueError, KeyboardInterrupt, EOFError):
                continue
            if delay > 0:
                log(f"[*] 将在 {delay} 分钟后开始自动签到循环...")
                time.sleep(delay * 60)
            log("[*] 开始自动签到循环（每 60 秒检测一次，Ctrl+C 返回菜单）")
            try:
                while True:
                    lesson_id, classroom_id = helper.get_active_lesson_data()
                    if lesson_id:
                        helper.sign_in(lesson_id, classroom_id=classroom_id)
                    else:
                        log("[-] 当前没有正在进行的课堂，60 秒后重试...")
                    time.sleep(60)
            except KeyboardInterrupt:
                log("[*] 已停止定时签到，返回菜单")
        elif choice == "3":
            sys.exit(0)
