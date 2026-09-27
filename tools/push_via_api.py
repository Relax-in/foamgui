#!/usr/bin/env python3
"""用 GitHub REST API 推送本仓库(备用方案)。

为什么需要它
------------------------------------------------------------------
这台机器上 `github.com` 的 git 端口(HTTPS / SSH 22)经常不通, 只有
`api.github.com` 稳定可用。本脚本不依赖 git 传输协议, 直接用 REST API
把当前 HEAD 的快照推到 GitHub:

    POST /user/repos                      (仓库不存在时创建)
    POST /repos/{o}/{r}/git/blobs         (逐个文件上传)
    POST /repos/{o}/{r}/git/trees         (建目录树)
    POST /repos/{o}/{r}/git/commits       (建提交)
    POST /repos/{o}/{r}/git/refs          (建分支 / 更新分支)

用法
------------------------------------------------------------------
    # token 从环境变量或文件读取(推荐放文件, 避免出现在命令行历史里)
    echo 'github_pat_xxx' > ~/.foamgui_token && chmod 600 ~/.foamgui_token
    python3 tools/push_via_api.py --repo foamgui --private

    选项:
      --repo NAME     仓库名(默认 foamgui)
      --private       建为私有仓库(默认私有)
      --public        建为公开仓库
      --branch NAME   分支名(默认 main)
      --owner NAME    用户名(默认用 token 对应的登录名)
      --token-file F  token 文件(默认 ~/.foamgui_token, 其次 $GITHUB_TOKEN)
      --message MSG   提交信息(默认取当前 git 提交信息)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent.parent


def api(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    url = path if path.startswith("http") else API + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "foamgui-push")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode() or "{}"
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode() or "{}"
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"message": body[:400]}


def git(*args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True
    )
    return out.stdout


def read_token(explicit: str | None) -> str:
    if explicit:
        return Path(explicit).expanduser().read_text().strip()
    env = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env:
        return env.strip()
    default = Path.home() / ".foamgui_token"
    if default.exists():
        return default.read_text().strip()
    print(
        "[错误] 没有找到 token。请执行:\n"
        "    echo 'github_pat_xxx' > ~/.foamgui_token && chmod 600 ~/.foamgui_token\n"
        "或设置环境变量 GITHUB_TOKEN。",
        file=sys.stderr,
    )
    raise SystemExit(2)


def main() -> int:
    ap = argparse.ArgumentParser(description="用 GitHub REST API 推送仓库")
    ap.add_argument("--repo", default="foamgui")
    ap.add_argument("--owner", default=None)
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", default=None)
    ap.add_argument("--token-file", default=None)
    ap.add_argument("--private", action="store_true", default=True)
    ap.add_argument("--public", dest="private", action="store_false")
    args = ap.parse_args()

    token = read_token(args.token_file)

    status, me = api("GET", "/user", token)
    if status != 200:
        print(f"[错误] token 无效或没有权限: {status} {me.get('message')}", file=sys.stderr)
        return 2
    owner = args.owner or me["login"]
    print(f"[1] 已认证为 {me['login']}")

    # 仓库是否存在
    status, _info = api("GET", f"/repos/{owner}/{args.repo}", token)
    if status == 404:
        status, info = api(
            "POST",
            "/user/repos",
            token,
            {
                "name": args.repo,
                "private": bool(args.private),
                "description": "OpenFOAM 13 前处理 GUI (PyQt6 + VTK): 网格显示 / 边界条件 / 生成字典",
                "has_issues": True,
                "has_wiki": False,
            },
        )
        if status not in (200, 201):
            print(f"[错误] 创建仓库失败: {status} {info.get('message')}", file=sys.stderr)
            return 2
        print(f"[2] 已创建{'私有' if args.private else '公开'}仓库 {owner}/{args.repo}")
    else:
        print(f"[2] 仓库已存在: {owner}/{args.repo}")

    # 收集文件
    tracked = [p for p in git("ls-files").splitlines() if p.strip()]
    if not tracked:
        print("[错误] 当前仓库没有被 git 跟踪的文件", file=sys.stderr)
        return 2
    message = args.message or git("log", "-1", "--pretty=%B").strip()
    author_name = git("log", "-1", "--pretty=%an").strip() or owner
    author_email = git("log", "-1", "--pretty=%ae").strip() or f"{owner}@users.noreply.github.com"

    entries = []
    for i, rel in enumerate(tracked, 1):
        path = ROOT / rel
        if not path.is_file():
            continue
        content = base64.b64encode(path.read_bytes()).decode()
        status, blob = api("POST", f"/repos/{owner}/{args.repo}/git/blobs", token,
                           {"content": content, "encoding": "base64"})
        if status not in (200, 201):
            print(f"[错误] 上传 {rel} 失败: {status} {blob.get('message')}", file=sys.stderr)
            return 2
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        if i % 10 == 0 or i == len(tracked):
            print(f"    已上传 {i}/{len(tracked)} 个文件", flush=True)

    status, tree = api("POST", f"/repos/{owner}/{args.repo}/git/trees", token,
                       {"tree": entries})
    if status not in (200, 201):
        print(f"[错误] 创建目录树失败: {status} {tree.get('message')}", file=sys.stderr)
        return 2
    print(f"[3] 目录树: {tree['sha'][:10]} ({len(entries)} 个文件)")

    # 是否已有分支(决定要不要带 parent)
    parents = []
    status, ref = api("GET", f"/repos/{owner}/{args.repo}/git/ref/heads/{args.branch}", token)
    if status == 200:
        parents = [ref["object"]["sha"]]

    status, commit = api("POST", f"/repos/{owner}/{args.repo}/git/commits", token,
                         {"message": message, "tree": tree["sha"], "parents": parents,
                          "author": {"name": author_name, "email": author_email},
                          "committer": {"name": author_name, "email": author_email}})
    if status not in (200, 201):
        print(f"[错误] 创建提交失败: {status} {commit.get('message')}", file=sys.stderr)
        return 2
    print(f"[4] 提交: {commit['sha'][:10]}")

    if parents:
        status, res = api("PATCH", f"/repos/{owner}/{args.repo}/git/refs/heads/{args.branch}",
                          token, {"sha": commit["sha"], "force": False})
    else:
        status, res = api("POST", f"/repos/{owner}/{args.repo}/git/refs", token,
                          {"ref": f"refs/heads/{args.branch}", "sha": commit["sha"]})
    if status not in (200, 201):
        print(f"[错误] 更新分支失败: {status} {res.get('message')}", file=sys.stderr)
        return 2

    print(f"[完成] https://github.com/{owner}/{args.repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
