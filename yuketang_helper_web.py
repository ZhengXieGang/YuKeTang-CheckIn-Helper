#!/usr/bin/env python3
import atexit
import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from io import BytesIO

import qrcode
import requests

try:
    import fcntl
except ImportError:
    fcntl = None

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
BASE_DOMAIN = "changjiang.yuketang.cn"  # 默认长江雨课堂，自行更换
AUTO_LOGIN_PHONE = ""
AUTO_LOGIN_PSWD = ""
CHECKIN_COOLDOWN_MINUTES = 15
SCHEDULE_INTERVAL_SECONDS = 60  # 持续检测间隔（秒）
SCHEDULE_TIMEOUT_MINUTES = 30  # 首轮超时时间（分钟）
THROTTLED_LOG_INTERVAL_SECONDS = 300  # 同类重复日志节流窗口（秒）

PUSHPLUS_TOKEN = ""  # 留空则关闭推送
PUSHPLUS_CHANNEL = "wechat"  # wechat / mail / webhook / cp / sms
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
STATE_FILE = os.path.join(SCRIPT_DIR, "yuketang_session_web.json")
DESKTOP_STATE_FILE = os.path.join(SCRIPT_DIR, "yuketang_session.json")
RUNTIME_LOCK_FILE = os.path.join(SCRIPT_DIR, ".yuketang_runtime_web.lock")
BROWSER_SYNC_WAIT_SECONDS = 6


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
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


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

    def owner_text(self):
        data = self._read_owner()
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


def runtime_lock_busy_text():
    lock = RuntimeFileLock(RUNTIME_LOCK_FILE)
    if lock.acquire():
        lock.release()
        return ""
    owner = lock.owner_text()
    lock.release()
    return owner or "后台 Web 任务"


def acquire_runtime_lock():
    lock = RuntimeFileLock(RUNTIME_LOCK_FILE)
    if lock.acquire():
        atexit.register(lock.release)
        return lock
    owner = lock.owner_text()
    suffix = f"（{owner}）" if owner else ""
    log(f"[!] 检测到同目录已有另一个 Web 实例正在运行{suffix}，为避免覆盖登录态，本次退出")
    lock.release()
    return None


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
            import urllib.request

            def dl(url):
                request = urllib.request.Request(
                    url if url.startswith("http") else "https:" + url,
                    headers={"User-Agent": "Mozilla"},
                )
                return urllib.request.urlopen(request).read()

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
        import urllib.request

        url = match.group(1) if match.group(1).startswith("http") else "https:" + match.group(1)
        bg_b = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla"})).read()
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

    @staticmethod
    def _is_authenticated_basic_info(data):
        if not isinstance(data, dict):
            return False
        if data.get("code") == 0:
            return True
        return bool(data.get("success"))

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
            return "invalid"
        original_cookies = self._cookie_records_from_jar()
        last_network_error = None
        for attempt in range(3):
            try:
                self.session.get(f"{BASE_URL}/v2/web/index", timeout=10)
                self._refresh_session_fields()
                resp = self.session.get(f"{BASE_URL}/api/v3/user/basic-info", timeout=10)
                if "application/json" not in resp.headers.get("Content-Type", ""):
                    self._set_cookie_records(original_cookies, clear=True)
                    return "invalid"
                data = resp.json()
                if self._is_authenticated_basic_info(data):
                    self._refresh_session_fields()
                    self.save_session()
                    return "valid"
                self._set_cookie_records(original_cookies, clear=True)
                return "invalid"
            except requests.RequestException as e:
                last_network_error = e
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
            except Exception:
                self._set_cookie_records(original_cookies, clear=True)
                return "invalid"
        self._set_cookie_records(original_cookies, clear=True)
        if last_network_error is not None:
            log(f"[!] Web 登录态探测网络异常: {last_network_error}")
            return "network_error"
        return "invalid"

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
        probe_result = self._probe_session()
        if probe_result == "valid":
            log("[+] 已同步浏览器登录态")
            return True
        if probe_result == "network_error":
            log("[!] 浏览器态已同步，但网络异常，先沿用当前 Cookie")
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
        probe_result = self._probe_session()
        if probe_result == "valid":
            log("[+] 已从浏览器登录态恢复会话")
            return True
        if probe_result == "network_error":
            log("[!] 已恢复 Cookie，但网络异常，先沿用当前会话")
            return True
        return False

    def save_session(self):
        self._refresh_session_fields()
        persist_cookie_records(self._cookie_records_from_jar())

    def load_session(self, verify_online=True):
        try:
            if self._load_cookies_from_state():
                if not verify_online:
                    return bool(self.session.cookies.get("sessionid"))
                probe_result = self._probe_session()
                if probe_result == "valid":
                    if HAS_PLAYWRIGHT and not load_browser_state():
                        self._bootstrap_browser_state()
                    return True
                if probe_result == "network_error":
                    log("[!] Web 登录态在线校验失败（网络异常），先沿用本地 Cookie")
                    return True
            if not verify_online:
                return False
            return self._rehydrate_session_from_browser_state()
        except Exception:
            return False

    def _check_cooldown(self, lesson_id, lesson_info=None):
        state = self._load_state()
        last = state.get("last_checkin") if isinstance(state, dict) else None
        if not last or str(last.get("lesson_id")) != str(lesson_id):
            return False
        try:
            elapsed = (
                datetime.now() - datetime.strptime(last["time"], "%Y-%m-%d %H:%M:%S")
            ).total_seconds() / 60
            if elapsed < CHECKIN_COOLDOWN_MINUTES:
                display_info = lesson_info if isinstance(lesson_info, dict) and lesson_info else last
                lesson_display = self._lesson_display(lesson_id, display_info)
                log(f"[*] 课堂 {lesson_display} 在 {int(elapsed)} 分钟前已签到，跳过")
                return True
        except Exception:
            pass
        return False

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
            "backend": "web",
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
            if not active:
                self._last_active_lesson_state = "idle"
                self._last_active_lesson_info = {}
                return None, None
            self._last_active_lesson_state = "active"
            lesson_info = self._extract_lesson_info(active[0])
            self._last_active_lesson_info = lesson_info
            return (
                lesson_info["lesson_id"] or active[0].get("lessonId"),
                lesson_info["classroom_id"] or active[0].get("classroomId"),
            )
        except Exception as e:
            message = f"[!] 获取课堂列表异常: {e}"
            log_key = f"active_lesson_error:{self._normalize_log_key(message)}"
            self._last_active_lesson_state = "error"
            self._last_active_lesson_info = {}
            self._log_throttled(log_key, message)
            return None, None

    def sign_in(self, lesson_id, classroom_id=None, source=1, lesson_info=None):
        lesson_info = self._lesson_info_for(lesson_id, lesson_info)
        if self._check_cooldown(lesson_id, lesson_info=lesson_info):
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
                lesson_display = self._lesson_display(lesson_id, lesson_info)
                log(f"[+] 签到成功 (课堂: {lesson_display}, 编号: {lesson_id})")
                self._record_checkin(lesson_id, lesson_info=lesson_info)
                self.save_session()
                self._pushplus_notify_success(lesson_id, lesson_info=lesson_info)
                return True
            else:
                lesson_display = self._lesson_display(lesson_id, lesson_info)
                message = f"[-] 课堂 {lesson_display} 签到失败: {res.get('msg')}"
                log_key = f"sign_in:{lesson_id}:failed:{self._normalize_log_key(res.get('msg'))}"
                self._log_throttled(log_key, message)
                return False
        except Exception as e:
            lesson_display = self._lesson_display(lesson_id, lesson_info)
            message = f"[!] 课堂 {lesson_display} 签到请求异常: {e}"
            log_key = f"sign_in:{lesson_id}:error:{self._normalize_log_key(message)}"
            self._log_throttled(log_key, message)
            return False

    def keep_alive(self):
        original_cookies = self._cookie_records_from_jar()
        last_network_error = None
        for attempt in range(3):
            try:
                self.session.get(f"{BASE_URL}/v2/web/index", timeout=10)
                self._refresh_session_fields()
                self.session.headers.update({"X-CSRFToken": self.csrftoken, "User-Agent": GLOBAL_UA})
                resp = self.session.get(f"{BASE_URL}/api/v3/user/basic-info", timeout=10)
                if "application/json" not in resp.headers.get("Content-Type", ""):
                    snippet = resp.text[:120].replace("\n", " ")
                    log(f"[-] Web 会话保活失败: 接口返回非 JSON ({snippet})")
                    self._set_cookie_records(original_cookies, clear=True)
                    return False
                data = resp.json()
                if self._is_authenticated_basic_info(data):
                    log("[+] Web 会话保活成功")
                    self.save_session()
                    return True
                log(f"[-] Web 会话保活失败: {data.get('msg')}")
                self._set_cookie_records(original_cookies, clear=True)
                return False
            except requests.RequestException as e:
                last_network_error = e
                if attempt < 2:
                    log(f"[!] Web 会话保活网络异常，第 {attempt + 1} 次重试: {e}")
                    time.sleep(1 + attempt)
                    continue
                break
            except Exception as e:
                log(f"[!] Web 会话保活异常: {e}")
                self._set_cookie_records(original_cookies, clear=True)
                return False
        self._set_cookie_records(original_cookies, clear=True)
        if last_network_error is not None:
            log(f"[!] Web 会话保活网络异常: {last_network_error}")
        return False


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


def run_once(helper, allow_interactive_login=False):
    if not ensure_login(helper, allow_interactive_login=allow_interactive_login):
        log("[-] 一次扫描签到结果：登录态不可用，需要重新扫码登录")
        return 3

    lesson_id, classroom_id = helper.get_active_lesson_data()
    if not lesson_id:
        if helper._last_active_lesson_state == "error":
            log("[-] 一次扫描签到结果：获取课堂列表失败")
            return 1
        log("[*] 一次扫描签到结果：当前没有正在进行的课堂")
        return 2

    lesson_info = helper._lesson_info_for(lesson_id)
    if helper.sign_in(lesson_id, classroom_id=classroom_id, lesson_info=lesson_info):
        lesson_display = helper._lesson_display(lesson_id, lesson_info)
        log(f"[+] 一次扫描签到结果：签到成功 (课堂: {lesson_display}, 编号: {lesson_id})")
        return 0
    lesson_display = helper._lesson_display(lesson_id, lesson_info)
    log(f"[-] 一次扫描签到结果：课堂 {lesson_display} 本次未签到成功")
    return 1


def ensure_login(helper, allow_interactive_login):
    auth = helper.load_session()
    if auth:
        return True
    if not allow_interactive_login:
        log("[!] 当前没有可用 Web 登录态，且本次环境不适合交互扫码登录")
        return False
    log("[*] 进入二维码扫码登录...")
    state, uuid = helper.get_login_qrcode()
    if not state:
        return False
    return helper.wait_for_login_and_callback(state, uuid)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="雨课堂自动签到助手（Web 账密登录版）", add_help=False)
    parser.add_argument("-h", action="help", help="show this help message and exit")
    parser.add_argument("-a", "-auto", dest="auto", action="store_true", help="持续扫描课堂并签到，直到成功")
    parser.add_argument("-o", "-once", dest="once", action="store_true", help="只扫描一次当前课堂并签到，无课堂则立即返回")
    parser.add_argument("-k", "-keepalive", dest="keepalive", action="store_true", help="仅执行会话保活")
    parser.add_argument("-qr", dest="qr", action="store_true", help="强制使用二维码扫码登录")
    parser.add_argument("-p", "-phone", dest="phone", type=str, help="手机号（覆盖脚本内置配置）")
    parser.add_argument("-pw", "-password", dest="password", type=str, help="密码（覆盖脚本内置配置）")
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
    if args.once:
        busy = runtime_lock_busy_text()
        if busy:
            log("[*] 一次扫描签到结果：已有后台 Web 任务正在运行，本次不重复执行")
            sys.exit(0)

    runtime_lock = acquire_runtime_lock()
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
        state, uuid = helper.get_login_qrcode()
        sys.exit(0 if state and helper.wait_for_login_and_callback(state, uuid) else 1)

    if args.once:
        sys.exit(run_once(helper, allow_interactive_login=interactive_login_allowed))

    if args.auto:
        if not ensure_login(helper, allow_interactive_login=interactive_login_allowed):
            sys.exit(1)
        sys.exit(run_until_success(helper, delay_minutes=0))

    if args.schedule is not None:
        if not ensure_login(helper, allow_interactive_login=interactive_login_allowed):
            sys.exit(1)
        sys.exit(run_until_success(helper, delay_minutes=max(0, int(args.schedule))))

    if not ensure_login(helper, allow_interactive_login=interactive_login_allowed):
        sys.exit(1)

    while True:
        print("\n1. 自动扫描签到\n2. 扫码登录\n3. 定时签到\n4. 退出")
        try:
            choice = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.exit(0)
        if choice == "1":
            if not ensure_login(helper, allow_interactive_login=interactive_login_allowed):
                continue
            run_until_success(helper, delay_minutes=0, return_to_menu=True)
        elif choice == "2":
            state, uuid = helper.get_login_qrcode()
            if state:
                helper.wait_for_login_and_callback(state, uuid)
        elif choice == "3":
            try:
                delay = int(input("请输入延迟分钟数 (0 = 立即开始): ").strip())
            except (ValueError, KeyboardInterrupt, EOFError):
                continue
            if not ensure_login(helper, allow_interactive_login=interactive_login_allowed):
                continue
            run_until_success(helper, delay_minutes=max(0, delay), return_to_menu=True)
        elif choice == "4":
            sys.exit(0)
