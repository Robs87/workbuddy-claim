#!/bin/bash
# 在 Unraid 主机上运行（root）。把模板装进 DockerMan 并用 DockerMan 建房。
#
# 用法：
#   WB_TOKEN='eyJ...' ./install-on-unraid.sh <github-owner> [--recreate]
#
# 说明：
#   * 令牌从环境变量传入，避免进入 shell 历史与 ps 输出。
#   * 已存在的同名容器默认不覆盖，需显式 --recreate。
set -euo pipefail

OWNER="${1:-}"
RECREATE="${2:-}"
TMPL_DIR=/boot/config/plugins/dockerMan/templates-user
TMPL="$TMPL_DIR/my-workbuddy-claim.xml"
APPDATA=/mnt/user/appdata/workbuddy-claim
SRC_TMPL="$(dirname "$0")/../dockerman/my-workbuddy-claim.xml"
BACKUP_DIR=/boot/config/plugins/dockerMan/template-backups/workbuddy-claim-$(date +%Y%m%d-%H%M%S)-pre

die() { echo "ERROR: $*" >&2; exit 1; }

[ -n "$OWNER" ] || die "缺少 GitHub 用户/组织名。用法: WB_TOKEN=... $0 <github-owner> [--recreate]"
[ -f "$SRC_TMPL" ] || die "找不到模板源文件: $SRC_TMPL"
[ -n "${WB_TOKEN:-}" ] || echo "WARN: 未提供 WB_TOKEN，将以空令牌建房（容器会打印 CONFIG_ERROR 但不动作），随后在 Unraid WebUI 的模板编辑里补填 Access Token"

echo "== 1/5 备份现有模板与容器基线 =="
mkdir -p "$BACKUP_DIR"
if [ -f "$TMPL" ]; then
  cp -a "$TMPL" "$BACKUP_DIR/"
  echo "已备份旧模板"
fi
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}' > "$BACKUP_DIR/containers-baseline.tsv"
echo "基线: $(wc -l < "$BACKUP_DIR/containers-baseline.tsv") 个容器 -> $BACKUP_DIR"

echo "== 2/5 准备 appdata 并修正属主 =="
mkdir -p "$APPDATA"
chown -R 1000:1000 "$APPDATA"
ls -ldn "$APPDATA"

echo "== 3/5 渲染 DockerMan 模板 =="
esc_token=$(printf '%s' "$WB_TOKEN" | sed -e 's/[\\&|]/\\&/g')
mkdir -p "$TMPL_DIR"
sed -e "s|ghcr.io/OWNER/|ghcr.io/${OWNER}/|g" \
    -e "s|github.com/OWNER/|github.com/${OWNER}/|g" \
    -e "s|WB_TOKEN_PLACEHOLDER|${esc_token}|g" \
    "$SRC_TMPL" > "$TMPL"
chmod 600 "$TMPL"
echo "已写入 $TMPL (权限 600，含令牌)"

echo "== 4/5 拉取镜像（先暴露私有包/网络问题） =="
if ! docker pull "ghcr.io/${OWNER}/workbuddy-claim:latest"; then
  die "拉取失败。若 Actions 还没跑完，先等构建；若包是私有的，需在 Unraid 配置 ghcr 凭据或把包改成 public。"
fi

echo "== 5/5 通过 DockerMan 创建容器 =="
php -d docroot=/usr/local/emhttp "$(dirname "$0")/create-container.php" "$TMPL" $RECREATE

echo
echo "== 验证 =="
sleep 5
docker inspect workbuddy-claim \
  --format 'Name={{.Name}} Image={{.Config.Image}} Status={{.State.Status}} Restart={{.RestartCount}}'
LABEL=$(docker inspect workbuddy-claim --format '{{index .Config.Labels "net.unraid.docker.managed"}}' 2>/dev/null || true)
echo "dockerman 标签: ${LABEL:-<缺失>}"
[ "$LABEL" = "dockerman" ] || echo "警告：标签不是 dockerman，容器未受 DockerMan 管理"

echo "--- 最近日志 ---"
docker logs --tail 20 workbuddy-claim 2>&1 || true
echo
echo "完成。手动补签一次: docker exec workbuddy-claim python3 /app/claim.py --once"
echo "回滚快照: $BACKUP_DIR"
