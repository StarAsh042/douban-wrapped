"""导出编排：分页抓取三类记录 → 元数据补全 → 合并导出 CSV/JSONL。"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

from . import enrich as enrich_mod
from . import parser
from .browser_cookie import describe, extract_douban_cookie, login_and_get_cookie
from .client import ChallengeError, DoubanClient, LoginRequiredError

STATUS_PATHS = {"watched": "collect", "watching": "do", "wish": "wish"}
STATUS_NAMES = {"watched": "看过", "watching": "在看", "wish": "想看"}

CSV_FIELDS = [
    "status", "sid", "title", "original_title", "year", "is_tv",
    "my_rating", "mark_date", "comment", "comment_likes", "tags",
    "genres", "countries", "directors", "actors", "languages",
    "douban_rating", "douban_votes", "url",
]


def resolve_uid(client: DoubanClient, uid_arg: str | None) -> str:
    """确定豆瓣用户 ID：--uid > cookie 里的 dbcl2 > /mine 重定向。"""
    if uid_arg:
        return uid_arg
    m = re.search(r'dbcl2="?(\d{5,15}):', client.cookie)
    if m:
        print(f"[uid] 从 cookie 的 dbcl2 解析出用户 ID: {m.group(1)}")
        return m.group(1)
    for url in ("https://movie.douban.com/mine", "https://www.douban.com/mine/"):
        final = client.get_final_url(url)
        m = re.search(r"people/(\w+)/?", final)
        if m:
            print(f"[uid] 通过重定向解析出用户 ID: {m.group(1)}")
            return m.group(1)
    raise LoginRequiredError(
        "无法确定豆瓣用户 ID：cookie 缺失或已失效。\n"
        "请按 README 重新复制完整 Cookie 到 cookie.txt，或用 --uid 指定用户 ID。"
    )


def _load_state(out_dir: Path) -> dict:
    f = out_dir / "state.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(out_dir: Path, state: dict) -> None:
    (out_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def export_list(
    client: DoubanClient, uid: str, status: str, out_dir: Path,
    max_pages: int = 0, known: set[str] | None = None,
) -> list[dict]:
    """抓取一类记录的所有分页，逐页落盘 JSONL，返回去重后的记录。

    known 为已同步过的 sid 集合（增量模式）：列表按标记时间倒序，
    翻到整页都是已知记录即可停 —— 日常同步只抓新增。
    """
    path = STATUS_PATHS[status]
    out_file = out_dir / f"{status}.jsonl"
    records: list[dict] = []
    page, total, max_page = 1, None, 1
    while True:
        url = (
            f"https://movie.douban.com/people/{uid}/{path}"
            f"?sort=time&mode=detail&start={(page - 1) * parser.PAGE_SIZE}"
        )
        html = client.get(url)
        if "accounts.douban.com" in html[:3000]:
            raise LoginRequiredError(f"登录态失效（{STATUS_NAMES[status]} 第 {page} 页）")
        total = parser.parse_total(html) or total
        max_page = max(max_page, parser.parse_max_page(html))
        if total:
            max_page = max(max_page, -(-total // parser.PAGE_SIZE))
        items = parser.parse_items(html, status)
        if not items:
            break
        if known is not None and all(it["sid"] in known for it in items):
            records.extend(items)
            print(f"[{STATUS_NAMES[status]}] 第 {page} 页均为已同步记录，增量结束")
            break
        records.extend(items)
        with out_file.open("a", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        print(
            f"[{STATUS_NAMES[status]}] 第 {page}/{max_page} 页，"
            f"累计 {len(records)} 条（共 {total if total is not None else '?'}）",
            flush=True,
        )
        if max_pages and page >= max_pages:
            print(f"[{STATUS_NAMES[status]}] 达到 --max-pages 限制，提前结束")
            break
        if page >= max_page or (total and len(records) >= total):
            break
        page += 1
    dedup: dict[str, dict] = {}
    for it in records:
        dedup[it["sid"]] = it
    return list(dedup.values())


def enrich_records(
    client: DoubanClient, records: list[dict], out_dir: Path, limit: int = 0
) -> dict[str, dict]:
    """为所有唯一条目补全元数据，带缓存与连续失败熔断。"""
    cache_dir = out_dir / "enrich_cache"
    seen: set[str] = set()
    sids: list[str] = []
    for r in records:
        if r["sid"] not in seen:
            seen.add(r["sid"])
            sids.append(r["sid"])
    if limit:
        sids = sids[:limit]

    meta: dict[str, dict] = {}
    consecutive_fail = 0
    for i, sid in enumerate(sids, 1):
        try:
            info = enrich_mod.enrich_cached(client, sid, cache_dir)
        except ChallengeError as e:
            info = None
            consecutive_fail += 1
            if consecutive_fail >= 5:
                print(
                    f"\n[enrich] 连续 {consecutive_fail} 次触发反爬，中止补全。\n"
                    f"已缓存的条目不受影响，稍后重跑会自动跳过它们。建议：降低频率或更新 cookie。\n{e}",
                    file=sys.stderr,
                )
                break
        except (RuntimeError, OSError, ValueError) as e:
            info = None
            print(f"[enrich] sid={sid} 失败: {e}", file=sys.stderr)
        else:
            consecutive_fail = 0
        if info:
            meta[sid] = info
        if i % 20 == 0 or i == len(sids):
            print(f"[enrich] 进度 {i}/{len(sids)}，成功 {len(meta)}", flush=True)
    return meta


def _rescue_records(out_dir: Path, current: list[dict]) -> list[dict]:
    """找回批量接口不返回的记录：在看条目、已删除/下架条目（旧 JSONL 里犹存者）。"""
    have = {(r["status"], r["sid"]) for r in current}
    rescued: list[dict] = []
    for f in sorted(out_dir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (r.get("status"), r.get("sid"))
            if key in have or key[0] not in STATUS_PATHS or not key[1]:
                continue
            have.add(key)
            rescued.append(r)
    return rescued


def merge_csv(records: list[dict], meta: dict[str, dict], out_file: Path) -> None:
    """合并记录与元数据，导出 CSV（utf-8-sig，Excel 可直接打开）。"""
    with out_file.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in records:
            m = meta.get(r["sid"], {})
            rating = m.get("douban_rating")
            w.writerow(
                {
                    "status": r["status"],
                    "sid": r["sid"],
                    "title": m.get("title") or r["title"],
                    "original_title": m.get("original_title") or r.get("foreign_title", ""),
                    "year": m.get("year", ""),
                    "is_tv": int(bool(m.get("is_tv"))),
                    "my_rating": r.get("my_rating") or "",
                    "mark_date": r.get("mark_date", ""),
                    "comment": r.get("comment", ""),
                    "comment_likes": r.get("comment_likes") or "",
                    "tags": "|".join(r.get("tags") or []),
                    "genres": "|".join(m.get("genres") or []),
                    "countries": "|".join(m.get("countries") or []),
                    "directors": "|".join(m.get("directors") or []),
                    "actors": "|".join(m.get("actors") or []),
                    "languages": "|".join(m.get("languages") or []),
                    "douban_rating": "" if rating is None else rating,
                    "douban_votes": m.get("douban_votes") or "",
                    "url": r.get("url", ""),
                }
            )


def _extract_browser_cookie(browser: str, cookie_file: Path,
                            login_timeout: float = 300.0) -> str:
    """从本机浏览器提取豆瓣 cookie（无登录态时自动弹出登录窗口）并存盘。"""
    try:
        cookie, where = extract_douban_cookie(browser)
    except (RuntimeError, OSError) as e:
        print(f"[cookie] 浏览器提取失败: {e}", file=sys.stderr)
        return ""
    if "dbcl2=" not in cookie:
        print(f"[cookie] {describe(cookie)}", file=sys.stderr)
        try:
            cookie, where = login_and_get_cookie(browser, timeout=login_timeout)
        except (RuntimeError, OSError) as e:
            print(f"[cookie] 登录流程未完成: {e}", file=sys.stderr)
            return ""
    print(f"[cookie] 已从 {where} 获取: {describe(cookie)}")
    try:
        cookie_file.write_text(cookie, encoding="utf-8")
        print(f"[cookie] 已保存到 {cookie_file}，下次运行直接复用")
    except OSError as e:
        print(f"[cookie] 保存失败（不影响本次运行）: {e}", file=sys.stderr)
    return cookie


def load_cookie(args) -> str:
    """优先级：--cookie > 环境变量 > 浏览器提取/登录 > cookie.txt。"""
    cookie = args.cookie or os.environ.get("DOUBAN_COOKIE", "")
    if cookie:
        return cookie
    cookie_file = Path(getattr(args, "cookie_file", "cookie.txt"))
    cfb = getattr(args, "cookie_from_browser", "") or ""
    if cfb != "none":
        # cookie.txt 里已有登录态才直接复用，否则重新提取（必要时弹登录窗口）
        if cookie_file.exists():
            stale = cookie_file.read_text(encoding="utf-8", errors="ignore").strip()
            if "dbcl2=" in stale:
                return stale
        cookie = _extract_browser_cookie(cfb or "auto", cookie_file)
        if cookie:
            return cookie
    if cookie_file.exists():
        return cookie_file.read_text(encoding="utf-8", errors="ignore").strip()
    print(
        "提示: 未获得豆瓣登录态。可运行 python -m douban_wrapped login 完成登录，\n"
        "或手动复制浏览器 Cookie 保存到 cookie.txt。\n",
        file=sys.stderr,
    )
    return ""


def build_client(args) -> DoubanClient:
    cookie = load_cookie(args)
    return DoubanClient(cookie=cookie, delay=(args.delay, args.delay * 1.8))


def _safe_stdio() -> None:
    """Windows GBK 控制台下避免特殊字符导致打印崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            pass


def run(argv: list[str] | None = None) -> int:
    _safe_stdio()
    ap = argparse.ArgumentParser(
        prog="douban-wrapped", description="导出豆瓣观影记录并生成品味画像数据"
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="抓取看过/在看/想看 + 元数据补全，输出 CSV")
    for p in (p_export,):
        p.add_argument("--cookie-file", default="cookie.txt", help="cookie 文件路径")
        p.add_argument("--cookie", default=None, help="直接传 Cookie 字符串（或用环境变量 DOUBAN_COOKIE）")
        p.add_argument("--cookie-from-browser", default="", metavar="BROWSER",
                       help="指定从哪个浏览器提取 cookie: auto/cent/edge/chrome/firefox，none 禁用自动提取")
        p.add_argument("--uid", default=None, help="豆瓣用户 ID（默认自动从 cookie 解析）")
        p.add_argument("--types", default="watched,wish", help="导出哪些列表: watched,watching,wish")
        p.add_argument("--out", default="data", help="输出目录")
        p.add_argument("--delay", type=float, default=2.0, help="平均请求间隔秒数（默认 2.0，宁可慢勿封号）")
        p.add_argument("--max-pages", type=int, default=0, help="每个列表最多抓几页（0=全部，调试用）")
        p.add_argument("--no-enrich", action="store_true", help="跳过条目元数据补全")
        p.add_argument("--no-fast", action="store_true", help="禁用批量接口，强制走列表抓取")
        p.add_argument("--enrich-limit", type=int, default=0, help="最多补全多少条（0=全部，调试用）")
        p.add_argument("--incremental", action="store_true",
                       help="增量同步：翻到整页已同步记录即停（state.json 记录进度）")
        p.add_argument("--no-login", action="store_true", help="登录态失效时不自动弹登录窗口")

    p_enrich = sub.add_parser("enrich", help="基于已有 JSONL 重跑元数据补全并重建 CSV")
    p_enrich.add_argument("--cookie-file", default="cookie.txt")
    p_enrich.add_argument("--cookie", default=None)
    p_enrich.add_argument("--cookie-from-browser", default="", metavar="BROWSER")
    p_enrich.add_argument("--uid", default=None)
    p_enrich.add_argument("--out", default="data")
    p_enrich.add_argument("--delay", type=float, default=2.0)
    p_enrich.add_argument("--enrich-limit", type=int, default=0)

    p_cookie = sub.add_parser("cookie", help="从本机浏览器提取豆瓣 Cookie（无登录态会弹出登录窗口）")
    p_cookie.add_argument("--browser", default="auto", choices=["auto", "cent", "edge", "chrome", "firefox"])
    p_cookie.add_argument("--cookie-file", default="cookie.txt")
    p_cookie.add_argument("--timeout", type=float, default=300.0, help="登录窗口最长等待秒数")

    p_login = sub.add_parser("login", help="弹出豆瓣登录窗口，登录后自动保存 Cookie")
    p_login.add_argument("--browser", default="auto", choices=["auto", "cent", "edge", "chrome", "firefox"])
    p_login.add_argument("--cookie-file", default="cookie.txt")
    p_login.add_argument("--timeout", type=float, default=300.0, help="登录窗口最长等待秒数")

    p_analyze = sub.add_parser("analyze", help="基于 data/movies.csv 生成品味画像 profile.json")
    p_analyze.add_argument("--csv", default="data/movies.csv")
    p_analyze.add_argument("--out", default="data/profile.json")
    p_analyze.add_argument("--uid", default="")

    sub.add_parser("report", help="基于 data/profile.json 生成年度报告页 output/report.html")

    p_all = sub.add_parser("all", help="全流程：登录(如需) -> 导出 -> 画像 -> 报告页")
    p_all.add_argument("--cookie-file", default="cookie.txt")
    p_all.add_argument("--cookie", default=None)
    p_all.add_argument("--cookie-from-browser", default="", metavar="BROWSER")
    p_all.add_argument("--uid", default=None)
    p_all.add_argument("--types", default="watched,watching,wish")
    p_all.add_argument("--out", default="data")
    p_all.add_argument("--delay", type=float, default=2.0)
    p_all.add_argument("--max-pages", type=int, default=0)
    p_all.add_argument("--no-enrich", action="store_true")
    p_all.add_argument("--no-fast", action="store_true")
    p_all.add_argument("--enrich-limit", type=int, default=0)
    p_all.add_argument("--incremental", action="store_true")
    p_all.add_argument("--no-login", action="store_true")
    p_all.add_argument("--report-out", default="output/report.html")
    p_all.add_argument("--open", action="store_true", help="完成后用系统默认浏览器打开报告页")

    sub.add_parser("ai", help="调用 OpenAI 兼容 API 生成 AI 观影人格（需设置 API Key）")

    p_ui = sub.add_parser("ui", help="启动傻瓜式控制台（本地网页，浏览器自动打开）")
    p_ui.add_argument("--port", type=int, default=8767)

    p_pair = sub.add_parser("pair", help="双人契合度：与另一份 movies.csv 对比")
    p_pair.add_argument("--csv1", default="data/movies.csv")
    p_pair.add_argument("--csv2", required=True, help="对方的 movies.csv")
    p_pair.add_argument("--name2", default="TA")
    p_pair.add_argument("--out", default="data/pair.json")

    p_lb = sub.add_parser("letterboxd", help="把「看过」记录导出为 Letterboxd 兼容 CSV")
    p_lb.add_argument("--csv", default="data/movies.csv")
    p_lb.add_argument("--out", default="output/letterboxd.csv")

    args = ap.parse_args(argv)

    if args.command in ("cookie", "login"):
        cookie = _extract_browser_cookie(args.browser, Path(args.cookie_file),
                                         login_timeout=args.timeout)
        return 0 if cookie else 1

    if args.command == "ui":
        from . import ui
        return ui.run(args.port)

    if args.command == "pair":
        from . import pair
        return pair.run(args.csv1, args.csv2, args.name2, args.out)

    if args.command == "analyze":
        from . import analyze
        return analyze.run(args.csv, args.out, args.uid)

    if args.command == "report":
        from . import report
        return report.run()

    if args.command == "ai":
        from . import ai
        return ai.run()

    if args.command == "letterboxd":
        from . import letterboxd
        return letterboxd.run(args.csv, args.out)
    client = build_client(args)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.command in ("export", "all"):
        uid = ""
        all_records: list[dict] = []
        meta: dict[str, dict] = {}
        state = _load_state(out_dir) if args.incremental else {}
        for attempt in (1, 2):
            try:
                uid = resolve_uid(client, args.uid)
                all_records, meta = [], {}
                fast_used = False
                # 快路径：批量 interests 接口，一次带回条目元数据（~20 次请求搞定全部）
                if not getattr(args, "no_fast", False) and client.cookie:
                    try:
                        from . import interests
                        print("[fast] 使用批量兴趣接口导出（含元数据）...")
                        all_records, meta = interests.export_all(client, uid)
                        fast_used = True
                        want = {s.strip() for s in args.types.split(",") if s.strip() in STATUS_PATHS}
                        all_records = [r for r in all_records if r["status"] in want]
                        rescued = _rescue_records(out_dir, all_records)
                        if rescued:
                            print(f"[rescue] 从旧 JSONL 找回 {len(rescued)} 条批量接口未返回的记录")
                            all_records.extend(rescued)
                    except (ChallengeError, LoginRequiredError, RuntimeError, ValueError) as e:
                        print(f"\n[fast] 批量接口不可用，回退列表抓取: {e}\n", file=sys.stderr)
                        all_records = []
                if not fast_used:
                    for status in [s.strip() for s in args.types.split(",") if s.strip() in STATUS_PATHS]:
                        known = set(state.get(status, [])) if args.incremental else None
                        # 登录失效要穿透到重试层，不能按列表吞掉
                        try:
                            all_records.extend(
                                export_list(client, uid, status, out_dir, args.max_pages, known)
                            )
                        except (ChallengeError, RuntimeError) as e:
                            print(f"\n[{STATUS_NAMES[status]}] 导出中断: {e}\n", file=sys.stderr)
                break
            except LoginRequiredError as e:
                if attempt == 2 or getattr(args, "no_login", False):
                    print(f"\n[export] 需要登录: {e}", file=sys.stderr)
                    return 1
                print("\n[login] 登录态失效，自动弹出登录窗口...")
                try:
                    cookie, _ = login_and_get_cookie(
                        getattr(args, "cookie_from_browser", "") or "auto"
                    )
                except (RuntimeError, OSError) as e2:
                    print(f"[login] 重新登录失败: {e2}", file=sys.stderr)
                    return 1
                client.cookie = cookie
                Path(args.cookie_file).write_text(cookie, encoding="utf-8")
        if not all_records:
            print("未导出任何记录。")
            return 1

        if args.incremental:
            state["synced_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            for status in STATUS_PATHS:
                state[status] = sorted({r["sid"] for r in all_records if r["status"] == status})
            _save_state(out_dir, state)

        # 落盘 JSONL（覆盖写，含 tags 字段）
        for status in STATUS_PATHS:
            sub = [r for r in all_records if r["status"] == status]
            if sub:
                with (out_dir / f"{status}.jsonl").open("w", encoding="utf-8") as f:
                    for it in sub:
                        f.write(json.dumps(it, ensure_ascii=False) + "\n")

        if not args.no_enrich:
            missing = [r for r in all_records if r["sid"] not in meta]
            if missing:
                print(f"\n补全 {len(missing)} 个缺失元数据的条目（带缓存，可 Ctrl+C 中断后续跑）...")
                meta.update(enrich_records(client, missing, out_dir, args.enrich_limit))
            else:
                print("\n元数据已由批量接口全部带回，无需逐条补全。")
            # 国家回填：批量接口的 subject 常缺 countries，逐条补齐（带缓存）
            need_cc = [r for r in all_records if r["status"] == "watched"
                       and not (meta.get(r["sid"], {}).get("countries"))]
            if need_cc:
                print(f"\n回填 {len(need_cc)} 个条目的国家/地区（带缓存）...")
                meta.update(enrich_records(client, need_cc, out_dir, args.enrich_limit))
        # 持久化元数据（封面、国家等，供海报墙与画像使用）
        (out_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    else:  # enrich
        do_lists = False
        all_records = []
        for status in STATUS_PATHS:
            f = out_dir / f"{status}.jsonl"
            if f.exists():
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        all_records.append(json.loads(line))
        dedup: dict[tuple[str, str], dict] = {}
        for r in all_records:
            dedup[(r["status"], r["sid"])] = r
        all_records = list(dedup.values())
        if not all_records:
            print(f"{out_dir} 下没有 JSONL 数据，请先运行 export。")
            return 1
        meta = enrich_records(client, all_records, out_dir, args.enrich_limit)
        try:
            uid = resolve_uid(client, getattr(args, "uid", None))
        except (LoginRequiredError, RuntimeError):
            uid = "unknown"

    csv_file = out_dir / "movies.csv"
    merge_csv(all_records, meta, csv_file)
    summary = {
        "uid": uid,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "counts": {s: sum(1 for r in all_records if r["status"] == s) for s in STATUS_PATHS},
        "enriched": len(meta),
    }
    (out_dir / "export_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n完成! CSV: {csv_file}")
    print(f"摘要: {json.dumps(summary, ensure_ascii=False)}")

    if args.command == "all":
        from . import analyze, report

        analyze.run(str(out_dir / "movies.csv"), str(out_dir / "profile.json"), str(uid))
        # 配了 API Key 就顺手生成 AI 叙事（失败不影响报告）
        import os as _os

        if _os.environ.get("DOUBAN_WRAPPED_API_KEY") or _os.environ.get("OPENAI_API_KEY"):
            try:
                from . import ai

                ai.run(str(out_dir / "profile.json"), str(out_dir / "movies.csv"),
                       str(out_dir / "narrative.json"))
            except (RuntimeError, OSError) as e:
                print(f"AI 叙事跳过: {e}")
        report.run(str(out_dir / "profile.json"), args.report_out)
        if getattr(args, "open", False):
            import webbrowser

            webbrowser.open(Path(args.report_out).resolve().as_uri())
    return 0
