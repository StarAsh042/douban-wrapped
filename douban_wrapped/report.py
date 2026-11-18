"""年度报告页生成：data/profile.json -> output/report.html（单文件，ECharts）。

构建时把 templates/echarts.min.js 内联进 HTML，报告页离线可开、无外网依赖；
BGM 曲目（templates/music/）复制到报告页同级 music/ 目录；
profile 中的封面 URL 会下载到 output/covers/ 并改写为相对路径，避免远程防盗链导致封面不显示。
"""

from __future__ import annotations

import json
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE_FILE = ROOT / "templates" / "report.html"
PAIR_TEMPLATE_FILE = ROOT / "templates" / "pair.html"
ECHARTS_FILE = ROOT / "templates" / "echarts.min.js"
MUSIC_DIR = ROOT / "templates" / "music"

CDN_TAG = '<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>'
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
COVER_FIELDS = ("top_rated", "gems", "niche_gems", "strict_picks")


def _echarts_script() -> str:
    if ECHARTS_FILE.exists():
        lib = ECHARTS_FILE.read_text(encoding="utf-8", errors="replace")
        if "</script" not in lib:
            return "<script>\n" + lib + "\n</script>"
        return CDN_TAG
    return CDN_TAG


def build_html(profile: dict, template: str) -> str:
    payload = json.dumps(profile, ensure_ascii=False).replace("</", "<\\/")
    html = template.replace("<!--ECHARTS_SCRIPT-->", _echarts_script())
    return html.replace("__PROFILE_JSON__", payload)


def _copy_music(dst: Path) -> None:
    if MUSIC_DIR.exists():
        music_dst = dst / "music"
        if music_dst.exists():
            shutil.rmtree(music_dst)
        shutil.copytree(MUSIC_DIR, music_dst)


def _slug(url: str) -> str:
    """从封面 URL 里取一个稳定的文件名片段。"""
    name = re.sub(r"[\\/]+$", "", url.split("?", 1)[0]).split("/")[-1]
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:64] or "cover"


def _upgrade_cover_url(url: str) -> str:
    """小图链接升级为大图（参考开源项目 douban-image-grid 的做法）。"""
    return url.replace("s_ratio_poster", "l_ratio_poster").replace("subject/s/", "subject/l/")


def _is_image(data: bytes) -> bool:
    if len(data) < 128:
        return False
    return (
        data[:3] == b"\xff\xd8\xff"
        or data[:8] == b"\x89PNG\r\n\x1a\n"
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        or data[:6] in (b"GIF87a", b"GIF89a")
    )


def _image_headers() -> dict[str, str]:
    """浏览器式请求头 + 登录 Cookie：豆瓣图片 CDN 带 Cookie 时极少 403。"""
    headers = {
        "User-Agent": UA,
        "Referer": "https://movie.douban.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    cookie_file = Path("cookie.txt")
    if cookie_file.exists():
        try:
            cookie = cookie_file.read_text(encoding="utf-8", errors="ignore").strip()
            if cookie:
                headers["Cookie"] = cookie
        except OSError:
            pass
    return headers


def _fetch_image(url: str, headers: dict[str, str]) -> bytes | None:
    """带重试 + 大图备用链的封面下载；豆瓣 CDN 偶发 403，重试可救回多数。"""
    for candidate in (url, _upgrade_cover_url(url)):
        for attempt in range(3):
            try:
                req = urllib.request.Request(candidate, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                if _is_image(data):
                    return data
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                pass
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    return None


def _cache_covers(profile: dict, covers_dir: Path) -> int:
    """把各片单远程封面下载到本地，改写 cover 为相对路径。

    失败条目保留原 URL（页面 onerror 退回文字占位符）。返回成功张数。
    """
    covers_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    seen: dict[str, str] = {}
    headers = _image_headers()
    items = [f for field in COVER_FIELDS for f in (profile.get(field) or [])]
    for item in items:
        url = item.get("cover") or ""
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        if url in seen:
            item["cover"] = seen[url]
            continue
        fname = _slug(url)
        if "." not in fname:
            fname += ".jpg"
        target = covers_dir / fname
        if not target.exists():
            data = _fetch_image(url, headers)
            if data is None:
                continue  # 保留远程 URL，交给页面占位符兜底
            target.write_bytes(data)
            time.sleep(0.15)  # 轻微限速，避免连发触发豆瓣 CDN 403
        rel = "covers/" + fname
        item["cover"] = rel
        seen[url] = rel
        saved += 1
    return saved


def run(profile_file: str = "data/profile.json",
        out_file: str = "output/report.html") -> int:
    src, dst = Path(profile_file), Path(out_file)
    if not src.exists():
        print(f"未找到 {src}，请先运行 analyze。")
        return 1
    profile = json.loads(src.read_text(encoding="utf-8"))
    # 有 AI 叙事就并入报告（双人契合度已改为独立页面，不再并入报告页）
    narrative_file = src.parent / "narrative.json"
    if narrative_file.exists():
        try:
            profile["narrative"] = json.loads(narrative_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    dst.parent.mkdir(parents=True, exist_ok=True)
    cached = _cache_covers(profile, dst.parent / "covers")
    if cached:
        print(f"已缓存 {cached} 张封面到 {dst.parent / 'covers'}")
    html = build_html(profile, TEMPLATE_FILE.read_text(encoding="utf-8"))
    dst.write_text(html, encoding="utf-8")
    # BGM 曲目复制到报告页同级 music/ 目录
    _copy_music(dst.parent)
    mode = "内联 ECharts（离线可用）" if ECHARTS_FILE.exists() else "CDN 引入 ECharts"
    print(f"报告页已生成 -> {dst.resolve()}（{mode}）")
    return 0


def run_pair(pair_file: str = "data/pair.json",
             out_file: str = "output/pair.html") -> int:
    """把双人契合度结果渲染成独立页面 output/pair.html。"""
    src, dst = Path(pair_file), Path(out_file)
    if not src.exists():
        print(f"未找到 {src}，请先运行 pair。")
        return 1
    pair = json.loads(src.read_text(encoding="utf-8"))
    if not PAIR_TEMPLATE_FILE.exists():
        print(f"未找到契合度模板 {PAIR_TEMPLATE_FILE}。")
        return 1
    html = build_html(pair, PAIR_TEMPLATE_FILE.read_text(encoding="utf-8"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(html, encoding="utf-8")
    print(f"契合度页面已生成 -> {dst.resolve()}")
    return 0
