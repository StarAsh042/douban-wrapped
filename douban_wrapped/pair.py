"""双人契合度：两份 movies.csv 的品味对比（roadmap「双人契合度」落地）。

用法:
    python -m douban_wrapped pair --csv2 TA的movies.csv --name2 小明

输出: 控制台摘要 + data/pair.json（供后续报告页扩展）
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path


def _load(csv_file: Path) -> dict:
    out: dict[str, dict] = {}
    if not csv_file.exists():
        return out
    with csv_file.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("status") != "watched":
                continue

            def _num(v):
                try:
                    return float(v) if v not in ("", None) else None
                except (TypeError, ValueError):
                    return None

            out[r["sid"]] = {
                "title": r.get("title", ""),
                "my": _num(r.get("my_rating")),
                "db": _num(r.get("douban_rating")),
                "year": r.get("year", ""),
            }
    return out


def compare(csv1: Path, csv2: Path, name1: str = "我", name2: str = "TA") -> dict:
    a, b = _load(csv1), _load(csv2)
    common = sorted(set(a) & set(b))
    if not common:
        return {"common": 0}

    both_rated = [(a[s]["my"], b[s]["my"]) for s in common if a[s]["my"] and b[s]["my"]]
    pearson = None
    if len(both_rated) >= 3:
        xs = [p[0] for p in both_rated]
        ys = [p[1] for p in both_rated]
        if statistics.pstdev(xs) > 0 and statistics.pstdev(ys) > 0:
            pearson = round(statistics.correlation(xs, ys), 3)

    def _rows(cond):
        return [
            {"title": a[s]["title"], "year": a[s]["year"],
             name1: a[s]["my"], name2: b[s]["my"]}
            for s in common if cond(a[s], b[s])
        ]

    agree = _rows(lambda x, y: x["my"] and y["my"] and x["my"] >= 4 and y["my"] >= 4)
    split = _rows(lambda x, y: x["my"] and y["my"] and (
        (x["my"] >= 4 and y["my"] <= 2) or (x["my"] <= 2 and y["my"] >= 4)))
    only1 = [s for s in set(a) - set(b)]

    return {
        "names": [name1, name2],
        "total": [len(a), len(b)],
        "common": len(common),
        "common_rate": round(len(common) / max(1, min(len(a), len(b))) * 100, 1),
        "pairs": len(both_rated),
        "pearson": pearson,
        "agree": agree[:12],
        "split": split[:12],
        "only1_count": len(only1),
    }


def _summary_text(res: dict) -> str:
    if res.get("common", 0) == 0:
        return "两部片单没有交集——各看各的宇宙。"
    lines = [
        f"共同看过 {res['common']} 部（占较小片单的 {res['common_rate']}%）",
    ]
    if res.get("pearson") is not None:
        v = res["pearson"]
        word = "高度同步" if v > 0.7 else ("口味相近" if v > 0.4 else ("各有偏好" if v > 0.1 else "南辕北辙"))
        lines.append(f"评分相关性 {v}（{word}，样本 {res['pairs']} 部）")
    lines.append(f"双四星默契片 {len(res.get('agree') or [])} 部 · 极限分歧 {len(res.get('split') or [])} 部")
    return "；".join(lines)


def run(csv1: str = "data/movies.csv", csv2: str = "", name2: str = "TA",
        out_file: str = "data/pair.json") -> int:
    if not csv2:
        print("用法: python -m douban_wrapped pair --csv2 对方的movies.csv [--name2 小明]")
        return 1
    res = compare(Path(csv1), Path(csv2), "我", name2)
    dst = Path(out_file)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(_summary_text(res))
    print(f"详情已写入 {dst}")
    for f in (res.get("agree") or [])[:5]:
        print(f"  默契 ✓ {f['title']}（{f.get(name1)}★ / {f.get(name2)}★）")
    for f in (res.get("split") or [])[:5]:
        print(f"  分歧 ⚡ {f['title']}（{f.get(name1)}★ / {f.get(name2)}★）")
    return 0


def main() -> None:
    import sys
    ap = argparse.ArgumentParser(prog="douban-wrapped pair", description="双人契合度对比")
    ap.add_argument("--csv1", default="data/movies.csv")
    ap.add_argument("--csv2", required=True, help="对方的 movies.csv")
    ap.add_argument("--name2", default="TA")
    ap.add_argument("--out", default="data/pair.json")
    a = ap.parse_args(sys.argv[1:])
    sys.exit(run(a.csv1, a.csv2, a.name2, a.out))


if __name__ == "__main__":
    main()
