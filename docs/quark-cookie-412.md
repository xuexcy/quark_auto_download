# 夸克直链 412 与防风控 Cookie（web-grey-id）

> 2026-11-19 现场定位。现象：任务的转存、接口调用全部正常，`/1/clouddrive/member` 也说 Cookie 有效，
> 但每个文件提交给 Aria2 后都被 CDN 回 `412 Precondition Failed`，速度恒为 0。
> 结论：**Cookie 里少了 `web-grey-id` / `web-grey-id.sig` 这两条防风控 Cookie**，接口不校验、直链 CDN 校验。

## 1. 一句话结论

```
Cookie 有效（member 200）  ≠  直链能下（CDN 200）
```

`POST /1/clouddrive/file/download` 只校验登录态（有 `__pus/__puus` 就 200，返回 525 字符直链），
真正拉文件时 CDN 还要校验会话的「防风控」字段。缺 `web-grey-id` 时：

```
GET <直链> → HTTP 412 Precondition Failed（Server: openresty，Content-Length 572）
```

## 2. 现场证据

| 项 | Mac（扫码得到的 555 字符 Cookie） | NAS（同一账号、同一 Cookie） |
| --- | --- | --- |
| 出口 IP | 144.34.245.144 | 144.34.245.144（AS25820 IT7 Networks，Los Angeles） |
| `/1/clouddrive/member` | 200，acc_status=0，NORMAL | 200，同样的 use_capacity |
| `file/download` | 200，url_len 525 | 200，url_len 525 |
| 直链 GET | 先补齐 Cookie：**200 / 2,485,593,063 字节**；不补：412 | 同左（补齐后 200） |

决定性实验（NAS 上 `./py_venv/bin/python3` + `requests`，Cookie 逐条 `set` 进 session jar，`domain=.quark.cn`）：

1. `GET https://pan.quark.cn/` → 200，响应 Set-Cookie 新增 **web-grey-id / web-grey-id.sig**；
2. `POST https://drive-pc.quark.cn/1/clouddrive/file/download?pr=ucpro&fr=pc` body `{"fids":["…"]}` → 200，url_len 525；
3. 用同一个 jar 的完整 Cookie + Referer 请求直链 → **200，Content-Length 2,485,593,063**。

同一份 Cookie，只要不走第 1 步（不拿 web-grey-id），第 3 步必然 412。

### 2.1 为什么之前误判成「账号被风控」

- 探针把 Cookie 写成 `session.headers["Cookie"] = <原始串>`，又让 session 自己拿到了 grey 两条；
  `requests` 的 `prepare_cookies` 会用 jar 覆盖 Cookie 头 —— 实际只发了 `web-grey-id/…/__puus/ctoken` 四条，
  于是怎么试都 412。（教训：Cookie 要么全在 jar 里，要么全在头里，别混用。）
- 当时还怀疑过出口 IP：实测 Mac 与 NAS 出口 IP 完全相同，排除。

### 2.2 为什么 OpenList 一直能下

NAS 上 OpenList 的夸克存储用的是浏览器里复制的 1553 字符 Cookie，自带
`b-user-id / __sdid / isg / tfstk / _c_WBKFRo / xlly_s / web-grey-id / web-grey-id.sig` 等一整套；
扫码（CAS 五步链路）拿到的 555 字符 Cookie 只有 `__kp/__kps/__ktd/__pus/__puus/__uid/ctoken`。

混合实验（扫码 Cookie + 补 OpenList 独有的字段）仍然 412 —— 这些字段与会话绑定，**不能跨会话拼**，
只能由「自己这个会话」去网页上过一遍拿新的。

## 3. 修复

### 3.1 `src/server/quark_login.py`：`warm_cookie()`

```python
COOKIE_WARM_STEPS = (
    "https://pan.quark.cn/",
    "https://pan.quark.cn/list",
    "https://drive-pc.quark.cn/",
)
```

- 把 Cookie 头解析成 `name -> value`，逐条 `session.cookies.set(name, value, domain=".quark.cn")`；
- 依次 GET 上面三个地址（异常 continue，绝不让补 Cookie 影响主流程）；
- 收集 jar 里 `quark.cn` 域、非 `_UP_*` 的 Cookie 覆盖回 pairs；
- 按固定优先级拼回：`__pus, __puus, __kp, __uid, kps, sign, vcode, web-grey-id, web-grey-id.sig`；
- 任何异常都返回原串。

调用点：

| 位置 | 作用 |
| --- | --- |
| `_exchange_ticket()` 成功分支 | 扫码刚换到 `__pus` 就先补齐，落盘的就是能下的 Cookie |
| `POST /api/quark/cookie`（`_save_quark_cookie`） | 手工粘贴旧 Cookie 时也补齐，避免「保存成功但下载 412」 |
| `check_cookie_health()` | 体检前先补 |
| `src/server/quark_main.py:init_clients()` | 子进程启动时再补一次（老的 conf/quark.yaml 也能自愈），日志写「已补齐防风控 Cookie」 |

### 3.2 三色体检 `check_cookie_health()`

设置页「测试当前 Cookie」不再只看 `/1/clouddrive/member`，而是把整条链路走一遍：

1. `warm_cookie` → `validate_cookie`（member 200）；
2. 在网盘里广度优先找一个普通文件当样本（最多 12 个目录 / 3 层，兜住「文件都埋在子目录里」）；
3. `get_download_urls([fid])` 换直链；
4. `GET <直链>`，带 `Range: bytes=0-0` + `stream=True`（只取 1 字节，随即 close，不会真把文件拖下来）。

| 结果 | 含义 | 界面 |
| --- | --- | --- |
| green | Cookie 有效且直链能下 | 绿 |
| yellow | 接口通、直链被 CDN 拒绝（401/403/412）或找不到样本 | 黄，文案点明缺 `web-grey-id` |
| red | Cookie 无效 / 网络不通 | 红 |

返回字段：`level / ok / message / nickname / cdn_status / sample`。

### 3.3 其它配套

- `src/server/aria2_api.py:describe_task_error()`：`412` 追加一句「直链被夸克 CDN 拒绝，通常是 Cookie 缺少防风控字段
  （web-grey-id / web-grey-id.sig），到设置页重新保存 Cookie（会自动补齐）或重新扫码后再试」。
  412（`errorCode=22`）本来就判永久错误，不会被重试 10 次。
- `src/web/static/settings.html`：保存 / 测试结果按 `level` 上色，黄灯用 `.msg.warn`。

## 4. 怎么自己验证

```bash
# 1. 设置页点「测试当前 Cookie」，应显示绿色「Cookie 有效，直链可下载」
curl -s -X POST http://<host>:8787/api/quark/cookie/test -H 'Origin: http://<host>:8787'
# → {"level":"green","ok":true,"cdn_status":206,"sample":"<文件名>"}

# 2. 看落盘 Cookie 里有没有防风控字段（不要打印整串）
grep -c 'web-grey-id' conf/quark.yaml     # ≥1 即已补齐
```

## 5. 仍未覆盖

- **Cookie 有效期**：补 Cookie 只是让「当前会话」能下，`web-grey-id` 本身也会过期；过期后的表现仍是 412，
  届时重新扫码即可（扫码链路的 `_exchange_ticket` 已经内置 warm）。
- **412 之外的风控**：如果哪天补齐了 grey 仍然 412，说明是账号级限制，本仓库层面只能提示，不能绕过。
- **配额**：`32003 capacity limit` 是另一条线（10 GiB 配额被「转存一个 → 下载 → 删云端副本」串行推进占用），
  与本篇无关，见 README「注意事项」。

## 6. 补充（2026-11-20 00:10）：扫码 Cookie 被拒、浏览器 Cookie 可用

补齐 `web-grey-id` 之后仍有 412，于是拿 OpenList 里那条 Cookie 做对照（同一账号、同一台 NAS、同一出口 IP、同一 UA）：

| Cookie 来源 | 长度 | warm 后 | 199 4K.mp4（2.4 GB） | 200 4K.mp4（2.3 GB） |
| --- | --- | --- | --- | --- |
| OpenList「薛成韵-夸克网盘」 | 1553 | 1711 | **206**（raw / warmed 都 206） | **206** |
| 本应用扫码登录（CAS 五步） | 555 | 666 | 412 | 412 |

两者 `/1/clouddrive/member` 都是 200、`use_capacity` 完全一致；E03.mp4 这种 42 字节小文件用扫码 Cookie 也是 412。

Cookie 字段差集：

```
扫码独有：ctoken
浏览器独有：__sdid、b-user-id、isg、tfstk、_c_WBKFRo、xlly_s（另有 UM_distinctid、Video-Auth 等）
共有：__pus、__puus、__kp、__kps、__ktd、__uid、web-grey-id、web-grey-id.sig
```

`isg` / `xlly_s` / `_c_WBKFRo` 是前端 JS 过 WAF 之后才下发的，服务端伪造不出来；
把它们硬拼到扫码 Cookie 上仍然是 412（会话绑定）。

**结论**：直链 CDN 认的是「浏览器会话指纹」，CAS 扫码会话拿不到这套字段。
所以：

- 扫码登录 → 接口（列表、转存、删除）都能用，**但直链可能被 412 拒**；
- 真要下载，请从浏览器里复制一份完整 Cookie（设置页 → 手工 Cookie）粘贴保存；
- 设置页「测试当前 Cookie」会把这种状态显示成黄灯「接口可用，但直链被 CDN 拒绝（HTTP 412）」。

2026-11-20 00:08 已把 NAS 上的 `conf/quark.yaml` 换成 OpenList 那条 Cookie（备份在
`.agent/agent_bak/quark.yaml.before_openlist_20261120_000854`），保存后体检立刻变绿
（`cdn_status=206`，样本 E03.mp4）。

设置页行为（2.8.23 起）：扫码登录成功、Cookie 落盘之后，服务端会顺手跑一次上面的体检，
把 `level` / `cdn_status` / `message` 一起回给页面；只要不是绿灯，页面就用红字提示
「只能调接口、不能下文件，请改用『手工 Cookie』粘贴浏览器里那份完整 Cookie」。
