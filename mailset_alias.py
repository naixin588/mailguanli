"""mail.com 别名（Sender Address）真实管理接口 —— 基于 navigator + mailset 设置 API。

完整链路（已从 HAR 逆向）：
    登录 → navigator sid → oauthbridge 换 token（scope 含 webmailer_setting_r/w）
         → settings-cats.mail.com 的 mailaccount API 增删查别名。

API：
    查   GET  /mailaccount/primary/emailAddresses?absoluteURI=false&q.state.in=ACTIVE&q.type.in=MANAGED,DOMAIN_HOSTING
    校验 POST /mailaccount/emailAddressValidations?absoluteURI=false         body: ["addr@mail.com"]
    增   POST /mailaccount/primary/emailAddresses?absoluteURI=false          body: {"address":..., "displayName":..., ...}
    删   POST /mailaccount/primary/emailAddressesRemovals/{addr}/removals?absoluteURI=false  (空 body)

认证：Authorization: Bearer <oauth2 access_token>
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import requests

import dpapi
from navigator_login import navigator_login

API_ROOT = "https://settings-cats.mail.com"
TOKEN_URL = "https://oauthbridge.navigator-lxa.mail.com/navigator/oauth2/token"
SCOPE = "mail_mailbox_w webmailer_setting_r webmailer_setting_w mail_confix_w"
CLIENT_ID = "mailcom_mailset_root_live"
CLIENT_SECRET = "*******"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"

# mail.com 单账号邮箱地址总数上限（含主账号，即可再加 9 个别名）。
ALIAS_LIMIT = 10

# mail.com 支持的免费域名（从 settings-cats /domains 接口真机拉取，138 个）。
MAILCOM_DOMAINS = [
    "mail.com", "2trom.com", "accountant.com", "acdcfan.com", "adexec.com",
    "africamail.com", "alumni.com", "angelic.com", "archaeologist.com",
    "arcticmail.com", "artlover.com", "asia.com", "atheist.com",
    "australiamail.com", "bartender.net", "berlin.com", "bikerider.com",
    "birdlover.com", "boardermail.com", "brazilmail.com", "brew-master.com",
    "bsdmail.com", "californiamail.com", "catlover.com", "cheerful.com",
    "chef.net", "chemist.com", "chinamail.com", "collector.org", "columnist.com",
    "comic.com", "consultant.com", "contractor.net", "counsellor.com",
    "cutey.com", "cyber-wizard.com", "cyberdude.com", "cybergal.com",
    "dallasmail.com", "dbzmail.com", "diplomats.com", "disciples.com",
    "discofan.com", "doglover.com", "doramail.com", "dr.com", "dublin.com",
    "dutchmail.com", "elvisfan.com", "email.com", "engineer.com",
    "englandmail.com", "europe.com", "europemail.com", "execs.com",
    "financier.com", "fireman.net", "galaxyhit.com", "gardener.com",
    "geologist.com", "germanymail.com", "graduate.org", "graphic-designer.com",
    "greenmail.net", "hackermail.com", "hairdresser.net", "hilarious.com",
    "hiphopfan.com", "iname.com", "innocent.com", "irelandmail.com",
    "israelmail.com", "italymail.com", "keromail.com", "kissfans.com",
    "kittymail.com", "koreamail.com", "legislator.com", "linuxmail.org",
    "lobbyist.com", "lovecat.com", "madonnafan.com", "marchmail.com",
    "metalfan.com", "mexicomail.com", "minister.com", "moscowmail.com",
    "munich.com", "musician.org", "muslim.com", "myself.com", "ninfan.com",
    "nonpartisan.com", "nycmail.com", "optician.com", "orthodontist.net",
    "pediatrician.com", "petlover.com", "photographer.net", "physicist.net",
    "polandmail.com", "politician.com", "post.com", "priest.com",
    "programmer.net", "protestant.com", "publicist.com", "ravemail.com",
    "realtyagent.com", "reborn.com", "reggaefan.com", "registerednurses.com",
    "reincarnate.com", "religious.com", "repairman.com", "safrica.com",
    "saintly.com", "sanfranmail.com", "scotlandmail.com", "secretary.net",
    "socialworker.net", "sociologist.com", "songwriter.net", "spainmail.com",
    "swedenmail.com", "swissmail.com", "teachers.org", "techie.com",
    "technologist.com", "theplate.com", "therapist.net", "toke.com",
    "toothfairy.com", "torontomail.com", "tvstar.com", "usa.com", "uymail.com",
    "webname.com",
]


class MailsetAliasClient:
    """封装 navigator 登录 + mailset 别名管理。支持会话序列化/恢复，避免重复登录。"""

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.session, self.sid = navigator_login(username, password)
        self.token_expires_at = 0.0
        self.token = self._get_token()

    def _get_token(self) -> str:
        basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        headers = {
            "accept": "*/*",
            "authorization": f"Basic {basic}",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://mailset-root.mail.com",
            "referer": "https://mailset-root.mail.com/",
            "user-agent": UA,
        }
        body = f"grant_type=urn:mam:oauth:grant-type:spa&scope={SCOPE}"
        resp = self.session.post(f"{TOKEN_URL}?sid={self.sid}", data=body, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"未拿到 access_token：{resp.text[:200]}")
        self.token_expires_at = time.time() + int(data.get("expires_in", 7200)) - 60
        return token

    def is_valid(self) -> bool:
        """token 尚未过期则视为会话有效。"""
        return bool(getattr(self, "token", "")) and time.time() < getattr(self, "token_expires_at", 0)

    def serialize(self) -> dict[str, Any]:
        """序列化会话（cookie + token + 凭据），敏感字段用 DPAPI 加密后落盘。"""
        from requests.utils import dict_from_cookiejar
        return {
            "username": self.username,
            "password": dpapi.encrypt_str(self.password),
            "token": dpapi.encrypt_str(self.token),
            "token_expires_at": self.token_expires_at,
            "cookies": dpapi.encrypt_str(json.dumps(dict_from_cookiejar(self.session.cookies))),
            "sid": self.sid,
            "encrypted": True,
        }

    @classmethod
    def restore(cls, data: dict[str, Any]) -> "MailsetAliasClient":
        """从序列化数据恢复会话，不重新登录。解密失败会抛异常，由调用方容错。"""
        from requests.utils import cookiejar_from_dict
        obj = cls.__new__(cls)
        obj.username = data.get("username", "")
        obj.password = dpapi.decrypt_str(data.get("password", ""))
        obj.token = dpapi.decrypt_str(data.get("token", ""))
        obj.session = requests.Session()
        obj.session.headers.update({"User-Agent": UA})
        cookies_dict: dict = {}
        cookies_raw = data.get("cookies", "")
        if cookies_raw:
            try:
                cookies_dict = json.loads(dpapi.decrypt_str(cookies_raw))
            except Exception:  # noqa: BLE001
                cookies_dict = {}
        obj.session.cookies = cookiejar_from_dict(cookies_dict)
        obj.sid = data.get("sid", "")
        obj.token_expires_at = data.get("token_expires_at", 0)
        return obj

    def _headers(self, accept: str, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "accept": accept,
            "authorization": f"Bearer {self.token}",
            "origin": "https://mailset-root.mail.com",
            "referer": "https://mailset-root.mail.com/",
            "x-ui-app": "mailcom.mailset-compose/1.0.8-build.377",
            "user-agent": UA,
        }
        if content_type:
            headers["content-type"] = content_type
        return headers

    def list_aliases(self) -> list[dict[str, Any]]:
        """返回账号下所有邮箱地址（含主账号），字段含 address/deletable/defaultSenderAddress。"""
        url = (
            f"{API_ROOT}/mailaccount/primary/emailAddresses"
            "?absoluteURI=false&q.state.in=ACTIVE&q.type.in=MANAGED,DOMAIN_HOSTING"
        )
        resp = self.session.get(
            url,
            headers=self._headers("application/vnd.ui.trinity.mailaddress.list-v5+json",
                                  "application/vnd.ui.trinity.mailaddress.list-v5+json"),
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json().get("mailaddresslist", [])

    def validate_alias(self, address: str) -> bool:
        """创建前校验别名地址是否可用，返回是否通过。"""
        url = f"{API_ROOT}/mailaccount/emailAddressValidations?absoluteURI=false"
        resp = self.session.post(
            url,
            data=f'["{address}"]'.encode("utf-8"),
            headers=self._headers(
                "application/vnd.ui.trinity.email-address-validation-response+json",
                "application/vnd.ui.trinity.email-address-validation-request+json",
            ),
            timeout=60,
        )
        resp.raise_for_status()
        # 校验通过通常返回 200 且 body 为空或列表
        return resp.status_code == 200

    def create_alias(self, address: str, display_name: str = "") -> dict[str, Any]:
        """新增一个别名地址。"""
        self.validate_alias(address)
        url = f"{API_ROOT}/mailaccount/primary/emailAddresses?absoluteURI=false"
        payload = {
            "address": address,
            "displayName": display_name,
            "deletable": True,
            "pgpEnabled": False,
            "defaultSenderAddress": False,
            "defaultReceiverAddress": False,
            "state": "ACTIVE",
        }
        import json as _json
        resp = self.session.post(
            url,
            data=_json.dumps(payload).encode("utf-8"),
            headers=self._headers(
                "application/vnd.ui.trinity.minimalmailaddress-v3+json",
                "application/vnd.ui.trinity.minimalmailaddress-v3+json",
            ),
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_alias(self, address: str) -> int:
        """删除一个别名地址，返回 HTTP 状态码。"""
        url = f"{API_ROOT}/mailaccount/primary/emailAddressesRemovals/{address}/removals?absoluteURI=false"
        resp = self.session.post(
            url,
            headers=self._headers("text/plain;charset=UTF-8", "text/plain;charset=UTF-8"),
            timeout=60,
        )
        resp.raise_for_status()
        return resp.status_code


def batch_create(client: MailsetAliasClient, base: str, domain: str, count: int,
                 display_name: str = "") -> list[str]:
    """批量创建别名，返回创建成功的完整地址列表。"""
    created = []
    existing = {a["address"] for a in client.list_aliases()}
    for index in range(1, count + 1):
        full = f"{base}{index}@{domain}"
        if full in existing:
            continue
        client.create_alias(full, display_name)
        created.append(full)
        time.sleep(1)
    return created


if __name__ == "__main__":
    import sys
    email, password = sys.argv[1], sys.argv[2]
    c = MailsetAliasClient(email, password)
    print("== 当前别名 ==")
    for a in c.list_aliases():
        print(f"  {a['address']:<32} deletable={a.get('deletable')} default={a.get('defaultSenderAddress')}")
