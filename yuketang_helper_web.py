#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from io import BytesIO

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
BASE_DOMAIN = "changjiang.yuketang.cn"  # 默认长江雨课堂，自行更换
AUTO_LOGIN_PHONE = ""
AUTO_LOGIN_PSWD = ""
CHECKIN_COOLDOWN_MINUTES = 30
# ==============================

GLOBAL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
BASE_URL = f"https://{BASE_DOMAIN}"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "yuketang_session_web.json")
BROWSER_SYNC_WAIT_SECONDS = 6


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
        json.dump(data, f, ensure_ascii=False)


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

    def _load_cookies_from_state(self):
        raw = self._load_state()
        if isinstance(raw, list):
            cookie_list = raw
        elif isinstance(raw, dict):
            cookie_list = raw.get("cookies", [])
            if not cookie_list and "sessionid" in raw:
                cookie_list = [
                    {"name": k, "value": v, "domain": BASE_DOMAIN, "path": "/"}
                    for k, v in raw.items()
                    if isinstance(v, str)
                ]
        else:
            return False
        self._set_cookie_records(cookie_list, clear=True)
        return bool(self.session.cookies)

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

    def _check_cooldown(self, lesson_id):
        state = self._load_state()
        last = state.get("last_checkin") if isinstance(state, dict) else None
        if not last or str(last.get("lesson_id")) != str(lesson_id):
            return False
        try:
            elapsed = (
                datetime.now() - datetime.strptime(last["time"], "%Y-%m-%d %H:%M:%S")
            ).total_seconds() / 60
            if elapsed < CHECKIN_COOLDOWN_MINUTES:
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

    def sign_in(self, lesson_id, classroom_id=None, source=1):
        if self._check_cooldown(lesson_id):
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
                log(f"[+] 签到成功 (课堂: {lesson_id})")
                self._record_checkin(lesson_id)
                self.save_session()
                return True
            else:
                log(f"[-] 签到失败: {res.get('msg')}")
                return False
        except Exception as e:
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="雨课堂自动签到助手（Web 账密登录版）")
    parser.add_argument("-a", "--auto", action="store_true", help="自动扫描课堂并签到")
    parser.add_argument("-k", "--keepalive", action="store_true", help="仅执行会话保活")
    parser.add_argument("--qr", action="store_true", help="强制使用二维码扫码登录")
    parser.add_argument("-p", "--phone", type=str, help="手机号（覆盖脚本内置配置）")
    parser.add_argument("-pw", "--password", type=str, help="密码（覆盖脚本内置配置）")
    parser.add_argument(
        "--cooldown",
        type=int,
        default=CHECKIN_COOLDOWN_MINUTES,
        help=f"签到去重冷却时间，分钟（默认 {CHECKIN_COOLDOWN_MINUTES}）",
    )
    parser.add_argument("-s", "--schedule", type=int, metavar="N", help="延迟 N 分钟后开始，每分钟自动检测并签到")
    args = parser.parse_args()

    CHECKIN_COOLDOWN_MINUTES = args.cooldown
    phone = args.phone or AUTO_LOGIN_PHONE
    password = args.password or AUTO_LOGIN_PSWD

    helper = YuketangHelper()
    auth = helper.load_session()

    if not auth and not args.qr:
        if HAS_AUTO_LOGIN:
            auth = helper.auto_login(phone, password)
            if auth:
                auth = helper.load_session()
        else:
            log("[*] 未检测到自动登录依赖 (ddddocr/playwright)，跳过自动登录")

    if not auth:
        log("[*] 进入二维码扫码登录...")
        state, uuid = helper.get_login_qrcode()
        if state and helper.wait_for_login_and_callback(state, uuid):
            auth = True

    if not auth:
        log("[!] 所有登录方式均失败，请检查网络或账号配置")
        sys.exit(1)

    if args.keepalive:
        sys.exit(0 if helper.keep_alive() else 1)

    if args.auto:
        lesson_id, classroom_id = helper.get_active_lesson_data()
        if lesson_id:
            sys.exit(0 if helper.sign_in(lesson_id, classroom_id=classroom_id) else 1)
        else:
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
        print("\n1. 自动扫描签到\n2. 扫码登录\n3. 定时签到\n4. 退出")
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
            state, uuid = helper.get_login_qrcode()
            if state:
                helper.wait_for_login_and_callback(state, uuid)
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
            sys.exit(0)
