import requests
import qrcode
import time
import json
import sys
import os
import re
import asyncio
import argparse
from datetime import datetime

# 检测可选依赖是否可用
try:
    import ddddocr
    from PIL import Image
    from io import BytesIO
    from playwright.async_api import async_playwright
    HAS_AUTO_LOGIN = True
except ImportError:
    HAS_AUTO_LOGIN = False

# ========== 用户配置 ==========
AUTO_LOGIN_PHONE = ""
AUTO_LOGIN_PSWD = ""
CHECKIN_COOLDOWN_MINUTES = 30
# ==============================

GLOBAL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
BASE_URL = "https://changjiang.yuketang.cn"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yuketang_session.json")

def log(msg):
    print(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}")


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

            await asyncio.sleep(2)
            cookies = await ctx.cookies()
            await browser.close()

            if ok and any(c['name'] == 'sessionid' for c in cookies):
                with open(STATE_FILE, "w") as f:
                    json.dump({"cookies": cookies}, f)
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
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            return {}

    def _save_state(self, state):
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, ensure_ascii=False)

    def save_session(self):
        """持久化 Cookie 和签到记录，增强 Session 提权逻辑"""
        state = self._load_state()
        if not isinstance(state, dict):
            state = {}
        cookies = []
        # 将无过期时间的 Session Cookie 提权为一年长效
        future = int(time.time()) + 86400 * 365
        for c in self.session.cookies:
            # 如果是 Session 级 Cookie (expires 为空或 0)，则强制设为长效
            exp = c.expires or future
            cookies.append({"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "expires": exp})
        state["cookies"] = cookies
        self._save_state(state)

    def load_session(self):
        """从文件恢复 Session，兼容多种历史格式"""
        try:
            with open(STATE_FILE, "r") as f:
                raw = json.load(f)

            if isinstance(raw, list):
                c_list = raw
            elif isinstance(raw, dict):
                c_list = raw.get("cookies", [])
                if not c_list and 'sessionid' in raw:
                    self.session.cookies.update(raw)
                    c_list = []
            else:
                return False

            for c in c_list:
                if not isinstance(c, dict) or 'name' not in c:
                    continue
                self.session.cookies.set(
                    c['name'], c['value'],
                    domain=c.get('domain', ''),
                    path=c.get('path', '/'),
                    expires=int(c.get('expires', 0)) if c.get('expires') else None
                )

            self.sessionid = self.session.cookies.get('sessionid')
            self.csrftoken = self.session.cookies.get('csrftoken')

            if self.sessionid:
                self.session.get(f"{BASE_URL}/v2/web/index", timeout=10)
                self.session.headers.update({"X-CSRFToken": self.csrftoken})
                # 更换为轻量级的基本信息探针
                resp = self.session.get(f"{BASE_URL}/api/v3/user/basic-info", timeout=10)
                if "application/json" not in resp.headers.get("Content-Type", ""):
                    return False
                data = resp.json()
                if isinstance(data, dict) and (data.get("code") == 0 or data.get("success")):
                    self.save_session()
                    return True
            return False
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
            self.csrftoken = resp.cookies.get('csrftoken')
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
            if self.session.cookies.get('sessionid'):
                self.save_session()
                return True
            return False
        except:
            return False

    def get_active_lesson_data(self):
        try:
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
