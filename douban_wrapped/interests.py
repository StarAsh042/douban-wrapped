"""批量兴趣接口导出（借鉴 doumark 等成熟项目的思路）。

接口: m.douban.com/rexxar/api/v2/user/{uid}/interests（需登录 cookie）
一次请求最多带回 50 条记录，且自带完整条目元数据（评分/类型/导演/主演/国家/标签），
可整体替代「列表页分页抓取 + 逐条补全」两阶段，请求量从 ~950 降到 ~20。
"""

from __future__ import annotations

import json

STATUS_MAP = {"done": "watched", "doing": "watching", "mark": "wish"}

# card_subtitle 第二段的国家/地区白名单（批量接口 countries 常为 null，从这里兜底）
_COUNTRIES = {
    "中国大陆", "中国香港", "中国台湾", "美国", "英国", "日本", "韩国", "法国", "德国",
    "意大利", "西班牙", "印度", "加拿大", "澳大利亚", "俄罗斯", "泰国", "丹麦", "瑞典",
    "巴西", "阿根廷", "新西兰", "爱尔兰", "比利时", "荷兰", "奥地利", "挪威", "芬兰",
    "希腊", "波兰", "墨西哥", "智利", "南非", "土耳其", "以色列", "伊朗", "越南",
    "新加坡", "马来西亚", "菲律宾", "印度尼西亚", "捷克", "匈牙利", "罗马尼亚",
    "乌克兰", "埃及", "尼日利亚", "肯尼亚", "黎巴嫩", "哥伦比亚", "冰岛", "格鲁吉亚",
}


def _countries_fallback(s: dict) -> list:
    """countries 为空时，从 card_subtitle（年份 / 国家 / ...）解析。"""
    card = s.get("card_subtitle") or ""
    segs = [x.strip() for x in card.split(" / ")]
    if len(segs) >= 2 and segs[1] in _COUNTRIES:
        return [segs[1]]
    return []


def normalize_interest(i: dict) -> tuple[dict, dict] | None:
    """把一条 interest 拆成 (记录, 元数据)，与旧导出格式保持兼容。"""
    s = i.get("subject") or {}
    sid = str(s.get("id") or "")
    if not sid:
        return None
    rating = i.get("rating") or {}
    srating = s.get("rating") or {}
    status = STATUS_MAP.get(i.get("status", ""), "watched")
    rec = {
        "cid": str(i.get("id") or ""),
        "raw_status": i.get("status", ""),
        "status": status,
        "sid": sid,
        "title": s.get("title") or "",
        "foreign_title": s.get("original_title") or "",
        "aka": s.get("aka") or [],
        "my_rating": int(rating.get("value") or 0),
        "mark_date": (i.get("create_time") or "")[:10],
        "comment": (i.get("comment") or "").strip(),
        "comment_likes": int(i.get("vote_count") or 0),
        "tags": i.get("tags") or [],
        "url": f"https://movie.douban.com/subject/{sid}/",
    }
    meta = {
        "sid": sid,
        "title": s.get("title") or "",
        "original_title": s.get("original_title") or "",
        "year": str(s.get("year") or ""),
        "is_tv": (s.get("subtype") == "tv") or bool(s.get("is_tv")),
        "subtype": s.get("subtype") or "",
        "genres": s.get("genres") or [],
        "countries": s.get("countries") or _countries_fallback(s),
        "languages": s.get("languages") or [],
        "durations": s.get("durations") or [],
        "directors": [p.get("name", "") for p in s.get("directors") or []],
        "actors": [p.get("name", "") for p in (s.get("actors") or [])][:8],
        "aka": s.get("aka") or [],
        "douban_rating": srating.get("value") or None,
        "douban_votes": srating.get("count") or 0,
        "cover_url": s.get("cover_url") or (s.get("pic") or {}).get("url") or "",
        "url": s.get("url") or f"https://movie.douban.com/subject/{sid}/",
    }
    return rec, meta


def export_all(client, uid: str, page_size: int = 50, max_items: int = 0) -> tuple[list[dict], dict]:
    """分页拉取全部 interests（含各状态），返回 (records, meta_by_sid)。"""
    records: list[dict] = []
    meta: dict[str, dict] = {}
    seen: set[str] = set()
    start, total = 0, None
    page = 1
    while True:
        url = (
            f"https://m.douban.com/rexxar/api/v2/user/{uid}/interests"
            f"?start={start}&count={page_size}&type=movie"
        )
        body = client.get(
            url,
            referer=f"https://m.douban.com/people/{uid}/",
            accept="application/json",
        )
        d = json.loads(body)
        items = d.get("interests") or []
        if total is None:
            total = d.get("total") or 0
        if not items:
            break
        for it in items:
            n = normalize_interest(it)
            if not n:
                continue
            rec, m = n
            # 接口按时间倒序，首次出现即最新状态
            if rec["sid"] in seen:
                continue
            seen.add(rec["sid"])
            records.append(rec)
            if m.get("title"):
                meta[rec["sid"]] = m
        print(
            f"[interests] 已取 {len(records)}/{total}"
            f"（第 {page} 页，done/wish/doing 按 raw_status 区分）",
            flush=True,
        )
        start += page_size
        page += 1
        if max_items and len(records) >= max_items:
            break
        if total and start >= total:
            break
        # 注意：接口单页可能返回 page_size-1 条，不能用「不满一页」判断结束
    return records, meta
