"""Letterboxd 兼容 CSV 导出（借鉴 dou_archive 的迁移思路）。

Letterboxd 官方导入格式核心列：Title, Year, Rating10, WatchedDate, Review。
「想看」无法映射，仅导出「看过」；片名优先用原名（匹配率更高）。
"""

from __future__ import annotations

import csv
from pathlib import Path

FIELDS = ["Title", "Year", "Rating10", "WatchedDate", "Review"]


def to_rows(csv_rows: list[dict]) -> list[dict]:
    """把 douban-wrapped 的 movies.csv 行转成 Letterboxd 行，按观看日期排序。"""
    rows = []
    for r in csv_rows:
        if r.get("status") != "watched":
            continue
        title = (r.get("original_title") or r.get("title") or "").strip()
        if not title:
            continue
        try:
            rating = float(r.get("my_rating") or 0)
        except (TypeError, ValueError):
            rating = 0
        rows.append({
            "Title": title,
            "Year": r.get("year") or "",
            "Rating10": f"{rating * 2:g}" if rating else "",
            "WatchedDate": r.get("mark_date") or "",
            "Review": (r.get("comment") or "").strip(),
        })
    rows.sort(key=lambda x: x["WatchedDate"])
    return rows


def run(csv_file: str = "data/movies.csv", out_file: str = "output/letterboxd.csv") -> int:
    src, dst = Path(csv_file), Path(out_file)
    if not src.exists():
        print(f"未找到 {src}，请先运行 export。")
        return 1
    with src.open("r", encoding="utf-8-sig", newline="") as f:
        csv_rows = list(csv.DictReader(f))
    rows = to_rows(csv_rows)
    if not rows:
        print("没有可导出的「看过」记录。")
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"已导出 {len(rows)} 条到 {dst.resolve()}")
    print("导入: letterboxd.com/import -> 选择该 CSV（评分为 10 分制）")
    return 0
