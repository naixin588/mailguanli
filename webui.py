"""mailguanli WebUI —— 浏览器界面 + 本地 HTTP API。

复用 engine.py 的登录 / 收取 / 搜索引擎，前端为单文件 webui.html。

运行：
    .\.venv\Scripts\python.exe webui.py

打开浏览器：
    http://127.0.0.1:8913/

本地接口：
    http://127.0.0.1:8913/search-mail?email=xxx@mail.com&keyword=ChatGPT&regex=\d{6}&to=别名@dr.com
    http://127.0.0.1:8913/search-mail-live?email=xxx@mail.com&password=xxx&keyword=ChatGPT&regex=\d{6}&to=别名@dr.com
"""

from __future__ import annotations

import json
import random
import string
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from mailset_alias import ALIAS_LIMIT, MAILCOM_DOMAINS, MailsetAliasClient
from engine import (
    USER_AGENT,
    MailAccount,
    MailComHttpReader,
    account_lines_from_text,
    build_preview_html,
    fetch_all,
    load_saved_state,
    message_sort_key,
    parse_accounts,
    save_saved_state,
    search_cached_messages,
    search_live_mail,
)

HOST = "127.0.0.1"
PORT = 8913
AUTO_FETCH_INTERVAL_S = 5
WEBUI_PATH = Path(__file__).with_name("webui.html")
ALIAS_CACHE_PATH = Path(__file__).with_name("alias_sessions.json")


def _read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    try:
        size = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        size = 0
    if size <= 0:
        return {}
    try:
        return json.loads(handler.rfile.read(size).decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _random_local_part(length: int = 7) -> str:
    """生成随机小写字母+数字的本地部分。"""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def _gen_unique_alias(existing: set[str], domain: str, length: int = 7) -> str:
    """生成一个不与 existing 冲突的别名：随机 7 字符，重复则结尾追加数字。"""
    base = _random_local_part(length)
    candidate = f"{base}@{domain}"
    suffix = 1
    while candidate in existing:
        candidate = f"{base}{suffix}@{domain}"
        suffix += 1
    return candidate


class Backend:
    """内存态：账号凭据 + 各账号已收取的邮件缓存 + 自动拉取控制。"""

    def __init__(self) -> None:
        self.accounts: dict[str, MailAccount] = {}
        self.results_by_account: dict[str, dict[str, Any]] = {}
        self.active_accounts: set[str] = set()
        self.auto_fetch = False
        self._lock = threading.Lock()
        self._auto_thread: threading.Thread | None = None
        self.alias_clients: dict[str, MailsetAliasClient] = {}
        self._load_alias_cache()
        self._restore_state()

    # ---- 别名登录会话缓存 / 持久化 ----
    def _get_alias_client(self, account: MailAccount) -> MailsetAliasClient:
        """复用已登录的别名客户端；token 过期才重新登录，避免每次操作都登录。"""
        with self._lock:
            cached = self.alias_clients.get(account.address)
        if cached and cached.is_valid():
            return cached
        client = MailsetAliasClient(account.address, account.password)
        with self._lock:
            self.alias_clients[account.address] = client
        self._save_alias_cache()
        return client

    def _load_alias_cache(self) -> None:
        try:
            if not ALIAS_CACHE_PATH.exists():
                return
            data = json.loads(ALIAS_CACHE_PATH.read_text(encoding="utf-8"))
            for item in data if isinstance(data, list) else []:
                try:
                    client = MailsetAliasClient.restore(item)
                    # 账号凭据总是恢复，这样 token 过期后还能用密码自动重登
                    self.accounts.setdefault(
                        client.username, MailAccount(client.username, client.password)
                    )
                    # 会话只在 token 仍有效时缓存，过期则在下次操作时自动重登
                    if client.is_valid():
                        self.alias_clients[client.username] = client
                except Exception:  # noqa: BLE001
                    continue
        except (OSError, json.JSONDecodeError):
            return

    def _save_alias_cache(self) -> None:
        try:
            data = [client.serialize() for client in self.alias_clients.values()]
            ALIAS_CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            return

    # ---- 账号导入 / 删除 ----
    def import_accounts(self, raw_text: str) -> dict[str, Any]:
        parsed = parse_accounts(raw_text)
        with self._lock:
            for account in parsed:
                self.accounts[account.address] = account
        return {"imported": len(parsed), "total": len(self.accounts)}

    def delete_accounts(self, addresses: list[str]) -> dict[str, Any]:
        removed = 0
        with self._lock:
            for address in addresses:
                if address in self.accounts:
                    self.accounts.pop(address, None)
                    self.results_by_account.pop(address, None)
                    removed += 1
        return {"removed": removed}

    # ---- 收取 ----
    def fetch(self, addresses: list[str] | None, max_messages: int, max_workers: int) -> dict[str, Any]:
        with self._lock:
            if addresses is None:
                addresses = list(self.accounts)
            accounts = [self.accounts[a] for a in addresses if a in self.accounts]
            self.active_accounts.update(a.address for a in accounts)
        payload = {
            "accounts": [{"address": a.address, "password": a.password} for a in accounts],
            "max_messages": max_messages,
            "max_workers": max_workers or len(accounts) or 1,
        }
        try:
            response = fetch_all(payload, progress=None)
            with self._lock:
                for result in response.get("results", []):
                    address = result.get("account", "")
                    if address:
                        result["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        self.results_by_account[address] = result
            return response
        finally:
            with self._lock:
                self.active_accounts.difference_update(a.address for a in accounts)

    # ---- 状态 / 消息 ----
    def status(self) -> dict[str, Any]:
        with self._lock:
            accounts = []
            for address, account in sorted(self.accounts.items()):
                result = self.results_by_account.get(address)
                accounts.append(
                    {
                        "address": address,
                        "ok": bool(result and result.get("ok")),
                        "total": int(result.get("total") or 0) if result and result.get("ok") else 0,
                        "error": (result or {}).get("error", ""),
                        "updated_at": (result or {}).get("updated_at", ""),
                        "active": address in self.active_accounts,
                    }
                )
            return {"accounts": accounts, "auto_fetch": self.auto_fetch}

    def messages(self, address: str) -> dict[str, Any]:
        with self._lock:
            result = self.results_by_account.get(address)
            if not result:
                return {"ok": False, "error": "该账号尚未收取或不存在。", "messages": []}
            if not result.get("ok"):
                return {"ok": False, "error": result.get("error", "收取失败"), "messages": []}
            messages = sorted(result.get("messages", []), key=message_sort_key, reverse=True)
            return {"ok": True, "messages": messages}

    def message_preview(self, address: str, index: int) -> dict[str, Any]:
        with self._lock:
            result = self.results_by_account.get(address)
            if not result or not result.get("ok"):
                return {"ok": False, "html": ""}
            for message in result.get("messages", []):
                if int(message.get("index") or -1) == index:
                    return {"ok": True, "html": build_preview_html(message)}
            return {"ok": False, "html": ""}

    # ---- 搜索（复用 engine.py） ----
    def search_cached(self, email: str, keyword: str, pattern: str, to: str) -> str:
        with self._lock:
            snapshot = {
                addr: {**result, "messages": list(result.get("messages", []))}
                for addr, result in self.results_by_account.items()
            }
        return search_cached_messages(snapshot, email, keyword, pattern, to)

    # ---- 别名管理 ----
    def _mail_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        return session

    def _resolve_account(self, email: str, password: str = "") -> MailAccount:
        address = (email or "").strip()
        with self._lock:
            account = self.accounts.get(address)
        if account and account.password:
            return account
        if address and password:
            return MailAccount(address, password.strip())
        raise ValueError("该账号未导入或未保存密码。请先在左侧批量导入该账号（email----密码）。")

    def list_aliases(self, email: str, password: str = "") -> dict[str, Any]:
        account = self._resolve_account(email, password)
        client = self._get_alias_client(account)
        aliases = client.list_aliases()
        addresses = [a["address"] for a in aliases]
        return {
            "ok": True,
            "aliases": addresses,
            "total": len(addresses),
            "limit": ALIAS_LIMIT,
            "remaining": max(0, ALIAS_LIMIT - len(addresses)),
            "detail": aliases,
        }

    def create_aliases(
        self, email: str, password: str, count: int, base: str, domain: str
    ) -> dict[str, Any]:
        account = self._resolve_account(email, password)
        client = self._get_alias_client(account)
        display_name = ""
        existing: set[str] = set()
        for alias in client.list_aliases():
            existing.add(alias["address"])
            if alias.get("defaultSenderAddress"):
                display_name = alias.get("displayName", "")
        remaining = ALIAS_LIMIT - len(existing)
        if remaining <= 0:
            return {
                "ok": False,
                "error": f"别名已达上限（{ALIAS_LIMIT} 个，含主账号），请先删除部分别名",
                "created": [],
                "total": len(existing),
                "limit": ALIAS_LIMIT,
            }
        count = min(count, remaining)
        created: list[dict[str, Any]] = []
        for _ in range(count):
            full = _gen_unique_alias(existing, domain)
            try:
                client.create_alias(full, display_name)
                created.append({"full_address": full, "status": 201})
                existing.add(full)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                if "409" in err:
                    err = "别名已达上限或地址已存在（409）"
                created.append({"full_address": full, "status": 0, "error": err})
            time.sleep(1)
        return {"ok": True, "created": created, "total": len(existing), "limit": ALIAS_LIMIT}

    def delete_aliases(self, email: str, password: str, addresses: list[str]) -> dict[str, Any]:
        account = self._resolve_account(email, password)
        client = self._get_alias_client(account)
        deleted: list[dict[str, Any]] = []
        for address in addresses:
            try:
                status = client.delete_alias(address.strip())
                deleted.append({"address": address, "status": status})
            except Exception as exc:  # noqa: BLE001
                deleted.append({"address": address, "status": 0, "error": str(exc)})
            time.sleep(1)
        return {"ok": True, "deleted": deleted}

    def create_aliases_batch(
        self, accounts: list[str] | None, count: int, domain: str, base: str
    ) -> dict[str, Any]:
        """为多个账号一次性批量创建别名。accounts 为 None 或空表示全部已导入账号。"""
        with self._lock:
            if accounts:
                addrs = [a for a in accounts if a in self.accounts and self.accounts[a].password]
            else:
                addrs = [a.address for a in self.accounts.values() if a.password]
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for addr in addrs:
            account = self.accounts.get(addr)
            if not account:
                errors.append({"account": addr, "error": "账号不存在或无密码"})
                continue
            try:
                client = self._get_alias_client(account)
                display_name = ""
                existing: set[str] = set()
                for alias in client.list_aliases():
                    existing.add(alias["address"])
                    if alias.get("defaultSenderAddress"):
                        display_name = alias.get("displayName", "")
                remaining = ALIAS_LIMIT - len(existing)
                if remaining <= 0:
                    results.append({
                        "account": addr,
                        "created": [],
                        "error": f"别名已达上限（{ALIAS_LIMIT} 个）",
                    })
                    continue
                limit_count = min(count, remaining)
                created: list[dict[str, Any]] = []
                for _ in range(limit_count):
                    full = _gen_unique_alias(existing, domain)
                    try:
                        client.create_alias(full, display_name)
                        created.append({"full_address": full, "status": 201})
                        existing.add(full)
                    except Exception as exc:  # noqa: BLE001
                        err = str(exc)
                        if "409" in err:
                            err = "别名已达上限或地址已存在（409）"
                        created.append({"full_address": full, "status": 0, "error": err})
                    time.sleep(1)
                results.append({"account": addr, "created": created})
            except Exception as exc:  # noqa: BLE001
                errors.append({"account": addr, "error": str(exc)})
        return {"ok": True, "results": results, "errors": errors}

    def export_aliases(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """批量导出所有已导入账号的别名。返回 (rows, errors)。"""
        with self._lock:
            accounts = [a for a in self.accounts.values() if a.password]
        rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for account in accounts:
            try:
                client = self._get_alias_client(account)
                for alias in client.list_aliases():
                    rows.append(
                        {
                            "account": account.address,
                            "alias": alias["address"],
                            "deletable": alias.get("deletable"),
                            "default_sender": alias.get("defaultSenderAddress"),
                            "display_name": alias.get("displayName", ""),
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append({"account": account.address, "error": str(exc)})
        return rows, errors

    def message_debug(self, address: str, index: int) -> dict[str, Any]:
        with self._lock:
            result = self.results_by_account.get(address)
            if not result or not result.get("ok"):
                return {"ok": False, "error": "账号未收取或不存在。", "tokens": []}
            for message in result.get("messages", []):
                if int(message.get("index") or -1) == index:
                    return {
                        "ok": True,
                        "from": message.get("from", ""),
                        "to": message.get("to", ""),
                        "date": message.get("date", ""),
                        "subject": message.get("subject", ""),
                        "tokens": list(message.get("_header_tokens", [])),
                    }
            return {"ok": False, "error": "未找到该邮件。", "tokens": []}

    # ---- 会话持久化 ----
    def _restore_state(self) -> None:
        state = load_saved_state()
        accounts = state.get("accounts", []) if isinstance(state, dict) else []
        results = state.get("results", []) if isinstance(state, dict) else []
        # 凭据不在 state 里（原版也不存密码），只恢复地址占位，等用户重新导入。
        for address in accounts if isinstance(accounts, list) else []:
            self.accounts.setdefault(str(address), MailAccount(str(address), ""))
        for result in results if isinstance(results, list) else []:
            if isinstance(result, dict) and result.get("account"):
                self.results_by_account[str(result["account"])] = result

    def save_state(self) -> dict[str, Any]:
        with self._lock:
            payload = {
                "version": 1,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "accounts": sorted(self.accounts),
                "results": [dict(r) for r in self.results_by_account.values()],
            }
        save_saved_state(payload)
        return {"saved": True, "saved_at": payload["saved_at"]}

    # ---- 自动拉取 ----
    def set_auto_fetch(self, enabled: bool) -> dict[str, Any]:
        self.auto_fetch = enabled
        if enabled and (self._auto_thread is None or not self._auto_thread.is_alive()):
            self._auto_thread = threading.Thread(target=self._auto_loop, daemon=True)
            self._auto_thread.start()
        return {"auto_fetch": self.auto_fetch}

    def _auto_loop(self) -> None:
        while self.auto_fetch:
            if self.accounts:
                try:
                    self.fetch(None, 0, 5)
                except Exception:  # noqa: BLE001 - 自动拉取失败不中断循环。
                    pass
            time.sleep(AUTO_FETCH_INTERVAL_S)


BACKEND = Backend()


class WebUIHandler(BaseHTTPRequestHandler):
    server_version = "mailguanli-webui/1.0"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status: int, value: str, content_type: str = "text/plain; charset=utf-8") -> None:
        body = value.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_download(self, status: int, body: bytes, content_type: str, filename: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f"attachment; filename={filename}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items() if v}

        if self.command == "GET" and path in ("/", "/index.html"):
            html = WEBUI_PATH.read_text(encoding="utf-8") if WEBUI_PATH.exists() else "<h1>webui.html missing</h1>"
            self._text(200, html, "text/html; charset=utf-8")
            return

        if path == "/health":
            self._json(200, {"ok": True})
            return

        # 兼容旧版搜索接口
        if path in ("/search-mail", "/search-mail-live"):
            body = _read_body(self) if self.command == "POST" else query
            email = str(body.get("email") or body.get("account") or body.get("mail") or "")
            keyword = str(body.get("keyword") or "")
            pattern = str(body.get("regex") or body.get("pattern") or "")
            to = str(body.get("to") or "")
            if path == "/search-mail-live":
                password = str(body.get("password") or "")
                try:
                    max_messages = int(body.get("max_messages") or 0)
                except (TypeError, ValueError):
                    max_messages = 0
                try:
                    value = search_live_mail(email, password, keyword, pattern, max_messages, to)
                except Exception:  # noqa: BLE001
                    value = "NullX"
            else:
                value = BACKEND.search_cached(email, keyword, pattern, to)
            self._text(200, value or "NullX")
            return

        if path == "/api/status":
            self._json(200, BACKEND.status())
            return

        if path == "/api/accounts":
            if self.command == "POST":
                body = _read_body(self)
                try:
                    result = BACKEND.import_accounts(str(body.get("text", "")))
                    self._json(200, result)
                except ValueError as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                return
            if self.command == "DELETE":
                body = _read_body(self)
                addresses = [str(a) for a in body.get("addresses", [])]
                self._json(200, BACKEND.delete_accounts(addresses))
                return
            self._json(200, BACKEND.status())
            return

        if path == "/api/fetch":
            if self.command != "POST":
                self._json(405, {"ok": False, "error": "POST only"})
                return
            body = _read_body(self)
            addresses = body.get("addresses")
            try:
                max_messages = int(body.get("max_messages") or 0)
                max_workers = int(body.get("max_workers") or 5)
            except (TypeError, ValueError):
                max_messages, max_workers = 0, 5
            response = BACKEND.fetch(addresses, max_messages, max_workers)
            self._json(200, response)
            return

        if path == "/api/messages":
            address = query.get("email", "")
            self._json(200, BACKEND.messages(address))
            return

        if path == "/api/preview":
            address = query.get("email", "")
            try:
                index = int(query.get("index", "-1"))
            except (TypeError, ValueError):
                index = -1
            result = BACKEND.message_preview(address, index)
            self._text(200, result["html"], "text/html; charset=utf-8")
            return

        if path == "/api/auto-fetch":
            body = _read_body(self) if self.command == "POST" else {}
            enabled = bool(body.get("enabled", False))
            self._json(200, BACKEND.set_auto_fetch(enabled))
            return

        if path == "/api/state/save":
            self._json(200, BACKEND.save_state())
            return

        if path == "/api/message-debug":
            address = query.get("email", "")
            try:
                index = int(query.get("index", "-1"))
            except (TypeError, ValueError):
                index = -1
            self._json(200, BACKEND.message_debug(address, index))
            return

        if path == "/api/alias/domains":
            self._json(200, {"ok": True, "domains": MAILCOM_DOMAINS})
            return

        if path == "/api/alias/list":
            body = _read_body(self) if self.command == "POST" else query
            email = str(body.get("email") or "")
            password = str(body.get("password") or "")
            try:
                self._json(200, BACKEND.list_aliases(email, password))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "error": str(exc)})
            return

        if path == "/api/alias/create":
            body = _read_body(self)
            email = str(body.get("email") or "")
            password = str(body.get("password") or "")
            try:
                count = int(body.get("count") or 1)
            except (TypeError, ValueError):
                count = 1
            base = str(body.get("base") or "alias")
            domain = str(body.get("domain") or "dr.com")
            try:
                self._json(200, BACKEND.create_aliases(email, password, count, base, domain))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "error": str(exc)})
            return

        if path == "/api/alias/create-batch":
            body = _read_body(self)
            accounts = body.get("accounts")  # None 或列表
            if accounts is not None and not isinstance(accounts, list):
                accounts = [str(accounts)]
            try:
                count = int(body.get("count") or 1)
            except (TypeError, ValueError):
                count = 1
            base = str(body.get("base") or "")
            domain = str(body.get("domain") or "mail.com")
            try:
                self._json(200, BACKEND.create_aliases_batch(accounts, count, domain, base))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "error": str(exc)})
            return

        if path == "/api/alias/delete":
            body = _read_body(self)
            email = str(body.get("email") or "")
            password = str(body.get("password") or "")
            addresses = [str(a) for a in body.get("addresses", [])]
            try:
                self._json(200, BACKEND.delete_aliases(email, password, addresses))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "error": str(exc)})
            return

        if path == "/api/alias/export":
            rows, errors = BACKEND.export_aliases()
            import csv
            import io
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["账号", "别名", "可删除", "默认发件", "显示名"])
            for row in rows:
                writer.writerow([
                    row["account"],
                    row["alias"],
                    "是" if row["deletable"] else "否",
                    "是" if row["default_sender"] else "否",
                    row["display_name"],
                ])
            if errors:
                writer.writerow([])
                writer.writerow(["# 以下账号导出失败", ""])
                for err in errors:
                    writer.writerow([err["account"], err["error"]])
            content = "\ufeff" + buf.getvalue()
            self._send_download(200, content.encode("utf-8"), "text/csv; charset=utf-8", "aliases.csv")
            return

        self._json(404, {"ok": False, "error": "Not found"})

    def do_GET(self) -> None:  # noqa: N802
        self._route()

    def do_POST(self) -> None:  # noqa: N802
        self._route()

    def do_DELETE(self) -> None:  # noqa: N802
        self._route()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), WebUIHandler)
    print(f"mailguanli WebUI 已启动：http://{HOST}:{PORT}/")
    print("本地搜索接口：")
    print(f"  缓存: http://{HOST}:{PORT}/search-mail?email=xxx@mail.com&keyword=ChatGPT&regex=\\d{{6}}&to=别名@dr.com")
    print(f"  实时: http://{HOST}:{PORT}/search-mail-live?email=xxx@mail.com&password=xxx&keyword=ChatGPT&regex=\\d{{6}}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
