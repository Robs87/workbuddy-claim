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

> **当前实例已部署**：Unraid `Tower`（192.168.31.190）上的容器 `workbuddy-claim`，镜像 `ghcr.io/robs87/workbuddy-claim:latest`，DockerMan 托管（`net.unraid.docker.managed=dockerman`），已加入开机自启，每日 09:00（Asia/Shanghai）签到。待补：模板里的 Access Token。

- 签到走的是 WorkBuddy 客户端自己用的官方接口，鉴权就是你的登录态，**没有绕过任何校验**，只是把一次手工点击换成一次 HTTP 请求。
- 接口对重复调用是幂等的：已签到返回 `code=10001`，不会多领，也不会扣分。
- 官方口径是"人工签到"，且平台对脚本签到有风控。**用不用、用多久，请自行判断**；本仓库不保证账号策略变化后仍然可用。真出问题时撤销方式见文末。
- 令牌等同登录态，只存在于 Unraid 的模板变量（权限 600）里，代码、日志、状态文件都不会记录它。

## 一、拿到 accessToken

浏览器登录 <https://www.codebuddy.cn>，按 F12 打开 DevTools：

1. 切到 **Network** 面板，随便点开一个页面功能，筛出 `/v2/billing` 开头的请求；
2. 看 **Request Headers** 里的 `Authorization: Bearer eyJhbGciOi...`；
3. 复制 `Bearer ` 后面那一整串（很长，别截断）。

令牌有效期约 **30 天**（`expiresIn=2592000`）。只要你平时用桌面客户端，登录态会自动续期；但容器拿不到刷新动作，所以**到期后要重新复制一次**填回 Unraid（下面第六节）。

> 本机 `workbuddy-desktop.info` 里的令牌是 AES-256-GCM 信封加密的，脚本解不出来，也不该去解——请走 DevTools 手工复制这一条正道。

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

## 六、令牌自动续期（接 Unraid 上的 CookieCloud）

容器每次运行都先向同机的 CookieCloud 要一次浏览器当前登录态，拿到就用，拿不到才回退到模板里手填的 `WB_TOKEN`。于是「令牌 30 天过期」这件事由浏览器接管了。

前置：Unraid 上有 `easychen/cookiecloud` 在跑，浏览器装了 CookieCloud 扩展且**已同步过 www.codebuddy.cn**（在该站点登录并正常浏览一次即可触发同步）。

模板里四个参数：

| 参数 | 说明 |
|---|---|
| `CC_URL` | `http://192.168.31.190:8089`（同机默认值） |
| `CC_UUID` | 扩展设置页里的 UUID，也是服务端数据文件名 |
| `CC_PASSWORD` | 扩展设置页里的密码，**必填** |
| `CC_DOMAIN` | 默认 `codebuddy.cn`；若登录态在 `workbuddy.cn` 下则改掉 |

服务端 `POST /get/<uuid>` 带 `password` 时会直接返回明文，所以容器内没有任何 AES 实现，也不需要保存浏览器密码之外的密钥。

凭据选择顺序：**localStorage 里的令牌 → 该域名的 cookie → 手动填的 `WB_TOKEN`**。两者都会随请求发出，服务端认哪个用哪个。

诊断（只打印结构与键名，不输出任何凭据值）：

```bash
docker exec workbuddy-claim python3 /app/claim.py --cc-probe
```

正常会看到类似：

```
CC_OK | hosts=www.codebuddy.cn cookie_count=6 token_present=True token_host=www.codebuddy.cn
```

失败排查：

- `CC_FAIL status=500` → 密码错；
- `CC_EMPTY` → 同步过，但里面没有 codebuddy.cn 的数据，去浏览器访问一次该站点再等扩展同步；
- `CC_SKIP` → `CC_UUID`/`CC_PASSWORD` 没填。

仍然手动处理的情况：CookieCloud 里也没有可用凭据（比如换了设备、清了浏览器数据、扩展停了），日志出现 `AUTH_FINAL`，此时按第一节重新取一个令牌填进去即可。

## 七、常见问题

| 现象 | 原因与处理 |
|---|---|
| `docker pull` 报 `unauthorized` | 包还是私有的，见第三节；或本地 ghcr 凭据失效 |
| `AUTH_FAILED ... HTTP 401` | 令牌过期/复制不全（必须含 `Bearer ` 之后全部字符） |
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
