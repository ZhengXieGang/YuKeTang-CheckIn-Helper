#!/usr/bin/env python3
"""
雨课堂签到协议深度诊断工具 V2
[源码事实版]
功能：
  1. 轮询活跃课堂。
  2. 后台开启 WebSocket (`/wsapp/`) 监听服务器实时指令（如 `tryzoomqrcode`, `signin`）。
  3. 捕获 Payload 并并行发起 `source=1`, `source=6`, `notkn` 等探测。
  4. 绝不通过伪造动态签名的假定（已被证实不可行），核心是截取服务器下发给网页端/桌面端的参数。
"""

import json
import time
import os
import threading
import requests
try:
    import websocket
except ImportError:
    print("请安装依赖: pip install websocket-client --break-system-packages")
    import sys; sys.exit(1)

from datetime import datetime

SESSION_FILE = "yuketang_session.json"
BASE = "https://changjiang.yuketang.cn"
LOG_FILE = f"ykt_diag_{datetime.now().strftime('%H%M%S')}.log"

def logger(msg, tag="INFO"):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] [{tag}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f: 
        f.write(line + "\n")

class YKT_Debugger:
    def __init__(self):
        self.session = requests.Session()
        self.lesson_id = None
        self.ws = None
        self.uid = 0
        self.load_session()
        self.fetch_basic_info()

    def load_session(self):
        if not os.path.exists(SESSION_FILE):
            logger(f"未找到 {SESSION_FILE}，请先获取 Cookie", "ERROR")
            return
        with open(SESSION_FILE, "r") as f:
            state = json.load(f)
            auth = state.get("desktop_auth", "")
            self.uid = state.get("uid", 0)
            
            for c in state.get("desktop_cookies", []):
                self.session.cookies.set(
                    c["name"], 
                    c["value"], 
                    domain=c.get("domain", "changjiang.yuketang.cn"), 
                    path=c.get("path", "/")
                )

            self.session.headers.update({
                "Authorization": f"Bearer {auth}", 
                "X-Client": "desktop",
                "xtbz": "ykt",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
            })


    def fetch_basic_info(self):
        try:
            r = self.session.get(f"{BASE}/api/v3/user/basic-info").json()
            if r.get("code") == 0:
                self.uid = r["data"].get("id", 0)
                logger(f"获取 UID 成功: {self.uid}", "INFO")
            else:
                logger(f"获取 UID 失败 (Cookie 可能过期): {r}", "ERROR")
        except Exception as e:
            logger(f"获取基本信息异常: {e}", "ERROR")

    def get_active_lesson(self):
        try:
            r = self.session.get(f"{BASE}/api/v3/classroom/on-lesson-upcoming-exam").json()
            if r.get("code") == 0 and r["data"].get("onLessonClassrooms"):
                self.lesson_id = str(r["data"]["onLessonClassrooms"][0]["lessonId"])
                logger(f"找到活跃课堂: {self.lesson_id}", "FACT")
                return self.lesson_id
        except Exception as e:
            logger(f"探测活跃课堂失败: {e}", "ERROR")
        logger("未找到活跃课堂或没有进行中的课程", "INFO")
        return None

    def on_ws_msg(self, ws, msg):
        try:
            data = json.loads(msg)
            op = data.get("op")
            # 过滤不需要的心跳或其他杂音
            if op not in ["fetchtimeline", "hello"]:
                logger(f"WS 数据包: {msg}", "WS_RAW")
            
            # 从前端解包中确认的 Opcodes
            if op in ["tryzoomqrcode", "signin", "checkin", "new_check_in", "qrcodezoomed"]:
                logger(f"!!! 捕获关键核心签到指令 [{op}]: {msg}", "CRITICAL")
                self.trigger_all_checkin()
        except:
            pass

    def start_ws(self):
        if not self.lesson_id:
            return
            
        cookie_str = "; ".join([f"{k}={v}" for k, v in self.session.cookies.get_dict().items()])
        ws_url = f"wss://changjiang.yuketang.cn/wsapp/"
        
        self.ws = websocket.WebSocketApp(
            ws_url,
            header={"Cookie": cookie_str},
            on_message=self.on_ws_msg,
            on_open=lambda ws: ws.send(json.dumps({
                "op":"hello",
                "userid": self.uid,
                "role":"student",
                "lessonid":self.lesson_id,
                "version": 6.3
            }))
        )
        t = threading.Thread(target=self.ws.run_forever, daemon=True)
        t.start()

    def run_api_probes(self):
        if not self.lesson_id: return
        
        logger("执行启发式 API 探测...", "PROBE")
        # 1. 尝试暗号模式 (Source 6)
        for code in ["AAAAA", "12345"]:
            try:
                r = self.session.post(f"{BASE}/api/v3/lesson/checkin", json={
                    "lessonId": self.lesson_id, "source": 6, "inviteCode": code
                }).json()
                logger(f"Source=6 (暗号 {code}): code={r.get('code')} msg={r.get('msg')}", "API")
            except: pass

        # 2. 尝试答题器模式 (Vote Machine)
        try:
            r = self.session.post(f"{BASE}/api/v3/vote-machine/lesson-check-in", json={
                "lesson_id": self.lesson_id, "source_type": 81, "submit_time": int(time.time()*1000)
            }).json()
            logger(f"Vote-Machine: code={r.get('code')} msg={r.get('msg')}", "API")
        except: pass

    def trigger_all_checkin(self):
        """当 WS 捕获到签到相关事件时，触发所有的突刺请求"""
        logger("触发全局并发签到突刺（如果提取到 inviteCode 可在此解冻）", "BURST")
        # 此处可以随时更新解析后的参数
        self.run_api_probes()


if __name__ == "__main__":
    logger("="*50)
    logger(" 初始化：雨课堂签到协议深度诊断工具 V2 ")
    logger("="*50)
    dbg = YKT_Debugger()
    if dbg.get_active_lesson():
        dbg.start_ws()
        logger("WebSocket 监听已开启，正在后台捕获凭证...")
        dbg.run_api_probes()
        logger("API 探测完成，脚本将维持 WS 监听 120 秒，请在此期间让教师端发签到...")
        for i in range(120):
            time.sleep(1)
            if i % 10 == 0: logger(f"WS 维持中 ({120-i}s)...")
        logger("监听结束")
