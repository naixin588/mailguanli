"""mail.com 邮件引擎 —— 用 requests 直接模拟 mail.com 网页邮箱流程。

覆盖：登录表单、lightmailer 启动、文件夹列表、消息列表、消息详情与正文、
完整头部解析（收件人/To），以及批量收取与验证码搜索。
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

MAIL_HOME_URL = "https://www.mail.com/"
LIGHT_START_URL = "https://lightmailer.mail.com/start?device=desktop&ott={ott}"
MAX_FETCH_WORKERS = 5
STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
STATE_PATH = STATE_DIR / "ccGptMailReader" / "mail_reader_state.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

@dataclass(frozen=True)
class MailAccount:
    address: str
    password: str

ProgressCallback = Callable[[dict[str, Any]], None]

@dataclass(frozen=True)
class LoginForm:
    action: str
    fields: dict[str, str]

class BasicHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self.forms: list[dict[str, str]] = []
        self.inputs: list[dict[str, str]] = []
        self.text: list[str] = []
        self._in_login_form = False
        self.login_action = ""
        self.login_inputs: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "form":
            self.forms.append(values)
            action = values.get("action", "")
            self._in_login_form = "login.mail.com/login" in action
            if self._in_login_form:
                self.login_action = action
        if tag == "a":
            self.links.append(values)
        if tag == "input":
            self.inputs.append(values)
            if self._in_login_form:
                self.login_inputs.append(values)

    def handle_data(self, data: str) -> None:
        value = normalize_text(data)
        if value:
            self.text.append(value)

def parse_accounts(raw_text: str) -> list[MailAccount]:
    accounts: list[MailAccount] = []
    errors: list[str] = []
    for line_number, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if "----" not in line:
            errors.append(f"第 {line_number} 行：格式应为 email----密码")
            continue
        address, password = line.split("----", 1)
        address = address.strip()
        password = password.strip()
        if "@" not in address or not password:
            errors.append(f"第 {line_number} 行：账号或密码无效")
            continue
        accounts.append(MailAccount(address, password))

    if errors:
        raise ValueError("\n".join(errors))
    if not accounts:
        raise ValueError("请至少输入一个 email----密码 格式的账号。")
    return accounts

def load_accounts_file(path: str) -> str:
    account_path = Path(path)
    content = account_path.read_text(encoding="utf-8-sig", errors="replace")
    if account_path.suffix.lower() != ".csv":
        return content

    rows: list[str] = []
    for row in csv.reader(StringIO(content)):
        if not row or len(row) < 2:
            continue
        address = row[0].strip()
        password = row[1].strip()
        if "@" in address and password:
            rows.append(f"{address}----{password}")
    return "\n".join(rows) if rows else content

def count_account_lines(raw_text: str) -> tuple[int, int]:
    valid = 0
    invalid = 0
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "----" not in line:
            invalid += 1
            continue
        address, password = line.split("----", 1)
        if "@" in address.strip() and password.strip():
            valid += 1
        else:
            invalid += 1
    return valid, invalid

def account_lines_from_text(raw_text: str) -> dict[str, MailAccount]:
    output: dict[str, MailAccount] = {}
    for account in parse_accounts(raw_text):
        output[account.address] = account
    return output

def now_label() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def load_saved_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

def save_saved_state(payload: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def clear_saved_state() -> None:
    try:
        STATE_PATH.unlink()
    except FileNotFoundError:
        return

def normalize_text(value: str) -> str:
    value = html.unescape(value.replace("\xa0", " "))
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    return value.strip()

def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</(p|div|tr|table|h[1-6])\s*>", "\n", value)
    value = re.sub(r"<[^>]+>", " ", value)
    lines = [normalize_text(line) for line in html.unescape(value).splitlines()]
    output: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank:
                output.append("")
            blank = True
            continue
        output.append(line)
        blank = False
    return "\n".join(output).strip()

def parse_login_form(home_html: str) -> LoginForm:
    parser = BasicHtmlParser()
    parser.feed(home_html)
    if not parser.login_action:
        raise RuntimeError("未找到 mail.com 登录表单。")

    fields: dict[str, str] = {}
    for input_tag in parser.login_inputs:
        name = input_tag.get("name")
        if name:
            fields[name] = input_tag.get("value", "")
    return LoginForm(action=parser.login_action, fields=fields)

def extract_ott(url: str, page_html: str) -> str:
    match = re.search(r"[?&]ott=([0-9a-f-]+)", url) or re.search(
        r"ott=([0-9a-f-]+)", page_html
    )
    if not match:
        raise RuntimeError(
            "mail.com 未返回 lightmailer 登录令牌。"
            "该账号可能需要验证码、额外验证或浏览器 Cookie。"
        )
    return match.group(1)

def extract_wicket_redirect(xml_text: str) -> str:
    match = re.search(r"<redirect><!\[CDATA\[(.*?)\]\]></redirect>", xml_text)
    if not match:
        raise RuntimeError("mail.com lightmailer 未返回启动跳转。")
    return match.group(1)

def unique_relative_urls(page_html: str, pattern: str) -> list[str]:
    found = [html.unescape(match) for match in re.findall(pattern, page_html)]
    output: list[str] = []
    seen: set[str] = set()
    for url in found:
        if url not in seen:
            seen.add(url)
            output.append(url)
    return output

def parse_message_timestamp(value: str) -> float | None:
    cleaned = value.strip()
    if cleaned.lower().startswith("received "):
        cleaned = cleaned[9:].strip()
    if not cleaned:
        return None
    try:
        parsed = parsedate_to_datetime(cleaned)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None

def message_sort_key(message: dict[str, Any]) -> tuple[int, float]:
    timestamp = parse_message_timestamp(str(message.get("date", "")))
    if timestamp is not None:
        return (1, timestamp)
    try:
        index = int(message.get("index") or 0)
    except (TypeError, ValueError):
        index = 0
    return (0, -float(index))

def account_matches_query(account: str, email_query: str) -> bool:
    account_value = account.strip().lower()
    query = email_query.strip().lower()
    if not query:
        return False
    local_part = account_value.split("@", 1)[0]
    return query in {account_value, local_part} or query in account_value

def message_search_text(message: dict[str, Any]) -> str:
    body_html = str(message.get("body_html") or "")
    parts = [
        str(message.get("subject", "")),
        str(message.get("from", "")),
        str(message.get("to", "")),
        str(message.get("date", "")),
        str(message.get("body", "")),
        html_to_text(body_html) if body_html else "",
    ]
    return "\n".join(part for part in parts if part)

def search_cached_messages(
    results_by_account: dict[str, dict[str, Any]],
    email_query: str,
    keyword: str,
    pattern: str,
    to_query: str = "",
) -> str:
    keyword_value = keyword.strip().lower()
    to_value = to_query.strip().lower()
    if not email_query.strip() or not keyword_value or not pattern.strip():
        return "NullX"

    try:
        compiled = re.compile(pattern, re.IGNORECASE | re.DOTALL)
    except re.error:
        return "NullX"

    for account, result in sorted(results_by_account.items()):
        if not account_matches_query(account, email_query):
            continue
        if not result.get("ok"):
            continue
        messages = sorted(result.get("messages", []), key=message_sort_key, reverse=True)
        for message in messages:
            if not isinstance(message, dict):
                continue
            if to_value and to_value not in str(message.get("to", "")).lower():
                continue
            text = message_search_text(message)
            if keyword_value not in text.lower():
                continue
            match = compiled.search(text)
            if not match:
                continue
            if match.groups():
                return match.group(1)
            return match.group(0)
    return "NullX"

def search_live_mail(
    email_address: str,
    password: str,
    keyword: str,
    pattern: str,
    max_messages: int = 0,
    to_query: str = "",
) -> str:
    if not email_address.strip() or not password or not keyword.strip() or not pattern.strip():
        return "NullX"
    reader = MailComHttpReader(status=[])
    result = reader.fetch_account(MailAccount(email_address.strip(), password), max_messages)
    return search_cached_messages({email_address.strip(): result}, email_address, keyword, pattern, to_query)

def strip_unsafe_html(value: str) -> str:
    value = re.sub(r"(?is)<script\b.*?>.*?</script>", "", value)
    value = re.sub(r"(?is)<object\b.*?>.*?</object>", "", value)
    value = re.sub(r"(?is)<embed\b.*?>", "", value)
    value = re.sub(r"(?is)<form\b.*?>.*?</form>", "", value)
    return value

def body_fragment(value: str) -> str:
    match = re.search(r"(?is)<body[^>]*>(.*?)</body>", value)
    return match.group(1) if match else value

def build_preview_html(message: dict[str, Any]) -> str:
    body_html = str(message.get("body_html") or "")
    if body_html:
        content = strip_unsafe_html(body_fragment(body_html))
    else:
        content = html.escape(str(message.get("body", ""))).replace("\n", "<br>")

    subject = html.escape(str(message.get("subject", "")))
    sender = html.escape(str(message.get("from", "")))
    recipient = html.escape(str(message.get("to", "")))
    date = html.escape(str(message.get("date", "")))
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{
    margin: 0;
    padding: 18px;
    color: #1f2933;
    background: #ffffff;
    font-family: "Microsoft YaHei UI", "Segoe UI", Arial, sans-serif;
    font-size: 14px;
    line-height: 1.55;
}}
.mail-header {{
    border-bottom: 1px solid #d7dde5;
    margin-bottom: 18px;
    padding-bottom: 14px;
}}
.mail-subject {{
    color: #0b4f8a;
    font-size: 20px;
    font-weight: 650;
    margin-bottom: 8px;
}}
.mail-meta {{
    color: #596775;
    margin: 3px 0;
}}
img {{
    max-width: 100%;
    height: auto;
}}
</style>
</head>
<body>
<section class="mail-header">
    <div class="mail-subject">{subject}</div>
    <div class="mail-meta"><strong>发件人：</strong> {sender}</div>
    <div class="mail-meta"><strong>收件人：</strong> {recipient}</div>
    <div class="mail-meta"><strong>时间：</strong> {date}</div>
</section>
<main>{content}</main>
</body>
</html>"""

class MailComHttpReader:
    def __init__(self, status: list[str], progress: ProgressCallback | None = None) -> None:
        self.status = status
        self.progress = progress
        self.current_account = ""

    def fetch_account(self, account: MailAccount, max_messages: int) -> dict[str, Any]:
        self.current_account = account.address
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        self._set_status(f"正在登录：{account.address}")

        folder_url, folder_html = self._login(session, account)
        inbox_url = self._find_inbox_url(folder_url, folder_html)
        messages = self._fetch_messages(session, inbox_url, max_messages)

        return {
            "account": account.address,
            "ok": True,
            "messages": messages,
            "total": len(messages),
        }

    def _set_status(self, value: str, event: str = "status", **fields: Any) -> None:
        self.status.append(value)
        if self.progress is None:
            return
        payload = {
            "event": event,
            "message": value,
            "account": self.current_account,
        }
        payload.update(fields)
        self.progress(payload)

    def _login(self, session: requests.Session, account: MailAccount) -> tuple[str, str]:
        home = session.get(MAIL_HOME_URL, timeout=30)
        home.raise_for_status()
        login_form = parse_login_form(home.text)

        fields = dict(login_form.fields)
        fields["username"] = account.address
        fields["password"] = account.password
        login = session.post(login_form.action, data=fields, timeout=60, allow_redirects=True)
        login.raise_for_status()

        ott = extract_ott(login.url, login.text)
        light = session.get(LIGHT_START_URL.format(ott=ott), timeout=60, allow_redirects=True)
        light.raise_for_status()

        ajax_headers = {
            "Wicket-Ajax": "true",
            "Wicket-Ajax-BaseURL": "start?0&device=desktop",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/xml, text/xml, */*; q=0.01",
            "Referer": light.url,
        }
        startup = session.get(
            urljoin(light.url, "./start?0-1.0-&device=desktop"),
            headers=ajax_headers,
            timeout=60,
        )
        startup.raise_for_status()
        redirect = extract_wicket_redirect(startup.text)

        folder = session.get(urljoin(light.url, redirect), timeout=60)
        folder.raise_for_status()
        if "FolderListPage" not in folder.text:
            raise RuntimeError("登录成功，但文件夹列表未加载。")
        self._set_status(f"登录成功：{account.address}")
        return folder.url, folder.text

    def _find_inbox_url(self, folder_url: str, folder_html: str) -> str:
        match = re.search(
            r'href="(\./messagelist\?folderId=[^"]+)"[^>]+data-webdriver="INBOX:Inbox"',
            folder_html,
        )
        if not match:
            raise RuntimeError("未找到收件箱链接。")
        return urljoin(folder_url, html.unescape(match.group(1)))

    def _fetch_messages(
        self,
        session: requests.Session,
        inbox_url: str,
        max_messages: int,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        page_url = inbox_url

        while page_url:
            listing = session.get(page_url, timeout=60)
            listing.raise_for_status()
            detail_urls = unique_relative_urls(
                listing.text,
                r'href="(\./messagedetail\?[^"]+)"',
            )
            if not detail_urls and not messages:
                self._set_status("收件箱为空。")
                return []

            for detail_url in detail_urls:
                if max_messages and len(messages) >= max_messages:
                    return messages
                index = len(messages) + 1
                self._set_status(
                    f"正在读取第 {index} 封邮件",
                    event="message",
                    current_message=index,
                )
                messages.append(
                    self._fetch_message_detail(
                        session,
                        urljoin(listing.url, detail_url),
                        index,
                    )
                )

            page_url = self._find_next_page_url(listing.url, listing.text)
            if page_url and max_messages and len(messages) >= max_messages:
                page_url = ""

        return messages

    def _find_next_page_url(self, current_url: str, listing_html: str) -> str:
        parser = BasicHtmlParser()
        parser.feed(listing_html)
        for link in parser.links:
            label = " ".join(
                value for key, value in link.items() if key in {"title", "aria-label", "data-webdriver"}
            ).lower()
            href = link.get("href", "")
            if href and ("next" in label or "paging=next" in href.lower()):
                return urljoin(current_url, href)
        return ""

    def _fetch_message_detail(
        self,
        session: requests.Session,
        detail_url: str,
        index: int,
    ) -> dict[str, Any]:
        detail = session.get(detail_url, timeout=60)
        detail.raise_for_status()

        body_match = re.search(r'<iframe id="bodyIFrame"[^>]+src="([^"]+)"', detail.text)
        body = ""
        body_html = ""
        body_url = ""
        if body_match:
            body_url = urljoin(detail.url, html.unescape(body_match.group(1)))
            body_response = session.get(body_url, headers={"Referer": detail.url}, timeout=60)
            body_response.raise_for_status()
            body_html = body_response.text
            body = html_to_text(body_html)

        metadata = self._extract_detail_metadata(detail.text)
        recipient = self._fetch_recipient(session, detail.url, detail.text)
        if recipient:
            metadata["to"] = recipient
        metadata["index"] = index
        metadata["body"] = body
        metadata["body_html"] = body_html
        metadata["body_url"] = body_url
        return metadata

    def _fetch_recipient(self, session: requests.Session, detail_url: str, detail_html: str) -> str:
        """收件人在详情页默认不显示，需要走 mailHeader-showMore 链接拿完整头部页。"""
        match = re.search(r'href="([^"]*mailHeader-showMore[^"]*)"', detail_html)
        if not match:
            return ""
        headers_url = urljoin(detail_url, html.unescape(match.group(1)))
        try:
            response = session.get(headers_url, timeout=60)
            response.raise_for_status()
        except Exception:  # noqa: BLE001 - 拿不到头部不影响整体收取。
            return ""
        return self._extract_recipient_from_headers(response.text)

    def _extract_recipient_from_headers(self, headers_html: str) -> str:
        parser = BasicHtmlParser()
        parser.feed(headers_html)
        tokens = parser.text
        email_re = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
        for index, token in enumerate(tokens):
            if token.strip().rstrip(":") != "To":
                continue
            for rest in tokens[index + 1 :]:
                email_match = email_re.search(rest)
                if email_match:
                    return email_match.group(0)
                if rest.strip().rstrip(":") in {"Date", "From", "Subject", "Reply to"}:
                    break
            break
        return ""

    def _extract_detail_metadata(self, detail_html: str) -> dict[str, str]:
        parser = BasicHtmlParser()
        parser.feed(detail_html)
        tokens = parser.text

        subject = self._next_after(tokens, "Subject")
        sender = self._next_after(tokens, "From:") or self._next_after(tokens, "Sender")
        recipient = self._extract_recipient(detail_html, tokens)
        date = next((token for token in tokens if token.startswith("Received ")), "")

        title_match = re.search(r"<title[^>]*>mail\.com - E-Mail: (.*?)</title>", detail_html)
        if not subject and title_match:
            subject = normalize_text(title_match.group(1))

        return {
            "from": sender,
            "to": recipient,
            "date": date,
            "subject": subject,
            "_header_tokens": tokens[:80],
        }

    def _extract_recipient(self, detail_html: str, tokens: list[str]) -> str:
        recipient = self._next_after(tokens, "To:") or self._next_after(tokens, "To")
        if recipient and recipient.lower() not in {"me", "我", "undisclosed-recipients", "undisclosed-recipients:;"}:
            return recipient
        # 兜底：从原始 HTML 抓 To / Delivered-To / X-Envelope-To 里的邮箱地址。
        # 别名场景下 Delivered-To / X-Envelope-To 才是真实投递到的别名地址。
        match = re.search(
            r"(?:Delivered-To|X-Envelope-To|Envelope-To|To:|>To<|Recipient)[^<>\n]{0,160}?([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
            detail_html,
            re.IGNORECASE,
        )
        if match:
            return normalize_text(match.group(1))
        return ""

    def _next_after(self, tokens: list[str], label: str) -> str:
        for index, token in enumerate(tokens[:-1]):
            if token == label:
                return tokens[index + 1]
        return ""

def fetch_all(payload: dict[str, Any], progress: ProgressCallback | None = None) -> dict[str, Any]:
    accounts = [
        MailAccount(str(item["address"]), str(item["password"])) for item in payload["accounts"]
    ]
    max_messages = int(payload.get("max_messages") or 0)
    worker_count = max(1, min(int(payload.get("max_workers") or MAX_FETCH_WORKERS), len(accounts)))
    status: list[str] = []
    if progress is not None:
        progress(
            {
                "event": "batch_start",
                "message": f"开始处理 {len(accounts)} 个账号，并发数 {worker_count}。",
                "total_accounts": len(accounts),
                "max_workers": worker_count,
            }
        )

    def fetch_one(index: int, account: MailAccount) -> tuple[int, dict[str, Any]]:
        if progress is not None:
            progress(
                {
                    "event": "account_start",
                    "message": f"正在收取 {account.address}（{index}/{len(accounts)}）",
                    "account": account.address,
                    "current_account": index,
                    "total_accounts": len(accounts),
                }
            )
        try:
            reader = MailComHttpReader(status=status, progress=progress)
            account_result = reader.fetch_account(account, max_messages)
            if progress is not None:
                progress(
                    {
                        "event": "account_done",
                        "message": f"{account.address} 已完成：{account_result['total']} 封邮件。",
                        "account": account.address,
                        "messages": account_result["total"],
                        "current_account": index,
                        "total_accounts": len(accounts),
                        "result": account_result,
                    }
                )
            return index, account_result
        except Exception as exc:  # noqa: BLE001 - return per-account error to GUI.
            account_result = {"account": account.address, "ok": False, "error": str(exc)}
            if progress is not None:
                progress(
                    {
                        "event": "account_error",
                        "message": f"{account.address} 失败：{exc}",
                        "account": account.address,
                        "error": str(exc),
                        "current_account": index,
                        "total_accounts": len(accounts),
                        "result": account_result,
                    }
                )
            return index, account_result

    results_by_index: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(fetch_one, index, account)
            for index, account in enumerate(accounts, start=1)
        ]
        for future in as_completed(futures):
            index, account_result = future.result()
            results_by_index[index] = account_result

    results = [results_by_index[index] for index in sorted(results_by_index)]
    if progress is not None:
        progress({"event": "batch_done", "message": "收取完成。", "results": results})
    return {"ok": True, "status": status, "results": results}
