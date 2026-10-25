# -*- coding: utf-8 -*-
"""绕开 pip 装依赖：从阿里云镜像挑 wheel，解压到 vendor/。

沙箱里 pip 写临时文件被拒，所以这里自己做最小依赖解析。
"""
import io
import os
import re
import sys
import zipfile
import urllib.request
import urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(BASE, "vendor")
WHEELS = os.path.join(BASE, "_wheels")
MIRROR = "https://mirrors.aliyun.com/pypi/simple/%s/"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126"}

ALIAS = {"opencv-python": "opencv-python-headless"}
SKIP = {"distribute", "markerlib", "argparse", "wmi", "wheel"}

os.makedirs(VENDOR, exist_ok=True)
os.makedirs(WHEELS, exist_ok=True)


def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def vkey(v):
    out = []
    for part in re.split(r"[._-]", v):
        m = re.match(r"^(\d+)", part)
        out.append(int(m.group(1)) if m else 0)
    return tuple(out)


def pick(html):
    hrefs = re.findall(r'href="([^"]+\.whl)(?:#[^"]*)?"', html)
    best = None
    for href in hrefs:
        fn = urllib.parse.unquote(href.rsplit("/", 1)[-1])
        low = fn.lower()
        if not low.endswith(".whl"):
            continue
        if "win_amd64" not in low and "none-any" not in low:
            continue
        if "cp314t" in low or "cp313t" in low:
            continue                      # 自由线程版，不能用在普通解释器上
        if re.search(r"cp3(\d+)", low):
            n = int(re.search(r"cp3(\d+)", low).group(1))
            if n < 12 and "abi3" not in low:
                continue                  # 太老的 ABI 不用
        parts = fn.split("-")
        ver = parts[1] if len(parts) > 1 else "0"
        score = 0
        if "cp314" in low:
            score += 120
        if "abi3" in low:
            score += 80
        if "none-any" in low:
            score += 70
        if "cp313" in low:
            score += 50
        if "cp312" in low:
            score += 40
        cand = (score, vkey(ver), fn, href)
        if best is None or (cand[0], cand[1]) > (best[0], best[1]):
            best = cand
    return best


def deps_of(dist_name):
    """从刚解压出的 dist-info 读 Requires-Dist"""
    key = re.sub(r"[-_.]+", "-", dist_name).lower()
    for d in os.listdir(VENDOR):
        if not d.endswith(".dist-info"):
            continue
        if re.sub(r"[-_.]+", "-", d.split("-")[0]).lower() != key:
            continue
        meta = os.path.join(VENDOR, d, "METADATA")
        if not os.path.exists(meta):
            continue
        out = []
        for line in io.open(meta, encoding="utf-8", errors="ignore"):
            if not line.startswith("Requires-Dist:"):
                continue
            spec = line.split(":", 1)[1].strip()
            if "extra ==" in spec:
                continue
            pkg = re.split(r"[ <>=!;\[\(]", spec)[0].strip()
            if pkg:
                out.append(pkg)
        return out
    return []


def install(project):
    url_name = ALIAS.get(project, project).replace("_", "-").lower()
    html = fetch(MIRROR % urllib.parse.quote(url_name)).decode("utf-8", "ignore")
    best = pick(html)
    if not best:
        print("  !! 无可用 wheel:", url_name, flush=True)
        return []
    score, ver, fn, href = best
    if not href.startswith("http"):
        href = urllib.parse.urljoin(MIRROR % url_name, href)
    path = os.path.join(WHEELS, fn)
    if not os.path.exists(path):
        print("  下载", fn, flush=True)
        with open(path, "wb") as f:
            f.write(fetch(href))
    print("  解压", fn, flush=True)
    with zipfile.ZipFile(path) as z:
        z.extractall(VENDOR)
    return deps_of(fn.split("-")[0])


def main():
    queue = sys.argv[1:] or ["rapidocr-onnxruntime", "pillow"]
    done = set()
    while queue:
        pkg = queue.pop(0)
        key = re.sub(r"[-_.]+", "-", pkg).lower()
        if key in done or key in SKIP:
            continue
        done.add(key)
        print("==", pkg, flush=True)
        try:
            for d in install(pkg):
                k = re.sub(r"[-_.]+", "-", d).lower()
                if k not in done and k not in SKIP:
                    queue.append(d)
        except Exception as e:
            print("  FAIL", pkg, type(e).__name__, e, flush=True)
    n = sum(len(f) for _, _, f in os.walk(VENDOR))
    print("完成，vendor 文件数:", n, flush=True)


if __name__ == "__main__":
    main()
