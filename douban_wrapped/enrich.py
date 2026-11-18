"""条目元数据补全：优先 rexxar JSON API（免登录），详情页 HTML 兜底，带本地缓存。"""

from __future__ import annotations

import json
import re
import urllib.error
from pathlib import Path

REXXAR_URL = "https://m.douban.com/rexxar/api/v2/movie/{sid}"
REXXAR_TV_URL = "https://m.douban.com/rexxar/api/v2/tv/{sid}"

_RATING_NUM_RE = re.compile(r'class="ll rating_num"[^>]*>\s*([\d.]+)')
_VOTES_RE = re.compile(r'property="v:votes">(\d+)<')
_GENRE_RE = re.compile(r'property="v:genre">([^<]+)<')
_DIRECTOR_RE = re.compile(r'rel="v:directedBy">([^<]+)<')
_ACTOR_RE = re.compile(r'rel="v:starring">([^<]+)<')
_YEAR_RE = re.compile(r'<span class="year">\((\d{4})\)</span>')
_COUNTRY_RE = re.compile(r"制片国家/地区:</span>\s*([^<]+)")
_LANG_RE = re.compile(r"语言:</span>\s*([^<]+)")
_TITLE_RE = re.compile(r"<title>(.*?)\s*\(\s*豆瓣\s*\)</title>", re.S)


def _normalize(d: dict, sid: str) -> dict:
    rating = d.get("rating") or {}
    return {
        "sid": str(d.get("id") or sid),
        "title": d.get("title") or "",
        "original_title": d.get("original_title") or "",
        "year": str(d.get("year") or ""),
        "is_tv": bool(d.get("is_tv")),
        "subtype": d.get("subtype") or "",
        "genres": d.get("genres") or [],
        "countries": d.get("countries") or [],
        "languages": d.get("languages") or [],
        "durations": d.get("durations") or [],
        "directors": [p.get("name", "") for p in d.get("directors") or []],
        "actors": [p.get("name", "") for p in d.get("actors") or []][:8],
        "aka": d.get("aka") or [],
        "douban_rating": rating.get("value"),
        "douban_votes": rating.get("count"),
        "cover_url": d.get("cover_url") or (d.get("pic") or {}).get("large") or "",
        "url": d.get("url") or f"https://movie.douban.com/subject/{sid}/",
    }


def parse_subject_html(html: str, sid: str) -> dict:
    """从条目详情页 HTML 提取元数据（rexxar 被拦时的兜底）。"""
    m = _TITLE_RE.search(html)
    title = m.group(1).strip() if m else ""
    m = _RATING_NUM_RE.search(html)
    rating = float(m.group(1)) if m else None
    m = _VOTES_RE.search(html)
    votes = int(m.group(1)) if m else None
    m = _YEAR_RE.search(html)
    year = m.group(1) if m else ""
    m = _COUNTRY_RE.search(html)
    countries = [c.strip() for c in m.group(1).split("/")] if m else []
    m = _LANG_RE.search(html)
    languages = [c.strip() for c in m.group(1).split("/")] if m else []
    return {
        "sid": sid,
        "title": title,
        "original_title": "",
        "year": year,
        "is_tv": False,
        "subtype": "",
        "genres": _GENRE_RE.findall(html),
        "countries": countries,
        "languages": languages,
        "durations": [],
        "directors": _DIRECTOR_RE.findall(html),
        "actors": _ACTOR_RE.findall(html)[:8],
        "aka": [],
        "douban_rating": rating,
        "douban_votes": votes,
        "url": f"https://movie.douban.com/subject/{sid}/",
    }


def _from_rexxar(client, sid: str) -> dict | None:
    last_404 = False
    # retries=1：rexxar 偶发 403 时不做长退避，交给上层落到详情页兜底
    for url in (REXXAR_URL.format(sid=sid), REXXAR_TV_URL.format(sid=sid)):
        try:
            body = client.get(
                url,
                referer=f"https://m.douban.com/movie/{sid}/",
                accept="application/json",
                retries=1,
            )
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                last_404 = True
                continue
            raise
        data = json.loads(body)
        if data.get("id"):
            return _normalize(data, sid)
    return None


def enrich_cached(client, sid: str, cache_dir: Path) -> dict | None:
    """带磁盘缓存的元数据抓取，可随时中断续跑。

    rexxar 失败（限流/网络）时自动落到网页版详情页兜底。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{sid}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    try:
        info = _from_rexxar(client, sid)
    except (urllib.error.HTTPError, RuntimeError, OSError):
        info = None
    if info is None:
        # rexxar 拿不到时尝试网页版详情页
        try:
            html = client.get(f"https://movie.douban.com/subject/{sid}/")
            info = parse_subject_html(html, sid)
        except (urllib.error.HTTPError, RuntimeError, OSError):
            info = None
    if info:
        cache_file.write_text(
            json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return info
