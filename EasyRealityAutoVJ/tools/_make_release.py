# -*- coding: utf-8 -*-
"""把当前发行包作为 GitHub Release 附件发布（本地工具，不进仓库）。

用法：
    set GITHUB_TOKEN=ghp_xxx
    python tools/_make_release.py            # 发布当前版本（草稿→上传→改名→发布）
    python tools/_make_release.py --draft    # 只建草稿，不发布

版本号与包名自动取自 src/_build_ver.py（打包脚本写的），不需要手填。

⚠ 三个必须记住的点（都是实测踩出来的）：
  1) **GitHub 附件名不允许非 ASCII** —— 带「测试版」的文件名会被吃掉字符
     （`..._测试版092615.zip` → `..._.092615.zip`）。所以上传后用 ASCII 名，
     并且上传完**再校验一次**，不一致就 PATCH 改名。
  2) **先建草稿再上传** —— 471 MB 要传约 1 分钟，直接发会把「空 Release」露给群友。
  3) **Git 推送要绕开凭据助手**（本机 `credential.helper=git-credential-manager.exe`
     在非交互环境会永久挂住）：`git -c credential.helper= push ...`。本脚本不走 git，不受影响。
"""
import http.client
import json
import os
import re
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = "mapotofu25"
REPO = "EasyRealityAutoVJ"
API = "https://api.github.com/repos/%s/%s" % (OWNER, REPO)


def read_build_info():
    """从 src/_build_ver.py 读版本号与包名。"""
    p = os.path.join(ROOT, "src", "_build_ver.py")
    if not os.path.exists(p):
        sys.exit("找不到 %s —— 先打一次包（tools/build_exe.py）" % p)
    ns = {}
    with open(p, encoding="utf-8") as f:
        exec(compile(f.read(), p, "exec"), ns)
    return ns.get("BUILD_VER", ""), ns.get("ZIP_NAME", "")


def ascii_name(zip_name):
    """去掉非 ASCII 字符（GitHub 不允许），并清理因此产生的 `_.` 之类。"""
    s = "".join(ch for ch in zip_name if ord(ch) < 128)
    s = re.sub(r"[_\-.]{2,}", lambda m: m.group(0)[0], s)
    return s


def call(token, url, method="GET", payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json; charset=utf-8"})
    return json.loads(urllib.request.urlopen(req, timeout=90).read().decode("utf-8"))


def upload_asset(token, release_id, path, name):
    """流式上传（不把整包读进内存）。"""
    size = os.path.getsize(path)
    conn = http.client.HTTPSConnection("uploads.github.com", timeout=3600)
    url = "/repos/%s/%s/releases/%s/assets?name=%s" % (
        OWNER, REPO, release_id, urllib.parse.quote(name))
    conn.putrequest("POST", url)
    conn.putheader("Authorization", "Bearer " + token)
    conn.putheader("Accept", "application/vnd.github+json")
    conn.putheader("Content-Type", "application/zip")
    conn.putheader("Content-Length", str(size))
    conn.putheader("User-Agent", "autovj-release")
    conn.endheaders()
    sent = 0
    with open(path, "rb") as f:
        while True:
            buf = f.read(4 * 1024 * 1024)
            if not buf:
                break
            conn.send(buf)
            sent += len(buf)
            if sent % (64 * 1024 * 1024) < 4 * 1024 * 1024:
                print("      %.0f%%（%.0f MB）" % (sent * 100.0 / size, sent / 1048576.0),
                      flush=True)
    r = conn.getresponse()
    body = r.read().decode("utf-8", "replace")
    if r.status not in (200, 201):
        sys.exit("上传失败 HTTP %s: %s" % (r.status, body[:400]))
    return json.loads(body)


def main():
    keep_draft = "--draft" in sys.argv
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        sys.exit("请先设环境变量 GITHUB_TOKEN（GitHub 个人访问令牌，需 repo 权限）")

    ver, zip_name = read_build_info()
    path = os.path.join(ROOT, zip_name)
    if not os.path.isfile(path):
        sys.exit("找不到发行包：%s" % path)
    tag = "v" + ver
    name = ascii_name(zip_name)

    print("版本   : %s" % ver)
    print("发行包 : %s（%.1f MB）" % (zip_name, os.path.getsize(path) / 1048576.0))
    print("标签   : %s" % tag)
    print("附件名 : %s（GitHub 不允许非 ASCII，已转换）" % name)
    print()

    print("1) 建草稿 release …")
    try:
        rel = call(token, API + "/releases", "POST", {
            "tag_name": tag, "target_commitish": "main",
            "name": "Easy Reality AutoVJ 测试版 %s" % ver,
            "body": "测试版 %s\n\n下载 **%s**（约 %.0f MB），解压后运行 "
                    "`EasyRealityAutoVJ/EasyRealityAutoVJ.exe`。\n\n"
                    "- 免安装，解压即用；首次运行会自动建好配置与缩略图缓存\n"
                    "- 用法见包内 `使用说明.txt` / `使用说明_EN.txt`\n"
                    "- 源代码见本仓库（MIT）；第三方组件与模型署名见 `NOTICE.md`\n"
                    "- ⚠ 包内含 Discogs-EffNet（CC BY-NC-SA 4.0）⇒ **本发行包仅限非商业使用**"
                    % (ver, name, os.path.getsize(path) / 1048576.0),
            "draft": True, "prerelease": False})
    except urllib.error.HTTPError as e:
        sys.exit("建草稿失败 HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:400]))
    rid = str(rel["id"])
    print("   id = %s" % rid)

    print("2) 上传附件 …")
    a = upload_asset(token, rid, path, name)
    print("   已上传: %s（%s bytes）→ %s" % (a["name"], a["size"], a["browser_download_url"]))

    if a["name"] != name:
        print("3) 名字被改写，改名 → %s" % name)
        a = call(token, "%s/releases/assets/%s" % (API, a["id"]), "PATCH", {"name": name})
        print("   现在: %s" % a["name"])

    if keep_draft:
        print("\n草稿已就绪（未发布）：%s" % rel["html_url"])
        print("确认后到网页点 Publish，或去掉 --draft 重跑。")
    else:
        d = call(token, "%s/releases/%s" % (API, rid), "PATCH", {"draft": False})
        print("\n已发布 ✅  %s" % d["html_url"])
        for x in d["assets"]:
            print("   %s  %.1f MB" % (x["name"], x["size"] / 1048576.0))


if __name__ == "__main__":
    main()
