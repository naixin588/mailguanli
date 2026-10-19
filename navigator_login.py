"""mail.com 经典版（navigator）登录流程 —— 已真机验证。

别名管理不在 lightmailer（新界面），而在经典版 navigator 的设置 App 里：
    navigator-lxa.mail.com/mail_settings?sid=...
该设置 App 实际由 mailset-root.mail.com 承载，其 API 根为：
    https://settings-cats.<domain>

本文件封装了「登录 → 拿到 navigator sid」的完整链路，供后续调用设置 API 复用。
"""

from __future__ import annotations

import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class _LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: dict[str, str] = {}
        self.action = ""
        self._in_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if tag == "form" and "login" in values.get("action", "").lower():
            self._in_form = True
            self.action = values.get("action", "")
        if self._in_form and tag == "input" and values.get("name"):
            self.inputs[values["name"]] = values.get("value", "")


def navigator_login(username: str, password: str, retries: int = 3) -> tuple[requests.Session, str]:
    """登录 mail.com 并返回 (session, navigator_sid)。

    流程：www.mail.com 登录表单 → login.mail.com/login（successURL 改成 navigator）
          → navigator-lxa.mail.com/login?ott=... → /halogin?... → /?sid=<sid>

    mail.com 对高频登录有临时限流，这里带重试与退避。
    """
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            return _login_once(username, password)
        except RuntimeError as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"navigator 登录失败（重试 {retries} 次）：{last_error}")


def _login_once(username: str, password: str) -> tuple[requests.Session, str]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    home = session.get("https://www.mail.com/", timeout=60, allow_redirects=True)
    home.raise_for_status()
    parser = _LoginFormParser()
    parser.feed(home.text)
    if not parser.action:
        raise RuntimeError("未找到 mail.com 登录表单。")

    data = dict(parser.inputs)
    data["successURL"] = "https://navigator-lxa.mail.com/login"
    data["username"] = username
    data["password"] = password

    login = session.post(parser.action, data=data, timeout=60, allow_redirects=False)
    location = login.headers.get("Location", "")
    if "navigator" not in location:
        raise RuntimeError("登录未跳转到 navigator，可能账号密码错误或需要额外验证。")

    halogin_url = location.replace("/login", "/halogin", 1)
    after = session.get(halogin_url, timeout=60, allow_redirects=True)
    after.raise_for_status()

    match = re.search(r"sid=([a-f0-9]{20,})", after.url)
    if not match:
        # 偶尔重定向链会多一跳，从历史里兜底找 sid
        for hist in reversed(after.history):
            m = re.search(r"sid=([a-f0-9]{20,})", hist.url)
            if m:
                return session, m.group(1)
        raise RuntimeError(f"登录成功但未拿到 navigator sid，最终 URL: {after.url[:120]}")
    return session, match.group(1)


def get_mailset_url(session: requests.Session, sid: str) -> str:
    """从 navigator 设置壳里提取 mailset-root 的设置 App URL（含 iac_token）。"""
    shell = session.get(
        "https://navigator-lxa.mail.com/mail_settings?sid=" + sid,
        timeout=60,
        allow_redirects=True,
    )
    shell.raise_for_status()
    match = re.search(r'"mail_settings".*?"url_map".*?"default":"([^"]+)"', shell.text)
    if not match:
        raise RuntimeError("未在设置壳里找到 mailset url_map。")
    return match.group(1).replace("\\/", "/").split("#")[0]
