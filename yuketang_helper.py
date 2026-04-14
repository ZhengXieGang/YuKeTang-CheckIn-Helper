#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import qrcode
import requests

# ========== 用户配置 ==========
BASE_DOMAIN = "changjiang.yuketang.cn"  # 雨课堂域名
CHECKIN_COOLDOWN_MINUTES = 15  # 重复签到冷却时间（分钟）
ENABLE_RUNTIME_LOG = False  # 是否输出运行日志（False=不输出运行日志）

WEEKLY_TASKS = [
    # 只需配置星期+时间点/时间段
    # {"days": [1, 3, 5], "time": "08:00"},
    # {"days": [2], "start": "14:00", "end": "14:20"},
]

ICS_ENABLED = False  # 是否启用 ICS 调度策略
ICS_FILENAME = "校验导出.ics"  # 只写文件名，脚本会在同目录读取
ICS_FILE = Path(__file__).resolve().with_name(ICS_FILENAME)
ICS_LOOKAHEAD_COUNT = 2  # 仅保留未来 N 个 ICS 文件时间点
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

SCHEDULER_STATE_FILE = Path(__file__).resolve().with_name("state.log")  # 调度状态+成功签到记录日志
SCHEDULER_STATE_RETENTION_DAYS = 7  # 状态保留天数
SCHEDULER_RETRY_INTERVAL_SECONDS = 20  # 窗口内失败重试间隔（秒）
SCHEDULER_LOOP_INTERVAL_SECONDS = 5  # 守护循环扫描间隔（秒）
# ==============================

GLOBAL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
BASE_URL = f"https://{BASE_DOMAIN}"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "yuketang_session.json")
DESKTOP_HEADERS = {
    "xtbz": "ykt",
    "desktop-v": "v2",
    "X-Client": "desktop",
    "Origin": "file://",
}


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
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_state_dict():
    state = read_json_file(STATE_FILE, {})
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


class YuketangHelper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": GLOBAL_UA})

    def _load_state(self):
        return load_state_dict()

    def _save_state(self, state):
        write_json_file(STATE_FILE, state)



    def _cookie_records_from_jar(self):
        records = []
        for cookie in self.session.cookies:
            records.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain or BASE_DOMAIN,
                    "path": cookie.path or "/",
                    "expires": int(cookie.expires) if cookie.expires else None,
                    "secure": bool(cookie.secure),
                }
            )
        return records

    def _set_cookie_records(self, cookies, clear=False):
        if clear:
            self.session.cookies.clear()
        for cookie in cookies:
            item = normalize_cookie_record(cookie)
            if not item:
                continue
            kwargs = {"domain": item["domain"], "path": item["path"]}
            if item["expires"]:
                kwargs["expires"] = item["expires"]
            self.session.cookies.set(item["name"], item["value"], **kwargs)

    def _cookie_signature(self):
        return tuple(
            sorted(
                (
                    cookie.name,
                    cookie.value,
                    cookie.domain or BASE_DOMAIN,
                    cookie.path or "/",
                    int(cookie.expires) if cookie.expires else None,
                    bool(cookie.secure),
                )
                for cookie in self.session.cookies
            )
        )

    def _get_cookie_map(self):
        result = {}
        for cookie in self.session.cookies:
            result[cookie.name] = {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or BASE_DOMAIN,
                "path": cookie.path or "/",
                "expires": int(cookie.expires) if cookie.expires else None,
                "secure": bool(cookie.secure),
            }
        return result

    def _describe_login_state(self):
        cookie_map = self._get_cookie_map()
        for name in ("sid", "sessionid"):
            cookie = cookie_map.get(name)
            if not cookie:
                continue
            expires = cookie.get("expires")
            expires_text = (
                datetime.fromtimestamp(expires).strftime("%Y-%m-%d %H:%M:%S")
                if expires
                else "session"
            )
            return f"{name} 有效至 {expires_text}"
        return "无可用登录态"

    def save_session(self):
        state = self._load_state()
        if not isinstance(state, dict):
            state = {}
        for legacy_key in ("cookies", "browser_state", "sessionid", "csrftoken",
                          "desktop_auth", "desktop_auth_updated_at"):
            state.pop(legacy_key, None)
        cookie_records = self._cookie_records_from_jar()
        if cookie_records:
            # 只有 jar 中有 Cookie 时才更新，防止意外清空
            state["desktop_cookies"] = cookie_records
            state["desktop_cookies_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 如果 jar 为空，保留文件中原有的 Cookie 数据不动
        self._save_state(state)

    def _desktop_request(self, method, path, timeout=20, headers=None, **kwargs):
        old_cookie_signature = self._cookie_signature()
        req_headers = {
            "User-Agent": GLOBAL_UA,
            "Content-Type": "application/json",
            **DESKTOP_HEADERS,
        }
        if headers:
            req_headers.update(headers)
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        resp = self.session.request(method, url, headers=req_headers, timeout=timeout, **kwargs)
        if self._cookie_signature() != old_cookie_signature:
            self.save_session()
        return resp

    def _probe_login_state(self):
        try:
            resp = self._desktop_request("get", "/api/v3/user/basic-info", timeout=10)
            if "application/json" not in resp.headers.get("Content-Type", ""):
                return None
            data = resp.json()
            if data.get("code") == 0:
                self.save_session()
                return "cookie"
        except Exception:
            return None
        return None

    def _bootstrap_login_state_after_login(self):
        probe_paths = [
            "/api/v3/user/basic-info",
            "/api/v3/classroom/on-lesson-upcoming-exam",
        ]
        for path in probe_paths:
            try:
                resp = self._desktop_request("get", path, timeout=10)
                if "application/json" not in resp.headers.get("Content-Type", ""):
                    continue
                data = resp.json()
                if data.get("code") == 0:
                    self.save_session()
                    return "cookie"
            except Exception:
                continue
        return None

    def load_session(self):
        try:
            state = self._load_state()
            self._set_cookie_records(state.get("desktop_cookies", []), clear=True)
            if not self.session.cookies:
                return False
            mode = self._probe_login_state()
            if mode == "cookie":
                log(f"[+] Cookie 加载成功，{self._describe_login_state()}")
                return True
            log("[-] 桌面端登录态已失效")
            return False
        except Exception as e:
            log(f"[-] 加载登录态失败: {e}")
            return False

    def _check_cooldown(self, lesson_id, emit_log=True):
        state = self._load_state()
        last = state.get("last_checkin") if isinstance(state, dict) else None
        if not last or str(last.get("lesson_id")) != str(lesson_id):
            return False, ""
        try:
            elapsed = (
                datetime.now() - datetime.strptime(last["time"], "%Y-%m-%d %H:%M:%S")
            ).total_seconds() / 60
            if elapsed < CHECKIN_COOLDOWN_MINUTES:
                message = f"课堂 {lesson_id} 在 {int(elapsed)} 分钟前已签到，跳过"
                if emit_log:
                    log(f"[*] {message}")
                return True, message
        except Exception:
            pass
        return False, ""

    def _record_checkin(self, lesson_id):
        state = self._load_state()
        if not isinstance(state, dict):
            state = {}
        state["last_checkin"] = {
            "lesson_id": str(lesson_id),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_state(state)

    def get_login_qrcode(self):
        log("[*] 正在请求桌面端登录二维码...")
        try:
            resp = self._desktop_request("get", "/api/v3/user/login/pre-info", timeout=20)
            data = resp.json()
            if data.get("code") != 0:
                log(f"[-] 获取二维码失败: {data.get('msg')}")
                return None, None
            info = data.get("data") or {}
            qr_content = info.get("qrContent")
            login_token = info.get("token")
            if not qr_content or not login_token:
                log("[-] 二维码响应缺少必要字段")
                return None, None
            qr = qrcode.QRCode()
            qr.add_data(qr_content)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
            log("[*] 请使用微信扫描上方二维码登录桌面端")
            return login_token, qr_content
        except Exception as e:
            log(f"[-] 获取二维码失败: {e}")
            return None, None

    def wait_for_login_and_callback(self, login_token, max_wait=300):
        log("[*] 等待手机端扫码并确认...")
        start = time.time()
        last_status = None
        while True:
            if max_wait and time.time() - start > max_wait:
                log("[-] 等待登录超时，请重新获取二维码")
                return False
            try:
                resp = self._desktop_request(
                    "post",
                    "/api/v3/user/login",
                    json={"token": login_token},
                    timeout=(10, 35),
                )
            except requests.ReadTimeout:
                continue
            except KeyboardInterrupt:
                return False
            except Exception as e:
                log(f"[!] 轮询登录状态失败: {e}")
                time.sleep(2)
                continue

            try:
                data = resp.json()
            except Exception:
                log("[-] 登录接口返回了非 JSON 响应")
                time.sleep(2)
                continue

            code = data.get("code")
            msg = data.get("msg") or data.get("message") or f"code={code}"
            if code == 0:
                mode = self._probe_login_state() or self._bootstrap_login_state_after_login()
                if mode == "cookie":
                    log(f"[+] 桌面端登录成功，{self._describe_login_state()}")
                    return True
                header_keys = ", ".join(sorted(resp.headers.keys()))
                log(
                    f"[-] 登录成功，但未建立可复用的登录态；"
                    f"响应头: {header_keys or '无'}；当前状态: {self._describe_login_state()}"
                )
                return False

            if code in (500, 50000, 50001):
                log(f"[-] 二维码已失效: {msg}")
                return False

            if msg != last_status:
                log(f"[*] 登录状态: {msg}")
                last_status = msg
            time.sleep(2)

    def _fetch_active_lesson_result(self):
        try:
            resp = self._desktop_request("get", "/api/v3/classroom/on-lesson-upcoming-exam", timeout=10)
            data = resp.json()
            if data.get("code") != 0:
                return {
                    "state": "error",
                    "log_prefix": "[-]",
                    "message": f"获取课堂列表失败: {data.get('msg')}",
                    "lesson_id": None,
                    "classroom_id": None,
                }
            active = data.get("data", {}).get("onLessonClassrooms", [])
            if not active:
                return {
                    "state": "idle",
                    "log_prefix": "[-]",
                    "message": "当前没有正在进行的课堂",
                    "lesson_id": None,
                    "classroom_id": None,
                }
            classroom = active[0]
            return {
                "state": "active",
                "log_prefix": "[*]",
                "message": f"检测到课堂 {classroom.get('lessonId')}",
                "lesson_id": classroom.get("lessonId"),
                "classroom_id": classroom.get("classroomId"),
            }
        except Exception as e:
            return {
                "state": "error",
                "log_prefix": "[!]",
                "message": f"获取课堂列表异常: {e}",
                "lesson_id": None,
                "classroom_id": None,
            }

    def get_active_lesson_data(self):
        result = self._fetch_active_lesson_result()
        if result["state"] == "error":
            log(f"{result['log_prefix']} {result['message']}")
        if result["state"] != "active":
            return None, None
        return result["lesson_id"], result["classroom_id"]

    def _perform_sign_in(self, lesson_id, classroom_id=None, source=1):
        on_cooldown, cooldown_message = self._check_cooldown(lesson_id, emit_log=False)
        if on_cooldown:
            return {
                "success": False,
                "state": "cooldown",
                "log_prefix": "[*]",
                "message": cooldown_message,
            }
        payload = {"lessonId": str(lesson_id), "source": source}
        headers = {"Referer": f"{BASE_URL}/v2/web/index"}
        if classroom_id:
            headers["Referer"] = f"{BASE_URL}/v2/web/studentLog/{classroom_id}"
        try:
            resp = self._desktop_request(
                "post",
                "/api/v3/lesson/checkin",
                headers=headers,
                json=payload,
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == 0:
                self._record_checkin(lesson_id)
                self.save_session()
                return {
                    "success": True,
                    "state": "success",
                    "log_prefix": "[+]",
                    "message": f"签到成功 (课堂: {lesson_id})",
                }
            return {
                "success": False,
                "state": "failed",
                "log_prefix": "[-]",
                "message": f"签到失败: {data.get('msg')}",
            }
        except Exception as e:
            return {
                "success": False,
                "state": "error",
                "log_prefix": "[!]",
                "message": f"签到请求异常: {e}",
            }

    def sign_in(self, lesson_id, classroom_id=None, source=1, emit_log=True):
        result = self._perform_sign_in(lesson_id, classroom_id=classroom_id, source=source)
        if emit_log:
            log(f"{result['log_prefix']} {result['message']}")
        return result["success"]

    def auto_sign_once(self, emit_log=True):
        lesson_result = self._fetch_active_lesson_result()
        if lesson_result["state"] != "active":
            if emit_log:
                log(f"{lesson_result['log_prefix']} {lesson_result['message']}")
            return lesson_result
        sign_result = self._perform_sign_in(
            lesson_result["lesson_id"],
            classroom_id=lesson_result["classroom_id"],
        )
        if emit_log:
            log(f"{sign_result['log_prefix']} {sign_result['message']}")
        return sign_result

    def keep_alive(self):
        try:
            resp = self._desktop_request("get", "/api/v3/user/basic-info", timeout=10)
            data = resp.json()
            if data.get("code") == 0:
                log(f"[+] 会话保活成功，{self._describe_login_state()}")
                self.save_session()
                return True
            log(f"[-] 会话保活失败: {data.get('msg')}")
            return False
        except Exception as e:
            log(f"[!] 会话保活异常: {e}")
            return False
def ensure_login(helper, allow_interactive_login):
    auth = helper.load_session()
    if auth:
        return True
    if not allow_interactive_login:
        log("[!] 当前没有可用登录态，且本次环境不适合交互扫码登录")
        return False
    login_token, _ = helper.get_login_qrcode()
    if not login_token:
        return False
    return helper.wait_for_login_and_callback(login_token)


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
            backend_name="desktop",
        ),
        sign_func=lambda window: helper.auto_sign_once(emit_log=False).get("success", False),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="雨课堂自动签到助手（桌面端登录版）")
    parser.add_argument("-a", "--auto", action="store_true", help="自动扫描课堂并签到")
    parser.add_argument("-k", "--keepalive", action="store_true", help="仅执行会话保活")
    parser.add_argument("--qr", action="store_true", help="显示桌面端登录二维码")
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

    helper = YuketangHelper()
    interactive_login_allowed = sys.stdin.isatty() and sys.stdout.isatty()

    if args.qr:
        if not interactive_login_allowed:
            log("[!] 当前环境不适合交互扫码登录")
            sys.exit(1)
        login_token, _ = helper.get_login_qrcode()
        sys.exit(0 if login_token and helper.wait_for_login_and_callback(login_token) else 1)

    auth = ensure_login(helper, allow_interactive_login=interactive_login_allowed)
    if not auth:
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
        print("\n1. 自动扫描签到\n2. 重新扫码登录\n3. 定时签到\n4. 会话保活\n5. 退出")
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
            login_token, _ = helper.get_login_qrcode()
            if login_token:
                helper.wait_for_login_and_callback(login_token)
        elif choice == "3":
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
        elif choice == "4":
            helper.keep_alive()
        elif choice == "5":
            sys.exit(0)
