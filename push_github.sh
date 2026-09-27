#!/usr/bin/env bash
# 把本仓库推送到 GitHub
#
# 用法:
#   ./push_github.sh                 # 使用默认设置(Relax-in/foamgui)
#   ./push_github.sh <用户名> [仓库名]
#
# 说明:
#   * 本机 github.com 的 22 端口时通时不通, 而 ssh.github.com:443 一直可用,
#     所以这里固定用 "SSH over 443" 的地址推送;
#   * 使用你已有的 SSH 私钥 ~/.ssh/id_ed25519(已登记在 GitHub 上), 也可以先
#     `ssh-add ~/.ssh/id_ed25519` 交给 ssh-agent;
#   * 前置条件: GitHub 上已经有这个空仓库(SSH 不能创建仓库)。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="${1:-Relax-in}"
REPO="${2:-foamgui}"
KEY="${HOME}/.ssh/id_ed25519"

if [[ ! -f "${KEY}" ]]; then
    echo "[错误] 找不到 ${KEY}，请先生成并加到 GitHub:" >&2
    echo "       ssh-keygen -t ed25519 -C 'your@email'" >&2
    exit 1
fi

git -C "${HERE}" config core.sshCommand \
    "ssh -i ${KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

URL="ssh://git@ssh.github.com:443/${USER_NAME}/${REPO}.git"
if git -C "${HERE}" remote get-url origin >/dev/null 2>&1; then
    git -C "${HERE}" remote set-url origin "${URL}"
else
    git -C "${HERE}" remote add origin "${URL}"
fi

echo "[信息] 推送到 ${URL}" >&2
if ! git -C "${HERE}" push -u origin main; then
    cat >&2 <<'MSG'

[提示] 推送失败, 常见原因:
  1. GitHub 上还没有这个仓库 —— 先到 https://github.com/new 新建一个空仓库
     (不要勾选 Add README / .gitignore / license);
  2. 用户名或仓库名写错;
  3. 换个端口试试: 把地址改成 ssh://git@github.com:22/<用户名>/<仓库>.git
MSG
    exit 1
fi
echo "[完成] https://github.com/${USER_NAME}/${REPO}" >&2
