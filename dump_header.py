"""调试工具：登录后抓取第一封邮件的详情页，打印头部解析结果。

用途：确认 mail.com 邮件详情页里「收件人/To」到底长什么样，用来校准
engine.py 里的 _extract_recipient 选择器。

用法：
    python dump_header.py --email main@mail.com --password xxx [--count 3]
"""

from __future__ import annotations

import argparse
import re
import sys
from urllib.parse import urljoin

import requests

from engine import (
    BasicHtmlParser,
    MailAccount,
    MailComHttpReader,
    USER_AGENT,
    normalize_text,
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="dump mail.com 邮件头部")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args(argv)

    account = MailAccount(args.email.strip(), args.password.strip())
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    reader = MailComHttpReader(status=[])
    print(f"登录：{account.address}")
    folder_url, folder_html = reader._login(session, account)  # noqa: SLF001
    inbox_url = reader._find_inbox_url(folder_url, folder_html)  # noqa: SLF001
    print(f"收件箱：{inbox_url}")

    listing = session.get(inbox_url, timeout=60)
    listing.raise_for_status()
    detail_urls = re.findall(r'href="(\./messagedetail\?[^"]+)"', listing.text)
    print(f"邮件详情链接 {len(detail_urls)} 个\n")

    for index, detail_url in enumerate(detail_urls[: args.count], start=1):
        detail = session.get(urljoin(listing.url, detail_url), timeout=60)
        detail.raise_for_status()

        bp = BasicHtmlParser()
        bp.feed(detail.text)
        tokens = bp.text

        print("=" * 70)
        print(f"第 {index} 封  URL: {detail_url}")
        print("-" * 70)
        print("标题解析：", re.search(r"<title[^>]*>(.*?)</title>", detail.text, re.I | re.S))
        print("tokens（前 60 个）：")
        for token in tokens[:60]:
            print("   |", repr(token))
        print("-" * 70)
        print("包含 To / Delivered / Envelope 的 HTML 片段：")
        for line in detail.text.splitlines():
            if re.search(r"(Delivered-To|X-Envelope-To|Envelope-To|\bTo:)", line, re.I):
                print("   >", line.strip()[:200])
        print("-" * 70)
        print("最终解析 -> from:", reader._extract_detail_metadata(detail.text)["from"])  # noqa: SLF001
        print("最终解析 -> to  :", reader._extract_detail_metadata(detail.text)["to"])  # noqa: SLF001
        print("最终解析 -> subject:", reader._extract_detail_metadata(detail.text)["subject"])  # noqa: SLF001

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
