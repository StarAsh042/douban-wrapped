"""「我的影视」列表页 HTML 解析。"""

from __future__ import annotations

import re
from html import unescape

PAGE_SIZE = 30

_ITEM_SPLIT = re.compile(r'<div class="item comment-item"')
_SID_RE = re.compile(r"movie\.douban\.com/subject/(\d+)/")
_TITLE_RE = re.compile(r'<a title="([^"]*)" href="https://movie\.douban\.com/subject/\d+/"')
_EM_RE = re.compile(r"<em>(.*?)</em>", re.S)
_AKA_RE = re.compile(r"</em>\s*/\s*(.*?)\s*</a>", re.S)
_STARS_RE = re.compile(r'class="rating(\d)-t"')
_DATE_RE = re.compile(r'<span class="date">(\d{4}-\d{2}-\d{2})</span>')
_COMMENT_RE = re.compile(r'<span class="comment">(.*?)</span>', re.S)
_LIKES_RE = re.compile(r'<span class="pl">\((\d+) 有用\)</span>')
_TOTAL_RE = re.compile(r"影视\((\d+)\)")
_PAGE_LINK_RE = re.compile(r'start=\d+[^>]*>\s*(\d+)\s*</a>')
_CID_RE = re.compile(r'data-cid="(\d+)"')

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", unescape(_TAG_RE.sub("", text))).strip()


def parse_total(html: str) -> int | None:
    """从页标题（如「xx看过的影视(218)」）解析记录总数。"""
    m = _TOTAL_RE.search(html)
    return int(m.group(1)) if m else None


def parse_max_page(html: str) -> int:
    """从分页链接推算最大页码。"""
    nums = [int(n) for n in _PAGE_LINK_RE.findall(html)]
    return max(nums) if nums else 1


def parse_items(html: str, status: str) -> list[dict]:
    """解析一页列表，返回记录字典列表。status ∈ watched/watching/wish。"""
    items: list[dict] = []
    for block in _ITEM_SPLIT.split(html)[1:]:
        m_sid = _SID_RE.search(block)
        if not m_sid:
            continue
        sid = m_sid.group(1)

        m = _TITLE_RE.search(block)
        title = unescape(m.group(1)).strip() if m else ""
        m_em = _EM_RE.search(block)
        em = unescape(m_em.group(1)).strip() if m_em else ""
        if not title:
            title = em.split(" / ")[0].strip()
        # em 里常带「中文名 / 原名」，最后一个分段若与 title 不同则视为原名
        foreign = ""
        if " / " in em:
            last_part = em.split(" / ")[-1].strip()
            if last_part and last_part != title:
                foreign = last_part
        # </em> 之后、</a> 之前的文本是又名（港台译名等）
        aka: list[str] = []
        m_aka = _AKA_RE.search(block)
        if m_aka:
            aka = [a.strip() for a in unescape(m_aka.group(1)).split("/")]
            aka = [a for a in aka if a and a != title and a != foreign]

        m = _STARS_RE.search(block)
        my_rating = int(m.group(1)) if m else 0
        m = _DATE_RE.search(block)
        mark_date = m.group(1) if m else ""
        m = _COMMENT_RE.search(block)
        comment = _clean(m.group(1)) if m else ""
        m = _LIKES_RE.search(block)
        likes = int(m.group(1)) if m else 0
        m = _CID_RE.search(block)
        cid = m.group(1) if m else ""

        items.append(
            {
                "cid": cid,
                "status": status,
                "sid": sid,
                "title": title,
                "foreign_title": foreign,
                "aka": aka,
                "my_rating": my_rating,
                "mark_date": mark_date,
                "comment": comment,
                "comment_likes": likes,
                "url": f"https://movie.douban.com/subject/{sid}/",
            }
        )
    return items
