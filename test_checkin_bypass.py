#!/usr/bin/env python3
"""
雨课堂签到协议深度诊断工具 V5
[极致详情版 + JWT保活机制]

核心特性：
  1. 借鉴积极探测逻辑，全局拦截并刷新 `set-auth`，实现 JWT 永不过期。
  2. 极致详细的日志输出（展开关键 JSON，记录完整错误码，杜绝信息被截断）。
  3. 按照真实源码修正了 WS 握手过程 (`detectlesson` -> `hello`)。
"""

import json
import time
import os
import sys
import hashlib
import threading
import requests

try:
    import websocket
except ImportError:
    print("请安装依赖: pip install websocket-client")
    sys.exit(1)

from datetime import datetime

SESSION_FILE = "yuketang_session.json"
BASE = "https://changjiang.yuketang.cn"
LOG_FILE = f"ykt_diag_{datetime.now().strftime('%H%M%S')}.log"


def logger(msg, tag="INFO"):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] [{tag}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


class YKT_Debugger:
    def __init__(self):
        self.session = requests.Session()
        self.lesson_id = None
        self.classroom_id = None
        self.ws = None
        self.uid = 0
        self.lesson_token = ""
        self.ws_connected = False
        self.ws_hello_ok = False
        self.current_jwt = ""
        self.session_data = {}

        self.load_session()
        self.fetch_basic_info()

    def load_session(self):
        if not os.path.exists(SESSION_FILE):
            logger(f"未找到 {SESSION_FILE}", "ERROR")
            return
        
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            self.session_data = json.load(f)

        self.current_jwt = self.session_data.get("desktop_auth", "")
        self.uid = self.session_data.get("uid", 0)

        # 注入 Cookie
        for c in self.session_data.get("desktop_cookies", []):
            self.session.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain", "changjiang.yuketang.cn"),
                path=c.get("path", "/")
            )
        
        # 初始化基础 Header
        self._update_session_headers()

    def _update_session_headers(self):
        headers = {
            "X-Client": "desktop",
            "desktop-v": "v2",
            "xtbz": "ykt",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        }
        if self.current_jwt:
            headers["Authorization"] = f"Bearer {self.current_jwt}"
        self.session.headers.update(headers)

    def save_session(self):
        """将最新的 JWT 保存回文件，实现保活"""
        self.session_data["desktop_auth"] = self.current_jwt
        self.session_data["desktop_auth_updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(self.session_data, f, ensure_ascii=False, indent=2)
        logger("JWT 已更新并保存到会话文件", "AUTH")

    # ==================== 核心 HTTP 请求封装 ====================

    def _api(self, method, path, label, **kwargs):
        """统一拦截响应，提取 set-auth 刷新 JWT，并输出极尽详细的日志"""
        try:
            r = self.session.request(method, f"{BASE}{path}", timeout=10, **kwargs)
            
            # 【借鉴点1】 JWT 动态续期
            new_auth = r.headers.get("set-auth") or r.headers.get("Set-Auth")
            if new_auth and new_auth != self.current_jwt:
                self.current_jwt = new_auth
                self._update_session_headers()
                self.save_session()
                logger(f"[{label}] 成功捕获并刷新 JWT!", "TOKEN")

            if "json" in r.headers.get("Content-Type", ""):
                d = r.json()
                code = d.get("code", "?")
                msg = d.get("msg", d.get("message", ""))
                
                # 记录核心日志，若 code!=0 且有特定数据，则展开打印
                success = (code == 0 or code == 200)
                tag = "API_OK" if success else "API_ERR"
                
                logger(f"[{label}] -> code={code} | msg={msg}", tag)
                
                # 【借鉴点2】 详细日志：如果存在实际有用的数据载荷，完整漂亮地打印它
                data_obj = d.get("data") or d.get("Data")
                if data_obj and (success or code not in [50004, 50000]):
                    dumped = json.dumps(data_obj, ensure_ascii=False, indent=2)
                    # 每行加缩进记录
                    for line in dumped.split("\n"):
                        logger(f"    {line}", tag)
                return d
            else:
                logger(f"[{label}] HTTP {r.status_code} (非 JSON 响应)", "API_RAW")
                return None
        except Exception as e:
            logger(f"[{label}] 异常抛出: {e}", "API_EXC")
        return None

    # ==================== 初始化与前置请求 ====================

    def fetch_basic_info(self):
        r = self._api("GET", "/api/v3/user/basic-info", "账号信息验证")
        if r and r.get("code") == 0:
            self.uid = r["data"]["id"]
            logger(f"登录上下文有效。UID: {self.uid} (角色: {r['data'].get('role')})", "AUTH")
        else:
            logger(f"登录可能过期！请检查 yuketang_session.json", "AUTH")

    def get_active_lesson(self):
        r = self._api("GET", "/api/v3/classroom/on-lesson-upcoming-exam", "活跃课堂探测")
        if r and r.get("code") == 0 and r["data"].get("onLessonClassrooms"):
            lesson = r["data"]["onLessonClassrooms"][0]
            self.lesson_id = str(lesson["lessonId"])
            self.classroom_id = str(lesson.get("classroomId", ""))
            course = lesson.get("courseName", "")
            logger(f"发现活跃课堂: '{course}' (LessonID={self.lesson_id})", "FACT")
            return True
        logger("当前未找到正在进行中的课堂。", "INFO")
        return False

    def obtain_lesson_token(self):
        """通过教师端协议(source=10)尝试提取 lessonToken，供 WS 使用"""
        logger("正在尝试提取 lessonToken...", "TOKEN")
        r = self._api("POST", "/api/v3/lesson/checkin", "获取WS票据", json={"lessonId": self.lesson_id, "source": 10})
        if r and r.get("code") == 0:
            data = r.get("data", {})
            self.lesson_token = data.get("lessonToken", "")
            if self.lesson_token:
                logger(f"成功攫取 lessonToken。长度: {len(self.lesson_token)}", "TOKEN")
                return True
        logger("无法攫取 lessonToken，将使用空权限模式建立 WS。", "TOKEN")
        return False

    # ==================== WebSocket ====================

    def _on_open(self, ws):
        self.ws_connected = True
        detect = {"op": "detectlesson", "lessonid": str(self.lesson_id)}
        logger(f"发送握手阶段 1: {json.dumps(detect)}", "WS_TX")
        ws.send(json.dumps(detect))

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            op = data.get("op", "")

            # 过滤高频且无价值的心跳
            if op in ["xintiao"]: return

            # detectlesson 握手应答
            if op == "detectlesson":
                hello = {
                    "op": "hello",
                    "userid": int(self.uid),
                    "role": "student",
                    "auth": self.lesson_token,
                    "lessonid": str(self.lesson_id),
                    "version": 5.5,
                    "call": -1
                }
                logger(f"收到 detect 回复。发送握手阶段 2 (hello): {json.dumps(hello)}", "WS_TX")
                ws.send(json.dumps(hello))
                return

            # hello 握手应答
            if op == "hello":
                self.ws_hello_ok = True
                logger(f"✅ WS 身份认证握手成功！详情: {message}", "WS_OK")
                self._start_heartbeat(ws)
                return

            # 【重要】签到相关的重要协议全量展开
            if op in ["tryzoomqrcode", "signin", "checkin", "new_check_in", "qrcodezoomed"]:
                logger(f"🔥🔥🔥 捕获高优指令: {op}", "WS_CRITICAL")
                dumped = json.dumps(data, ensure_ascii=False, indent=2)
                for line in dumped.split("\n"):
                    logger(f"    {line}", "WS_CRITICAL")
                
                # 触发猛烈探测
                threading.Thread(target=self._on_checkin_event, daemon=True).start()
                return
            
            # 其他未知指令，平铺打印
            logger(f"收到未分类消息: op={op} -> {message}", "WS_RX")

        except Exception as e:
            logger(f"解析 WS 消息异常: {e} -> {message}", "WS_ERR")

    def _on_error(self, ws, error):
        logger(f"连接抛错: {error}", "WS_ERR")

    def _on_close(self, ws, code, msg):
        self.ws_connected = False
        logger(f"连接断开: 代码={code} 信息={msg}", "WS_CLOSE")

    def _start_heartbeat(self, ws):
        # 1. WS 长链接纯二进制心跳（给 Socket 保活用）
        def _ws_beat():
            while self.ws_connected:
                try: ws.send(json.dumps({"op": "xintiao", "lessonid": str(self.lesson_id)}))
                except: break
                time.sleep(60)
                
        # 2. HTTP 频繁续签心跳（专攻 JWT 30s 死亡硬限）
        def _http_jwt_beat():
            while self.ws_connected:
                time.sleep(15)  # 15s续签，压秒防超时
                logger("触发后台静默 JWT 续签", "SYS_BEAT")
                self._api("GET", "/api/v3/user/basic-info", "JWT静默保活")

        threading.Thread(target=_ws_beat, daemon=True).start()
        threading.Thread(target=_http_jwt_beat, daemon=True).start()

    def start_ws(self):
        cookie_str = "; ".join([f"{c.name}={c.value}" for c in self.session.cookies])
        ws_url = "wss://changjiang.yuketang.cn/wsapp/"
        logger(f"正在建立通信信道: {ws_url}", "WS_INIT")

        self.ws = websocket.WebSocketApp(
            ws_url,
            header={
                "Cookie": cookie_str,
                "User-Agent": self.session.headers.get("User-Agent", ""),
                "Origin": "https://changjiang.yuketang.cn",
            },
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        threading.Thread(target=self.ws.run_forever, daemon=True).start()

        time.sleep(5) # 给定充足的握手窗口
        if not self.ws_connected: logger("⚠️ WS 底层建立失败，请检查网络阻断情况", "WS_WARN")
        elif not self.ws_hello_ok: logger("⚠️ WS 底层已建立，但认证无响应", "WS_WARN")
        else: logger("✅ WS 信令系统一切就绪，等待下发指示...", "WS_OK")

    # ==================== 激进式签到突刺 ====================

    def _on_checkin_event(self):
        """当 WS 收到签到指令时，并发式全面发包"""
        logger(">>> WS 推送接收，立即释放所有表单参数尝试签到 <<<", "BURST")
        self.run_api_probes()

    def run_api_probes(self):
        if not self.lesson_id: return
        logger("=" * 40, "PROBE")
        logger("执行启发式表单盲试", "PROBE")
        lid = self.lesson_id

        # 1. PC 网页常规扫码等途径覆盖
        self._api("POST", "/api/v3/lesson/checkin", "直接签到s1", json={"lessonId": lid, "source": 1})
        self._api("POST", "/api/v3/lesson/checkin", "桌面强制加入s43", json={"lessonId": lid, "source": 43, "joinIfNotIn": True})
        self._api("POST", "/api/v3/lesson/checkin", "暗号容错盲猜s6", json={"lessonId": lid, "source": 6, "inviteCode": "AAAAA"})

        # 2. 从源码扒出的各种老版本兼容路由
        self._api("POST", "/api/lesson/web_check_in", "古早端接口s14", json={"invite_code": "AAAAA", "source": 14})
        self._api("POST", "/api/v3/lesson/notkn/checkin", "无票据端接口s1", json={"source": 1})

        # 3. 硬件设施签到突破（极具潜力）
        dev_id = "PEN_" + hashlib.md5(str(self.uid).encode()).hexdigest()[:12].upper()
        now_ms = int(time.time() * 1000)
        for st in [81, 82]:
            self._api("POST", "/api/v3/vote-machine/lesson-check-in", f"硬件欺骗:st{st}", 
                      json={"lesson_id": lid, "devices": [{"id": dev_id, "dt": now_ms}], "submit_time": now_ms, "source_type": st})

        # 4. 尝试通过伪造越权拉取内容
        self._api("GET", "/api/v3/lesson/fetch-dynamic-invitation", "动态口令逆拉取", params={"v": 2})
        logger("=" * 40, "PROBE")


if __name__ == "__main__":
    logger("=" * 60)
    logger(" 雨课堂签到协议深度诊断工具 V5 (整合版) ")
    logger("=" * 60)

    dbg = YKT_Debugger()
    if not dbg.get_active_lesson():
        logger("退出监听引擎。", "SYS")
        sys.exit(0)

    dbg.obtain_lesson_token()
    dbg.start_ws()

    # 首轮空发，试探报错点并确立上下文
    dbg.run_api_probes()

    logger(f"引擎休眠进入深度监听，有效期 300 秒...", "SYS")
    logger(f"所有诊断输出将重定向至 -> {LOG_FILE}", "SYS")
    try:
        for i in range(300):
            time.sleep(1)
            if i % 30 == 0 and i > 0:
                conn_status = "在线" if dbg.ws_connected else "离线"
                auth_status = "已验证" if dbg.ws_hello_ok else "未验证"
                logger(f"心跳探测 | WS: {conn_status}/{auth_status} | 监听结束倒数: {300 - i}s", "HEARTBEAT")
    except KeyboardInterrupt:
        logger("通过中断机制安全回收...", "SYS")

    if dbg.ws: dbg.ws.close()
    logger("侦听引擎释放。请查看最新日志文件。")
