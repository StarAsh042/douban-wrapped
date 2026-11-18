"""品味画像分析：读取 data/movies.csv，计算统计画像，输出 data/profile.json。

仅用标准库（csv/json/statistics），供年度报告页与 AI 叙事消费。
"""

from __future__ import annotations

import csv
import json
import random
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

GENRE_MAJOR = [
    "剧情", "喜剧", "动作", "爱情", "科幻", "动画", "悬疑", "惊悚", "恐怖",
    "犯罪", "同性", "音乐", "歌舞", "传记", "历史", "战争", "西部", "奇幻",
    "冒险", "灾难", "武侠", "情色",
]


def _mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 2) if values else None


def _load_rows(csv_file: Path) -> list[dict]:
    with csv_file.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for key in ("my_rating", "douban_rating", "douban_votes", "year", "is_tv"):
            try:
                r[key] = float(r[key]) if key == "douban_rating" else int(r[key] or 0)
            except (ValueError, TypeError):
                r[key] = 0
        r["genres"] = [g for g in (r.get("genres") or "").split("|") if g]
        r["countries"] = [c for c in (r.get("countries") or "").split("|") if c]
        r["directors"] = [d for d in (r.get("directors") or "").split("|") if d]
        r["actors"] = [a for a in (r.get("actors") or "").split("|") if a]
        r["tags"] = [t for t in (r.get("tags") or "").split("|") if t]
    return rows


def _by_status(rows: list[dict], status: str) -> list[dict]:
    return [r for r in rows if r["status"] == status]


def _group_stat(rows: list[dict], key_fn, top: int = 0) -> list[dict]:
    """按 key_fn 分组，输出 count / avg_my / avg_douban / diff，按 count 降序。"""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        for key in key_fn(r):
            buckets[key].append(r)
    stats = []
    for key, items in buckets.items():
        my = [r["my_rating"] for r in items if r["my_rating"]]
        douban = [r["douban_rating"] for r in items if r["douban_rating"]]
        avg_my, avg_db = _mean(my), _mean(douban)
        stats.append({
            "name": key,
            "count": len(items),
            "avg_my": avg_my,
            "avg_douban": avg_db,
            "diff": round((avg_my or 0) * 2 - (avg_db or 0), 2)
            if avg_my and avg_db else None,
        })
    stats.sort(key=lambda s: (-s["count"], s["name"]))
    return stats[:top] if top else stats


def _top_films(rows: list[dict], key, top: int = 12, meta: dict | None = None) -> list[dict]:
    rows = sorted(rows, key=key, reverse=True)[:top]
    return [{
        "title": r["title"],
        "original_title": r["original_title"],
        "year": r["year"],
        "my_rating": r["my_rating"],
        "douban_rating": r["douban_rating"],
        "comment": r["comment"],
        "url": r["url"],
        "cover": (meta or {}).get(r["sid"], {}).get("cover_url", ""),
    } for r in rows]


_STOP_CHARS = set(
    "的了是在我有和就不人都也很到说要去你会着吧呢啊呀哦嘛么把被让向往从对与跟"
    "但可若因为所以如果虽然然而于是或者以及还再又才只更最挺蛮太特别真的一个这部"
    "那个什么没有就是觉得感觉出来上来下去时候时间"
)


def _top_bigrams(comments: list[str], top: int = 24, min_count: int = 3) -> list[dict]:
    """短评高频词：零依赖的二元组词频（过滤虚字），近似中文分词。"""
    counter: Counter[str] = Counter()
    for text in comments:
        for word in re.sub(r"[^\u4e00-\u9fff]+", " ", text).split():
            for i in range(len(word) - 1):
                bg = word[i:i + 2]
                if bg[0] in _STOP_CHARS or bg[1] in _STOP_CHARS:
                    continue
                counter[bg] += 1
    return [{"word": w, "count": c} for w, c in counter.most_common(top) if c >= min_count]


def _longest_streak(dates: list[str]) -> int:
    """最长连续观影天数。"""
    days = sorted({date.fromisoformat(d) for d in dates if d})
    best = cur = 0
    prev = None
    for d in days:
        cur = cur + 1 if (prev is not None and (d - prev).days == 1) else 1
        best = max(best, cur)
        prev = d
    return best


def build_profile(csv_file: Path, uid: str = "") -> dict:
    rows = _load_rows(csv_file)

    # 封面等逐条元数据（export 阶段写入 meta.json）
    meta: dict[str, dict] = {}
    meta_file = Path(csv_file).parent / "meta.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}

    watched = _by_status(rows, "watched")
    wish = _by_status(rows, "wish")
    watching = _by_status(rows, "watching")

    rated = [r for r in watched if r["my_rating"]]
    commented = [r for r in watched if r["comment"]]
    dates = sorted(r["mark_date"] for r in watched if r["mark_date"])

    my_all = [r["my_rating"] for r in rated]
    db_all = [r["douban_rating"] for r in watched if r["douban_rating"]]

    rating_dist = Counter(r["my_rating"] for r in rated)
    monthly = Counter(r["mark_date"][:7] for r in watched if r["mark_date"])
    by_year: dict[str, list[dict]] = defaultdict(list)
    for r in rated:
        if r["mark_date"]:
            by_year[r["mark_date"][:4]].append(r)
    ratings_by_year = []
    for y in sorted(by_year):
        items = by_year[y]
        year_rated = [r for r in items if r["my_rating"]]
        best = max(year_rated, key=lambda r: (r["my_rating"], r["douban_rating"]), default=None)
        ratings_by_year.append({
            "year": y,
            "count": len(items),
            "avg_my": _mean([r["my_rating"] for r in items]),
            "avg_douban": _mean([r["douban_rating"] for r in items if r["douban_rating"]]),
            "best": best["title"] if best else "",
            "best_stars": best["my_rating"] if best else 0,
        })

    # 与大众口味的分歧
    gems = [r for r in watched if r["my_rating"] >= 4 and r["douban_rating"]
            and r["my_rating"] * 2 - r["douban_rating"] >= 1.0]
    gems.sort(key=lambda r: r["my_rating"] * 2 - r["douban_rating"], reverse=True)
    strict = [r for r in watched if 0 < r["my_rating"] <= 2 and r["douban_rating"] >= 8.5]
    strict.sort(key=lambda r: r["douban_rating"] - r["my_rating"], reverse=True)

    # 冷门佳片：评分人数极少但我给了高分
    niche = [r for r in watched if r["my_rating"] >= 4 and 0 < r["douban_votes"] < 5000]

    busiest = max(monthly.items(), key=lambda kv: kv[1]) if monthly else ("", 0)

    # 日历热力图：按天计数 + 最疯狂的年份
    daily_counts = Counter(r["mark_date"] for r in watched if r["mark_date"])
    year_counts = Counter(r["mark_date"][:4] for r in watched if r["mark_date"])
    busiest_year = max(year_counts, key=year_counts.get) if year_counts else ""
    calendar = {d: c for d, c in daily_counts.items() if d.startswith(busiest_year)}
    calendar_all = dict(daily_counts)
    years = sorted(y for y in year_counts if y)
    year_range = [years[0], years[-1]] if years else ["", ""]

    # 品味标签（批量接口带回的自定义标签）
    tag_counter = Counter(t for r in watched for t in r["tags"])
    top_tags = [{"name": t, "count": c} for t, c in tag_counter.most_common(30) if c >= 2]

    # 电影 vs 剧集
    def _mini(rows: list[dict]) -> dict:
        my = [r["my_rating"] for r in rows if r["my_rating"]]
        db = [r["douban_rating"] for r in rows if r["douban_rating"]]
        return {"count": len(rows), "avg_my": _mean(my), "avg_douban": _mean(db)}

    tv_movie = {
        "movie": _mini([r for r in watched if not r["is_tv"]]),
        "tv": _mini([r for r in watched if r["is_tv"]]),
    }

    # 想看画像：接下来准备看什么方向
    wish_stats = {
        "count": len(wish),
        "genres": _group_stat(wish, lambda r: r["genres"], top=10),
        "countries": _group_stat(wish, lambda r: r["countries"], top=8),
    }

    # 品味象限：逐片（豆瓣星, 我的星）
    quadrant = [
        {"t": r["title"], "my": r["my_rating"],
         "db": round(r["douban_rating"] / 2, 2), "y": r["year"]}
        for r in watched if r["my_rating"] and r["douban_rating"]
    ]

    # 导演版图：拍过 ≥2 部的导演（treemap 数据）
    director_tree = [
        {"name": d["name"], "value": d["count"]}
        for d in _group_stat(watched, lambda r: r["directors"][:1])
        if d["count"] >= 2
    ][:40]

    decade_counts = Counter()
    for r in watched:
        if r["year"]:
            decade_counts[f"{int(r['year']) // 10 * 10}s"] += 1

    profile = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "uid": uid,
        "overview": {
            "watched": len(watched),
            "wish": len(wish),
            "watching": len(watching),
            "rated": len(rated),
            "commented": len(commented),
            "avg_my": _mean(my_all),
            "avg_douban": _mean(db_all),
            "first_mark": dates[0] if dates else "",
            "last_mark": dates[-1] if dates else "",
            "movies": sum(1 for r in watched if not r["is_tv"]),
            "tv": sum(1 for r in watched if r["is_tv"]),
        },
        "rating_distribution": {str(k): rating_dist.get(k, 0) for k in range(1, 6)},
        "unrated": len(watched) - len(rated),
        "monthly_activity": dict(sorted(monthly.items())),
        "ratings_by_year": ratings_by_year,
        "genre_stats": _group_stat(watched, lambda r: r["genres"]),
        "major_genre_stats": _group_stat(
            watched, lambda r: [g for g in r["genres"] if g in GENRE_MAJOR]),
        "country_stats": _group_stat(watched, lambda r: r["countries"], top=12),
        "director_stats": _group_stat(
            watched, lambda r: r["directors"][:1], top=15),
        "actor_stats": _group_stat(
            watched, lambda r: [a for a in r["actors"][:3] if a], top=12),
        "decade_stats": dict(sorted(decade_counts.items())),
        "top_rated": _top_films(rated, key=lambda r: (r["my_rating"], r["douban_rating"]), meta=meta),
        "gems": _top_films(gems, key=lambda r: r["my_rating"] * 2 - r["douban_rating"], top=10, meta=meta),
        "strict_picks": _top_films(strict, key=lambda r: r["douban_rating"] - r["my_rating"], top=10, meta=meta),
        "niche_gems": _top_films(niche, key=lambda r: (r["my_rating"], -r["douban_votes"]), top=10, meta=meta),
        "top_bigrams": _top_bigrams([r["comment"] for r in commented]),
        "fun_stats": {
            "longest_streak": _longest_streak([r["mark_date"] for r in watched]),
            "busiest_month": busiest[0],
            "busiest_month_count": busiest[1],
            "active_months": len(monthly),
        },
        "calendar": calendar,
        "calendar_all": calendar_all,
        "year_range": year_range,
        "busiest_year": busiest_year,
        "top_tags": top_tags,
        "tv_movie": tv_movie,
        "wish_stats": wish_stats,
        "quadrant": quadrant,
        "director_tree": director_tree,
    }

    # 规则版观影人格（无 AI Key 的兜底；AI 叙事可用时页面会优先用 narrative.json）
    o = profile["overview"]
    p_tags = []
    if o["avg_my"] is not None and o["avg_my"] <= 3.2:
        p_tags.append("严苛评分党")
    tg = (profile.get("major_genre_stats") or [])
    if tg and tg[0]["count"] >= o["watched"] * 0.35:
        p_tags.append(tg[0]["name"] + "重度患者")
    if o["watched"] and o["tv"] / max(1, o["watched"]) > 0.3:
        p_tags.append("剧集狂魔")
    if busiest[0] and busiest[1] >= o["watched"] * 0.3:
        p_tags.append("爆发式补档")
    if niche:
        p_tags.append("冷门挖掘机")
    tt = (top_tags or [{}])[0]
    if tt.get("name"):
        p_tags.append("「" + tt["name"] + "」爱好者")

    one_liner = ""
    if gems:
        one_liner = (f"大众给 {gems[0]['douban_rating']} 分的《{gems[0]['title']}》，"
                     f"你郑重打了 {gems[0]['my_rating']} 星——眼光这东西，确实不随大流。")
    elif strict:
        one_liner = (f"《{strict[0]['title']}》全网吹上天，你只给了 {strict[0]['my_rating']} 星"
                     "——你打分，从来不看脸色。")
    _avg10 = round(o['avg_my'] * 2, 1) if o['avg_my'] is not None else None
    top_genre = (profile.get('major_genre_stats') or [{}])[0].get('name', '')
    top_tag = tt.get('name', '')
    focus = gems[0]['title'] if gems else (strict[0]['title'] if strict else '')
    harsh = not profile["rating_distribution"].get("5") and (o["avg_my"] or 5) < 3.2
    peak_txt = (f"{busiest[0]} 那个月你一口气看了 {busiest[1]} 部，" if busiest[0] else "")
    # 拆分组合：开场 / 中段 / 收尾各自随机，排列出更多版本的观影人格
    _opens = [
        lambda: f"你共看过 {o['watched']} 部（电影 {o['movies']} / 剧集 {o['tv']}），留下 {_avg10} 分的平均笔迹。",
        lambda: f"银幕亮了 {o['watched']} 次，又暗了 {o['watched']} 次，你的均分停在 {_avg10} 分。",
        lambda: f"{o['watched']} 部片子，像 {o['watched']} 封没有寄出的信。",
        lambda: f"如果观影是一场漫长的漫游，你已走过 {o['watched']} 个站台。",
        lambda: f"你把 {o['watched']} 段光阴折进电影票根，每一段都算数。",
        lambda: f"影评人是别人，你是自己的观众：{o['watched']} 部，均分 {_avg10} 分。",
        lambda: f"这一年你按下 {o['watched']} 次播放键，把 {_avg10} 分悄悄留给银幕。",
        lambda: f"字幕亮起又熄灭，{o['watched']} 部电影在你这里住过，均分 {_avg10} 分。",
    ]
    _mids = [
        lambda: (f"{top_genre}是你反复回望的题材。" if top_genre else "你在各色题材里闲逛，从不设限。"),
        lambda: (f"{peak_txt}那些挤在一起的夜晚，你用{top_genre}取暖。" if busiest[0] and top_genre
                 else (f"你用{top_genre}取暖。" if top_genre else "热闹与冷清，你都照单全收。")),
        lambda: (f"{top_tag}是你的口头禅，{top_genre}是你的目的地。" if top_tag and top_genre
                 else (f"{top_genre}是你的目的地。" if top_genre else f"{top_tag}是你的口头禅。" if top_tag else "你跟着感觉走，从不问路。")),
        lambda: (f"其中 {busiest[1]} 部挤在 {busiest[0]}，像一场蓄谋已久的雨季。" if busiest[0] else "你把节奏握在自己手里。"),
        lambda: (f"你偏爱{top_genre}，从不轻易附和。" if top_genre else "你从不轻易附和。"),
        lambda: (f"你在{top_genre}里找到共鸣，也在冷门里淘金。" if top_genre else "你总在冷门里淘金。"),
        lambda: (f"豆瓣给的 {o['avg_douban']} 分，你只当参考。" if o['avg_douban'] else "大众的分数，你只当参考。"),
        lambda: ("五星吝啬，四星已是你最高的敬意。" if harsh else "你给分宽厚，却从不敷衍。"),
    ]
    _ends = [
        lambda: (f"《{focus}》这样的片子，只有你肯替它多说一句。" if focus else "每一帧都算数。"),
        lambda: "不逢迎，也不苛刻，这就是你的打分方式。",
        lambda: "均分是你留给自己的注脚，不必与人相同。",
        lambda: (f"《{focus}》让你停得最久。" if focus else "总有一部片子，让你迟迟不肯起身。"),
        lambda: "这一路，你给出的是温柔，也是态度。",
        lambda: "在众声喧哗里，你坚持了少数派的体面。",
        lambda: "电影散场，你却把余味带进了生活。",
        lambda: "你为每一部留了一盏灯，包括那些无人问津的。",
    ]
    persona = random.choice(_opens)() + random.choice(_mids)() + random.choice(_ends)()

    profile["rule_persona"] = {
        "tags": p_tags[:4],
        "one_liner": one_liner,
        "persona": persona,
    }
    return profile


def run(csv_file: str = "data/movies.csv", out_file: str = "data/profile.json",
        uid: str = "") -> int:
    src, dst = Path(csv_file), Path(out_file)
    if not src.exists():
        print(f"未找到 {src}，请先运行 export。")
        return 1
    profile = build_profile(src, uid)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(profile, ensure_ascii=False, indent=1), encoding="utf-8")
    o = profile["overview"]
    print(f"画像完成 -> {dst}")
    print(
        f"看过 {o['watched']}（电影 {o['movies']} / 剧集 {o['tv']}），想看 {o['wish']}，"
        f"评分 {o['rated']} 条，短评 {o['commented']} 条\n"
        f"平均我的评分 {o['avg_my']} vs 豆瓣均分 {o['avg_douban']}，"
        f"记录跨度 {o['first_mark']} ~ {o['last_mark']}"
    )
    return 0