"""解析器回归测试。

无参数时用仓库内置夹具（CI 友好）；提供真实页面快照路径时追加深度断言：
    python tests/test_parser.py ../samples/collect_ahbei.html ../samples/wish_ahbei.html
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from douban_wrapped import parser  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "list_page.html"


def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        raise SystemExit(1)
    print(f"ok: {msg}")


def check_fixture():
    html = FIXTURE.read_text(encoding="utf-8")
    check(parser.parse_total(html) == 2, "夹具总数 = 2")
    check(parser.parse_max_page(html) == 2, "夹具最大页码 = 2")
    items = parser.parse_items(html, "watched")
    check(len(items) == 2, f"解析出 2 条 (实际 {len(items)})")

    first = items[0]
    check(first["sid"] == "26752088", f"sid (实际 {first['sid']})")
    check(first["title"] == "我不是药神", f"片名 (实际 {first['title']!r})")
    check(first["my_rating"] == 5, f"评分 = 5 (实际 {first['my_rating']})")
    check(first["mark_date"] == "2018-07-16", f"日期 (实际 {first['mark_date']})")
    check(first["comment"] == "好电影", f"短评 (实际 {first['comment']!r})")
    check(first["comment_likes"] == 965, f"有用数 (实际 {first['comment_likes']})")
    check("中国药神" in first["aka"], f"别名 (实际 {first['aka']})")

    second = items[1]
    check(second["sid"] == "2129039", "第二条 sid")
    check(second["title"] == "Up", f"第二条片名取自 a[title] (实际 {second['title']!r})")
    check(second["my_rating"] == 0, "无评分 -> 0")
    check(second["comment"] == "", "无短评 -> 空")
    check(second["mark_date"] == "2017-08-01", "无评分条目仍解析日期")
    check("冲天救兵(港)" in second["aka"], f"第二条别名 (实际 {second['aka']})")


def check_real_snapshots(collect_html_path, wish_html_path):
    collect_html = Path(collect_html_path).read_text(encoding="utf-8")
    check(parser.parse_total(collect_html) == 218, "快照 collect 总数 = 218")
    items = parser.parse_items(collect_html, "watched")
    check(len(items) == 30, f"快照 collect 解析 30 条 (实际 {len(items)})")
    first = items[0]
    check(first["sid"] == "26752088" and first["title"] == "我不是药神", "快照第一条")
    check(first["my_rating"] == 5 and first["comment"] == "好电影", "快照第一条评分短评")

    wish_html = Path(wish_html_path).read_text(encoding="utf-8")
    witems = parser.parse_items(wish_html, "wish")
    check(len(witems) == 15, f"快照 wish 15 条 (实际 {len(witems)})")
    check(witems[0]["sid"] == "26647117" and witems[0]["my_rating"] == 0, "快照 wish 第一条")


def main(argv):
    check_fixture()
    if len(argv) >= 2:
        check_real_snapshots(argv[0], argv[1])
    print("\n全部通过 [OK]")


if __name__ == "__main__":
    main(sys.argv[1:])
