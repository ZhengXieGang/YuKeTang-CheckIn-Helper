#!/usr/bin/env python3
"""
雨课堂动态二维码签到测试工具
支持两种方案：API 越权获取 + WebSocket 监听
"""
import json
import os
import re
import sys
import time
from datetime import datetime

import requests

try:
    import paho.mqtt.client as mqtt

    HAS_MQTT = True
except ImportError:
    HAS_MQTT = False

# ========== 用户配置 ==========
BASE_DOMAIN = "changjiang.yuketang.cn"  # 默认长江雨课堂，自行更换
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(SCRIPT_DIR, "yuketang_session.json")
# ==============================

BASE_URL = f"https://{BASE_DOMAIN}"
GLOBAL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
DESKTOP_HEADERS = {
    "xtbz": "ykt",
    "desktop-v": "v2",
    "X-Client": "desktop",
    "Origin": "file://",
}
WEBSOCKET_ENDPOINTS = [f"wss://{BASE_DOMAIN}/wsapp/", "wss://pre-apple-emqx.xuetangonline.com:8083/mqtt"]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def read_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json_file(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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


class YuketangTester:
    def __init__(self, session_file=SESSION_FILE):
        self.session_file = session_file
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": GLOBAL_UA})
        self.desktop_auth = None

    def _load_state(self):
        state = read_json_file(self.session_file, {})
        return state if isinstance(state, dict) else {}

    def _save_state(self, state):
        write_json_file(self.session_file, state)

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
        req_headers = {"User-Agent": GLOBAL_UA, "Content-Type": "application/json", **DESKTOP_HEADERS}
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
            if resp.json().get("code") == 0:
                self.save_session()
                if self.desktop_auth:
                    return "token"
                if self.session.cookies:
                    return "cookie"
        except Exception:
            return None
        return None

    def load_session(self):
        try:
            raw = self._load_state()
            self._set_desktop_auth(raw.get("desktop_auth"))
            self._set_cookie_records(raw.get("desktop_cookies", []), clear=True)
            if not self.desktop_auth and not self.session.cookies:
                log("[-] 未找到桌面端登录态")
                return False
            mode = self._probe_login_state()
            if mode == "token":
                log("[+] Desktop Token 加载成功")
                return True
            if mode == "cookie":
                log("[+] Desktop Cookie 加载成功")
                return True
            log("[-] 桌面端登录态已失效")
            return False
        except Exception as e:
            log(f"[-] 加载桌面端登录态失败: {e}")
            return False

    def get_active_lesson(self):
        try:
            data = self._desktop_request("get", "/api/v3/classroom/on-lesson-upcoming-exam", timeout=10).json()
            active = data.get("data", {}).get("onLessonClassrooms", [])
            if active:
                lesson_id = active[0].get("lessonId")
                log(f"[+] 找到活跃课堂: {lesson_id}")
                return lesson_id
            log("[-] 当前没有活跃课堂")
            return None
        except Exception as e:
            log(f"[-] 获取课堂失败: {e}")
            return None

    def try_api_method(self, lesson_id):
        log("[*] 尝试 API 越权方案...")
        try:
            payload = {"lessonId": str(lesson_id), "source": 10}
            resp = self._desktop_request("post", "/api/v3/lesson/checkin", json=payload, timeout=10).json()
            if resp.get("code") != 0:
                log(f"[-] 获取 lessonToken 失败: {resp.get('msg')}")
                return None
            lesson_token = resp["data"].get("lessonToken")
            log("[+] 获取到 lessonToken")

            resp2 = self._desktop_request(
                "get",
                "/api/v3/lesson/fetch-dynamic-invitation",
                params={"v": 2},
                headers={"Authorization": f"Bearer {lesson_token}"},
                timeout=10,
            ).json()
            if resp2.get("code") != 0:
                log(f"[-] 获取暗号失败: {resp2.get('msg')}")
                return None

            qr_content = resp2["data"].get("qrContent", "")
            ticket_match = re.search(r"ticket=([A-Za-z0-9]+)", qr_content)
            if ticket_match:
                ticket = ticket_match.group(1)
                log(f"[+] 成功获取 ticket: {ticket}")
                return ticket
            log("[-] 无法从 qrContent 提取 ticket")
            return None
        except Exception as e:
            log(f"[-] API 方案异常: {e}")
            return None

    def try_websocket_method(self, lesson_id, timeout=60):
        if not HAS_MQTT:
            log("[-] 未安装 paho-mqtt，请运行: pip install paho-mqtt")
            return None
        log("[*] 尝试 WebSocket 监听方案...")
        ticket_found = [None]

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                log("[+] WebSocket 连接成功")
                client.subscribe("#")
            else:
                log(f"[-] 连接失败，错误码: {rc}")

        def on_message(client, userdata, msg):
            try:
                payload = msg.payload.decode("utf-8", errors="ignore")
                ticket = self._extract_ticket(payload)
                if ticket:
                    log(f"[+] 监听到 ticket: {ticket}")
                    ticket_found[0] = ticket
                    client.disconnect()
            except Exception:
                pass

        for endpoint in WEBSOCKET_ENDPOINTS:
            try:
                log(f"[*] 连接: {endpoint}")
                client = mqtt.Client(transport="websockets")
                client.on_connect = on_connect
                client.on_message = on_message
                parts = endpoint.replace("wss://", "").split("/", 1)
                host = parts[0].split(":")[0]
                port = int(parts[0].split(":")[1]) if ":" in parts[0] else 443
                path = "/" + parts[1] if len(parts) > 1 else "/"
                client.ws_set_options(path=path)
                client.tls_set()
                client.connect(host, port, 60)
                start = time.time()
                while time.time() - start < timeout and not ticket_found[0]:
                    client.loop(timeout=1.0)
                if ticket_found[0]:
                    return ticket_found[0]
                log(f"[-] 超时 ({timeout}秒)，未收到 ticket")
                client.disconnect()
                break
            except Exception as e:
                log(f"[-] 连接失败: {e}")
                continue
        return None

    def _extract_ticket(self, text):
        patterns = [
            r"ticket=([A-Za-z0-9]{5,})",
            r'"ticket"\s*:\s*"([A-Za-z0-9]{5,})"',
            r'inviteCode["\']?\s*:\s*["\']?([A-Za-z0-9]{5,})',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return None

    def checkin_with_ticket(self, lesson_id, ticket):
        log(f"[*] 使用 ticket [{ticket}] 签到...")
        try:
            payload = {"lessonId": str(lesson_id), "source": 14, "inviteCode": str(ticket)}
            res = self._desktop_request("post", "/api/v3/lesson/checkin", json=payload, timeout=10).json()
            if res.get("code") == 0:
                log("[+] 签到成功！")
                self.save_session()
                return True
            log(f"[-] 签到失败: {res.get('msg')}")
            return False
        except Exception as e:
            log(f"[-] 签到异常: {e}")
            return False


if __name__ == "__main__":
    print("=" * 50)
    print("雨课堂动态二维码签到测试工具")
    print("=" * 50)

    tester = YuketangTester()
    if not tester.load_session():
        log("[!] 请先使用 yuketang_helper.py 登录")
        sys.exit(1)

    lesson_id = tester.get_active_lesson()
    if not lesson_id:
        log("[!] 没有活跃课堂，无法测试")
        sys.exit(1)

    print("\n请选择测试方案:")
    print("1. API 越权方案 (source=10)")
    print("2. WebSocket 监听方案")
    print("3. 两种方案都测试")

    try:
        choice = input("\n请输入选项 (1/2/3): ").strip()
    except (KeyboardInterrupt, EOFError):
        sys.exit(0)

    ticket = None
    if choice in ["1", "3"]:
        ticket = tester.try_api_method(lesson_id)
        if ticket and choice == "1":
            sys.exit(0 if tester.checkin_with_ticket(lesson_id, ticket) else 1)

    if choice in ["2", "3"] and not ticket:
        ticket = tester.try_websocket_method(lesson_id, timeout=60)

    if ticket:
        tester.checkin_with_ticket(lesson_id, ticket)
    else:
        log("[!] 所有方案均未获取到 ticket")
        sys.exit(1)
