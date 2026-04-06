#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from datetime import datetime

import qrcode
import requests

# ========== 用户配置 ==========
BASE_DOMAIN = "changjiang.yuketang.cn"  # 默认长江雨课堂，自行更换
CHECKIN_COOLDOWN_MINUTES = 15
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


class YuketangHelper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": GLOBAL_UA})
        self.desktop_auth = None

    def _load_state(self):
        return load_state_dict()

    def _save_state(self, state):
        write_json_file(STATE_FILE, state)

    def _set_desktop_auth(self, auth):
        if isinstance(auth, str) and auth.startswith("Bearer "):
            auth = auth.split(" ", 1)[1].strip()
        self.desktop_auth = auth.strip() if isinstance(auth, str) and auth.strip() else None
        if self.desktop_auth:
            self.session.headers["Authorization"] = f"Bearer {self.desktop_auth}"
        else:
            self.session.headers.pop("Authorization", None)

    def _try_extract_auth_from_response(self, resp):
        candidates = []
        if resp is not None:
            candidates.append(resp)
            candidates.extend(getattr(resp, "history", []) or [])
        for item in candidates:
            headers = getattr(item, "headers", {}) or {}
            for key in ("set-auth", "Set-Auth", "authorization", "Authorization"):
                value = headers.get(key)
                if isinstance(value, str) and value.strip():
                    self._set_desktop_auth(value)
                    return True
        return False

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
        if self.desktop_auth:
            return "Authorization 已加载"
        return "无可用登录态"

    def _describe_login_channels(self):
        cookie_map = self._get_cookie_map()
        parts = [f"Authorization={'已加载' if self.desktop_auth else '无'}"]
        for name in ("sid", "sessionid"):
            cookie = cookie_map.get(name)
            if not cookie:
                parts.append(f"{name}=无")
                continue
            expires = cookie.get("expires")
            expires_text = (
                datetime.fromtimestamp(expires).strftime("%Y-%m-%d %H:%M:%S")
                if expires
                else "session"
            )
            parts.append(f"{name}=有效至 {expires_text}")
        return "；".join(parts)

    def save_session(self):
        state = self._load_state()
        if not isinstance(state, dict):
            state = {}
        for legacy_key in ("cookies", "browser_state", "sessionid", "csrftoken"):
            state.pop(legacy_key, None)
        if self.desktop_auth:
            state["desktop_auth"] = self.desktop_auth
            state["desktop_auth_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            state.pop("desktop_auth", None)
            state.pop("desktop_auth_updated_at", None)
        cookie_records = self._cookie_records_from_jar()
        if cookie_records:
            state["desktop_cookies"] = cookie_records
            state["desktop_cookies_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            state.pop("desktop_cookies", None)
            state.pop("desktop_cookies_updated_at", None)
        self._save_state(state)

    def _desktop_request(self, method, path, timeout=20, headers=None, **kwargs):
        old_auth = self.desktop_auth
        old_cookie_signature = self._cookie_signature()
        req_headers = {
            "User-Agent": GLOBAL_UA,
            "Content-Type": "application/json",
            **DESKTOP_HEADERS,
        }
        if self.desktop_auth:
            req_headers["Authorization"] = f"Bearer {self.desktop_auth}"
        if headers:
            req_headers.update(headers)
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        resp = self.session.request(method, url, headers=req_headers, timeout=timeout, **kwargs)
        if self._try_extract_auth_from_response(resp):
            pass
        if self.desktop_auth != old_auth or self._cookie_signature() != old_cookie_signature:
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
                if self.desktop_auth:
                    return "token"
                if self.session.cookies:
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
                    if self.desktop_auth:
                        return "token"
                    if self.session.cookies:
                        return "cookie"
            except Exception:
                continue
        return None

    def load_session(self):
        try:
            state = self._load_state()
            self._set_desktop_auth(state.get("desktop_auth"))
            self._set_cookie_records(state.get("desktop_cookies", []), clear=True)
            if not self.desktop_auth and not self.session.cookies:
                return False
            mode = self._probe_login_state()
            if mode == "token":
                log(f"[+] Desktop Token 加载成功，{self._describe_login_channels()}")
                return True
            if mode == "cookie":
                log(f"[+] Desktop Cookie 加载成功，{self._describe_login_channels()}")
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
                if mode == "token":
                    log(f"[+] 桌面端登录成功，{self._describe_login_channels()}")
                    return True
                if mode == "cookie":
                    log(f"[+] 桌面端登录成功，{self._describe_login_channels()}")
                    return True
                header_keys = ", ".join(sorted(resp.headers.keys()))
                log(
                    f"[-] 登录成功，但未建立可复用的桌面端登录态；"
                    f"响应头: {header_keys or '无'}；当前状态: {self._describe_login_channels()}"
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

    def sign_in(self, lesson_id, classroom_id=None, source=1):
        result = self._perform_sign_in(lesson_id, classroom_id=classroom_id, source=source)
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
                log(f"[+] 会话保活成功，{self._describe_login_channels()}")
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
    parser.add_argument("-s", "--schedule", type=int, metavar="N", help="延迟 N 分钟后开始，每分钟自动检测并签到")
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

    if args.auto:
        lesson_id, classroom_id = helper.get_active_lesson_data()
        if lesson_id:
            sys.exit(0 if helper.sign_in(lesson_id, classroom_id=classroom_id) else 1)
        log("[-] 当前没有正在进行的课堂")
        sys.exit(0)

    if args.schedule is not None:
        delay = args.schedule
        if delay > 0:
            log(f"[*] 将在 {delay} 分钟后开始自动签到循环...")
            time.sleep(delay * 60)
        log("[*] 开始自动签到循环（每 60 秒检测一次，Ctrl+C 退出）")
        try:
            while True:
                lesson_id, classroom_id = helper.get_active_lesson_data()
                if lesson_id:
                    helper.sign_in(lesson_id, classroom_id=classroom_id)
                else:
                    log("[-] 当前没有正在进行的课堂，60 秒后重试...")
                time.sleep(60)
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
