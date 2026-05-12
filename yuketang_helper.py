#!/usr/bin/env python3
import atexit
import argparse
import json
import os
import re
import signal
import sys
import tempfile
import time
from datetime import datetime, timedelta

import qrcode
import requests

try:
    import fcntl
except ImportError:
    fcntl = None

# ========== 用户配置 ==========
BASE_DOMAIN = "changjiang.yuketang.cn"  # 默认长江雨课堂，自行更换
CHECKIN_COOLDOWN_MINUTES = 15           # 签到冷却时长 （分钟）
SCHEDULE_INTERVAL_SECONDS = 20          # 持续检测间隔（秒）
SCHEDULE_TIMEOUT_MINUTES = 30           # 首轮超时时间（分钟）
THROTTLED_LOG_INTERVAL_SECONDS = 300    # 同类重复日志节流窗口（秒）

PUSHPLUS_TOKEN = ""                     # 留空则关闭推送
PUSHPLUS_CHANNEL = "wechat"             # wechat / mail / webhook / cp / sms
PUSHPLUS_TEMPLATE = "txt"
PUSHPLUS_TITLE_TEMPLATE = "雨课堂签到成功 - {lesson_name}"
PUSHPLUS_CONTENT_TEMPLATE = (
    "签到成功\n"
    "模式：{backend}\n"
    "课堂：{lesson_name}\n"
    "课堂号：{lesson_id}\n"
    "时间：{success_time}"
)
# ==============================

GLOBAL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
BASE_URL = f"https://{BASE_DOMAIN}"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "yuketang_session.json")
RUNTIME_LOCK_FILE = os.path.join(SCRIPT_DIR, ".yuketang_runtime.lock")
DESKTOP_HEADERS = {
    "xtbz": "ykt",
    "desktop-v": "v2",
    "X-Client": "desktop",
    "Origin": "file://",
}
SESSION_COOKIE_NAMES = ("sessionid", "sid", "csrftoken")


def log(msg):
    print(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}")


class SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def read_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json_file(path, data):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


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


class RuntimeFileLock:
    def __init__(self, path):
        self.path = path
        self._file = None
        self._locked = False

    def _read_owner(self):
        if not self._file:
            return {}
        try:
            self._file.seek(0)
            raw = self._file.read().strip()
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def owner_data(self):
        data = self._read_owner()
        return data if isinstance(data, dict) else {}

    def owner_text(self):
        data = self.owner_data()
        if not data:
            return ""
        parts = []
        pid = data.get("pid")
        if pid:
            parts.append(f"PID {pid}")
        started_at = data.get("started_at")
        if started_at:
            parts.append(f"启动于 {started_at}")
        argv = data.get("argv")
        if isinstance(argv, list) and argv:
            parts.append("命令: " + " ".join(str(item) for item in argv))
        return "，".join(parts)

    def acquire(self):
        if fcntl is None:
            return True
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._file = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self._locked = True
        self._file.seek(0)
        self._file.truncate()
        json.dump(
            {
                "pid": os.getpid(),
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "argv": sys.argv,
            },
            self._file,
            ensure_ascii=False,
        )
        self._file.flush()
        return True

    def release(self):
        if not self._file:
            return
        try:
            if fcntl is not None and self._locked:
                self._file.seek(0)
                self._file.truncate()
                self._file.flush()
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
            self._locked = False


def install_signal_handlers():
    sigterm = getattr(signal, "SIGTERM", None)
    if sigterm is None:
        return

    def handle_sigterm(signum, frame):
        log("[*] 收到停止信号，正在退出当前任务")
        raise SystemExit(0)

    signal.signal(sigterm, handle_sigterm)


def runtime_mode_from_argv(argv):
    argv = [str(item) for item in (argv or [])]
    if any(item in ("-a", "-auto") for item in argv):
        return "持续签到任务"
    if any(item in ("-s", "-schedule") for item in argv):
        return "定时签到任务"
    if any(item in ("-k", "-keepalive") for item in argv):
        return "会话保活任务"
    if any(item == "-qr" for item in argv):
        return "扫码登录任务"
    return "脚本任务"


def is_signin_task_argv(argv):
    argv = [str(item) for item in (argv or [])]
    return any(item in ("-a", "-auto", "-s", "-schedule") for item in argv)


def pid_exists(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_running_runtime_owner():
    lock = RuntimeFileLock(RUNTIME_LOCK_FILE)
    if lock.acquire():
        lock.release()
        return None
    owner = lock.owner_data()
    lock.release()
    return owner if owner else {}


def stop_runtime_owner(owner, signin_only=False, announce=True):
    owner = owner if isinstance(owner, dict) else {}
    if not owner:
        if announce:
            if signin_only:
                log("[*] 当前没有后台签到任务在运行")
            else:
                log("[*] 当前没有后台任务在运行")
        return True

    argv = owner.get("argv") if isinstance(owner.get("argv"), list) else []
    mode_text = runtime_mode_from_argv(argv)
    pid = owner.get("pid")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        pid = None

    if signin_only and not is_signin_task_argv(argv):
        if announce:
            log(f"[*] 当前运行中的不是签到任务（{mode_text}），无需停止")
        return True

    if pid == os.getpid():
        if announce:
            log("[*] 当前实例就是目标任务，无需停止")
        return True

    owner_suffix = []
    if pid:
        owner_suffix.append(f"PID {pid}")
    started_at = owner.get("started_at")
    if started_at:
        owner_suffix.append(f"启动于 {started_at}")
    suffix = f"（{'，'.join(owner_suffix)}）" if owner_suffix else ""

    if announce:
        log(f"[*] 检测到后台{mode_text}正在运行{suffix}，准备停止")

    if not pid:
        log("[!] 无法确定后台任务 PID，停止失败")
        return False

    if not pid_exists(pid):
        log(f"[*] 后台任务 PID {pid} 已不存在，视为已停止")
        return True

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        log(f"[!] 停止后台任务失败: {e}")
        return False

    deadline = time.time() + 8
    while time.time() < deadline:
        if not pid_exists(pid):
            log(f"[+] 后台任务已停止（PID {pid}）")
            return True
        time.sleep(0.2)

    sigkill = getattr(signal, "SIGKILL", None)
    if sigkill is not None:
        log(f"[!] 后台任务未及时退出，尝试强制停止（PID {pid}）")
        try:
            os.kill(pid, sigkill)
        except OSError as e:
            log(f"[!] 强制停止后台任务失败: {e}")
            return False
        deadline = time.time() + 3
        while time.time() < deadline:
            if not pid_exists(pid):
                log(f"[+] 后台任务已强制停止（PID {pid}）")
                return True
            time.sleep(0.2)

    log(f"[!] 后台任务仍在运行（PID {pid}），本次停止失败")
    return False


def stop_background_signin_tasks():
    owner = read_running_runtime_owner()
    return stop_runtime_owner(owner, signin_only=True, announce=True)


def acquire_runtime_lock(stop_conflict=False):
    lock = RuntimeFileLock(RUNTIME_LOCK_FILE)
    if lock.acquire():
        atexit.register(lock.release)
        return lock
    owner_data = lock.owner_data()
    owner = lock.owner_text()
    suffix = f"（{owner}）" if owner else ""
    lock.release()
    if stop_conflict:
        log(f"[*] 检测到同目录已有另一个实例正在运行{suffix}，先停止旧任务再继续")
        if stop_runtime_owner(owner_data, signin_only=False, announce=False):
            for _ in range(40):
                retry_lock = RuntimeFileLock(RUNTIME_LOCK_FILE)
                if retry_lock.acquire():
                    atexit.register(retry_lock.release)
                    return retry_lock
                retry_lock.release()
                time.sleep(0.2)
        log("[!] 停止旧任务后仍无法获取运行锁，本次退出")
        return None
    log(f"[!] 检测到同目录已有另一个实例正在运行{suffix}，为避免覆盖登录态，本次退出")
    return None


class YuketangHelper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": GLOBAL_UA})
        self._throttled_logs = {}
        self._last_active_lesson_state = "unknown"
        self._last_active_lesson_info = {}

    def _normalize_log_key(self, value):
        return re.sub(r"0x[0-9a-fA-F]+", "0x*", str(value)).strip()

    def _log_throttled(self, key, message, interval_seconds=THROTTLED_LOG_INTERVAL_SECONDS):
        now = time.time()
        entry = self._throttled_logs.get(key)
        if not entry:
            self._throttled_logs[key] = {"last_logged_at": now, "suppressed": 0}
            log(message)
            return

        if now - entry["last_logged_at"] >= interval_seconds:
            suppressed = entry["suppressed"]
            entry["last_logged_at"] = now
            entry["suppressed"] = 0
            if suppressed > 0:
                log(f"{message}（同类日志已抑制 {suppressed} 次）")
            else:
                log(message)
            return

        entry["suppressed"] += 1

    def _load_state(self):
        return load_state_dict()

    def _save_state(self, state):
        write_json_file(STATE_FILE, state)

    @staticmethod
    def _coerce_cookie_expiry(value):
        if value in (None, "", 0, "0"):
            return None
        try:
            expiry = float(value)
        except Exception:
            return None
        if expiry > 10_000_000_000:
            expiry /= 1000
        if expiry <= 0:
            return None
        return int(expiry)

    @staticmethod
    def _normalize_cookie_domain(value):
        return str(value or "").strip().lstrip(".")

    def _cookie_matches_base_domain(self, domain):
        cookie_domain = self._normalize_cookie_domain(domain)
        base_domain = self._normalize_cookie_domain(BASE_DOMAIN)
        if not cookie_domain or not base_domain:
            return True
        return (
            cookie_domain == base_domain
            or cookie_domain.endswith(f".{base_domain}")
            or base_domain.endswith(f".{cookie_domain}")
        )

    def _cookie_sort_key(self, cookie):
        domain = self._normalize_cookie_domain(cookie.get("domain"))
        path = str(cookie.get("path") or "/")
        return (
            1 if domain == self._normalize_cookie_domain(BASE_DOMAIN) else 0,
            1 if path == "/" else 0,
            1 if bool(cookie.get("secure")) else 0,
            1 if cookie.get("expires") else 0,
        )

    def _prune_cookie_records(self, cookie_records):
        kept = {}
        for item in cookie_records:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip().lower()
            value = item.get("value")
            if name not in SESSION_COOKIE_NAMES or value is None:
                continue
            if not self._cookie_matches_base_domain(item.get("domain")):
                continue
            normalized = {
                "name": name,
                "value": str(value),
                "domain": self._normalize_cookie_domain(item.get("domain")) or BASE_DOMAIN,
                "path": str(item.get("path") or "/"),
                "expires": self._coerce_cookie_expiry(
                    item.get("expires", item.get("expirationDate", item.get("expiry")))
                ),
                "secure": bool(item.get("secure", False)),
            }
            existing = kept.get(name)
            if existing is None or self._cookie_sort_key(normalized) >= self._cookie_sort_key(existing):
                kept[name] = normalized
        order = {name: index for index, name in enumerate(SESSION_COOKIE_NAMES)}
        return sorted(kept.values(), key=lambda item: order.get(item["name"], 99))

    def _normalize_auth(self, auth_value):
        raw = (auth_value or "").strip()
        if not raw:
            return "", ""
        if raw.lower().startswith("bearer "):
            token = raw[7:].strip()
            return token, (f"Bearer {token}" if token else "")
        return raw, f"Bearer {raw}"

    def _set_desktop_auth(self, auth_value):
        token, header = self._normalize_auth(auth_value)
        if header:
            self.session.headers["Authorization"] = header
        else:
            self.session.headers.pop("Authorization", None)
        return bool(token)

    def _try_extract_auth_from_response(self, resp):
        if resp is None:
            return False
        old_header = self.session.headers.get("Authorization") or ""
        candidates = [resp, *(getattr(resp, "history", []) or [])]
        for item in candidates:
            headers = getattr(item, "headers", {}) or {}
            for key in ("set-auth", "Set-Auth", "authorization", "Authorization"):
                value = headers.get(key)
                if not isinstance(value, str) or not value.strip():
                    continue
                self._set_desktop_auth(value)
                new_header = self.session.headers.get("Authorization") or ""
                return new_header != old_header
        return False

    def _sync_csrf_header(self):
        csrftoken = self.session.cookies.get("csrftoken")
        if csrftoken:
            self.session.headers["X-CSRFToken"] = csrftoken
        else:
            self.session.headers.pop("X-CSRFToken", None)

    def _cookie_records_from_jar(self):
        records = []
        for cookie in self.session.cookies:
            records.append(
                {
                    "name": str(cookie.name).lower(),
                    "value": cookie.value,
                    "domain": self._normalize_cookie_domain(cookie.domain or BASE_DOMAIN),
                    "path": cookie.path or "/",
                    "expires": self._coerce_cookie_expiry(cookie.expires),
                    "secure": bool(cookie.secure),
                }
            )
        return self._prune_cookie_records(records)

    def _set_cookie_records(self, cookies, clear=False):
        if clear:
            self.session.cookies.clear()
        for cookie in cookies:
            item = normalize_cookie_record(cookie)
            if not item:
                continue
            kwargs = {
                "domain": self._normalize_cookie_domain(item["domain"]) or BASE_DOMAIN,
                "path": item["path"],
                "secure": bool(item.get("secure", False)),
            }
            if item["expires"]:
                kwargs["expires"] = item["expires"]
            self.session.cookies.set(str(item["name"]).lower(), item["value"], **kwargs)
        self._sync_csrf_header()

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
        for cookie in self._cookie_records_from_jar():
            result[cookie["name"]] = cookie
        return result

    def _describe_login_state(self):
        cookie_map = self._get_cookie_map()
        token, _ = self._normalize_auth(self.session.headers.get("Authorization"))
        auth_text = "，Authorization 已加载" if token else ""
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
            return f"{name} 有效至 {expires_text}{auth_text}"
        if token:
            return "Authorization 已加载"
        return "无可用登录态"

    def save_session(self):
        state = self._load_state()
        if not isinstance(state, dict):
            state = {}
        for legacy_key in ("cookies", "browser_state", "sessionid", "csrftoken"):
            state.pop(legacy_key, None)
        state["base_domain"] = BASE_DOMAIN
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        token, _ = self._normalize_auth(self.session.headers.get("Authorization"))
        if token:
            state["desktop_auth"] = token
            state["desktop_auth_updated_at"] = now_text
        else:
            state.pop("desktop_auth", None)
            state.pop("desktop_auth_updated_at", None)
        cookie_records = self._cookie_records_from_jar()
        if cookie_records:
            state["desktop_cookies"] = cookie_records
            state["desktop_cookies_updated_at"] = now_text
        else:
            state.pop("desktop_cookies", None)
            state.pop("desktop_cookies_updated_at", None)
        self._save_state(state)

    def _desktop_request(
        self,
        method,
        path,
        timeout=20,
        headers=None,
        use_auth="auto",
        persist_state_on_change=False,
        **kwargs,
    ):
        old_auth = self.session.headers.get("Authorization") or ""
        old_cookie_signature = self._cookie_signature()
        req_headers = {
            "User-Agent": GLOBAL_UA,
            "Content-Type": "application/json",
            **DESKTOP_HEADERS,
        }
        auth_header = self.session.headers.get("Authorization")
        if use_auth == "auto":
            should_send_auth = bool(auth_header)
        else:
            should_send_auth = bool(auth_header) and bool(use_auth)
        if should_send_auth:
            req_headers["Authorization"] = auth_header
        elif auth_header:
            req_headers["Authorization"] = None
        if headers:
            req_headers.update(headers)
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        resp = self.session.request(method, url, headers=req_headers, timeout=timeout, **kwargs)
        auth_changed = self._try_extract_auth_from_response(resp)
        self._sync_csrf_header()
        current_auth = self.session.headers.get("Authorization") or ""
        cookie_changed = self._cookie_signature() != old_cookie_signature
        if persist_state_on_change and (auth_changed or current_auth != old_auth or cookie_changed):
            self.save_session()
        return resp

    def _probe_login_state(self):
        original_cookies = self._cookie_records_from_jar()
        original_auth = self.session.headers.get("Authorization")
        last_network_error = None
        for attempt in range(3):
            try:
                resp = self._desktop_request(
                    "get",
                    "/api/v3/user/basic-info",
                    timeout=10,
                    persist_state_on_change=False,
                )
                if "application/json" not in resp.headers.get("Content-Type", ""):
                    self._set_cookie_records(original_cookies, clear=True)
                    self._set_desktop_auth(original_auth)
                    return None
                data = resp.json()
                if self._is_authenticated_basic_info(data):
                    self.save_session()
                    token, _ = self._normalize_auth(self.session.headers.get("Authorization"))
                    if token:
                        return "token"
                    if self.session.cookies:
                        return "cookie"
                self._set_cookie_records(original_cookies, clear=True)
                self._set_desktop_auth(original_auth)
                return None
            except requests.RequestException as e:
                last_network_error = e
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
            except Exception:
                self._set_cookie_records(original_cookies, clear=True)
                self._set_desktop_auth(original_auth)
                return None
        self._set_cookie_records(original_cookies, clear=True)
        self._set_desktop_auth(original_auth)
        if last_network_error is not None:
            log(f"[!] 登录态探测网络异常: {last_network_error}")
            return "network_error"
        return None

    @staticmethod
    def _is_authenticated_basic_info(data):
        if not isinstance(data, dict) or data.get("code") != 0:
            return False
        payload = data.get("data")
        if not isinstance(payload, dict) or not payload:
            return False
        for key in ("user_id", "id", "userId", "username", "name"):
            value = payload.get(key)
            if isinstance(value, str):
                if value.strip():
                    return True
            elif value:
                return True
        return bool(payload)

    @staticmethod
    def _extract_lesson_info(classroom):
        classroom = classroom if isinstance(classroom, dict) else {}
        lesson_id = classroom.get("lessonId")
        classroom_id = classroom.get("classroomId")
        lesson_name = (
            classroom.get("courseName")
            or classroom.get("lessonName")
            or classroom.get("classroomName")
            or classroom.get("name")
            or classroom.get("title")
            or ""
        )
        classroom_name = classroom.get("classroomName") or ""
        return {
            "lesson_id": str(lesson_id or "").strip(),
            "classroom_id": str(classroom_id or "").strip(),
            "lesson_name": str(lesson_name or "").strip(),
            "classroom_name": str(classroom_name or "").strip(),
        }

    @staticmethod
    def _lesson_display(lesson_id, lesson_info=None):
        info = lesson_info if isinstance(lesson_info, dict) else {}
        name = str(info.get("lesson_name") or "").strip()
        classroom_name = str(info.get("classroom_name") or "").strip()
        if name and classroom_name and classroom_name != name:
            return f"{name} / {classroom_name}"
        if name:
            return name
        return str(lesson_id)

    def _lesson_info_for(self, lesson_id, lesson_info=None):
        if isinstance(lesson_info, dict) and lesson_info:
            return lesson_info
        last_info = self._last_active_lesson_info
        if isinstance(last_info, dict) and str(last_info.get("lesson_id") or "") == str(lesson_id):
            return last_info
        return {}

    def _bootstrap_login_state_after_login(self):
        probe_paths = ["/api/v3/user/basic-info"]
        for path in probe_paths:
            try:
                resp = self._desktop_request("get", path, timeout=10)
                if "application/json" not in resp.headers.get("Content-Type", ""):
                    continue
                data = resp.json()
                if self._is_authenticated_basic_info(data):
                    self.save_session()
                    token, _ = self._normalize_auth(self.session.headers.get("Authorization"))
                    if token:
                        return "token"
                    if self.session.cookies:
                        return "cookie"
            except Exception:
                continue
        return None

    def _try_finalize_login_success(self, attempts=1, delay_seconds=1):
        attempts = max(1, int(attempts or 1))
        delay_seconds = max(0, float(delay_seconds or 0))
        for attempt in range(attempts):
            mode = self._probe_login_state() or self._bootstrap_login_state_after_login()
            if mode in ("cookie", "token"):
                log(f"[+] 桌面端登录成功，{self._describe_login_state()}")
                return True
            if attempt < attempts - 1 and delay_seconds > 0:
                time.sleep(delay_seconds)
        return False

    def load_session(self, verify_online=True):
        try:
            state = self._load_state()
            self._set_desktop_auth(state.get("desktop_auth"))
            self._set_cookie_records(state.get("desktop_cookies", []), clear=True)
            token, _ = self._normalize_auth(self.session.headers.get("Authorization"))
            if not self.session.cookies and not token:
                return False
            if not verify_online:
                return True
            mode = self._probe_login_state()
            if mode in ("cookie", "token"):
                label = "Authorization" if mode == "token" else "Cookie"
                log(f"[+] {label} 加载成功，{self._describe_login_state()}")
                return True
            if mode == "network_error":
                log(f"[!] 登录态在线校验失败（网络异常），先沿用本地会话：{self._describe_login_state()}")
                return True
            log("[-] 桌面端登录态已失效")
            return False
        except Exception as e:
            log(f"[-] 加载登录态失败: {e}")
            return False

    def _check_cooldown(self, lesson_id, emit_log=True, lesson_info=None):
        state = self._load_state()
        last = state.get("last_checkin") if isinstance(state, dict) else None
        if not last or str(last.get("lesson_id")) != str(lesson_id):
            return False, ""
        try:
            elapsed = (
                datetime.now() - datetime.strptime(last["time"], "%Y-%m-%d %H:%M:%S")
            ).total_seconds() / 60
            if elapsed < CHECKIN_COOLDOWN_MINUTES:
                display_info = lesson_info if isinstance(lesson_info, dict) and lesson_info else last
                lesson_display = self._lesson_display(lesson_id, display_info)
                message = f"课堂 {lesson_display} 在 {int(elapsed)} 分钟前已签到，跳过"
                if emit_log:
                    log(f"[*] {message}")
                return True, message
        except Exception:
            pass
        return False, ""

    def _record_checkin(self, lesson_id, lesson_info=None):
        state = self._load_state()
        if not isinstance(state, dict):
            state = {}
        info = lesson_info if isinstance(lesson_info, dict) else {}
        state["last_checkin"] = {
            "lesson_id": str(lesson_id),
            "lesson_name": str(info.get("lesson_name") or "").strip(),
            "classroom_id": str(info.get("classroom_id") or "").strip(),
            "classroom_name": str(info.get("classroom_name") or "").strip(),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_state(state)

    def _pushplus_notify_success(self, lesson_id, lesson_info=None):
        token = str(PUSHPLUS_TOKEN).strip()
        if not token:
            return

        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        info = lesson_info if isinstance(lesson_info, dict) else {}
        context = {
            "backend": "desktop",
            "lesson_id": str(lesson_id),
            "lesson_name": self._lesson_display(lesson_id, info),
            "classroom_id": str(info.get("classroom_id") or ""),
            "classroom_name": str(info.get("classroom_name") or ""),
            "success_time": now_text,
        }
        title = PUSHPLUS_TITLE_TEMPLATE.format_map(SafeFormatDict(context))
        content = PUSHPLUS_CONTENT_TEMPLATE.format_map(SafeFormatDict(context))
        payload = {
            "token": token,
            "title": title,
            "content": content,
            "template": PUSHPLUS_TEMPLATE,
            "channel": PUSHPLUS_CHANNEL,
        }
        try:
            resp = requests.post("https://www.pushplus.plus/send", json=payload, timeout=10)
            data = resp.json() if "application/json" in resp.headers.get("Content-Type", "") else {}
            if str(data.get("code")) == "200":
                log("[+] PushPlus 推送成功")
            else:
                log(f"[!] PushPlus 推送失败: {data.get('msg') or resp.text[:120]}")
        except Exception as e:
            log(f"[!] PushPlus 推送异常: {e}")

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
        wait_for_scan_enabled = True
        interactive_input = sys.stdin.isatty() and sys.stdout.isatty()
        handled_verify_tokens = set()
        while True:
            if max_wait and time.time() - start > max_wait:
                log("[-] 等待登录超时，请重新获取二维码")
                return False
            if wait_for_scan_enabled:
                try:
                    wait_resp = self._desktop_request(
                        "post",
                        "/api/v3/user/login/wait-for-scan",
                        json={"token": login_token},
                        timeout=(8, 20),
                        use_auth=False,
                        persist_state_on_change=False,
                    )
                    wait_data = wait_resp.json()
                    verify_token = self._extract_verify_code_token(wait_data)
                    if verify_token:
                        if verify_token not in handled_verify_tokens:
                            if not self._handle_verify_code_challenge(verify_token, interactive_input):
                                return False
                            handled_verify_tokens.add(verify_token)
                            if self._try_finalize_login_success(attempts=2, delay_seconds=1):
                                return True
                            last_status = "验证码已提交，等待登录完成"
                            time.sleep(1)
                            continue
                        if self._try_finalize_login_success(attempts=1, delay_seconds=0):
                            return True
                        time.sleep(1)
                    wait_code = wait_data.get("code")
                    wait_msg = wait_data.get("msg") or wait_data.get("message")
                    if wait_code in (500, 50000, 50001):
                        log(f"[-] 二维码已失效: {wait_msg or wait_code}")
                        return False
                    if wait_msg and wait_msg != last_status:
                        log(f"[*] 扫码状态: {wait_msg}")
                        last_status = wait_msg
                except requests.ReadTimeout:
                    pass
                except KeyboardInterrupt:
                    return False
                except requests.RequestException as e:
                    message = f"wait-for-scan 网络异常，稍后重试: {e}"
                    if message != last_status:
                        log(f"[!] {message}")
                        last_status = message
                except Exception as e:
                    wait_for_scan_enabled = False
                    log(f"[!] wait-for-scan 不可用，改用 login 轮询: {e}")
            try:
                resp = self._desktop_request(
                    "post",
                    "/api/v3/user/login",
                    json={"token": login_token},
                    timeout=(10, 35),
                    use_auth=False,
                    persist_state_on_change=False,
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

            verify_token = self._extract_verify_code_token(data)
            if verify_token:
                if verify_token not in handled_verify_tokens:
                    if not self._handle_verify_code_challenge(verify_token, interactive_input):
                        return False
                    handled_verify_tokens.add(verify_token)
                    if self._try_finalize_login_success(attempts=2, delay_seconds=1):
                        return True
                    last_status = "验证码已提交，等待登录完成"
                elif self._try_finalize_login_success(attempts=1, delay_seconds=0):
                    return True
                time.sleep(1)
                continue

            code = data.get("code")
            msg = data.get("msg") or data.get("message") or f"code={code}"
            if code == 0:
                if self._try_finalize_login_success(attempts=2, delay_seconds=1):
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

    @staticmethod
    def _extract_verify_code_token(data):
        if not isinstance(data, dict):
            return ""
        direct_token = str(data.get("token") or "").strip()
        if direct_token:
            return direct_token
        payload = data.get("data")
        if not isinstance(payload, dict):
            return ""
        for key in ("token", "verify_token", "verifyToken", "need_code_token", "needCodeToken"):
            token = str(payload.get(key) or "").strip()
            if token:
                return token
        return ""

    def _prompt_verify_code(self):
        while True:
            try:
                verify_code = input("请输入手机上显示的 4 位验证码: ").strip()
            except (KeyboardInterrupt, EOFError):
                return None
            if len(verify_code) == 4 and verify_code.isdigit():
                return verify_code
            log("[-] 验证码格式错误，请输入 4 位数字")

    def _handle_verify_code_challenge(self, verify_token, interactive_input):
        token = str(verify_token or "").strip()
        if not token:
            return False
        log("[*] 检测到需要输入验证码")
        if not interactive_input:
            log("[!] 当前环境无法交互输入验证码，请在交互终端中执行扫码登录")
            return False

        attempts = 3
        for attempt in range(1, attempts + 1):
            verify_code = self._prompt_verify_code()
            if not verify_code:
                return False
            if self._submit_login_with_code(token, verify_code):
                log("[*] 验证码校验通过，等待登录态完成...")
                return True
            if attempt < attempts:
                log("[-] 验证码校验失败，请重新输入")
        return False

    def _submit_login_with_code(self, need_code_token, verify_code):
        token = str(need_code_token or "").strip()
        code = str(verify_code or "").strip()
        if len(code) != 4 or not code.isdigit() or not token:
            log("[-] 验证码登录参数无效")
            return False

        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                resp = self._desktop_request(
                    "post",
                    "/api/v3/user/login/login-with-code",
                    json={"token": token, "code": code},
                    timeout=(8, 20),
                    use_auth=False,
                    persist_state_on_change=False,
                )
                data = resp.json()
                if data.get("code") == 0:
                    log("[+] 验证码提交成功")
                    return True
                log(f"[-] 验证码登录失败: {data.get('msg')}")
                return False
            except requests.RequestException as e:
                if attempt >= attempts:
                    log(f"[!] 验证码登录网络异常: {e}")
                    return False
                log(f"[!] 验证码登录网络异常，第 {attempt} 次重试: {e}")
                time.sleep(min(2.0, 0.5 * attempt))
            except Exception as e:
                log(f"[!] 验证码登录异常: {e}")
                return False
        return False

    def _fetch_active_lesson_result(self):
        try:
            resp = self._desktop_request("get", "/api/v3/classroom/on-lesson-upcoming-exam", timeout=10)
            data = resp.json()
            if data.get("code") != 0:
                message = f"获取课堂列表失败: {data.get('msg')}"
                return {
                    "state": "error",
                    "log_prefix": "[-]",
                    "message": message,
                    "log_key": f"active_lesson_error:{self._normalize_log_key(message)}",
                    "lesson_id": None,
                    "classroom_id": None,
                    "lesson_info": {},
                }
            active = data.get("data", {}).get("onLessonClassrooms", [])
            if not active:
                return {
                    "state": "idle",
                    "log_prefix": "[-]",
                    "message": "当前没有正在进行的课堂",
                    "lesson_id": None,
                    "classroom_id": None,
                    "lesson_info": {},
                }
            classroom = active[0]
            lesson_info = self._extract_lesson_info(classroom)
            lesson_id = lesson_info["lesson_id"] or classroom.get("lessonId")
            return {
                "state": "active",
                "log_prefix": "[*]",
                "message": f"检测到课堂 {self._lesson_display(lesson_id, lesson_info)}",
                "lesson_id": lesson_id,
                "classroom_id": lesson_info["classroom_id"] or classroom.get("classroomId"),
                "lesson_info": lesson_info,
            }
        except Exception as e:
            message = f"获取课堂列表异常: {e}"
            return {
                "state": "error",
                "log_prefix": "[!]",
                "message": message,
                "log_key": f"active_lesson_error:{self._normalize_log_key(message)}",
                "lesson_id": None,
                "classroom_id": None,
                "lesson_info": {},
            }

    def get_active_lesson_data(self):
        result = self._fetch_active_lesson_result()
        self._last_active_lesson_state = result["state"]
        self._last_active_lesson_info = result.get("lesson_info") or {}
        if result["state"] == "error":
            message = f"{result['log_prefix']} {result['message']}"
            log_key = result.get("log_key")
            if log_key:
                self._log_throttled(log_key, message)
            else:
                log(message)
        if result["state"] != "active":
            return None, None
        return result["lesson_id"], result["classroom_id"]

    def _perform_sign_in(self, lesson_id, classroom_id=None, source=1, lesson_info=None):
        lesson_info = self._lesson_info_for(lesson_id, lesson_info)
        on_cooldown, cooldown_message = self._check_cooldown(
            lesson_id,
            emit_log=False,
            lesson_info=lesson_info,
        )
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
            lesson_display = self._lesson_display(lesson_id, lesson_info)
            if data.get("code") == 0:
                self._record_checkin(lesson_id, lesson_info=lesson_info)
                self.save_session()
                self._pushplus_notify_success(lesson_id, lesson_info=lesson_info)
                return {
                    "success": True,
                    "state": "success",
                    "log_prefix": "[+]",
                    "message": f"签到成功 (课堂: {lesson_display}, 编号: {lesson_id})",
                }
            return {
                "success": False,
                "state": "failed",
                "log_prefix": "[-]",
                "message": f"课堂 {lesson_display} 签到失败: {data.get('msg')}",
            }
        except Exception as e:
            lesson_display = self._lesson_display(lesson_id, lesson_info)
            return {
                "success": False,
                "state": "error",
                "log_prefix": "[!]",
                "message": f"课堂 {lesson_display} 签到请求异常: {e}",
            }

    def sign_in(self, lesson_id, classroom_id=None, source=1, lesson_info=None):
        result = self._perform_sign_in(
            lesson_id,
            classroom_id=classroom_id,
            source=source,
            lesson_info=lesson_info,
        )
        message = f"{result['log_prefix']} {result['message']}"
        if result["success"]:
            log(message)
        else:
            log_key = f"sign_in:{lesson_id}:{result['state']}:{self._normalize_log_key(result['message'])}"
            self._log_throttled(log_key, message)
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
            lesson_info=lesson_result.get("lesson_info") or {},
        )
        if emit_log:
            log(f"{sign_result['log_prefix']} {sign_result['message']}")
        return sign_result

    def keep_alive(self):
        original_cookies = self._cookie_records_from_jar()
        original_auth = self.session.headers.get("Authorization")
        last_network_error = None
        for attempt in range(3):
            try:
                resp = self._desktop_request(
                    "get",
                    "/api/v3/user/basic-info",
                    timeout=10,
                    persist_state_on_change=False,
                )
                if "application/json" not in resp.headers.get("Content-Type", ""):
                    snippet = resp.text[:120].replace("\n", " ")
                    log(f"[-] 会话保活失败: 接口返回非 JSON ({snippet})")
                    self._set_cookie_records(original_cookies, clear=True)
                    self._set_desktop_auth(original_auth)
                    return False
                data = resp.json()
                if data.get("code") == 0:
                    log(f"[+] 会话保活成功，{self._describe_login_state()}")
                    self.save_session()
                    return True
                log(f"[-] 会话保活失败: {data.get('msg')}")
                self._set_cookie_records(original_cookies, clear=True)
                self._set_desktop_auth(original_auth)
                return False
            except requests.RequestException as e:
                last_network_error = e
                if attempt < 2:
                    log(f"[!] 会话保活网络异常，第 {attempt + 1} 次重试: {e}")
                    time.sleep(1 + attempt)
                    continue
                break
            except Exception as e:
                log(f"[!] 会话保活异常: {e}")
                self._set_cookie_records(original_cookies, clear=True)
                self._set_desktop_auth(original_auth)
                return False
        self._set_cookie_records(original_cookies, clear=True)
        self._set_desktop_auth(original_auth)
        if last_network_error is not None:
            log(f"[!] 会话保活网络异常: {last_network_error}")
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


def run_until_success(helper, delay_minutes=0, return_to_menu=False):
    interval_seconds = max(5, int(SCHEDULE_INTERVAL_SECONDS))
    timeout_minutes = max(1, int(SCHEDULE_TIMEOUT_MINUTES))

    if delay_minutes > 0:
        log(f"[*] 将在 {delay_minutes} 分钟后开始持续签到...")
        time.sleep(delay_minutes * 60)

    deadline = datetime.now() + timedelta(minutes=timeout_minutes)
    log(
        f"[*] 已启动持续签到（间隔 {interval_seconds} 秒，"
        f"窗口期 {timeout_minutes} 分钟）"
    )
    try:
        while True:
            lesson_id, classroom_id = helper.get_active_lesson_data()
            if lesson_id:
                lesson_info = helper._lesson_info_for(lesson_id)
                if helper.sign_in(lesson_id, classroom_id=classroom_id, lesson_info=lesson_info):
                    log("[+] 已签到成功，结束持续签到")
                    return 0
                lesson_display = helper._lesson_display(lesson_id, lesson_info)
                helper._log_throttled(
                    f"sign_retry_wait:{lesson_id}",
                    f"[-] 课堂 {lesson_display} 本次未签到成功，继续重试...",
                )
            elif helper._last_active_lesson_state != "error":
                helper._log_throttled(
                    "waiting_no_active_lesson",
                    f"[-] 当前没有正在进行的课堂，{interval_seconds} 秒后重试...",
                )

            if datetime.now() >= deadline:
                log(f"[-] 已到签到窗口期上限（{timeout_minutes} 分钟），仍未签到成功，退出")
                return 1
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        log("[*] 已停止持续签到")
        return 0 if return_to_menu else 130


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="雨课堂自动签到助手（桌面端登录版）", add_help=False)
    parser.add_argument("-h", action="help", help="show this help message and exit")
    parser.add_argument("-a", "-auto", dest="auto", action="store_true", help="持续扫描课堂并签到，直到成功")
    parser.add_argument("-c", dest="clear", action="store_true", help="停止当前后台运行的签到任务")
    parser.add_argument("-k", "-keepalive", dest="keepalive", action="store_true", help="仅执行会话保活")
    parser.add_argument("-qr", dest="qr", action="store_true", help="显示桌面端登录二维码")
    parser.add_argument(
        "-cooldown",
        dest="cooldown",
        type=int,
        default=CHECKIN_COOLDOWN_MINUTES,
        help=f"签到去重冷却时间，分钟（默认 {CHECKIN_COOLDOWN_MINUTES}）",
    )
    parser.add_argument("-s", "-schedule", dest="schedule", type=int, metavar="N", help="延迟 N 分钟后开始，持续签到直到成功")
    args = parser.parse_args()

    CHECKIN_COOLDOWN_MINUTES = args.cooldown
    install_signal_handlers()

    if args.clear:
        sys.exit(0 if stop_background_signin_tasks() else 1)

    should_takeover_conflict = bool(args.auto)
    runtime_lock = acquire_runtime_lock(stop_conflict=should_takeover_conflict)
    if runtime_lock is None:
        sys.exit(1)

    helper = YuketangHelper()
    interactive_login_allowed = sys.stdin.isatty() and sys.stdout.isatty()

    if args.keepalive:
        if not ensure_login(helper, allow_interactive_login=interactive_login_allowed):
            sys.exit(1)
        sys.exit(0 if helper.keep_alive() else 1)

    if args.qr:
        if not interactive_login_allowed:
            log("[!] 当前环境不适合交互扫码登录")
            sys.exit(1)
        login_token, _ = helper.get_login_qrcode()
        sys.exit(0 if login_token and helper.wait_for_login_and_callback(login_token) else 1)

    if args.auto:
        auth = ensure_login(helper, allow_interactive_login=interactive_login_allowed)
        if not auth:
            sys.exit(1)
        sys.exit(run_until_success(helper, delay_minutes=0))

    if args.schedule is not None:
        auth = ensure_login(helper, allow_interactive_login=interactive_login_allowed)
        if not auth:
            sys.exit(1)
        sys.exit(run_until_success(helper, delay_minutes=max(0, int(args.schedule))))

    # 直接运行脚本时先检查登录态；Cookie 失效则自动进入扫码登录
    if not ensure_login(helper, allow_interactive_login=interactive_login_allowed):
        sys.exit(1)

    while True:
        print("\n1. 自动扫描签到\n2. 重新扫码登录\n3. 定时签到\n4. 会话保活\n5. 退出")
        try:
            choice = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.exit(0)

        if choice == "1":
            if not ensure_login(helper, allow_interactive_login=interactive_login_allowed):
                continue
            run_until_success(helper, delay_minutes=0, return_to_menu=True)
        elif choice == "2":
            login_token, _ = helper.get_login_qrcode()
            if login_token:
                helper.wait_for_login_and_callback(login_token)
        elif choice == "3":
            try:
                delay = int(input("请输入延迟分钟数 (0 = 立即开始): ").strip())
            except (ValueError, KeyboardInterrupt, EOFError):
                continue
            if not ensure_login(helper, allow_interactive_login=interactive_login_allowed):
                continue
            run_until_success(helper, delay_minutes=max(0, delay), return_to_menu=True)
        elif choice == "4":
            if not ensure_login(helper, allow_interactive_login=interactive_login_allowed):
                continue
            helper.keep_alive()
        elif choice == "5":
            sys.exit(0)
