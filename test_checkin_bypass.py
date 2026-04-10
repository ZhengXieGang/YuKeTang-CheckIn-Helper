#!/usr/bin/env python3
"""
雨课堂签到方法验证脚本 — 基于源码逆向确认的 API

源码确认的签到相关端点：
  [桌面端]  /api/v3/lesson/checkin            POST {lessonId, source, inviteCode?}
  [Web端]   /api/v3/lesson/notkn/checkin      POST {source, inviteCode?}
  [旧Web]   /api/lesson/web_check_in          POST {invite_code?, source}
  [投票器]  /api/v3/vote-machine/lesson-check-in  POST {lesson_id, devices, source_type}
  [动态码]  /api/v3/lesson/check-in/dynamic-qr-code  GET ?c=&t=&s=&v=
  [课堂总结] /api/v3/lesson-summary/checkin    (未知用法)
  [legacy]  /api/legacy/lesson/get-token       GET

源码确认的 source 值：
  1  = 普通签到
  6  = 暗号签到 (inviteCode = 5位字母数字)
  10 = 教师端进入课堂（返回 lessonToken）
  14 = 二维码签到 (inviteCode 以数字开头)
  81 = 投票器 tryVoteCheckin
  82 = 投票器 checkinByDigitalPen
"""
import json
import time
import sys
import os
import hashlib
from datetime import datetime

import requests

SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yuketang_session.json")
BASE = "https://changjiang.yuketang.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")
DESKTOP_HEADERS = {"xtbz": "ykt", "desktop-v": "v2", "X-Client": "desktop", "Origin": "file://"}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:12]}] {msg}", flush=True)


def load_session():
    with open(SESSION_FILE, "r") as f:
        state = json.load(f)
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Content-Type": "application/json"})
    s.headers.update(DESKTOP_HEADERS)
    for c in state.get("desktop_cookies", []):
        s.cookies.set(c["name"], c["value"],
                      domain=c.get("domain", "changjiang.yuketang.cn"),
                      path=c.get("path", "/"))
    return s, state


def api(session, method, path, label="", **kwargs):
    """发起请求，输出详细日志"""
    url = f"{BASE}{path}"
    try:
        r = session.request(method, url, timeout=10, **kwargs)
        ct = r.headers.get("Content-Type", "")
        if "json" in ct:
            j = r.json()
            code = j.get("code", j.get("Status", "?"))
            msg = j.get("msg", j.get("Message", j.get("message", "")))
            data = j.get("data", j.get("Data", None))
            log(f"  {label}: HTTP {r.status_code} | code={code} | msg={msg}")
            if data and code in (0, 200):
                log(f"    → data: {json.dumps(data, ensure_ascii=False)[:200]}")
            return j, r.status_code
        else:
            body = r.text[:100] if r.text else "(空)"
            log(f"  {label}: HTTP {r.status_code} | 非JSON | {body}")
            return None, r.status_code
    except Exception as e:
        log(f"  {label}: 异常 | {e}")
        return None, str(e)


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def main():
    session, state = load_session()

    # ============ 0. 验证 Cookie ============
    section("0. 验证 Cookie 登录态")
    j, _ = api(session, "GET", "/api/v3/user/basic-info", label="basic-info")
    if not j or j.get("code") != 0:
        log("❌ Cookie 无效，退出。")
        sys.exit(1)
    uid = j["data"]["id"]
    log(f"  用户={j['data'].get('name')} uid={uid}")

    # ============ 1. 获取活跃课堂 ============
    section("1. 获取活跃课堂")
    j, _ = api(session, "GET", "/api/v3/classroom/on-lesson-upcoming-exam",
               label="on-lesson")
    active_lessons = []
    if j and j.get("code") == 0:
        active_lessons = j["data"].get("onLessonClassrooms", [])
        if active_lessons:
            for al in active_lessons:
                log(f"  ★ 活跃: lesson={al.get('lessonId')} "
                    f"classroom={al.get('classroomId')} "
                    f"course={al.get('courseName','')}")
        else:
            log("  无活跃课堂")

    lesson_id = None
    if active_lessons:
        lesson_id = active_lessons[0]["lessonId"]
    else:
        lesson_id = state.get("last_checkin", {}).get("lesson_id")
        if lesson_id:
            log(f"  使用历史 lesson_id={lesson_id}（课堂已结束，结果仅供参考）")
        else:
            log("  无历史 lesson_id，跳过需要 lessonId 的测试")

    is_active = bool(active_lessons)

    # ============ 2. 桌面端 /api/v3/lesson/checkin ============
    section("2. 桌面端 /api/v3/lesson/checkin")
    log("源码: API.index.lesson_checkin / API.lesson.checkin")
    log("源码: 教师端用 source=10 进入课堂，学生端用 source=6+inviteCode")

    if lesson_id:
        # source=1 普通签到
        api(session, "POST", "/api/v3/lesson/checkin",
            label="source=1",
            json={"lessonId": str(lesson_id), "source": 1})

        # source=6 暗号签到（假暗号，看错误码）
        api(session, "POST", "/api/v3/lesson/checkin",
            label="source=6 inv=AAAAA",
            json={"lessonId": str(lesson_id), "source": 6,
                  "inviteCode": "AAAAA", "joinIfNotIn": True})

        # source=10 教师端模式
        api(session, "POST", "/api/v3/lesson/checkin",
            label="source=10",
            json={"lessonId": str(lesson_id), "source": 10})

        # source=14 二维码签到
        api(session, "POST", "/api/v3/lesson/checkin",
            label="source=14 inv=12345",
            json={"lessonId": str(lesson_id), "source": 14,
                  "inviteCode": "12345"})
    else:
        log("  跳过（无 lessonId）")

    # ============ 3. Web端 /api/v3/lesson/notkn/checkin ============
    section("3. Web端 /api/v3/lesson/notkn/checkin")
    log("源码: API.pc.index.new_check_in")
    log("源码: checkin(e,n) → POST {source:e, inviteCode:n}")
    log("特性: 不需要 lessonId！返回 INVITE_CODE_TIMEOUT 而非 LESSON_END")

    # source=1
    api(session, "POST", "/api/v3/lesson/notkn/checkin",
        label="source=1",
        json={"source": 1})

    # source=6 暗号
    api(session, "POST", "/api/v3/lesson/notkn/checkin",
        label="source=6 inv=AAAAA",
        json={"source": 6, "inviteCode": "AAAAA", "joinIfNotIn": True})

    # source=10 教师端
    api(session, "POST", "/api/v3/lesson/notkn/checkin",
        label="source=10",
        json={"source": 10})

    # source=14 二维码
    api(session, "POST", "/api/v3/lesson/notkn/checkin",
        label="source=14 inv=12345",
        json={"source": 14, "inviteCode": "12345"})

    # ============ 4. 旧Web /api/lesson/web_check_in ============
    section("4. 旧Web /api/lesson/web_check_in")
    log("源码: API.pc.index.old_check_in")

    api(session, "POST", "/api/lesson/web_check_in",
        label="source=14 invite_code=12345",
        json={"invite_code": "12345", "source": 14})

    # ============ 5. 投票器 /api/v3/vote-machine/lesson-check-in ============
    section("5. 投票器 /api/v3/vote-machine/lesson-check-in")
    log("源码: API.device.lesson_check_in")
    log("源码: tryVoteCheckin → source_type=81")
    log("源码: checkinByDigitalPen → source_type=82")

    if lesson_id:
        now_ms = int(time.time() * 1000)
        dev81 = "VOTE_" + hashlib.md5(str(uid).encode()).hexdigest()[:8].upper()
        dev82 = "PEN_" + hashlib.md5(str(uid).encode()).hexdigest()[:12].upper()

        api(session, "POST", "/api/v3/vote-machine/lesson-check-in",
            label="source_type=81",
            json={"lesson_id": str(lesson_id),
                  "devices": [{"id": dev81, "dt": now_ms}],
                  "submit_time": now_ms, "source_type": 81})

        api(session, "POST", "/api/v3/vote-machine/lesson-check-in",
            label="source_type=82",
            json={"lesson_id": str(lesson_id),
                  "devices": [{"id": dev82, "dt": now_ms}],
                  "submit_time": now_ms, "source_type": 82})
    else:
        log("  跳过（无 lessonId）")

    # ============ 6. 动态码端点 ============
    section("6. 动态码 /api/v3/lesson/check-in/dynamic-qr-code")
    log("源码: 二维码 URL 就是这个 GET 端点")
    log("源码: qrContent 由 fetch-dynamic-invitation 返回，签名 s 后端生成")

    # 无参数访问
    api(session, "GET", "/api/v3/lesson/check-in/dynamic-qr-code",
        label="无参数")

    # 用采集到的过期参数访问
    api(session, "GET", "/api/v3/lesson/check-in/dynamic-qr-code",
        label="过期参数",
        params={"c": "-jhm-oJeqAO6UIQG170Zzp20f4DYdtgZm9h8JKhHesY",
                "t": "1775698745320", "s": "540F4598F6BB5E80", "v": "2"})

    # ============ 7. 教师端邀请码获取 ============
    section("7. 教师端邀请码获取（需教师权限）")
    log("源码: API.teacher.get_dynamic_invitation / get_invitation")

    api(session, "GET", "/api/v3/lesson/fetch-dynamic-invitation",
        label="fetch-dynamic-invitation", params={"v": 2})

    api(session, "GET", "/api/v3/lesson/get-invitation",
        label="get-invitation")

    # ============ 8. 其他端点 ============
    section("8. 其他已发现端点")

    log("-- /api/v3/lesson-summary/checkin (课堂总结中的签到) --")
    api(session, "GET", "/api/v3/lesson-summary/checkin",
        label="GET lesson-summary/checkin")
    if lesson_id:
        api(session, "POST", "/api/v3/lesson-summary/checkin",
            label="POST lesson-summary/checkin",
            json={"lessonId": str(lesson_id)})

    log("\n-- /api/legacy/lesson/get-token (获取课堂 token) --")
    api(session, "GET", "/api/legacy/lesson/get-token",
        label="legacy get-token")

    log("\n-- /api/v3/lesson/checkin/revise (补签) --")
    api(session, "POST", "/api/v3/lesson/checkin/revise",
        label="checkin/revise",
        json={"identityId": str(uid)})

    log("\n-- /api/web/checkin/pro_bind (专业版绑定检查) --")
    api(session, "GET", "/api/web/checkin/pro_bind",
        label="pro_bind", params={"user_id": uid})

    # ============ 汇总 ============
    section("汇总")
    log("以上为基于源码确认的所有签到相关端点的详细响应。")
    if not is_active:
        log("⚠️  当前无活跃课堂，所有签到操作的结果仅反映端点本身行为，不代表签到可行性。")
        log("⚠️  必须在上课期间重新运行此脚本以获取确定性结论。")


if __name__ == "__main__":
    main()
