"""HTTP 客户端：统一请求头、限速、重试与反爬挑战检测。仅用标准库。"""

from __future__ import annotations

import random
import sys
import time
import urllib.error
import urllib.request

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class ChallengeError(RuntimeError):
    """触发了豆瓣反爬验证（sec 挑战页）。"""


class LoginRequiredError(RuntimeError):
    """Cookie 缺失或失效。"""


def _is_challenge(body: str) -> bool:
    head = body[:2000]
    return "<title>豆瓣</title>" in head or 'name="sec" id="sec"' in head


class DoubanClient:
    """带限速的豆瓣 HTTP 客户端。cookie 为完整的 Cookie 请求头字符串。"""

    def __init__(
        self,
        cookie: str = "",
        delay: tuple[float, float] = (1.5, 3.0),
        ua: str = DEFAULT_UA,
        verbose: bool = True,
    ):
        self.cookie = cookie.strip()
        self.delay = delay
        self.ua = ua
        self.verbose = verbose

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr)

    def _headers(self, referer: str, accept: str) -> dict[str, str]:
        headers = {
            "User-Agent": self.ua,
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": referer,
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def get(
        self,
        url: str,
        *,
        referer: str = "https://movie.douban.com/",
        accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        retries: int = 3,
    ) -> str:
        """GET 一个页面，带限速与重试。404 直接抛 HTTPError 由调用方处理。"""
        last: Exception | None = None
        for attempt in range(1, retries + 1):
            time.sleep(random.uniform(*self.delay))
            req = urllib.request.Request(url, headers=self._headers(referer, accept))
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                if _is_challenge(body):
                    raise ChallengeError(f"触发反爬挑战页: {url}")
                return body
            except urllib.error.HTTPError as e:
                if e.code in (400, 404):
                    # 条目已删除/无权限等永久性错误，重试没有意义
                    raise
                last = e
                wait = 30 * attempt
                self.log(f"HTTP {e.code}，疑似限流，等待 {wait}s 后重试 ({attempt}/{retries}): {url}")
                time.sleep(wait)
            except ChallengeError as e:
                last = e
                wait = 45 * attempt
                self.log(f"触发反爬挑战，等待 {wait}s 后重试 ({attempt}/{retries}): {url}")
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError) as e:
                last = e
                wait = 10 * attempt
                self.log(f"网络错误，等待 {wait}s 后重试 ({attempt}/{retries}): {url}")
                time.sleep(wait)
        raise RuntimeError(f"请求多次失败: {url}") from last

    def get_final_url(self, url: str) -> str:
        """跟随重定向，返回最终 URL（用于从 /mine 解析用户 ID）。"""
        time.sleep(random.uniform(*self.delay))
        req = urllib.request.Request(
            url, headers=self._headers("https://www.douban.com/", "text/html,*/*")
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.geturl()
        except urllib.error.HTTPError as e:
            if e.code in (403, 418):
                raise LoginRequiredError(f"访问被拒绝(HTTP {e.code})，cookie 可能已失效") from e
            raise
