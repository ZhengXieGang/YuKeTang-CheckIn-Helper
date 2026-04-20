#!/usr/bin/env python3
"""
雨课堂动态二维码签到深度分析工具

用法:
  python3 test_checkin_bypass.py qr.png
  python3 test_checkin_bypass.py -url "https://changjiang.yuketang.cn/api/v3/..."
  python3 test_checkin_bypass.py -watch ~/Screenshots
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlparse

import requests

try:
    import cv2

    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

GLOBAL_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Mobile Safari/537.36"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
DESKTOP_HEADERS = {
    "xtbz": "ykt",
    "desktop-v": "v2",
    "X-Client": "desktop",
    "Origin": "file://",
}
DEFAULT_BASE_DOMAIN = "changjiang.yuketang.cn"
KNOWN_BASE_DOMAINS = (
    "changjiang.yuketang.cn",
    "huanghe.yuketang.cn",
    "pro.yuketang.cn",
    "yuketang.cn",
)
SESSION_COOKIE_NAMES = ("sessionid", "sid", "csrftoken")
DEFAULT_ANALYSIS_SCRIPT = """from __future__ import annotations


def run_analysis(analyzer, session, url: str, params: dict, lesson_id) -> None:
    analyzer.log_separator("签到流程深度分析")
    analyzer.run_checkin_analysis(session, url, params, lesson_id=lesson_id)
    analyzer.run_extra_probes(session, lesson_id=lesson_id, params=params)
"""


class AnalysisError(RuntimeError):
    pass


class DesktopLoginNeedCode(AnalysisError):
    def __init__(self, message: str, need_code_token: str) -> None:
        super().__init__(message)
        self.need_code_token = need_code_token


class RainClassroomAnalyzer:
    def __init__(
        self,
        session_file: os.PathLike[str] | str = "yuketang_session.json",
        base_domain: str | None = None,
        report_dir: os.PathLike[str] | str = "reports",
        log_callback: Callable[[str], None] | None = None,
        timestamp: str | None = None,
    ) -> None:
        self.session_file = Path(session_file)
        self.log_callback = log_callback
        self.timestamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.report_file = self.report_dir / f"qr_report_{self.timestamp}.log"
        self.active_context: dict[str, Any] = {}

        state = self.read_state(silent=True)
        inferred_domain = base_domain or state.get("base_domain")
        if not inferred_domain:
            cookie_records = self._normalize_cookie_records(state)
            if cookie_records:
                inferred_domain = cookie_records[0].get("domain")
        self.base_domain = (inferred_domain or DEFAULT_BASE_DOMAIN).strip()
        self.base_url = self._build_base_url(self.base_domain)

    def _build_base_url(self, domain_or_url: str) -> str:
        text = (domain_or_url or DEFAULT_BASE_DOMAIN).strip()
        if text.startswith("http://") or text.startswith("https://"):
            return text.rstrip("/")
        return f"https://{text.rstrip('/')}"

    def sync_base_domain_from_url(self, url: str) -> None:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return
        host = parsed.netloc.strip()
        if host.endswith(".local"):
            return
        self.base_domain = host
        self.base_url = f"{parsed.scheme}://{host}"

    def log(self, message: str, tag: str = "INFO") -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S.%f')[:12]}] [{tag}] {message}"
        print(line, flush=True)
        if self.log_callback:
            self.log_callback(line)
        with self.report_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def log_separator(self, title: str = "") -> None:
        self.log("=" * 60)
        if title:
            self.log(f" {title}")
            self.log("=" * 60)

    def dump_dict(self, payload: Any, indent: int = 2) -> None:
        for line in json.dumps(payload, ensure_ascii=False, indent=indent).splitlines():
            self.log(f"  {line}", "DATA")

    def read_state(self, silent: bool = False) -> dict[str, Any]:
        if not self.session_file.exists():
            if silent:
                return {}
            raise AnalysisError(f"会话文件不存在: {self.session_file}")
        try:
            state = json.loads(self.session_file.read_text(encoding="utf-8"))
        except Exception as exc:
            if silent:
                return {}
            raise AnalysisError(f"读取会话文件失败: {exc}") from exc
        if not isinstance(state, dict):
            if silent:
                return {}
            raise AnalysisError("会话 JSON 顶层必须是对象")
        return state

    def write_state(self, state: dict[str, Any]) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _coerce_timestamp(self, value: Any) -> int | None:
        if value in (None, "", 0, "0"):
            return None
        try:
            number = float(value)
        except Exception:
            return None
        if number > 10_000_000_000:
            number /= 1000
        if number <= 0:
            return None
        return int(number)

    def _format_timestamp(self, value: int | None) -> str:
        if not value:
            return "未知"
        try:
            return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return "未知"

    def get_session_status(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        state = dict(state or self.read_state(silent=True))
        cookie_records = self._normalize_cookie_records(state)
        token, _ = self._normalize_auth(state.get("desktop_auth"))
        expiry_candidates = [
            self._coerce_timestamp(cookie.get("expires"))
            for cookie in cookie_records
            if str(cookie.get("name") or "").lower() in {"sid", "sessionid"}
        ]
        expiry_candidates = [item for item in expiry_candidates if item]
        expires_at = max(expiry_candidates) if expiry_candidates else None
        now_ts = int(time.time())
        expired = bool(expires_at and expires_at <= now_ts)
        base_domain = (
            state.get("base_domain")
            or (cookie_records[0].get("domain") if cookie_records else "")
            or self.base_domain
        )
        return {
            "base_domain": str(base_domain or "").strip() or DEFAULT_BASE_DOMAIN,
            "cookie_count": len(cookie_records),
            "has_cookie": bool(cookie_records),
            "has_auth": bool(token),
            "expires_at": expires_at,
            "expires_text": self._format_timestamp(expires_at),
            "expired": expired,
            "updated_at": state.get("desktop_cookies_updated_at")
            or state.get("desktop_auth_updated_at")
            or "",
        }

    def format_session_status(self, state: dict[str, Any] | None = None) -> str:
        status = self.get_session_status(state=state)
        if not status["has_cookie"] and not status["has_auth"]:
            return "未检测到可用会话"
        parts = [f"域名 {status['base_domain']}"]
        if status["has_auth"]:
            parts.append("含 Authorization")
        if status["expires_at"]:
            expiry_prefix = "已过期" if status["expired"] else "本地 Cookie 预计到期"
            parts.append(f"{expiry_prefix} {status['expires_text']}")
        else:
            parts.append("无显式过期时间")
        return "，".join(parts)

    def _normalize_cookie_domain(self, value: Any) -> str:
        return str(value or "").strip().lstrip(".")

    def _cookie_matches_base_domain(self, domain: Any) -> bool:
        cookie_domain = self._normalize_cookie_domain(domain)
        base_domain = self._normalize_cookie_domain(self.base_domain)
        if not cookie_domain or not base_domain:
            return True
        return (
            cookie_domain == base_domain
            or base_domain.endswith(f".{cookie_domain}")
            or cookie_domain.endswith(f".{base_domain}")
        )

    def _cookie_sort_key(self, cookie: dict[str, Any]) -> tuple[int, int, int, int]:
        domain = self._normalize_cookie_domain(cookie.get("domain"))
        path = str(cookie.get("path") or "/")
        return (
            1 if domain == self._normalize_cookie_domain(self.base_domain) else 0,
            1 if path == "/" else 0,
            1 if bool(cookie.get("secure")) else 0,
            1 if cookie.get("expires") else 0,
        )

    def _prune_cookie_records(self, cookie_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept: dict[str, dict[str, Any]] = {}
        for item in cookie_records:
            name = str(item.get("name") or "").strip().lower()
            value = item.get("value")
            if name not in SESSION_COOKIE_NAMES or value is None:
                continue
            if not self._cookie_matches_base_domain(item.get("domain")):
                continue
            normalized = {
                "name": name,
                "value": str(value),
                "domain": self._normalize_cookie_domain(item.get("domain")) or self.base_domain,
                "path": str(item.get("path") or "/"),
                "expires": self._coerce_timestamp(item.get("expires")),
                "secure": bool(item.get("secure", False)),
            }
            existing = kept.get(name)
            if existing is None or self._cookie_sort_key(normalized) >= self._cookie_sort_key(existing):
                kept[name] = normalized

        order = {name: index for index, name in enumerate(SESSION_COOKIE_NAMES)}
        return sorted(kept.values(), key=lambda item: order.get(item["name"], 99))

    def _normalize_cookie_records(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        cookie_records: list[dict[str, Any]] = []
        raw = state.get("desktop_cookies") or state.get("cookies") or []
        if isinstance(raw, dict):
            raw = [
                {"name": key, "value": value}
                for key, value in raw.items()
                if isinstance(value, str)
            ]
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = item.get("value")
            if not name or value is None:
                continue
            expires = self._coerce_timestamp(
                item.get("expires", item.get("expirationDate", item.get("expiry")))
            )
            cookie_records.append(
                {
                    "name": name,
                    "value": str(value),
                    "domain": self._normalize_cookie_domain(
                        item.get("domain") or state.get("base_domain") or self.base_domain
                    ),
                    "path": str(item.get("path") or "/"),
                    "expires": expires,
                    "secure": bool(item.get("secure", False)),
                }
            )
        legacy = {
            "sessionid": state.get("sessionid"),
            "csrftoken": state.get("csrftoken"),
            "sid": state.get("sid"),
        }
        existing = {item["name"] for item in cookie_records}
        for key, value in legacy.items():
            if key in existing or not value:
                continue
            cookie_records.append(
                {
                    "name": key,
                    "value": str(value),
                    "domain": self._normalize_cookie_domain(state.get("base_domain") or self.base_domain),
                    "path": "/",
                    "expires": None,
                    "secure": True,
                }
            )
        return self._prune_cookie_records(cookie_records)

    def _create_session(
        self,
        user_agent: str,
        extra_headers: dict[str, str] | None = None,
    ) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent})
        if extra_headers:
            session.headers.update(extra_headers)
        return session

    def create_desktop_session(self) -> requests.Session:
        session = self._create_session(DESKTOP_UA, DESKTOP_HEADERS)
        session.headers.setdefault("Accept", "application/json, text/plain, */*")
        session.headers.setdefault("Referer", f"{self.base_url}/")
        return session

    def _apply_cookie_records(
        self,
        session: requests.Session,
        cookie_records: list[dict[str, Any]],
    ) -> None:
        for cookie in cookie_records:
            name = str(cookie.get("name") or "").strip()
            value = cookie.get("value")
            if not name or value is None:
                continue
            kwargs = {
                "domain": str(cookie.get("domain") or self.base_domain),
                "path": str(cookie.get("path") or "/"),
            }
            expires = cookie.get("expires")
            if expires:
                kwargs["expires"] = expires
            session.cookies.set(name, str(value), **kwargs)
        self._sync_csrftoken_header(session)

    def _sync_csrftoken_header(self, session: requests.Session) -> None:
        csrftoken = session.cookies.get("csrftoken")
        if csrftoken:
            session.headers["X-CSRFToken"] = csrftoken
        else:
            session.headers.pop("X-CSRFToken", None)

    def _cookie_records_from_session(self, session: requests.Session) -> list[dict[str, Any]]:
        cookie_records: list[dict[str, Any]] = []
        for cookie in session.cookies:
            cookie_records.append(
                {
                    "name": str(cookie.name).lower(),
                    "value": cookie.value,
                    "domain": self._normalize_cookie_domain(cookie.domain or self.base_domain),
                    "path": cookie.path or "/",
                    "expires": int(cookie.expires) if cookie.expires else None,
                    "secure": bool(cookie.secure),
                }
            )
        return self._prune_cookie_records(cookie_records)

    def _normalize_auth(self, auth_value: str | None) -> tuple[str, str]:
        raw = (auth_value or "").strip()
        if not raw:
            return "", ""
        if raw.lower().startswith("bearer "):
            token = raw[7:].strip()
            return token, f"Bearer {token}" if token else ""
        return raw, f"Bearer {raw}"

    def _set_session_auth(self, session: requests.Session, auth_value: str | None) -> bool:
        token, header = self._normalize_auth(auth_value)
        if header:
            session.headers["Authorization"] = header
        else:
            session.headers.pop("Authorization", None)
        return bool(token)

    def build_session_state(
        self,
        session: requests.Session,
        existing_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = dict(existing_state or {})
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cookie_records = self._cookie_records_from_session(session)
        if cookie_records:
            state["desktop_cookies"] = cookie_records
            state["desktop_cookies_updated_at"] = now_text
            state["base_domain"] = cookie_records[0].get("domain") or self.base_domain
        else:
            state.pop("desktop_cookies", None)
            state.pop("desktop_cookies_updated_at", None)
            state["base_domain"] = self.base_domain
        for legacy_key in ("sessionid", "sid", "csrftoken", "cookies"):
            state.pop(legacy_key, None)

        token, header = self._normalize_auth(session.headers.get("Authorization"))
        if token:
            state["desktop_auth"] = token
            state["desktop_auth_updated_at"] = now_text
        elif "desktop_auth" in state:
            state.pop("desktop_auth", None)
            state.pop("desktop_auth_updated_at", None)
        if header:
            self._set_session_auth(session, header)
        return state

    def save_session_state(
        self,
        session: requests.Session,
        existing_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.build_session_state(session, existing_state=existing_state)
        self.write_state(state)
        return state

    def _cookie_signature(self, session: requests.Session) -> tuple[Any, ...]:
        return tuple(
            sorted(
                (
                    cookie.name,
                    cookie.value,
                    cookie.domain or self.base_domain,
                    cookie.path or "/",
                    int(cookie.expires) if cookie.expires else None,
                    bool(cookie.secure),
                )
                for cookie in session.cookies
            )
        )

    def _update_auth_from_response(
        self,
        session: requests.Session,
        response: requests.Response,
    ) -> bool:
        old_header = session.headers.get("Authorization") or ""
        candidates = [response, *(getattr(response, "history", []) or [])]
        for item in candidates:
            headers = getattr(item, "headers", {}) or {}
            for key in ("set-auth", "Set-Auth", "authorization", "Authorization"):
                value = headers.get(key)
                if not isinstance(value, str) or not value.strip():
                    continue
                self._set_session_auth(session, value)
                return (session.headers.get("Authorization") or "") != old_header
        return False

    @staticmethod
    def _is_authenticated_basic_info(body: dict[str, Any] | None) -> bool:
        if not isinstance(body, dict) or body.get("code") != 0:
            return False
        payload = body.get("data")
        if not isinstance(payload, dict) or not payload:
            return False
        for key in ("id", "user_id", "userId", "name", "username"):
            value = payload.get(key)
            if isinstance(value, str):
                if value.strip():
                    return True
            elif value:
                return True
        return bool(payload)

    @staticmethod
    def _extract_verify_code_token(body: dict[str, Any] | None) -> str:
        if not isinstance(body, dict):
            return ""
        direct_token = str(body.get("token") or "").strip()
        if direct_token:
            return direct_token
        payload = body.get("data")
        if not isinstance(payload, dict):
            return ""
        for key in ("token", "verify_token", "verifyToken", "need_code_token", "needCodeToken"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _mask_secret(value: Any) -> str:
        text = str(value or "")
        if len(text) <= 12:
            return "*" * len(text)
        return f"{text[:6]}...{text[-4:]}"

    def _fetch_desktop_json(
        self,
        session: requests.Session,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> tuple[requests.Response, dict[str, Any]]:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        old_cookies = self._cookie_signature(session)
        response = session.request(method, url, **kwargs)
        auth_changed = self._update_auth_from_response(session, response)
        self._sync_csrftoken_header(session)
        if auth_changed or old_cookies != self._cookie_signature(session):
            self.save_session_state(session, existing_state=self.read_state(silent=True))

        content_type = response.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            snippet = response.text[:240].replace("\n", " ")
            raise AnalysisError(f"{path} 返回非 JSON: {snippet}")
        try:
            body = response.json()
        except Exception as exc:
            raise AnalysisError(f"{path} JSON 解析失败: {exc}") from exc
        if not isinstance(body, dict):
            raise AnalysisError(f"{path} 返回了非对象 JSON")
        return response, body

    def create_desktop_login_context(self) -> dict[str, Any]:
        session = self.create_desktop_session()
        _, body = self._fetch_desktop_json(
            session,
            "get",
            "/api/v3/user/login/pre-info",
            timeout=20,
        )
        if body.get("code") != 0:
            raise AnalysisError(body.get("msg") or "获取登录二维码失败")
        info = body.get("data") or {}
        qr_content = info.get("qrContent")
        login_token = info.get("token")
        if not qr_content or not login_token:
            raise AnalysisError("登录二维码响应缺少 qrContent 或 token")
        return {
            "login_token": login_token,
            "qr_content": qr_content,
            "desktop_cookies": self._cookie_records_from_session(session),
            "base_domain": self.base_domain,
        }

    def _poll_login_endpoint(
        self,
        session: requests.Session,
        path: str,
        login_token: str,
        timeout: tuple[float, float] = (10, 35),
    ) -> dict[str, Any]:
        _, body = self._fetch_desktop_json(
            session,
            "post",
            path,
            json={"token": login_token},
            timeout=timeout,
        )
        return body

    def wait_for_desktop_login(
        self,
        login_token: str,
        desktop_cookies: list[dict[str, Any]] | None = None,
        max_wait_seconds: float = 300,
        poll_interval_seconds: float = 2,
        stop_event: threading.Event | None = None,
        request_timeout_seconds: float = 8,
    ) -> dict[str, Any]:
        session = self.create_desktop_session()
        self._apply_cookie_records(session, desktop_cookies or [])
        start = time.time()
        last_status = None
        wait_for_scan_enabled = True
        request_timeout = (5, max(1, request_timeout_seconds))
        while True:
            if stop_event and stop_event.is_set():
                raise AnalysisError("登录已取消")
            if max_wait_seconds and time.time() - start > max_wait_seconds:
                raise AnalysisError("等待微信确认登录超时")
            if wait_for_scan_enabled:
                try:
                    wait_body = self._poll_login_endpoint(
                        session,
                        "/api/v3/user/login/wait-for-scan",
                        login_token,
                        timeout=request_timeout,
                    )
                    if stop_event and stop_event.is_set():
                        raise AnalysisError("登录已取消")
                    verify_token = self._extract_verify_code_token(wait_body)
                    if verify_token:
                        raise DesktopLoginNeedCode(
                            (wait_body.get("msg") or wait_body.get("message") or "扫码成功，需要输入验证码"),
                            need_code_token=verify_token,
                        )
                    wait_message = wait_body.get("msg") or wait_body.get("message")
                    wait_code = wait_body.get("code")
                    if wait_code in (500, 50000, 50001):
                        raise AnalysisError(f"二维码已失效: {wait_message or wait_code}")
                    if wait_message and wait_message != last_status:
                        self.log(f"扫码状态: {wait_message}", "AUTH")
                        last_status = wait_message
                except requests.ReadTimeout:
                    pass
                except requests.RequestException as exc:
                    self.log(f"wait-for-scan 网络异常，稍后重试: {exc}", "WARN")
                except AnalysisError:
                    raise
                except Exception as exc:
                    wait_for_scan_enabled = False
                    self.log(f"wait-for-scan 不可用，改用 login 轮询: {exc}", "WARN")
            try:
                body = self._poll_login_endpoint(
                    session,
                    "/api/v3/user/login",
                    login_token,
                    timeout=request_timeout,
                )
            except requests.ReadTimeout:
                continue
            except requests.RequestException as exc:
                message = f"登录轮询网络异常，稍后重试: {exc}"
                if message != last_status:
                    self.log(message, "WARN")
                    last_status = message
                time.sleep(min(1.5, poll_interval_seconds or 1.0))
                continue
            if stop_event and stop_event.is_set():
                raise AnalysisError("登录已取消")
            verify_token = self._extract_verify_code_token(body)
            if verify_token:
                raise DesktopLoginNeedCode(
                    (body.get("msg") or body.get("message") or "扫码成功，需要输入验证码"),
                    need_code_token=verify_token,
                )
            code = body.get("code")
            message = body.get("msg") or body.get("message") or f"code={code}"
            if code == 0:
                user_info = self.validate_session(session)
                self.save_session_state(session, existing_state=self.read_state(silent=True))
                return {
                    "user_info": user_info,
                    "session_state": self.read_state(silent=True),
                }
            if code in (500, 50000, 50001):
                raise AnalysisError(f"二维码已失效: {message}")
            if message != last_status:
                self.log(f"登录状态: {message}", "AUTH")
                last_status = message
            slept = 0.0
            while slept < poll_interval_seconds:
                if stop_event and stop_event.is_set():
                    raise AnalysisError("登录已取消")
                chunk = min(0.2, poll_interval_seconds - slept)
                time.sleep(chunk)
                slept += chunk

    def submit_desktop_login_code(
        self,
        need_code_token: str,
        verify_code: str,
        desktop_cookies: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        token = str(need_code_token or "").strip()
        code = str(verify_code or "").strip()
        if not token:
            raise AnalysisError("验证码登录缺少 token")
        if len(code) != 4 or not code.isdigit():
            raise AnalysisError("验证码必须是 4 位数字")

        session = self.create_desktop_session()
        self._apply_cookie_records(session, desktop_cookies or [])
        attempts = 4
        body: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                _, body = self._fetch_desktop_json(
                    session,
                    "post",
                    "/api/v3/user/login/login-with-code",
                    json={
                        "token": token,
                        "code": code,
                    },
                    timeout=(8, 20),
                )
                break
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= attempts:
                    raise AnalysisError(f"验证码登录网络异常: {exc}") from exc
                self.log(
                    f"验证码登录网络异常，重试 {attempt}/{attempts - 1}: {exc}",
                    "WARN",
                )
                time.sleep(min(2.0, 0.5 * attempt))
        if body is None:
            raise AnalysisError(f"验证码登录失败: {last_error or '未知网络错误'}")
        if body.get("code") != 0:
            message = body.get("msg") or body.get("message") or f"code={body.get('code')}"
            raise AnalysisError(f"验证码登录失败: {message}")

        self.save_session_state(session, existing_state=self.read_state(silent=True))
        return {
            "code_submitted": True,
            "session_state": self.read_state(silent=True),
        }

    def load_session(self) -> requests.Session:
        state = self.read_state()
        cookie_records = self._normalize_cookie_records(state)
        if cookie_records:
            self.base_domain = cookie_records[0].get("domain") or self.base_domain
            self.base_url = self._build_base_url(self.base_domain)
        elif state.get("base_domain"):
            self.base_domain = str(state["base_domain"]).strip()
            self.base_url = self._build_base_url(self.base_domain)

        session = self.create_desktop_session()
        self._apply_cookie_records(session, cookie_records)
        token, header = self._normalize_auth(state.get("desktop_auth"))
        if header:
            session.headers["Authorization"] = header

        if not cookie_records and not token:
            raise AnalysisError(
                f"会话文件里没有可用的 desktop_cookies / desktop_auth: {self.session_file}"
            )
        return session

    def validate_session(self, session: requests.Session) -> dict[str, Any]:
        _, body = self._fetch_desktop_json(
            session,
            "get",
            "/api/v3/user/basic-info",
            timeout=15,
        )
        if not self._is_authenticated_basic_info(body):
            raise AnalysisError(body.get("msg") or "会话验证失败")
        user = body.get("data") or {}
        token, _ = self._normalize_auth(session.headers.get("Authorization"))
        auth_suffix = "，含 Authorization" if token else "，未带 Authorization"
        self.log(
            f"已登录: {user.get('name') or '未知用户'} (UID: {user.get('id')}){auth_suffix}",
            "AUTH",
        )
        return user

    def validate_lesson_access(
        self,
        session: requests.Session,
        lesson_id: str | int | None,
    ) -> dict[str, Any]:
        if lesson_id in (None, "", [], {}):
            raise AnalysisError("缺少 lessonId，无法校验 lesson 级权限")

        url = f"{self.base_url}/api/v3/lesson/basic-info"
        old_cookies = self._cookie_signature(session)
        response = session.get(
            url,
            params={"lessonId": str(lesson_id)},
            timeout=15,
        )
        auth_changed = self._update_auth_from_response(session, response)
        self._sync_csrftoken_header(session)
        if auth_changed or old_cookies != self._cookie_signature(session):
            self.save_session_state(session, existing_state=self.read_state(silent=True))

        if response.status_code == 401:
            token, _ = self._normalize_auth(session.headers.get("Authorization"))
            hint = "缺少桌面端 Authorization 或登录流程未最终完成" if not token else "当前登录态没有该课堂权限或会话已失效"
            raise AnalysisError(f"lesson/basic-info 返回 401，{hint}")

        content_type = response.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            snippet = response.text[:240].replace("\n", " ")
            raise AnalysisError(f"lesson/basic-info 返回非 JSON: {snippet}")
        try:
            body = response.json()
        except Exception as exc:
            raise AnalysisError(f"lesson/basic-info JSON 解析失败: {exc}") from exc
        if not isinstance(body, dict):
            raise AnalysisError("lesson/basic-info 返回了非对象 JSON")
        if body.get("code") != 0:
            message = body.get("msg") or body.get("message") or f"code={body.get('code')}"
            raise AnalysisError(f"lesson/basic-info 校验失败: {message}")
        self.log(f"lesson 级接口校验通过: lessonId={lesson_id}", "AUTH")
        return body.get("data") or {}

    def keep_alive_session(self) -> dict[str, Any]:
        session = self.load_session()
        user_info = self.validate_session(session)
        state = self.read_state(silent=True)
        self.save_session_state(session, existing_state=state)
        return {
            "user_info": user_info,
            "session_state": self.read_state(silent=True),
        }

    def fetch_active_context(self, session: requests.Session) -> dict[str, Any]:
        def extract_items(body: dict[str, Any]) -> list[dict[str, Any]]:
            data = body.get("data") or {}
            candidates: list[Any] = []
            if isinstance(data, dict):
                for key in ("onLessonClassrooms", "classrooms", "list", "lessonClassrooms"):
                    value = data.get(key)
                    if isinstance(value, list):
                        candidates.extend(value)
                for key in ("classroom", "currentClassroom", "currentLesson"):
                    value = data.get(key)
                    if isinstance(value, dict):
                        candidates.append(value)
            elif isinstance(data, list):
                candidates.extend(data)
            return [item for item in candidates if isinstance(item, dict)]

        for path in (
            "/api/v3/classroom/on-lesson-upcoming-exam",
            "/api/v3/classroom/on-lesson",
        ):
            _, body = self._fetch_desktop_json(
                session,
                "get",
                path,
                timeout=15,
            )
            if body.get("code") != 0:
                continue
            active = extract_items(body)
            if not active:
                continue
            classroom = active[0] or {}
            context = {
                "lesson_id": self._first_value_by_keys(
                    classroom,
                    ("lessonId", "lesson_id", "currentLessonId"),
                ),
                "classroom_id": self._first_value_by_keys(
                    classroom,
                    ("classroomId", "classroom_id", "id"),
                ),
                "course_name": self._first_value_by_keys(
                    classroom,
                    ("courseName", "classroomName", "name"),
                ),
                "raw": classroom,
            }
            self.log(
                f"活跃课堂: {context.get('course_name') or '未知课程'} "
                f"(lessonId={context.get('lesson_id')}, classroomId={context.get('classroom_id')})",
                "FACT",
            )
            return context
        self.log("当前没有正在进行的课堂", "FACT")
        return {}

    def fetch_active_lesson_id(self, session: requests.Session) -> str | None:
        context = self.fetch_active_context(session)
        return str(context["lesson_id"]) if context.get("lesson_id") is not None else None

    def deep_analyze_url(self, url: str) -> dict[str, Any]:
        self.sync_base_domain_from_url(url)
        self.log_separator("URL 深度解析")
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        info: dict[str, Any] = {}

        self.log(f"完整 URL: {url}", "URL")
        self.log(f"协议: {parsed.scheme}", "URL")
        self.log(f"主机: {parsed.netloc}", "URL")
        self.log(f"路径: {parsed.path}", "URL")
        self.log(f"参数数量: {len(params)}", "URL")

        for key, values in params.items():
            value = values[0]
            info[key] = value
            self.log(f"  {key} = {value}", "PARAM")
            if key == "c":
                self.log(f"    长度: {len(value)}", "PARAM")
                try:
                    padded = value + ("=" * ((-len(value)) % 4))
                    decoded = base64.urlsafe_b64decode(padded)
                    preview = decoded.hex()
                    if len(preview) > 120:
                        preview = preview[:120] + "..."
                    self.log(f"    Base64URL 解码: {preview}", "PARAM")
                except Exception:
                    self.log("    Base64URL 解码失败", "PARAM")
            if key == "t":
                try:
                    t_ms = int(value)
                    current_ms = int(time.time() * 1000)
                    age_sec = (current_ms - t_ms) / 1000
                    self.log(
                        f"    时间: {datetime.fromtimestamp(t_ms / 1000).strftime('%Y-%m-%d %H:%M:%S.%f')}",
                        "PARAM",
                    )
                    self.log(f"    距今: {age_sec:.1f}s", "PARAM")
                    if age_sec > 10:
                        self.log("    可能已超过动态码时效窗口", "WARN")
                    info["_age_sec"] = age_sec
                except Exception:
                    pass
            if key == "s":
                self.log(f"    签名长度: {len(value)}", "PARAM")
                try:
                    int(value, 16)
                    self.log("    形态看起来像十六进制签名", "PARAM")
                except Exception:
                    self.log("    非十六进制签名", "PARAM")
            if key == "v":
                self.log(f"    版本号: {value}", "PARAM")

        info["_host"] = parsed.netloc
        info["_path"] = parsed.path
        info["_full_url"] = url
        return info

    def deep_request(
        self,
        session: requests.Session,
        method: str,
        url: str,
        label: str,
        _depth: int = 0,
        **kwargs: Any,
    ) -> tuple[requests.Response | None, dict[str, Any] | None]:
        target_url = url if url.startswith("http") else urljoin(f"{self.base_url}/", url.lstrip("/"))
        self.log_separator(f"HTTP 请求: {label}")
        self.log(f"→ {method.upper()} {target_url}", "REQ")

        merged_headers = dict(session.headers)
        if "headers" in kwargs and kwargs["headers"]:
            merged_headers.update(kwargs["headers"])
        self.log("→ 请求头:", "REQ")
        for key, value in merged_headers.items():
            value_text = str(value)
            if key.lower() == "authorization":
                value_text = self._mask_secret(value_text)
            if len(value_text) > 220:
                value_text = value_text[:220] + "..."
            self.log(f"    {key}: {value_text}", "REQ")

        if session.cookies:
            self.log("→ Cookie:", "REQ")
            for cookie in session.cookies:
                preview = cookie.value if len(cookie.value) <= 40 else cookie.value[:40] + "..."
                self.log(
                    f"    {cookie.name}={preview} (domain={cookie.domain} path={cookie.path})",
                    "REQ",
                )

        if "params" in kwargs and kwargs["params"] is not None:
            self.log("→ Query:", "REQ")
            self.dump_dict(kwargs["params"])
        if "json" in kwargs and kwargs["json"] is not None:
            self.log("→ Body (JSON):", "REQ")
            self.dump_dict(kwargs["json"])
        if "data" in kwargs and kwargs["data"] is not None:
            self.log(f"→ Body (RAW): {kwargs['data']!r}", "REQ")

        old_cookies = self._cookie_signature(session)
        try:
            response = session.request(
                method,
                target_url,
                timeout=kwargs.pop("timeout", 15),
                allow_redirects=False,
                **kwargs,
            )
        except Exception as exc:
            self.log(f"← 异常: {exc}", "ERROR")
            return None, None

        auth_changed = self._update_auth_from_response(session, response)
        self._sync_csrftoken_header(session)
        if auth_changed or old_cookies != self._cookie_signature(session):
            self.save_session_state(session, existing_state=self.read_state(silent=True))

        self.log(
            f"← HTTP {response.status_code} ({response.elapsed.total_seconds():.3f}s)",
            "RESP",
        )
        self.log("← 响应头:", "RESP")
        for key, value in response.headers.items():
            preview = value if len(value) <= 320 else value[:320] + "..."
            if key.lower() in {"authorization", "set-auth"}:
                preview = self._mask_secret(preview)
            self.log(f"    {key}: {preview}", "RESP")
            if key.lower() in {"set-cookie", "set-auth", "location", "x-request-id"}:
                important_value = self._mask_secret(value) if key.lower() == "set-auth" else value
                self.log(f"    ★ 重要头: {key} = {important_value}", "KEY")

        content_type = response.headers.get("Content-Type", "")
        body: dict[str, Any] | None = None
        if "application/json" in content_type:
            try:
                body = response.json()
                self.log("← 响应体 (JSON):", "RESP")
                self.dump_dict(body)
            except Exception as exc:
                self.log(f"← JSON 解析失败: {exc}; 内容: {response.text[:500]}", "RESP")
        else:
            text_preview = response.text[:500] if response.text else ""
            if "text/html" in content_type:
                self.log(f"← 响应体 (HTML, {len(response.text)} chars):", "RESP")
            else:
                self.log(
                    f"← 响应体 ({content_type or 'unknown'}, {len(response.content)} bytes):",
                    "RESP",
                )
            if text_preview:
                self.log(f"    {text_preview}", "RESP")

        if (
            response.status_code in {301, 302, 303, 307, 308}
            and _depth < 5
            and response.headers.get("Location")
        ):
            redirect_url = urljoin(str(response.url), response.headers["Location"])
            self.log(f"↳ 重定向到: {redirect_url}", "REDIRECT")
            next_kwargs = {key: value for key, value in kwargs.items() if key not in {"json", "data"}}
            return self.deep_request(
                session,
                "GET",
                redirect_url,
                f"{label}→重定向",
                _depth=_depth + 1,
                **next_kwargs,
            )
        return response, body

    def _first_value_by_keys(self, payload: Any, keys: tuple[str, ...]) -> Any:
        if isinstance(payload, dict):
            for key in keys:
                value = payload.get(key)
                if value not in (None, "", [], {}):
                    return value
            for value in payload.values():
                found = self._first_value_by_keys(value, keys)
                if found not in (None, "", [], {}):
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = self._first_value_by_keys(item, keys)
                if found not in (None, "", [], {}):
                    return found
        return None

    def run_checkin_analysis(
        self,
        session: requests.Session,
        url: str,
        params: dict[str, Any],
        lesson_id: str | int | None = None,
    ) -> None:
        mobile_headers = {"User-Agent": GLOBAL_UA}
        self.deep_request(session, "GET", url, "标准扫码(手机 UA)", headers=mobile_headers)
        self.deep_request(session, "GET", url, "标准扫码(桌面会话)")

        bare_session = self._create_session(GLOBAL_UA)
        self.deep_request(bare_session, "GET", url, "无登录态裸请求")

        self.deep_request(
            session,
            "POST",
            url,
            "实验性: 直接 POST 动态码 URL",
            json={key: params.get(key, "") for key in ("c", "t", "s", "v")},
        )

        if params.get("c"):
            # 组合 1：原版（不带 lessonId，source=14）
            self.deep_request(
                session,
                "POST",
                "/api/v3/lesson/checkin",
                "实验性: 原版 POST checkin",
                json={
                    "source": 14,
                    "ticket": params.get("c", ""),
                    "t": params.get("t", ""),
                    "s": params.get("s", ""),
                },
            )
            
            # 组合 2：补全 lessonId，各种 Source (5=小程序, 14=PC)
            if lesson_id:
                for src in [5, 14]:
                    self.deep_request(
                        session,
                        "POST",
                        "/api/v3/lesson/checkin",
                        f"探索: 带 lessonId, Source={src}",
                        json={
                            "source": src,
                            "lessonId": str(lesson_id),
                            "ticket": params.get("c", ""),
                            "t": params.get("t", ""),
                            "s": params.get("s", ""),
                        },
                    )
            
            # 组合 3：猜测性短链端点，当前未在已解包客户端中检索到直证
            if lesson_id:
                self.deep_request(
                    session,
                    "POST",
                    "/api/v3/lesson/notkn/checkin",
                    "猜测性: notkn/checkin",
                    json={
                        "invite_code": params.get("c", ""),
                        "ticket": params.get("c", ""),
                        "t": params.get("t", ""),
                        "s": params.get("s", ""),
                        "source": 5,
                        "lessonId": str(lesson_id),
                    },
                )

        if params.get("t") and params.get("s"):
            try:
                original_t = int(params["t"])
                modified_url = url.replace(f"t={params['t']}", f"t={original_t - 3000}", 1)
                self.deep_request(session, "GET", modified_url, "时间戳 -3s 容错测试")
            except Exception:
                pass

        if "&s=" in url:
            no_sig_url = url.split("&s=")[0]
            if "v=" not in no_sig_url:
                no_sig_url += "&v=2"
            self.deep_request(session, "GET", no_sig_url, "去掉签名 s")

    def run_extra_probes(
        self,
        session: requests.Session,
        lesson_id: str | int | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        params = params or {}
        classroom_id = (self.active_context or {}).get("classroom_id")
        if not lesson_id:
            lesson_id = (self.active_context or {}).get("lesson_id")

        self.log_separator("桌面端接口探测")
        self.deep_request(session, "GET", "/api/v3/connection/get-token", "连接 Token")
        self.deep_request(session, "GET", "/api/v3/classroom/on-lesson", "进行中课堂(on-lesson)")
        self.deep_request(session, "GET", "/api/v3/classroom/drop-down", "课堂下拉列表(drop-down)")

        if classroom_id:
            self.deep_request(
                session,
                "GET",
                "/api/v3/classroom/basic-info",
                "课堂基础信息(classroom/basic-info)",
                params={"classroomId": classroom_id},
            )
            self.deep_request(
                session,
                "GET",
                "/api/v3/classroom/branch-type",
                "课堂分支类型(branch-type)",
                params={"classroomId": classroom_id},
            )
            self.deep_request(
                session,
                "GET",
                "/api/v3/classroom/lesson-config",
                "课堂课程配置(lesson-config)",
                params={"classroomId": classroom_id, "lessonId": lesson_id},
            )

        if not lesson_id:
            self.log("没有 lesson_id，跳过 lesson 相关探测", "WARN")
            return

        lesson_id = str(lesson_id)
        self.deep_request(
            session,
            "GET",
            "/api/v3/lesson/basic-info",
            "课堂基础信息(lesson/basic-info)",
            params={"lessonId": lesson_id},
        )

        _response, body = self.deep_request(
            session,
            "GET",
            "/api/v3/lesson/checkin-list",
            "签到列表(checkin-list)",
            params={"lessonId": lesson_id},
        )
        self.deep_request(
            session,
            "GET",
            "/api/v3/lesson/uncheckin-list",
            "未签到列表(uncheckin-list)",
            params={"lessonId": lesson_id},
        )
        self.deep_request(
            session,
            "GET",
            "/api/v3/lesson/get-invitation",
            "邀请码(get-invitation)",
            params={"lessonId": lesson_id},
        )
        self.deep_request(
            session,
            "GET",
            "/api/v3/lesson/fetch-dynamic-invitation",
            "动态码(fetch-dynamic-invitation)",
            params={"lessonId": lesson_id, "v": params.get("v", 2)},
        )
        self.deep_request(
            session,
            "GET",
            "/api/v3/vote-machine/get-vote-machine-list",
            "设备侧投票机列表(get-vote-machine-list)",
            params={"lessonId": lesson_id, "classroomId": classroom_id},
        )

        checkin_id = self._first_value_by_keys(body, ("checkinId", "id"))
        if checkin_id not in (None, "", [], {}):
            self.deep_request(
                session,
                "GET",
                "/api/v3/lesson/checkin/detail",
                "签到详情(checkin/detail)",
                params={"lessonId": lesson_id, "checkinId": checkin_id},
            )
        if params.get("c") and params.get("t") and params.get("s"):
            self.deep_request(
                session,
                "POST",
                "/api/v3/vote-machine/lesson-check-in",
                "实验性: 设备侧签到(vote-machine/lesson-check-in)",
                json={
                    "lessonId": lesson_id,
                    "classroomId": classroom_id,
                    "ticket": params.get("c"),
                    "t": params.get("t"),
                    "s": params.get("s"),
                    "v": params.get("v", 2),
                },
            )

    def run_with_workflow(
        self,
        url: str,
        workflow: Callable[..., Any],
    ) -> dict[str, Any]:
        self.log_separator("雨课堂动态二维码签到深度分析工具")
        self.log(f"会话文件: {self.session_file}")
        self.sync_base_domain_from_url(url)
        self.log(f"目标域名: {self.base_domain}")

        session = self.load_session()
        self.log_separator("登录验证")
        user_info = self.validate_session(session)

        self.log_separator("课堂探测")
        self.active_context = self.fetch_active_context(session)
        lesson_id = self.active_context.get("lesson_id")
        if lesson_id not in (None, "", [], {}):
            self.log_separator("会话权限校验")
            self.validate_lesson_access(session, lesson_id)

        params = self.deep_analyze_url(url)
        workflow(self, session, url, params, lesson_id)

        self.log_separator("分析完成")
        self.log(f"详细报告已保存到: {self.report_file}")
        return {
            "report_file": str(self.report_file),
            "user_info": user_info,
            "lesson_id": self.active_context.get("lesson_id"),
            "classroom_id": self.active_context.get("classroom_id"),
            "params": params,
        }


def run_analysis(
    analyzer: RainClassroomAnalyzer,
    session,
    url: str,
    params: dict,
    lesson_id,
) -> None:
    analyzer.log_separator("签到流程深度分析")
    analyzer.run_checkin_analysis(session, url, params, lesson_id=lesson_id)
    analyzer.run_extra_probes(session, lesson_id=lesson_id, params=params)


def decode_qr(image_path: str) -> str | None:
    if not HAS_CV2:
        raise AnalysisError("缺少 opencv-python，无法从图片解码二维码")
    if not os.path.exists(image_path):
        raise AnalysisError(f"文件不存在: {image_path}")

    image = cv2.imread(image_path)
    if image is None:
        raise AnalysisError(f"无法读取图片: {image_path}")

    detector = cv2.QRCodeDetector()
    candidates: list[str] = []

    data, _, _ = detector.detectAndDecode(image)
    if data:
        candidates.append(data)

    if not candidates and hasattr(cv2, "wechat_qrcode_WeChatQRCode"):
        try:
            decoder = cv2.wechat_qrcode_WeChatQRCode()
            results, _ = decoder.detectAndDecode(image)
            candidates.extend(item for item in results if item)
        except Exception:
            pass

    if not candidates:
        for scale in (0.5, 1.5, 2.0):
            height, width = image.shape[:2]
            resized = cv2.resize(image, (int(width * scale), int(height * scale)))
            data, _, _ = detector.detectAndDecode(resized)
            if data:
                candidates.append(data)
                break

    if not candidates:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(
            gray,
            0,
            255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
        data, _, _ = detector.detectAndDecode(binary)
        if data:
            candidates.append(data)

    if not candidates:
        raise AnalysisError("二维码解码失败")
    return candidates[0]


def analyze_single(
    url: str,
    base_domain: str | None = None,
    session_file: str = "yuketang_session.json",
    report_dir: str = "reports",
) -> str:
    analyzer = RainClassroomAnalyzer(
        base_domain=base_domain,
        session_file=session_file,
        report_dir=report_dir,
    )
    result = analyzer.run_with_workflow(url, run_analysis)
    return result["report_file"]


def watch_mode(
    folder: str,
    base_domain: str | None = None,
    session_file: str = "yuketang_session.json",
    report_dir: str = "reports",
) -> None:
    watch_dir = Path(folder).expanduser()
    if not watch_dir.exists():
        raise AnalysisError(f"目录不存在: {watch_dir}")

    print(f"监控目录: {watch_dir}")
    processed = {
        str(path)
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.bmp")
        for path in watch_dir.glob(pattern)
    }
    print(f"已跳过 {len(processed)} 个现有文件")

    while True:
        try:
            for pattern in ("*.png", "*.jpg", "*.jpeg", "*.bmp"):
                for path in sorted(watch_dir.glob(pattern)):
                    path_str = str(path)
                    if path_str in processed:
                        continue
                    processed.add(path_str)
                    print(f"\n新图片: {path.name}")
                    try:
                        qr_url = decode_qr(path_str)
                        if qr_url:
                            analyze_single(
                                qr_url,
                                base_domain=base_domain,
                                session_file=session_file,
                                report_dir=report_dir,
                            )
                    except Exception as exc:
                        print(f"分析失败: {exc}")
            time.sleep(1)
        except KeyboardInterrupt:
            print("已退出监控")
            return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="雨课堂动态二维码签到深度分析工具")
    parser.add_argument("image", nargs="?", help="二维码截图路径")
    parser.add_argument("-url", dest="url", help="直接传入动态二维码 URL")
    parser.add_argument("-watch", dest="watch", metavar="FOLDER", help="监控文件夹里的新截图")
    parser.add_argument(
        "-base-domain",
        dest="base_domain",
        help="手动指定域名，如 changjiang.yuketang.cn",
    )
    parser.add_argument(
        "-session-file",
        dest="session_file",
        default="yuketang_session.json",
        help="会话 JSON 路径，默认 yuketang_session.json",
    )
    parser.add_argument(
        "-report-dir",
        dest="report_dir",
        default="reports",
        help="报告输出目录，默认 reports",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.image and not args.url and not args.watch:
        parser.print_help()
        print("\n示例:")
        print("  python3 test_checkin_bypass.py qr_photo.jpg")
        print('  python3 test_checkin_bypass.py -url "https://changjiang.yuketang.cn/api/v3/..."')
        print("  python3 test_checkin_bypass.py -watch ~/Screenshots")
        print("  python3 test_checkin_bypass.py -url \"...\" -session-file ./yuketang_session.json")
        raise SystemExit(1)

    if args.watch:
        watch_mode(
            args.watch,
            base_domain=args.base_domain,
            session_file=args.session_file,
            report_dir=args.report_dir,
        )
        return

    try:
        qr_url = args.url or decode_qr(args.image)
        if not qr_url:
            raise AnalysisError("没有可分析的二维码 URL")
        report_file = analyze_single(
            qr_url,
            base_domain=args.base_domain,
            session_file=args.session_file,
            report_dir=args.report_dir,
        )
        print(f"报告已保存到: {report_file}")
    except AnalysisError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
