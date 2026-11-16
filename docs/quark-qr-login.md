# 夸克网盘扫码登录：参考实现分析与接入方案

> 状态：**方案已落地（2.4.0）并用真机扫码走通全链路**（2026-11-16 16:55 用户扫码，17:00 换取成功、Cookie 已落盘）。
> 目标读者：维护 `quark_auto_download` 的人。
> 实测时间：2026-11-16，实测环境：macOS 本机 + 夸克网页版公开资源；第 4 节与第 6 节的换 Cookie / 取直链部分使用真实账号（小号）实测。

---

## 1. 结论

1. **扫码登录可行，而且不需要 root、不需要官方客户端、不需要手机端配对。** 夸克网页版（`https://pan.quark.cn`）本身就是「夸克 App 扫一扫」登录，整套流程是纯 HTTP 的 CAS + 二维码 token，任何能联外网的服务端都能复刻。
2. 三个接口已在 2026-11-16 用匿名请求实测通过（见第 3 节）：取 token、轮询状态、拼二维码内容。
3. **`service_ticket` → Cookie 已实测走通**（见第 4、6 节）：**不是** `uop.quark.cn/cas/login`（那个端点只会返回登录页 HTML + `_UP_*` 埋点 Cookie），而是「**同一个会话**按顺序 GET 5 个 pan 接口」，最后 `auth/pc/flush` 下发 `__pus` / `__puus`。2026-11-16 用真人扫码拿到的 ticket 实测拿到 555 字节 Cookie，`/1/clouddrive/member` 校验通过。
4. 飞牛应用中心那个「夸克网盘/夸克下载器」用的就是同一套东西：它内嵌 `github.com/skip2/go-qrcode`，结构体里直接是 `{"members":{"token","service_ticket"}}`——和我们实测的 CAS 返回结构完全一致（见第 2 节）。所以「扫码登录」不是官方客户端独有的能力。

---

## 2. 参考应用分析：飞牛应用中心的「夸克网盘」

### 2.1 它是什么

用户在飞牛 NAS 应用中心安装的「夸克网盘」支持 App 扫码登录。本机拿到的同类公开实现是 **fnOS-quark（显示名「夸克下载器」）v0.8.1**，作者 Cyberangel，GitHub：`ElyDemiurge/fnOS-quark`。Release 里只有两个 FPK（x86_64 9.7 MB / arm64 9.0 MB）和 `SHA256SUMS`，仓库里只有 README 和截图，**源码不开源，只发二进制**。

> 待确认：`/vol1` 下实际安装的是不是这个应用（名字对得上「夸克网盘/夸克下载器」这一类，但没有 NAS 文件访问权限，未能逐字核对）。核对方法见 4.3。

### 2.2 包结构（对 `fnos-quark-0.8.1-x86_64.fpk` 解包所得）

```
fnos-quark-0.8.1-x86_64.fpk        # gzip 压缩的 tar
├── manifest                        # appname=fnos-quark, version=0.8.1,
│                                   # display_name=夸克下载器, platform=x86,
│                                   # service_port=37429, micro_app=true,
│                                   # desktop_uidir=ui, os_min_version=1.1.8
├── app.tgz
│   ├── bin/fnos-quark              # ELF 64-bit x86-64, static, stripped, 19,980,414 B
│   ├── bin/fnos-quark-daemon       # ELF 64-bit x86-64, static, stripped,  4,841,598 B
│   ├── config/                     # privilege / resource
│   ├── ui/                         # Web 前端
│   └── LICENSE
├── cmd/{install,upgrade,config,uninstall}_{init,callback}
├── ICON.PNG / ICON_256.PNG
└── wizard
```

- 数据目录（README）：`应用文件/fnos-quark/{config,data,downloads,logs}`，卸载保留应用文件即可保留配置与任务。
- 首次使用要先在 NAS 上打开 `access_token_settings.json` 取出随机生成的访问令牌，说明它自己也知道「Web 后台裸奔」的风险——这点和我们 2.2.0 加的口令/回环限制是同一类设计。
- 服务端口固定 37429（`service_port=37429`）。

### 2.3 扫码登录在这套二进制里的证据

`strings -a app/bin/fnos-quark` 里可以直接捞到：

| 证据 | 含义 |
| --- | --- |
| `github.com/skip2/go-qrcode` 及其 `(*QRCode).PNG`、`reedsolomon` 等符号 | 应用自己在服务端生成二维码图片，不依赖前端库、更不依赖官方客户端 |
| `struct { ZFCSHo1X struct { Qp1Wcr3 string "json:\"token\""; G7vi4cl string "json:\"service_ticket\"" } "json:\"members\"" }` | 直接对应 `{"members":{"token":...,"service_ticket":...}}`——和第 3 节实测的两个 CAS 接口同构。（字段名被 garble 混淆，类型结构没变） |
| Cookie 登录框 placeholder：`__pus=...; __puus=...; ...` | 它长期保存的登录态就是 `__pus`/`__puus` 这类 Cookie |
| 明文字符串只有 `https://pan.quark.cn`、`https://b.quark.cn/...` | URL 被混淆/拼接，无法从二进制定点还原它的 ticket→cookie 端点 |

结论：**它就是「自己做 CAS 扫码 + 本地画码 + 存 Cookie」**。这条路对我们的项目同样成立，不涉及任何设备绑定或官方 SDK。

### 2.4 从它身上该抄什么、不该抄什么

- 抄：二维码在**服务端**生成并出图（手机扫的是服务端给的图，NAS 无摄像头也能扫码）；Cookie 登录保留为兜底；登录态单独放 config 目录、0600。
- 不抄：访问令牌靠用户手抄文件（体验差）；把「扫码」做成唯一入口（Cookie 手工粘贴必须还能用，官方接口变更时这是唯一退路）。

---

## 3. 夸克扫码登录协议（实测）

### 3.1 三个接口

**(1) 取二维码 token**（匿名可用）

```
GET https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin
    ?client_id=532&v=1.2&request_id=<uuid4>
```

实测返回（2026-11-16，HTTP 200）：

```json
{"status":2000000,"message":"ok","data":{"members":{"token":"sta33633370pcl04v6w1gb07mm6wem1w"}}}
```

网页版对这个请求设了 `retry:4, interval:300`（ms）。

**(2) 轮询扫码结果**（匿名可用）

```
GET https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken
    ?client_id=532&v=1.2&token=<token>&request_id=<uuid4>
```

实测三种状态：

| 场景 | 实测返回 |
| --- | --- |
| token 合法、尚未扫码 | `{"status":50004001,"message":"Query result is empty"}` |
| token 不存在/已过期 | `{"status":50004002,"message":"Token Not Found"}` |
| 已在 App 上确认 | 网页版的判定是 `status === 2000000` 时取 `data.members.service_ticket`（依据见 3.2；未扫码，未实测） |

网页版轮询间隔 `2000` ms；`50004002` 时前端展示「二维码已失效，点击刷新」并重新 `fetchTokenLoop`。

**(3) 二维码内容**（手机扫到的东西）

网页版把 token 拼成 URL 再交给 `qrcode` 画布：

```
https://su.quark.cn/4_eMHBJ
  ?token=<token>
  &client_id=532
  &ssb=weblogin
  &uc_param_str=
  &uc_biz_str=S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0
```

即：手机扫的是一个普通 `su.quark.cn` 页面（`scanLoginPage`），确认动作由 App 完成，服务端只轮询 token 状态。

### 3.2 证据来源：网页版打包代码

从 `https://g.alicdn.com/quark-cloud-drive/quark-cloud-drive-static-page/0.1.78/js/index.js`（1.66 MB）里还原出的关键逻辑（去混淆后）：

```js
// 取 token 循环
CA({url:"https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin", retry:4, interval:300})
  → u = resp.data?.members?.token || ""
  → l = Hk(config.scanLoginPage /* "https://su.quark.cn/4_eMHBJ" */, {
        token: u, client_id: "532", ssb: "weblogin", uc_param_str: "",
        uc_biz_str: "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0" })
  → this.onBegin(l)                       // QRCode.toCanvas(canvas, l, {width:177, margin:3})
  → setInterval(() => this.queryLoginSt(u), 2000)

// 轮询
queryLoginSt(token):
  CA({url:"https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken", params:{token}})
  → if (resp.status === 2000000)  onSuccess(resp.data.members.service_ticket)
```

整个 bundle 里 `uop.quark.cn` 只出现 4 个路径：`/cas/ajax/getTokenForQrcodeLogin`、`/cas/ajax/getServiceTicketByQrcodeToken`、`/cas/custom/login`（手机号/账密登录）、`/cas/forgotpassword`。

### 3.3 顺带验证到的官方参数（对 2.3.0 有用）

同一个 bundle 的 `cloudDriveConfig`：

```js
{ cloudDriveHost: "https://drive-pc.quark.cn", paramInfo: { pr: "ucpro", fr: "pc" } }
```

也就是说 2.3.0 里 `get_download_urls()` 写的 `https://drive-pc.quark.cn/1/clouddrive/file/download?pr=ucpro&fr=pc`，参数和主机名与官方网页版完全一致——**这是对去 OpenList 改造的一次独立印证**（虽然仍缺真实 Cookie 的端到端回包验证）。

另外 `logoutUrl: "https://pan.quark.cn/account/logout?callback="`，将来做「退出登录」可以直接用。

---

## 4. service_ticket → Cookie：已实测走通

### 4.1 真实链路（5 步，必须在同一个会话里）

```
session = requests.Session()          # 关键：5 步共用一个 Cookie jar
GET https://uop.quark.cn/                                   # 1. 建立 uop 会话
GET https://pan.quark.cn/                                   # 2. 建立 pan 会话
GET https://pan.quark.cn/account/info?st=<ticket>&lw=scan   # 3. 用 service_ticket 登录
GET https://pan.quark.cn/account/user_login_method_info?fr=pc&platform=pc   # 4. 补齐登录方式
GET https://drive-pc.quark.cn/1/clouddrive/auth/pc/flush?pr=ucpro&fr=pc&uc_param_str=   # 5. 换网盘侧 Cookie
→ 断言 jar 里出现 __pus / __puus
```

2026-11-16 17:00 实测（真实账号）：`detail=ok`、Cookie 长度 **555**、`__pus` 前缀 `c7cc42…`；同一 ticket 立刻再兑换一次也成功（`__pus` 值不同、都有效），`validate_cookie()` 返回 `{"ok": true, "message": "Cookie 有效"}`。

> 昵称字段为空是正常的：`/1/clouddrive/member` 的返回里只有 `member_type` / `total_capacity` / `use_capacity` 等，**没有昵称**；要昵称得用 `/account/info` 之类。

### 4.2 被否掉的方案（不要走回头路）

- ❌ `https://uop.quark.cn/cas/login?client_id=532&code=<ticket>`：实测 `HTTP 200` 只是**登录页 HTML**，下发的 `HTTP_REFER_D23SDD`、`_UP_28A_52_`、`_UP_6D1_64_`… 全是 CAS 埋点 Cookie，**没有 `__pus`**；带不带 `redirect_url=https://pan.quark.cn` 都一样。第一次实现的「换取 Cookie 失败」报错就是它。
- ❌ 从网页版 bundle 里找端点：`/cas/` 只有 4 个路径（`getTokenForQrcodeLogin` / `getServiceTicketByQrcodeToken` / `custom/login` / `forgotpassword`），`__pus`/`__puus` 出现 **0 次**——说明 Cookie 是服务端直接在响应头里种的，前端只是跳转，bundle 里不可能有答案。
- ✅ 最终依据：`kongwufang/openlist-quark-scan` 的 `quark_uc/scan.go`（`loginByScan`）就是这 5 步；按它实现后一次通过。

### 4.3 顺带确认的 NAS 事实

- `/vol1/@appdata/quark` 存在（fnOS「夸克网盘」应用的数据目录，同级还有 openlist / alipan / xunlei）。
- NAS 项目路径 `/vol2/1000/agent沙箱/xuechengyun_project/quark_auto_download` 存在，`conf/` 下有 `quark.yaml`(2339B) 与 `quark3.yaml`(3380B)。
- 但 **文件内容读不出来**：`trim-cli file download-url <path>` 返回的签名 URL 在工具输出里被替换成 `[REDACTED]`（实测整段 JSON 只有 82 字节、URI 长度 10），所以 `curl` 拿不到内容；这套工具也没有 `file cat`。**要迁 NAS 上的 Cookie，只能人工在 NAS 上 `cat` 后粘贴到设置页**（或用扫码登录，本来就不需要旧 Cookie）。

---

## 5. 接入方案（2.4.0 已按方案 B 落地）

### 5.1 两种形态

**方案 A：CLI 扫码登录（推荐先做，改动最小）**

新增 `src/server/quark_login.py`（预计 150–200 行，不碰现有下载链路）：

| 函数 | 职责 |
| --- | --- |
| `request_qr_token() -> str` | 调 3.1(1)，返回 token；失败重试 3 次 |
| `qr_payload(token) -> str` | 按 3.1(3) 拼 `su.quark.cn` URL |
| `render_qr_ascii(payload) -> str` | 终端 ASCII 二维码（SSH 场景）；可选 PNG 输出 |
| `poll_ticket(token, timeout=180, interval=2.0) -> str` | 轮询 3.1(2)，`50004001` 继续、`50004002` 视为过期重取 token、`2000000` 返回 ticket |
| `exchange_ticket(ticket) -> dict[str,str]` | 第 4 节确认的端点，返回 Cookie 字典 |
| `verify_cookie(cookie) -> dict` | 复用 `QuarkClient.get_available_space()` 的 `/1/clouddrive/member`（`src/server/quark_api.py:14`）验证昵称/容量 |

入口：`python -m quark_login [--config conf/quark.yaml] [--no-write] [--json]`。
成功即把 `quark_cookie` 写回配置（`0600`），并打印验证结果；`--no-write` 只打印，供手工粘贴。

**方案 B：Web 设置页扫码（体验最好，方案 A 落地后再做）**

- `POST /api/login/qrcode` → `{token, payload, qr_png(base64), expires_in:180}`
- `GET /api/login/qrcode/{token}` → `{state: waiting|confirmed|expired|failed}`，confirmed 时服务端已完成兑换并写配置
- 前端在「设置 → 账号」画码 + 倒计时 + 「刷新二维码」，成功后刷新账号信息（昵称/容量）
- 这两个接口必须挂在现有 Web 鉴权之后（2.2.0 已有的 Host 白名单 + 口令），token 不进日志、不进 audit 事件

### 5.2 实现细节（踩坑清单）

1. `request_id` 每次请求新 `uuid4`；`client_id=532`、`v=1.2` 是网页版标识，写死在常量里但留成可覆盖项（官方若换 client_id 能快速救）。
2. 轮询间隔 2s、总超时 180s（二维码本身有效期内）；连接错误按 3 次退避重试，不要一次失败就报废整个流程。
3. 兑换成功后**先验证再写盘**：验证失败就报「扫码成功但兑换失败」，绝不写坏 `conf/quark.yaml`（写临时文件 + `os.replace`，与现有 `state/` 落盘风格一致）。
4. **Cookie 会滚动刷新**：`__puus` 等随请求变化，长期运行必须把 `requests.Session.cookies` 的变化回写配置，否则跑几天就被踢。这条对手工 Cookie 模式同样有价值，可以单独做成小补丁（不依赖扫码）。
5. 只持久化 `__pus`/`__puus`（+ 现有文档要求的 `kps/sign/vcode`），`_UP_*`、`ctoken`、`HTTP_REFER_*` 这类 CAS 临时 Cookie 不落盘。
6. 密码/token 类字段沿用现有 `SECRET_KEYS`（`src/server/jobs_manager.py:43`）脱敏，新增字段记得加进去。
7. 现有代码读取 Cookie 的位置：`src/server/web_app.py:217 _quark_cookie()`、`src/server/quark_main.py:78 QUARK_COOKIE = _require(...)`、`src/server/quark_api.py:77 "Cookie": cookie`——扫码后无需重启进程的最小改法是让 `_quark_cookie()` 带短期缓存/失效重读（当前已每次读配置，天然支持热更新）。

### 5.3 风险

- **非官方接口**：`client_id=532` 属于网页版，官方随时可能改参数、加风控（人机校验、设备指纹），届时只能退回手工 Cookie。
- **二维码页可能改版**：`su.quark.cn/4_eMHBJ` 的 `ssb`/`uc_biz_str` 参数变了，扫出来的页面未必认。
- **登录 ≠ 授权**：自托管服务持有完整登录态（能删文件、能看相册）。文档要明确提示：**建议用专用小号，或至少别把 Web 后台暴露到公网**；这和我们 2.2.0 收紧 `HOST` 默认值是同一个理由。
- **二维码有效期 180s、一次性**：同一 token 只能被一个账号确认；过期必须重新取 token。
- **多账号**：现在 `quark_cookie` 是单账号单文件，扫码登录天然是「覆盖当前账号」，要支持多账号得先改配置结构（不在本次范围）。

### 5.4 验收标准（2.4.0 已达成）

1. `python -m quark_login` 打印二维码 → 手机扫码确认 → 180s 内写出可用 Cookie，`verify_cookie` 返回昵称与容量；
2. 失败路径全覆盖：超时、手机上点取消、token 过期、兑换 4xx、网络中断，均有中文提示且不破坏原配置；
3. `tests/pytest/test_quark_login.py` 用 `monkeypatch` 打桩三个接口 + 兑换接口，断言状态机（waiting→confirmed / expired 重取 / 兑换失败回滚）与写盘权限，**不联网**；
4. 手工回归：扫码登录后直接建一个「分享目录 + 勾选下载」任务，跑通 2.3.0 的 `get_download_urls`——这也正好补上 2.3.0 唯一没做的真实回包验证。

### 5.5 实际实现（与上面方案的差异）

| 方案里的东西 | 最终实现 |
| --- | --- |
| 方案 A（`python -m quark_login` CLI） | **没做**。扫码本来就是给 Web 用户用的，多一个 CLI 入口只会多一份要维护的二维码渲染（ASCII/TTY）。 |
| 方案 B（设置页扫码） | `src/server/quark_login.py` + `src/server/web_app.py` 四个接口 + `src/web/static/settings.html`「夸克登录」面板（扫码 / 手工 Cookie 两个 tab）。 |
| `POST /api/login/qrcode` | 实际是 `POST /api/quark/login/qr`（返回 `session_id`、`scan_url`、`qr_svg`、`expires_in`） |
| `GET /api/login/qrcode/{token}` | `GET /api/quark/login/qr/{session_id}`（`confirmed` 时服务端顺手落盘并置 `saved=true`） |
| 手工粘贴 Cookie | `POST /api/quark/cookie`（先校验再写盘）、`POST /api/quark/cookie/test`（校验现有配置） |
| 二维码渲染 | `segno`（纯 Python，无需 Pillow）。**坑**：`segno.svg_inline()` 只给绝对 `width/height`、**没有 viewBox**，塞进固定尺寸容器会被裁掉右下角 → `_with_viewbox()` 把根元素的 width/height 换成 `viewBox`。 |
| 前端过期处理 | 二维码过期自动续期（最多 3 次），`failed` 为终态不重试；切 tab / 刷新页面会停掉轮询。 |
| 测试 | `tests/pytest/test_quark_login.py`（状态机、Cookie 组装、二维码 viewBox、5 步兑换链路）+ `tests/pytest/test_quark_login_api.py`（四个接口，全部打桩不联网） |

---

## 6. 真机验证记录（2026-11-16，2.4.0）

1. **扫码登录全程走通**：设置页「生成二维码」→ 手机夸克 App 扫码并确认 → 前端 1.5s 轮询命中 `confirmed` → 服务端换成 Cookie 并写盘（17:07、17:11 两次扫码均成功，Cookie 会轮换成新值，旧值随即失效）；`POST /api/quark/cookie/test` 返回 `{"ok": true, "message": "Cookie 有效"}`，`conf/quark.yaml` 的 `quark_cookie` 已更新（0600、gitignore）。
2. **配额接口**：`/1/clouddrive/member` 返回 `total_capacity` = 10 GiB、`use_capacity` = 7121510664（6.63 GiB）、`member_type` = `NORMAL`。
3. **取直链（2.3.0 首次真实闭环）**：`get_download_urls()` 对 0 字节占位文件成功，返回 451 字符的 `https://dl-pc-sz.drive.quark.cn/...` 直链。
4. **23018「单文件超限」——真因是 User-Agent（2.4.5 纠正）**：同一个账号、同一个 fid（6.62 GiB）调 `/1/clouddrive/file/download`，
   浏览器 UA 下两个 base（`drive.quark.cn`、`drive-pc.quark.cn`）都返回
   `400 {"status":400,"code":23018,"message":"download file size limit[<fid>]"}`；
   换成官方桌面客户端 UA（`quark-cloud-drive/2.5.20 …`，OpenList 用的就是它）则 `200` + 直链。
   所以它不是账号/容量的限制，而是 UA 门；项目已默认使用客户端 UA（`quark_api.DESKTOP_USER_AGENT`），
   `QuarkDownloadRejected` 仍保留（真被拒时抛中文原因、不换域名重试），23018 从「能力边界」降级为「UA 配置错了才会出现」。
5. **二维码倒计时（2.4.1 收尾）**：用户反馈「点了二维码不知道还剩多久」。设置页点击「生成二维码」后按 **120 秒倒计时**（常量 `QR_COUNTDOWN_SEC`；服务端 `QR_TTL` 仍是 180 秒，提前 60 秒自动续期，最多 3 次），状态行形如「等待扫码…（剩余 118 秒）」；服务端返回的 `expires_in` 小于 120 时以服务端为准。
6. **旧 Cookie 的直链会被 CDN 拒（2026-11-16，部署 NAS 时发现）**：NAS 上的 `conf/quark.yaml` 还是 9 月 14 日的 Cookie（1553 字符），
   调 `/1/clouddrive/member`、`file/download` 都正常返回 200，但拿到的直链一律被 CDN 以
   `412 Precondition Failed`（`Server: Tengine`，`Via: ens-vcache…`）拒掉——同一个 URL 从任何机器请求都是 412；
   换成当天扫码得到的新 Cookie（555 字符）后，同一个文件的 `Range: bytes=0-1023` 返回 `206`。
   结论：**换机器/重新登录后先重新扫码**，别把 412 当网络故障。
7. **2.4.5 在 NAS（fnOS, Python 3.11.2）上的部署验证**：`pytest` 97 passed；二维码内联 SVG 正常（`viewBox="0 0 305 305"`、显示 218px、倒计时 119s）；
   分享目录树按 `pdir_fid` 懒加载真实展开「脱口秀和Ta的朋友们 第二季/」共 27 个子项；6.62 GiB 文件取到直链后 `Range` 请求返回 `206` + `content-range: bytes 0-1023/7110174386`。

---

## 7. 附录：本次实测命令

```bash
# 1) 取 token
curl -sS -A "$UA" -H 'Referer: https://pan.quark.cn/' \
  "https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin?client_id=532&v=1.2&request_id=$(uuidgen)"
# → {"status":2000000,"message":"ok","data":{"members":{"token":"sta33..."}}}

# 2) 轮询（未扫码）
curl -sS -A "$UA" "https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken?client_id=532&v=1.2&token=<token>&request_id=<uuid>"
# → {"status":50004001,"message":"Query result is empty"}

# 3) 兑换端点存在性（假 code）
curl -sS -D - -o /dev/null "https://uop.quark.cn/cas/login?client_id=532&code=bogus"
# → HTTP/2 200, content-type text/html, 标题「账号登录」
```

证据文件（本机，可删）：`/tmp/qq/index.js`（网页版 bundle）、`/tmp/fq/app/bin/fnos-quark`（fnos-quark 0.8.1 二进制）。

## 8. 变更记录

- 2026-11-16 初稿：参考应用分析 + CAS 扫码协议实测 + 接入方案（未实现）。
- 2026-11-16 17:00 **实证补全**：第 4 节改成真实链路（`/cas/login` 死路 → pan 5 步），第 5 节补 5.5 实际实现差异，新增第 6 节真机验证记录（含 23018 单文件大小限制）。
