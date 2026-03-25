#!/usr/bin/env python3
"""
雨课堂动态二维码签到测试工具
支持两种方案：API 越权获取 + WebSocket 监听
"""
import json
import re
import sys
import time
import requests
from datetime import datetime

try:
    import paho.mqtt.client as mqtt
    HAS_MQTT = True
except ImportError:
    HAS_MQTT = False

# ========== 用户配置 ==========
BASE_DOMAIN = "changjiang.yuketang.cn"  # 可改为: huanghe/hehua/changjiang 等
SESSION_FILE = "yuketang_session.json"
# ==============================

BASE_URL = f"https://{BASE_DOMAIN}"
GLOBAL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
WEBSOCKET_ENDPOINTS = [f"wss://{BASE_DOMAIN}/wsapp/", "wss://pre-apple-emqx.xuetangonline.com:8083/mqtt"]

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

class YuketangTester:
    def __init__(self, session_file=SESSION_FILE):
        self.session_file = session_file
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": GLOBAL_UA, "xtbz": "ykt", "Content-Type": "application/json"})
        self.csrftoken = None

    def load_session(self):
        try:
            with open(self.session_file, 'r') as f:
                data = json.load(f)
            cookies = data.get("cookies", [])
            for c in cookies:
                if isinstance(c, dict) and 'name' in c:
                    self.session.cookies.set(c['name'], c['value'], domain=c.get('domain', ''), path=c.get('path', '/'))
            self.csrftoken = self.session.cookies.get('csrftoken')
            self.session.headers.update({"X-CSRFToken": self.csrftoken})
            resp = self.session.get(f"{BASE_URL}/api/v3/user/basic-info", timeout=10)
            if resp.json().get("code") == 0:
                log("[+] Session 加载成功")
                return True
            log("[-] Session 已失效")
            return False
        except Exception as e:
            log(f"[-] 加载 session 失败: {e}")
            return False

    def get_active_lesson(self):
        try:
            data = self.session.get(f"{BASE_URL}/api/v3/classroom/on-lesson-upcoming-exam").json()
            active = data.get('data', {}).get('onLessonClassrooms', [])
            if active:
                lesson_id = active[0].get('lessonId')
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
            resp = self.session.post(f"{BASE_URL}/api/v3/lesson/checkin", json=payload).json()
            if resp.get('code') != 0:
                log(f"[-] 获取 lessonToken 失败: {resp.get('msg')}")
                return None
            lesson_token = resp['data'].get('lessonToken')
            log(f"[+] 获取到 lessonToken")
            
            headers = {"Authorization": f"Bearer {lesson_token}", "User-Agent": GLOBAL_UA, "xtbz": "ykt"}
            resp2 = self.session.get(f"{BASE_URL}/api/v3/lesson/fetch-dynamic-invitation", params={"v": 2}, headers=headers).json()
            if resp2.get('code') != 0:
                log(f"[-] 获取暗号失败: {resp2.get('msg')}")
                return None
            
            qr_content = resp2['data'].get('qrContent', '')
            ticket_match = re.search(r'ticket=([A-Za-z0-9]+)', qr_content)
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
                payload = msg.payload.decode('utf-8', errors='ignore')
                ticket = self._extract_ticket(payload)
                if ticket:
                    log(f"[+] 监听到 ticket: {ticket}")
                    ticket_found[0] = ticket
                    client.disconnect()
            except:
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
        patterns = [r'ticket=([A-Za-z0-9]{5,})', r'"ticket"\s*:\s*"([A-Za-z0-9]{5,})"', r'inviteCode["\']?\s*:\s*["\']?([A-Za-z0-9]{5,})']
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return None

    def checkin_with_ticket(self, lesson_id, ticket):
        log(f"[*] 使用 ticket [{ticket}] 签到...")
        try:
            payload = {"lessonId": str(lesson_id), "source": 14, "inviteCode": str(ticket)}
            headers = {"X-CSRFToken": self.csrftoken, "User-Agent": GLOBAL_UA, "xtbz": "ykt"}
            res = self.session.post(f"{BASE_URL}/api/v3/lesson/checkin", headers=headers, json=payload).json()
            if res.get('code') == 0:
                log(f"[+] 签到成功！")
                return True
            else:
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
            if tester.checkin_with_ticket(lesson_id, ticket):
                sys.exit(0)
            sys.exit(1)
    
    if choice in ["2", "3"]:
        if not ticket:
            ticket = tester.try_websocket_method(lesson_id, timeout=60)
    
    if ticket:
        tester.checkin_with_ticket(lesson_id, ticket)
    else:
        log("[!] 所有方案均未获取到 ticket")
        sys.exit(1)
