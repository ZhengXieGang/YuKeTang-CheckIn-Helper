#!/usr/bin/env python3
"""
雨课堂动态二维码签到深度分析工具

用途：解析动态二维码 → 执行签到 → 抓取所有 HTTP 细节 → 生成详细报告
用法：
  python3 test_checkin_bypass.py <二维码图片>
  python3 test_checkin_bypass.py --url "https://changjiang.yuketang.cn/api/v3/..."
  python3 test_checkin_bypass.py --watch ~/Screenshots/
"""

import json
import time
import os
import sys
import argparse
import glob
import hashlib
import base64
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode

import requests

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

SESSION_FILE = "yuketang_session.json"
BASE = "https://changjiang.yuketang.cn"
ts = datetime.now().strftime('%H%M%S')
REPORT_FILE = f"qr_report_{ts}.log"

# ==================== 日志 ====================

def log(msg, tag="INFO"):
    line = f"[{datetime.now().strftime('%H:%M:%S.%f')[:12]}] [{tag}] {msg}"
    print(line, flush=True)
    with open(REPORT_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def log_separator(title=""):
    log("=" * 60)
    if title:
        log(f" {title}")
        log("=" * 60)

def dump_dict(d, indent=2):
    """漂亮打印字典到日志"""
    for line in json.dumps(d, ensure_ascii=False, indent=indent).split("\n"):
        log(f"  {line}", "DATA")

# ==================== Session 加载 ====================

def load_session():
    with open(SESSION_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    s = requests.Session()
    jwt = state.get("desktop_auth", "")

    for c in state.get("desktop_cookies", []):
        s.cookies.set(c["name"], c["value"],
                      domain=c.get("domain", "changjiang.yuketang.cn"),
                      path=c.get("path", "/"))

    s.headers.update({
        "xtbz": "ykt",
        "X-Client": "desktop",
        "desktop-v": "v2",
    })
    if jwt:
        s.headers["Authorization"] = f"Bearer {jwt}"

    return s, state

# ==================== 二维码解码 ====================

def decode_qr(image_path):
    if not HAS_CV2:
        log("需要 opencv-python: pip install opencv-python", "ERROR")
        return None
    if not os.path.exists(image_path):
        log(f"文件不存在: {image_path}", "ERROR")
        return None

    img = cv2.imread(image_path)
    if img is None:
        log(f"无法读取图片: {image_path}", "ERROR")
        return None

    detector = cv2.QRCodeDetector()
    urls = []

    # 原图尝试
    data, _, _ = detector.detectAndDecode(img)
    if data:
        urls.append(data)

    # WeChat 检测器
    if not urls and hasattr(cv2, "wechat_qrcode_WeChatQRCode"):
        try:
            wd = cv2.wechat_qrcode_WeChatQRCode()
            results, _ = wd.detectAndDecode(img)
            urls.extend([r for r in results if r])
        except:
            pass

    # 多尺度
    if not urls:
        for scale in [0.5, 1.5, 2.0]:
            h, w = img.shape[:2]
            resized = cv2.resize(img, (int(w * scale), int(h * scale)))
            data, _, _ = detector.detectAndDecode(resized)
            if data:
                urls.append(data)
                break

    # 灰度增强
    if not urls:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        data, _, _ = detector.detectAndDecode(binary)
        if data:
            urls.append(data)

    if urls:
        log(f"解码成功: {urls[0][:100]}...", "QR")
        return urls[0]
    log("二维码解码失败", "ERROR")
    return None

# ==================== URL 深度解析 ====================

def deep_analyze_url(url):
    """深度解析二维码 URL 的每一个参数"""
    log_separator("URL 深度解析")

    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    log(f"完整 URL: {url}", "URL")
    log(f"协议: {parsed.scheme}", "URL")
    log(f"主机: {parsed.netloc}", "URL")
    log(f"路径: {parsed.path}", "URL")
    log(f"参数数量: {len(params)}", "URL")

    info = {}
    for key, vals in params.items():
        val = vals[0]
        info[key] = val
        log(f"  {key} = {val}", "PARAM")

        # c 参数分析
        if key == "c":
            log(f"    长度: {len(val)}", "PARAM")
            log(f"    可能为 Base64URL 编码的课堂标识", "PARAM")
            try:
                # 尝试 base64 解码
                padded = val + "=" * (4 - len(val) % 4)
                decoded = base64.urlsafe_b64decode(padded)
                log(f"    Base64 解码 ({len(decoded)} bytes): {decoded.hex()}", "PARAM")
            except:
                log(f"    Base64 解码失败", "PARAM")

        # t 参数分析
        if key == "t":
            try:
                t_ms = int(val)
                t_sec = t_ms / 1000
                dt = datetime.fromtimestamp(t_sec)
                now_ms = int(time.time() * 1000)
                age_ms = now_ms - t_ms
                age_sec = age_ms / 1000
                log(f"    时间戳(ms): {t_ms}", "PARAM")
                log(f"    对应时间: {dt.strftime('%Y-%m-%d %H:%M:%S.%f')}", "PARAM")
                log(f"    距今: {age_sec:.1f}s", "PARAM")
                if age_sec > 10:
                    log(f"    ⚠️ 已超过 6 秒有效期，签到可能失败", "PARAM")
                info["_age_sec"] = age_sec
                info["_t_ms"] = t_ms
            except:
                pass

        # s 参数分析（签名）
        if key == "s":
            log(f"    长度: {len(val)} 字符 = {len(val)*4} bits", "PARAM")
            try:
                s_int = int(val, 16)
                log(f"    十进制: {s_int}", "PARAM")
                log(f"    这是服务端生成的 HMAC 签名，无法本地伪造", "PARAM")
            except:
                log(f"    非十六进制", "PARAM")

    return info

# ==================== HTTP 请求深度抓取 ====================

def deep_request(session, method, url, label, **kwargs):
    """发起请求并记录极致详细的 HTTP 交互"""
    log_separator(f"HTTP 请求: {label}")

    log(f"→ {method} {url}", "REQ")

    # 记录请求头
    log("→ 请求头:", "REQ")
    merged_headers = dict(session.headers)
    if "headers" in kwargs:
        merged_headers.update(kwargs["headers"])
    for k, v in merged_headers.items():
        # 截断过长的 header
        v_str = str(v)
        if len(v_str) > 200:
            v_str = v_str[:200] + "..."
        log(f"    {k}: {v_str}", "REQ")

    # 记录请求 Cookie
    log("→ Cookie:", "REQ")
    for c in session.cookies:
        log(f"    {c.name}={c.value[:30]}... (domain={c.domain} path={c.path})", "REQ")

    # 记录请求体
    if "json" in kwargs:
        log(f"→ Body (JSON):", "REQ")
        dump_dict(kwargs["json"])

    try:
        # 关键：禁止自动重定向，手动跟踪每一步
        r = session.request(method, url, timeout=15, allow_redirects=False, **kwargs)
        elapsed = r.elapsed.total_seconds()

        log(f"← HTTP {r.status_code} ({elapsed:.3f}s)", "RESP")

        # 记录所有响应头
        log("← 响应头:", "RESP")
        for k, v in r.headers.items():
            log(f"    {k}: {v[:300]}", "RESP")
            # 特别关注
            if k.lower() in ["set-cookie", "set-auth", "location", "x-request-id"]:
                log(f"    ★ 重要头: {k} = {v}", "KEY")

        # 记录 Set-Cookie
        if r.cookies:
            log("← 新 Cookie:", "RESP")
            for c in r.cookies:
                log(f"    {c.name}={c.value} (domain={c.domain} path={c.path})", "RESP")

        # 响应体
        ct = r.headers.get("Content-Type", "")
        if "json" in ct:
            try:
                body = r.json()
                log("← 响应体 (JSON):", "RESP")
                dump_dict(body)
                return r, body
            except:
                log(f"← 响应体 (JSON解析失败): {r.text[:500]}", "RESP")
        elif "html" in ct:
            log(f"← 响应体 (HTML, {len(r.text)} chars):", "RESP")
            log(f"    {r.text[:500]}", "RESP")
        else:
            log(f"← 响应体 ({ct}, {len(r.content)} bytes):", "RESP")
            log(f"    {r.text[:500]}", "RESP")

        # 如果是重定向，手动跟踪
        if r.status_code in [301, 302, 303, 307, 308]:
            loc = r.headers.get("Location", "")
            log(f"↳ 重定向到: {loc}", "REDIRECT")
            if loc:
                return deep_request(session, "GET", loc, f"{label}→重定向", **{
                    k: v for k, v in kwargs.items() if k != "json"
                })

        return r, None

    except Exception as e:
        log(f"← 异常: {e}", "ERROR")
        return None, None

# ==================== 签到执行与变体测试 ====================

def run_checkin_analysis(session, url, params):
    """用各种姿势尝试签到，对比服务器行为"""

    # ====== 测试 1: 原始 GET（标准扫码流程）======
    # 用手机 UA 模拟真实扫码
    mobile_headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/116.0.0.0 Mobile Safari/537.36"
    }
    deep_request(session, "GET", url, "标准扫码(手机UA)", headers=mobile_headers)

    # ====== 测试 2: 桌面 UA ======
    deep_request(session, "GET", url, "桌面UA签到")

    # ====== 测试 3: 不带 Cookie 的裸请求 ======
    bare_session = requests.Session()
    bare_session.headers.update({"User-Agent": mobile_headers["User-Agent"]})
    deep_request(bare_session, "GET", url, "无Cookie裸请求")

    # ====== 测试 4: POST 方式 ======
    deep_request(session, "POST", url, "POST方式",
                 json={"c": params.get("c",""), "t": params.get("t",""),
                       "s": params.get("s",""), "v": params.get("v","")})

    # ====== 测试 5: 拆分参数调用 checkin 接口 ======
    if params.get("c"):
        deep_request(session, "POST", f"{BASE}/api/v3/lesson/checkin",
                     "用QR参数走checkin接口",
                     json={"source": 14, "ticket": params.get("c",""),
                           "t": params.get("t",""), "s": params.get("s","")})

    # ====== 测试 6: 修改时间戳测试容错 ======
    if params.get("t") and params.get("s"):
        t_orig = int(params["t"])
        # 用原始 t-3000ms（往前挪 3 秒）
        modified_url = url.replace(f"t={params['t']}", f"t={t_orig - 3000}")
        deep_request(session, "GET", modified_url, "时间戳-3s测试")

    # ====== 测试 7: 去掉 s 参数 ======
    nosig_url = url.split("&s=")[0] + "&v=2" if "&s=" in url else url
    deep_request(session, "GET", nosig_url, "去掉签名s")


def run_extra_probes(session, lesson_id):
    """额外的 API 探测"""
    if not lesson_id:
        return

    log_separator("额外 API 探测")

    deep_request(session, "GET",
                 f"{BASE}/api/v3/lesson/fetch-dynamic-invitation",
                 "拉取动态码(学生身份)", params={"v": 2})

    deep_request(session, "GET",
                 f"{BASE}/api/v3/lesson/get-invitation",
                 "拉取邀请码")

    deep_request(session, "GET",
                 f"{BASE}/api/v3/connection/get-token",
                 "获取连接Token")

    deep_request(session, "POST",
                 f"{BASE}/api/v3/lesson/checkin",
                 "source=1直签",
                 json={"lessonId": lesson_id, "source": 1})


# ==================== 监控模式 ====================

def watch_mode(session, folder):
    """监控文件夹中的新截图"""
    log(f"📂 监控模式: {folder}", "WATCH")
    processed = set()
    for ext in ("*.png", "*.jpg", "*.jpeg", "*.bmp"):
        for f in glob.glob(os.path.join(folder, ext)):
            processed.add(f)
    log(f"  跳过 {len(processed)} 个已存在文件", "WATCH")

    while True:
        try:
            for ext in ("*.png", "*.jpg", "*.jpeg", "*.bmp"):
                for f in sorted(glob.glob(os.path.join(folder, ext))):
                    if f not in processed:
                        processed.add(f)
                        log(f"\n📷 新图片: {os.path.basename(f)}", "WATCH")
                        url = decode_qr(f)
                        if url and "dynamic-qr-code" in url:
                            params = deep_analyze_url(url)
                            run_checkin_analysis(session, url, params)
            time.sleep(1)
        except KeyboardInterrupt:
            log("退出监控", "WATCH")
            break


# ==================== 主流程 ====================

def main():
    parser = argparse.ArgumentParser(description="雨课堂动态二维码签到深度分析工具")
    parser.add_argument("image", nargs="?", help="二维码截图路径")
    parser.add_argument("--url", help="直接传入二维码 URL")
    parser.add_argument("--watch", metavar="FOLDER", help="监控文件夹中的新截图")
    args = parser.parse_args()

    if not args.image and not args.url and not args.watch:
        parser.print_help()
        print("\n示例:")
        print("  python3 test_checkin_bypass.py qr_photo.jpg")
        print('  python3 test_checkin_bypass.py --url "https://changjiang.yuketang.cn/api/v3/..."')
        print("  python3 test_checkin_bypass.py --watch ~/Screenshots/")
        sys.exit(1)

    log_separator("雨课堂动态二维码签到深度分析工具")
    log(f"报告文件: {REPORT_FILE}")

    # 加载 Session
    session, state = load_session()

    # 验证登录
    log_separator("登录验证")
    r, body = deep_request(session, "GET",
                           f"{BASE}/api/v3/user/basic-info", "身份验证")
    if not (body and body.get("code") == 0):
        log("Cookie 无效，退出", "ERROR")
        sys.exit(1)

    uid = body["data"]["id"]
    log(f"已登录: {body['data'].get('name')} (UID: {uid})", "AUTH")

    # 获取活跃课堂
    log_separator("课堂探测")
    r, body = deep_request(session, "GET",
                           f"{BASE}/api/v3/classroom/on-lesson-upcoming-exam",
                           "活跃课堂")
    lesson_id = None
    if body and body.get("code") == 0:
        active = body.get("data", {}).get("onLessonClassrooms", [])
        if active:
            lesson_id = str(active[0]["lessonId"])
            log(f"★ 活跃课堂: {active[0].get('courseName','')} (ID={lesson_id})", "FACT")

    # 监控模式
    if args.watch:
        watch_mode(session, args.watch)
        return

    # 获取 URL
    if args.url:
        url = args.url
    else:
        log_separator("二维码解码")
        url = decode_qr(args.image)
        if not url:
            sys.exit(1)

    # 深度分析 URL
    params = deep_analyze_url(url)

    # 签到测试
    log_separator("签到流程深度分析")
    run_checkin_analysis(session, url, params)

    # 额外探测
    run_extra_probes(session, lesson_id)

    # 生成总结
    log_separator("分析完成")
    log(f"详细报告已保存到: {REPORT_FILE}")
    log(f"请将此文件发给 AI 分析，寻找突破方向。")


if __name__ == "__main__":
    main()
