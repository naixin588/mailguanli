# mailguanli · mail.com 邮件与别名批量管理工具

一个面向 [mail.com](https://www.mail.com) 的本地 Web 管理工具。mail.com 免费账号不开放 IMAP/POP3，无法用传统方式批量收信，本项目用 Python 直接模拟其网页邮箱流程（登录、收件箱、邮件详情、完整头部解析），提供浏览器 WebUI 与本地 HTTP API，支持批量账号管理、按别名精准取验证码、别名增删查导出等能力。

> ⚠️ 仅供个人账号自动化与授权用途。请遵守 mail.com 服务条款，勿用于批量注册、垃圾邮件等违规场景。

## 功能特性

- **批量账号管理**：`邮箱----密码` 每行一个批量导入，并发收取，自动拉新邮件
- **收件人识别**：解析邮件完整头部，精准区分每封邮件是发给哪个别名
- **验证码精准查询**：按 邮箱 + 关键词 + 正则 + 收件别名（`to`）过滤，命中返回验证码、未命中返回 `NullX`
- **别名管理**：自动识别账号下别名数量，随机 7 字符批量创建（撞名自动加数字），批量删除，批量导出 CSV
- **多域名支持**：内置 mail.com 全部 138 个免费域名（`mail.com`/`email.com`/`usa.com`/`dr.com` 等），下拉选择
- **别名上限感知**：自动识别单账号 10 个地址上限（含主账号），满额即提示
- **会话持久化**：登录会话用 Windows DPAPI 加密落盘，token 过期自动重登，重启免重新登录

## 环境要求

- Python 3.10+
- 依赖仅 `requests`（见 `requirements.txt`）

## 快速开始

```powershell
pip install -r requirements.txt
python webui.py
```

浏览器打开 `http://127.0.0.1:8913/`，左侧粘贴账号（`邮箱----密码`）导入后即可使用。

## HTTP API

### 验证码查询

```text
# 查已缓存邮件（按收件别名过滤）
http://127.0.0.1:8913/search-mail?email=主账号@mail.com&to=别名@mail.com&keyword=ChatGPT&regex=\d{6}

# 实时登录查询（带密码，同样支持 to）
http://127.0.0.1:8913/search-mail-live?email=主账号@mail.com&password=xxx&keyword=ChatGPT&regex=\d{6}&to=别名@mail.com
```

参数：`email`（主账号）、`to`（可选，收件别名）、`keyword`（关键词）、`regex`（正则）。命中返回字符串，未命中返回 `NullX`。

### 其它接口

| 接口 | 方法 | 作用 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/api/status` | GET | 账号列表与收取状态 |
| `/api/accounts` | POST / DELETE | 批量导入 / 删除账号 |
| `/api/fetch` | POST | 触发批量收取 |
| `/api/messages?email=` | GET | 某账号邮件列表（含收件人 to） |
| `/api/preview?email=&index=` | GET | 单封邮件 HTML 预览 |
| `/api/alias/domains` | GET | 可用域名列表（138 个） |
| `/api/alias/list` | POST | 列出某账号别名（含剩余名额） |
| `/api/alias/create` | POST | 为单账号批量创建随机别名 |
| `/api/alias/create-batch` | POST | 为多个账号批量创建别名 |
| `/api/alias/delete` | POST | 批量删除别名 |
| `/api/alias/export` | GET | 导出所有账号别名 CSV |

## 文件结构

| 文件 | 作用 |
|------|------|
| `engine.py` | 核心引擎：登录 / 收取 / 邮件解析 / 完整头部解析 / 搜索（无 GUI） |
| `webui.py` | WebUI 后端（HTTP API + 别名管理 + 会话缓存 + 加密持久化） |
| `webui.html` | WebUI 前端（单文件，无构建依赖） |
| `mailset_alias.py` | 别名增删查客户端（settings-cats API + 域名列表） |
| `navigator_login.py` | mail.com 经典版（navigator）登录，获取 sid |
| `dpapi.py` | Windows DPAPI 加密工具（无需管理密钥） |
| `dump_header.py` | 邮件头部调试工具 |

## 工作原理

1. **登录**：`www.mail.com` 登录表单 → `login.mail.com` → navigator 经典版 `/?sid=...`
2. **换 token**：`oauthbridge.navigator-lxa.mail.com` 用 sid 换 OAuth2 access_token（scope 含 `webmailer_setting_r/w`）
3. **邮件**：`lightmailer.mail.com` 模拟网页邮箱流程，额外请求 `/messagedetail/headers` 取完整头部（收件人 `To`）
4. **别名**：`settings-cats.mail.com` 的 `mailaccount` API 增删查

## 安全说明

- 账号密码与登录 token 用 Windows DPAPI 加密后存于本地 `alias_sessions.json`，绑定当前机器 + Windows 用户，换机/换用户无法解密
- 该服务默认只监听 `127.0.0.1`，仅本机可访问；如需部署到服务器，请自行加 API Key 鉴权与 HTTPS

## 免责声明

本工具仅供学习研究与个人授权场景使用，使用者需自行确保遵守 mail.com 服务条款及当地法律法规。
