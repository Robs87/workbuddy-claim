# workbuddy-claim

WorkBuddy「Buddy 加油站」每日签到的无人值守容器。镜像由 **GitHub Actions 构建并推送到 ghcr.io**，Unraid 只负责拉取运行，不在本地构建。

```
GitHub Actions ──build──▶ ghcr.io/<owner>/workbuddy-claim:latest
                                      │ docker pull（DockerMan 模板）
                                      ▼
                        Unraid: workbuddy-claim 容器
                                      │ 每天 08:00 (Asia/Shanghai)
                                      ▼
                    POST www.codebuddy.cn/v2/billing/meter/daily-checkin
```

## 先读这一段

> **当前实例已部署**：Unraid `Tower`（192.168.31.190）上的容器 `workbuddy-claim`，镜像 `ghcr.io/robs87/workbuddy-claim:latest`，DockerMan 托管（`net.unraid.docker.managed=dockerman`），已加入开机自启，每日 09:00（Asia/Shanghai）签到。**唯一待补**：模板里的 Access Token（见第一节，令牌本体是 `eyJ` 开头的长串）。

- 签到走的是 WorkBuddy 客户端自己用的官方接口，鉴权就是你的登录态，**没有绕过任何校验**，只是把一次手工点击换成一次 HTTP 请求。
- 接口对重复调用是幂等的：已签到返回 `code=10001`，不会多领，也不会扣分。
- 官方口径是"人工签到"，且平台对脚本签到有风控。**用不用、用多久，请自行判断**；本仓库不保证账号策略变化后仍然可用。真出问题时撤销方式见文末。
- 令牌等同登录态，只存在于 Unraid 的模板变量（权限 600）里，代码、日志、状态文件都不会记录它。

## 一、拿到 accessToken

> **别搞混这两个东西**：本节会出现一段 **JS 脚本**，它要在浏览器 Console 里**运行**；而 Unraid 的 Access Token 框要填的是它**跑出来**的那一行令牌（以 `eyJ` 开头，200–400 字符）。
> 把脚本整段贴进令牌框会立刻报 `TOKEN_INVALID | reason=看起来是 JS 代码而不是令牌`（1.2.0 及更早版本会抛更晦涩的 `UnicodeEncodeError: latin-1`）。

请打开**控制台页面**而不是官网首页：<https://www.codebuddy.cn/profile/plan> 。官网首页的请求全发给 CDN 静态资源（`download.codebuddy.cn`），永远看不到鉴权头。

### 方式 A：Network 面板（最稳，推荐）

1. 在该页面按 F12 → **Network** → 选中 **Fetch/XHR**；
2. 勾上 **Preserve log**，再按 Cmd/Ctrl+R 刷新；
3. Filter 框输入 `billing`（**不要**输入 `v2/billing`——网页端路径没有 `v2` 前缀，`/v2` 是桌面客户端用的）；
4. 点开 `get-user-resource-summary` 或 `checkin` 类请求 → **Headers** → **Request Headers** → 复制 `authorization:` 后面那一整串（`Bearer eyJ...`，很长，别截断）；
5. 贴进 Unraid → Docker → `workbuddy-claim` → **Edit** → **Access Token** → **Apply**。

### 方式 B：Console 钩子（少点几下，但别刷新）

在该页面按 F12 → **Console** → 粘贴下面这段 → 回车：

```js
(()=>{const show=t=>{if(t){console.log('%cTOKEN','color:#0f0;font-weight:700',t);try{copy(t);console.log('已复制到剪贴板')}catch(e){}}};
const f=window.fetch;window.fetch=function(...a){try{const h=a[1]&&a[1].headers;if(h){const v=h.get?h.get('Authorization'):(h.Authorization||h.authorization);show(v)}}catch(e){}return f.apply(this,a)};
const s=XMLHttpRequest.prototype.setRequestHeader;XMLHttpRequest.prototype.setRequestHeader=function(k,v){try{if(/^authorization$/i.test(k))show(v)}catch(e){}return s.apply(this,arguments)};
console.log('钩子已就位，刷新页面');})()
```

装好后**不要整页刷新**（刷新会清掉钩子，得重来）。直接在页面里点一下「签到」按钮、或点左侧菜单切个页（前端路由不算刷新），控制台就会打出绿色 `TOKEN` 并自动复制到剪贴板。复制到的值形如 `Bearer eyJhbGciOi...`。

贴进模板时带不带 `Bearer ` 前缀都能识别（脚本会自动去掉前缀、引号和换行）。

令牌有效期约 **30 天**（`expiresIn=2592000`）。桌面客户端会自己续期，但容器拿不到刷新动作，所以**到期后要重新复制一次**填回 Unraid。

> 桌面客户端的 `workbuddy-desktop.info` 与浏览器的登录态都是信封加密的（密文带 `v2:` 前缀，密钥在 IndexedDB 内且不可导出），脚本解不出来，也不该去解——请走上面两条正道。

## 二、建仓并推送

```bash
cd workbuddy-claim
git init -b main
git add -A
git commit -m "feat: WorkBuddy daily check-in container"
git remote add origin git@github.com:<你的账号>/workbuddy-claim.git
git push -u origin main
```

推送后 Actions 自动构建。**仓库名建议全小写**（镜像名不能有大写字母，workflow 里已做小写化处理，但少一层转换更省心）。

构建完成后镜像地址：

```
ghcr.io/robs87/workbuddy-claim:latest
```

## 三、包可见性

GHCR 的包**不会随仓库转公开而自动公开**：包在首次发布时继承仓库可见性，之后再改仓库可见性不会回溯。

当前状态：仓库 public、包 private，但 Unraid 的 `/root/.docker/config.json` 里已有可用的 ghcr 凭据，所以拉取正常。

若想让拉取完全不依赖任何凭据（换机、撤销令牌后也不受影响），去改一次即可：

GitHub → 你的头像 → **Your profile** → **Packages** → `workbuddy-claim` → **Package settings** → **Danger Zone** → **Change visibility** → Public。

> 这一步无法用 API 完成：`PATCH /user/packages/...` 对用户级包返回 404，删除包版本需要 `delete:packages` scope。

## 四、部署到 Unraid

前提：容器必须由 DockerMan 托管（`net.unraid.docker.managed=dockerman`），不要用裸 `docker run`。

### 方式 A：WebUI 导入模板（推荐，后续可视化改参数）

1. 把 `dockerman/my-workbuddy-claim.xml` 上传到：
   `/boot/config/plugins/dockerMan/templates-user/my-workbuddy-claim.xml`
   （仓库里已经是 `ghcr.io/robs87/...`，无需再替换占位符）
2. Unraid → **Docker** → **Add Container** → 模板选 `workbuddy-claim`；
3. 填 **Access Token**（第一节复制的值），其余默认；
4. **Apply**。

### 方式 B：一条命令（我用的方式）

把 `deploy/` 与 `dockerman/` 一起传到 Unraid（例如 `/boot/config/workbuddy-claim/`），然后：

```bash
cd /boot/config/workbuddy-claim
WB_TOKEN='粘贴令牌' bash deploy/install-on-unraid.sh <你的GitHub账号>
```

脚本会依次：备份旧模板与容器基线 → 建 appdata 并 `chown 1000:1000` → 渲染模板（权限 600）→ `docker pull` 先暴露权限/网络问题 → 通过 DockerMan 的 `xmlToCommand()` 建房 → 校验 dockerman 标签并打印日志。

已存在同名容器时加 `--recreate`。

## 五、验证

```bash
# 配置自检（不打印令牌，只报长度）
docker exec workbuddy-claim python3 /app/claim.py --print-config

# 立刻补签一次
docker exec workbuddy-claim python3 /app/claim.py --once

# 看日志
docker logs --tail 50 workbuddy-claim
```

成功长这样：

```
[2026-10-29 08:00:03] CHECKIN_OK | base=https://www.codebuddy.cn msg=OK today_credit=100 total_credits=2951 streak_days=1
```

当天已领过：

```
[2026-10-29 08:00:03] ALREADY_CHECKED_IN | msg=今天已签到，请明天再来 total_credits=2951 streak_days=1
```

状态机在 `/mnt/user/appdata/workbuddy-claim/state.json`，含最近 90 天历史；`last_success` 是健康检查用的心跳文件，超过 36 小时未更新容器会被 Docker 标记为 `unhealthy`。

## 六、CookieCloud 自动续期：已验证不可行（附证据）

结论先写在这里，避免重复投入：**CookieCloud 供不上签到所需的凭据，这不是配置问题，是原理问题。**

### 6.1 三次对照实验：网关只认 Bearer

在 2026-10-28 用同一时刻、同一网关做对照（cookie 取自 CookieCloud 解密的全量数据）：

| 请求 | 不带凭据 | 带全套 cookie |
|---|---|---|
| `POST /billing/meter/daily-checkin` | nginx 401 | nginx 401（响应体一字不差） |
| `POST /v2/billing/meter/daily-checkin` | nginx 401 | nginx 401 |
| `POST /v2/billing/meter/check-gift-claimed`（不存在的路由） | **应用层 404 JSON** | 应用层 404 JSON |

判据是最后一行：一条不存在的路由能穿透 nginx 打到后端并返回 JSON，说明网络、路径、DNS 全都通。**401 是鉴权层对「没有 `Authorization: Bearer`」的判定**，与 cookie 无关。

### 6.2 浏览器里也取不到那个 Bearer

| 检查项 | 结果 |
|---|---|
| `local_storage_data`（已开启关键词同步后） | `www.codebuddy.cn` 67 个键 |
| 其中 JWT 形态（`eyJ...`） | **0 个** |
| 键的构成 | `AEGIS_ID`、约 40 个 `__BEACON_*` 埋点、`loggedIn`、`i18nextLng`、`CODEBUDDY_IDE_SELECTED_ACCOUNT_ID` |
| 唯一像凭据的值 | `9c412d60d29118e0` = `v2:` 开头、403 字节 → **信封密文** |

网页端前端（`assets.workbuddy.cn/.../config.js`）的响应拦截器在 401 后会调用 `session.renew()` 再重放请求——它用的是 **OAuth/Keycloak 会话 + 短期 access token**，运行时换票，令牌从不落盘明文。解密密钥是 IndexedDB 里的不可导出 WebCrypto key，**任何同步机制都拿不到**。

顺带一个和直觉相反的细节：网页端 `baseURL` 是空字符串（同源），路径是 `/billing/meter/...` **没有 `v2`**；`/v2` 前缀是桌面客户端用的。两者并存，别混用。

### 6.3 所以现在的做法

`WB_TOKEN` 是唯一被接口接受的凭据形态，优先级为：**手填 `WB_TOKEN` → CookieCloud（兜底）**。有令牌时连 CookieCloud 都不会被请求，避免每天先撞一次必然失败的 401。

CookieCloud 相关四个参数（`CC_URL`/`CC_UUID`/`CC_PASSWORD`/`CC_DOMAIN`）在模板里降级为 advanced，仅保留诊断价值：

```bash
docker exec workbuddy-claim python3 /app/claim.py --cc-probe
```

该命令只打印结构与键名，绝不输出凭据值。

### 6.4 如果将来一定要全自动

唯一剩下的路径是**在容器里跑无头浏览器**：用 CookieCloud 的 cookie 打开 `https://www.codebuddy.cn/profile/plan`，让页面自己走 SSO 换票，截下 `Authorization` 头再调签到接口。可行性依赖浏览器侧登录态，实测 cookie 寿命：

| cookie | 过期 |
|---|---|
| `KEYCLOAK_SESSION` | 2027-10-16（约一年） |
| `session` / `session_2` | 2026-11-04（约 6 天，随访问刷新） |

代价：镜像从 ~60MB 涨到 ~1.6GB，失败模式变成「浏览器侧登出」。当前选择不做，记录在此备用。

## 七、常见问题

| 现象 | 原因与处理 |
|---|---|
| `docker pull` 报 `unauthorized` | 包还是私有的，见第三节；或本地 ghcr 凭据失效 |
| `AUTH_FAILED ... HTTP 401` | 令牌过期/复制不全（必须含 `Bearer ` 之后全部字符） |
| `CC_FALLBACK` | 没填 `WB_TOKEN`，正在用 CookieCloud 兜底；该路径已证明无法通过鉴权，去填令牌即可 |
| `unexpected code=xxx` | 上游业务码变化，看日志里的 `msg`；接口路径可能改了，用 `WB_CLAIM_PATH` 覆盖，无需重建镜像 |
| `响应不是 JSON` | 被网关/WAF 拦了，通常是 IP 或请求头不匹配；先设 `DRY_RUN=true` 确认出网 |
| 日志时间不对 | 改 `TZ`（默认 `Asia/Shanghai`），同时确认 Unraid 宿主时区 |
| 每天都没触发 | 看 `NEXT_RUN` 那行；容器重启会补签当天，如果一直 `SKIP_TODAY` 说明当天已领 |
| 需要走代理 | 加 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量指向 mihomo（`192.168.31.190:20171`）；codebuddy.cn 属国内域名，默认规则会走 DIRECT，一般不需要 |

## 八、撤销与回滚

- **停用**：Unraid 里停掉容器即可，不影响任何账号状态。
- **彻底撤销**：停容器 → 删容器与 `my-workbuddy-claim.xml` → 删 `/mnt/user/appdata/workbuddy-claim`。令牌本身通过「重新登录客户端」即作废，这是平台侧的撤销路径。
- **改回旧模板**：`install-on-unraid.sh` 每次运行前会把旧模板和容器基线存到
  `/boot/config/plugins/dockerMan/template-backups/workbuddy-claim-<时间戳>-pre/`。

## 目录

```
app/claim.py                     签到主程序（纯标准库）
Dockerfile                       python:3.12-alpine，非 root（uid 1000）
.github/workflows/docker.yml     GitHub Actions → ghcr.io
dockerman/my-workbuddy-claim.xml Unraid DockerMan 模板
deploy/install-on-unraid.sh      Unraid 侧一键安装
deploy/create-container.php      通过 DockerMan 的 xmlToCommand() 建房
```
