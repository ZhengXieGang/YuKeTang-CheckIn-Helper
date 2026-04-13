#!/usr/bin/env python3
"""
雨课堂签到协议深度诊断工具 V4
[源码级修正版]

核心修正（基于 5942.js 逆向）：
  1. WS 握手：先发 detectlesson，等回复后再发 hello（之前直接发 hello 是错的）
  2. hello 的 auth 字段需要 lessonToken（来自 /api/v3/lesson/checkin 响应）
  3. 心跳用 op: "xintiao"，每 60 秒
  4. 尝试调用教师端 API：get-invitation / fetch-dynamic-invitation
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
    print("请安装依赖: pip install websocket-client --break-system-packages")
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
        self.lesson_token = ""       # 关键：从 checkin 响应获取
        self.ws_connected = False
        self.ws_hello_ok = False
        self.load_session()
        self.fetch_basic_info()

    def load_session(self):
        if not os.path.exists(SESSION_FILE):
            logger(f"未找到 {SESSION_FILE}", "ERROR")
            return
        with open(SESSION_FILE, "r") as f:
            state = json.load(f)
        for c in state.get("desktop_cookies", []):
            self.session.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain", "changjiang.yuketang.cn"),
                path=c.get("path", "/")
            )
        self.session.headers.update({
            "X-Client": "desktop",
            "xtbz": "ykt",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/123.0.0.0 Safari/537.36"
        })

    def fetch_basic_info(self):
        try:
            r = self.session.get(f"{BASE}/api/v3/user/basic-info").json()
            if r.get("code") == 0:
                self.uid = r["data"]["id"]
                logger(f"登录有效: {r['data'].get('name')} (UID: {self.uid})", "AUTH")
            else:
                logger(f"Cookie 过期: {r}", "ERROR")
        except Exception as e:
            logger(f"登录检查异常: {e}", "ERROR")

    def get_active_lesson(self):
        try:
            r = self.session.get(f"{BASE}/api/v3/classroom/on-lesson-upcoming-exam").json()
            if r.get("code") == 0 and r["data"].get("onLessonClassrooms"):
                lesson = r["data"]["onLessonClassrooms"][0]
                self.lesson_id = str(lesson["lessonId"])
                self.classroom_id = str(lesson.get("classroomId", ""))
                course = lesson.get("courseName", "")
                logger(f"★ 活跃课堂: {course} (lesson={self.lesson_id})", "FACT")
                return True
        except Exception as e:
            logger(f"查找课堂异常: {e}", "ERROR")
        logger("无活跃课堂", "INFO")
        return False

    # ==================== 获取 lessonToken ====================

    def obtain_lesson_token(self):
        """
        源码确认：lessonToken 来自 /api/v3/lesson/checkin 的响应 data.lessonToken
        用 source=10 尝试获取（教师端进入课堂的方式）
        """
        logger("尝试获取 lessonToken ...", "TOKEN")
        for src in [10, 1]:
            try:
                r = self.session.post(f"{BASE}/api/v3/lesson/checkin", json={
                    "lessonId": self.lesson_id, "source": src
                }).json()
                code = r.get("code")
                data = r.get("data", {})
                msg = r.get("msg", "")
                logger(f"  checkin(source={src}): code={code} msg={msg}", "TOKEN")

                if code == 0 and isinstance(data, dict):
                    token = data.get("lessonToken", "")
                    role = data.get("role", "")
                    if token:
                        self.lesson_token = token
                        logger(f"  ✅ 获取到 lessonToken (role={role}, len={len(token)})", "TOKEN")
                        return True
                    else:
                        logger(f"  响应无 lessonToken，data keys: {list(data.keys())}", "TOKEN")
            except Exception as e:
                logger(f"  checkin(source={src}) 异常: {e}", "ERROR")
        logger("  ❌ 未获取到 lessonToken", "TOKEN")
        return False

    # ==================== WebSocket（源码级修正） ====================

    def _on_open(self, ws):
        self.ws_connected = True
        # 源码：onopen 后先发 detectlesson，不是直接发 hello！
        detect = {"op": "detectlesson", "lessonid": str(self.lesson_id)}
        logger(f"[WS] 连接建立，发送 detectlesson: {json.dumps(detect)}", "WS")
        ws.send(json.dumps(detect))

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            op = data.get("op", "")

            # 过滤心跳回复
            if op in ["xintiao"]:
                return

            # 全量记录
            logger(f"[WS] op={op}: {message[:1000]}", "WS_RAW")

            # detectlesson 回复 → 紧接着发 hello
            if op == "detectlesson":
                logger(f"[WS] 收到 detectlesson 回复，发送 hello ...", "WS")
                hello = {
                    "op": "hello",
                    "userid": int(self.uid),
                    "role": "student",
                    "auth": self.lesson_token,  # 源码确认必须传 lessonToken
                    "lessonid": str(self.lesson_id),
                    "version": 5.5,
                    "call": -1
                }
                logger(f"[WS] hello 报文: {json.dumps(hello)}", "WS")
                ws.send(json.dumps(hello))
                return

            # hello 回复
            if op == "hello":
                self.ws_hello_ok = True
                logger(f"[WS] ✅ hello 握手成功: {message[:500]}", "WS_OK")
                # 启动心跳
                self._start_heartbeat(ws)
                return

            # 签到相关指令
            if op in ["tryzoomqrcode", "signin", "checkin", "new_check_in", "qrcodezoomed"]:
                logger(f"🔥🔥🔥 [{op}]: {message}", "CRITICAL")
                threading.Thread(target=self._on_checkin_event, args=(data,), daemon=True).start()

        except Exception as e:
            logger(f"[WS] 解析异常: {e}, raw={message[:300]}", "WS_ERR")

    def _on_error(self, ws, error):
        logger(f"[WS] 错误: {error}", "WS_ERR")

    def _on_close(self, ws, code, msg):
        self.ws_connected = False
        logger(f"[WS] 关闭: code={code} msg={msg}", "WS_CLOSE")

    def _start_heartbeat(self, ws):
        """源码确认心跳是 op=xintiao，每 60 秒"""
        def _beat():
            while self.ws_connected:
                try:
                    ws.send(json.dumps({"op": "xintiao", "lessonid": str(self.lesson_id)}))
                except:
                    break
                time.sleep(60)
        threading.Thread(target=_beat, daemon=True).start()

    def start_ws(self):
        cookie_str = "; ".join([f"{c.name}={c.value}" for c in self.session.cookies])
        ws_url = "wss://changjiang.yuketang.cn/wsapp/"
        logger(f"[WS] 连接 {ws_url} (lessonToken={'有' if self.lesson_token else '无'})", "WS")

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

        # 等待握手完成
        time.sleep(5)
        if not self.ws_connected:
            logger("[WS] ⚠️ 5 秒内未连接", "WS_WARN")
        elif not self.ws_hello_ok:
            logger("[WS] ⚠️ 已连接但 hello 未回复", "WS_WARN")
        else:
            logger("[WS] ✅ 连接正常，监听中", "WS_OK")

    # ==================== 签到事件处理 ====================

    def _on_checkin_event(self, data):
        """WS 收到签到指令后的处理"""
        logger(">>> 触发签到突刺 <<<", "BURST")
        self.run_api_probes()

    # ==================== API 探测 ====================

    def _api(self, method, path, label, **kwargs):
        try:
            r = self.session.request(method, f"{BASE}{path}", timeout=10, **kwargs)
            if "json" in r.headers.get("Content-Type", ""):
                d = r.json()
                code = d.get("code", "?")
                msg = d.get("msg", d.get("message", ""))
                data_str = json.dumps(d.get("data", {}), ensure_ascii=False)[:300]
                logger(f"[{label}] code={code} msg={msg} data={data_str}", "API")
                return d
            else:
                logger(f"[{label}] HTTP {r.status_code}", "API")
        except Exception as e:
            logger(f"[{label}] 异常: {e}", "API_ERR")
        return None

    def run_api_probes(self):
        if not self.lesson_id:
            return

        logger("=" * 40, "PROBE")
        lid = self.lesson_id

        # 直接签到
        self._api("POST", "/api/v3/lesson/checkin", "直接 s=1",
                  json={"lessonId": lid, "source": 1})

        # source=43（桌面端加入课堂，源码 6251.js 确认）
        self._api("POST", "/api/v3/lesson/checkin", "桌面加入 s=43",
                  json={"lessonId": lid, "source": 43, "joinIfNotIn": True})

        # 暗号测试
        self._api("POST", "/api/v3/lesson/checkin", "暗号 s=6",
                  json={"lessonId": lid, "source": 6, "inviteCode": "AAAAA"})

        # 旧版 Web 签到（源码确认路径 /api/lesson/web_check_in）
        self._api("POST", "/api/lesson/web_check_in", "旧版 web s=14",
                  json={"invite_code": "AAAAA", "source": 14})

        # notkn（Web 新版 checkin）
        self._api("POST", "/api/v3/lesson/notkn/checkin", "notkn s=1",
                  json={"source": 1})

        # 投票器
        dev_id = "PEN_" + hashlib.md5(str(self.uid).encode()).hexdigest()[:12].upper()
        now_ms = int(time.time() * 1000)
        for st in [81, 82]:
            self._api("POST", "/api/v3/vote-machine/lesson-check-in", f"投票器 st={st}",
                      json={"lesson_id": lid, "devices": [{"id": dev_id, "dt": now_ms}],
                            "submit_time": now_ms, "source_type": st})

        # 教师端 API（拿到 lessonToken 后可能有权限调用）
        self._api("GET", "/api/v3/lesson/get-invitation", "教师端-邀请码")
        self._api("GET", "/api/v3/lesson/fetch-dynamic-invitation", "教师端-动态码",
                  params={"v": 2})

        # connection token
        self._api("GET", "/api/v3/connection/get-token", "连接Token")

        logger("=" * 40, "PROBE")


if __name__ == "__main__":
    logger("=" * 50)
    logger(" 雨课堂签到协议深度诊断工具 V4 (源码级修正) ")
    logger("=" * 50)

    dbg = YKT_Debugger()
    if not dbg.get_active_lesson():
        logger("无活跃课堂，退出。")
        sys.exit(0)

    # 第一步：先获取 lessonToken
    dbg.obtain_lesson_token()

    # 第二步：带 lessonToken 开启 WS
    dbg.start_ws()

    # 第三步：跑一轮 API 探测
    dbg.run_api_probes()

    # 第四步：持续监听
    logger(f"进入持续监听（300 秒）... 日志: {LOG_FILE}", "LISTEN")
    try:
        for i in range(300):
            time.sleep(1)
            if i % 30 == 0 and i > 0:
                st = "已连接" if dbg.ws_connected else "未连接"
                hk = "已握手" if dbg.ws_hello_ok else "未握手"
                logger(f"状态: WS={st}/{hk} | 剩余 {300 - i}s", "HEARTBEAT")
    except KeyboardInterrupt:
        logger("手动中断")

    if dbg.ws:
        dbg.ws.close()
    logger("诊断结束。")
