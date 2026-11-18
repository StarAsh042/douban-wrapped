"""从本机浏览器直接提取豆瓣 Cookie。

Chromium 系（Cent Browser / Edge / Chrome）：
    最小化复制用户配置 -> 无头浏览器加载豆瓣 -> CDP Storage.getCookies -> 过滤 douban.com
    （浏览器自己负责解密 cookie，因此对 v10/v20 各种加密方案都通用）

Firefox：直接读 cookies.sqlite（明文）。

安全说明：cookie 值只写入本地文件/内存，任何打印输出都不包含 cookie 值。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DOUBAN_URL = "https://movie.douban.com/"
_DEVTOOLS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# Edge 是 Windows 自带浏览器，放最前面保证任何电脑开箱即用；
# Chrome 次之；Cent Browser 等小众 Chromium 系再往后兜底。
_AUTO_ORDER = ("edge", "chrome", "cent", "firefox")
_LOGIN_ORDER = ("edge", "chrome", "cent")


# ---------------------------------------------------------------- 浏览器发现


def _reg_query_value(key_path: str, value_name: str = "ve") -> str | None:
    """读取注册表值（仅 Windows）。value_name='ve' 表示默认值。"""
    try:
        args = ["reg", "query", key_path]
        if value_name == "ve":
            args.append("/ve")
        else:
            args += ["/v", value_name]
        raw = subprocess.run(args, capture_output=True, timeout=10).stdout or b""
    except (OSError, subprocess.SubprocessError):
        return None
    # reg.exe 输出为本地 ANSI 编码（中文系统是 GBK）
    if os.name == "nt":
        out = raw.decode("mbcs", errors="replace")
    else:
        out = raw.decode("utf-8", errors="replace")
    for line in out.splitlines():
        if "REG_SZ" in line:
            parts = line.split("REG_SZ", 1)
            if len(parts) == 2:
                return parts[1].strip().strip('"')
    return None


def _find_chromium(browser: str) -> tuple[Path | None, Path | None]:
    """返回 (可执行文件, User Data 目录)，找不到则为 None。"""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    lad = Path(os.environ.get("LOCALAPPDATA", ""))
    exe_candidates: list[Path] = []
    if browser == "cent":
        # 注册表 Uninstall 里的 DisplayIcon / InstallLocation 最可靠
        for key in (
            r"HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\CentBrowser",
            r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\CentBrowser",
            r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\CentBrowser",
        ):
            icon = _reg_query_value(key, "DisplayIcon")
            if icon:
                exe = re.sub(r",\s*\d+\s*$", "", icon).strip('"')
                exe_candidates.append(Path(exe))
                exe_candidates.append(Path(exe).parent / "chrome.exe")
            loc = _reg_query_value(key, "InstallLocation")
            if loc:
                exe_candidates.append(Path(loc) / "chrome.exe")
        for base in (pf86, pf, lad):
            exe_candidates += list(Path(base).glob("CentBrowser/*/chrome.exe"))
            exe_candidates += list(Path(base).glob("CentBrowser/chrome.exe"))
        userdata = lad / "CentBrowser" / "User Data"
    elif browser == "edge":
        exe_candidates = [
            Path(pf86) / "Microsoft/Edge/Application/msedge.exe",
            Path(pf) / "Microsoft/Edge/Application/msedge.exe",
            lad / "Microsoft/Edge/Application/msedge.exe",
        ]
        userdata = lad / "Microsoft" / "Edge" / "User Data"
    elif browser == "chrome":
        exe_candidates = [
            Path(pf) / "Google/Chrome/Application/chrome.exe",
            Path(pf86) / "Google/Chrome/Application/chrome.exe",
            lad / "Google/Chrome/Application/chrome.exe",
        ]
        userdata = lad / "Google" / "Chrome" / "User Data"
    else:
        return None, None
    exe = next((e for e in exe_candidates if e.is_file()), None)
    if not userdata.exists():
        return exe, None
    return exe, userdata


def _find_firefox_profiles() -> list[Path]:
    root = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox" / "Profiles"
    if not root.exists():
        return []
    return [p for p in root.iterdir() if (p / "cookies.sqlite").is_file()]


# ------------------------------------------------------------ 配置文件复制


def _copy_retry(src: Path, dst: Path, tries: int = 4) -> bool:
    for i in range(tries):
        try:
            shutil.copy2(src, dst)
            return True
        except OSError:
            time.sleep(0.3 * (i + 1))
    return False


def _snapshot_sqlite(src: Path, dst: Path) -> bool:
    """被浏览器锁住时的兜底：以 immutable 只读方式做一份 SQLite 快照。"""
    try:
        src_con = sqlite3.connect(f"{src.as_uri()}?immutable=1", uri=True)
        dst_con = sqlite3.connect(str(dst))
        with dst_con:
            src_con.backup(dst_con)
        src_con.close()
        dst_con.close()
        return dst.stat().st_size > 0
    except (sqlite3.Error, OSError):
        return False


def _copy_profile_minimal(src: Path, dst: Path) -> None:
    """只复制解密 cookie 所需的最小文件集：Local State + 各 profile 的 Network/。"""
    dst.mkdir(parents=True, exist_ok=True)
    _copy_retry(src / "Local State", dst / "Local State")
    for prof in src.iterdir():
        if not (prof.is_dir() and (prof.name == "Default" or re.fullmatch(r"Profile \d+", prof.name))):
            continue
        p_dst = dst / prof.name
        p_dst.mkdir(exist_ok=True)
        net = prof / "Network"
        if net.is_dir():
            (p_dst / "Network").mkdir(exist_ok=True)
            for f in net.iterdir():
                if f.is_file():
                    _copy_retry(f, p_dst / "Network" / f.name)
        # 旧版布局：Cookies 直接放在 profile 根目录
        for legacy in ("Cookies", "Cookies-journal"):
            f = prof / legacy
            if f.is_file() and not (p_dst / "Network" / legacy).exists():
                _copy_retry(f, p_dst / legacy)
        # Network/Cookies 若复制失败（被锁），用 SQLite 快照兜底
        target = p_dst / "Network" / "Cookies"
        if net.is_dir() and (not target.exists() or target.stat().st_size == 0):
            _snapshot_sqlite(net / "Cookies", target)


# ------------------------------------------------------------------ CDP


class _MiniWS:
    """最小 RFC6455 WebSocket 客户端（仅够 CDP 用，不启用压缩扩展）。"""

    def __init__(self, host: str, port: int, path: str, timeout: float = 30.0):
        self.deadline = time.monotonic() + timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手中断")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise RuntimeError(f"WebSocket 握手被拒绝: {status!r}")
        accept = base64.b64encode(
            hashlib.sha1((key + _DEVTOOLS_GUID).encode()).digest()
        ).decode()
        if accept.encode() not in head:
            raise RuntimeError("WebSocket Sec-WebSocket-Accept 校验失败")
        self.buf = rest

    def _read(self, n: int) -> bytes:
        data = self.buf
        while len(data) < n:
            if time.monotonic() > self.deadline:
                raise TimeoutError("WebSocket 读取超时")
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket 连接中断")
            data += chunk
        self.buf, out = data[n:], data[:n]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode())

    def recv_message(self) -> str:
        """读取一条完整文本消息（自动拼分片，回应 ping）。"""
        msg = b""
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if b2 & 0x80:  # 规范上服务端不掩码，防御性处理
                mask = payload[:4]
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload[4:]))
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise RuntimeError("WebSocket 已被服务端关闭")
            if opcode in (0x1, 0x2, 0x0):
                msg += payload
                if fin:
                    return msg.decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _poll_devtools(port: int, proc: subprocess.Popen, timeout: float = 20.0) -> str | None:
    """轮询 DevTools HTTP 端点，返回浏览器级 WebSocket URL。"""
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data.get("webSocketDebuggerUrl")
        except (urllib.error.URLError, json.JSONDecodeError, OSError):
            time.sleep(0.4)
    return None


def _cdp_douban_cookies(exe: Path, user_data: Path, profile: str,
                        timeout: float = 60.0) -> list[dict]:
    """用复制的配置启动无头浏览器，经 CDP 取全部 cookie，过滤 douban.com。"""
    tmp = Path(tempfile.mkdtemp(prefix="dwb_profile_"))
    proc: subprocess.Popen | None = None
    try:
        _copy_profile_minimal(user_data, tmp)
        last_err: Exception | None = None
        for port in random.sample(range(20000, 30000), 4):
            args = [
                str(exe),
                "--headless",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={tmp}",
                f"--profile-directory={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--disable-extensions",
                "--remote-allow-origins=*",
                "--window-size=800,600",
                DOUBAN_URL,
            ]
            try:
                proc = subprocess.Popen(
                    args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except OSError as e:
                raise RuntimeError(f"无法启动浏览器: {e}") from e
            ws_url = _poll_devtools(port, proc)
            if not ws_url:
                proc.kill()
                proc.wait(timeout=5)
                proc = None
                continue
            try:
                cookies = _cdp_query_cookies(ws_url, timeout)
            except (RuntimeError, OSError, json.JSONDecodeError) as e:
                last_err = e
                proc.kill()
                proc.wait(timeout=5)
                proc = None
                continue
            douban = [c for c in cookies
                      if c.get("domain", "").lstrip(".").endswith("douban.com")]
            if douban:
                return douban
            last_err = last_err or RuntimeError("浏览器已启动但未返回任何 cookie")
        if last_err:
            raise RuntimeError(f"CDP 连接失败: {last_err}")
        return []
        if last_err:
            raise RuntimeError(f"CDP 连接失败: {last_err}")
        raise RuntimeError("无头浏览器未能开启调试端口（可能被安全软件拦截，或版本过旧）")
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        time.sleep(0.2)
        shutil.rmtree(tmp, ignore_errors=True)


def _cdp_query_cookies(ws_url: str, timeout: float) -> list[dict]:
    """连上浏览器级 WebSocket，取全部 cookie。"""
    m = re.match(r"ws://([^:/]+):(\d+)(/.*)$", ws_url)
    if not m:
        raise RuntimeError(f"无法解析 DevTools 地址: {ws_url}")
    ws = _MiniWS(m.group(1), int(m.group(2)), m.group(3), timeout=timeout)
    try:
        for msg_id, method in ((1, "Storage.getCookies"), (2, "Network.getAllCookies")):
            ws.send_text(json.dumps({"id": msg_id, "method": method}))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                resp = json.loads(ws.recv_message())
                if resp.get("id") != msg_id:
                    continue
                result = resp.get("result") or {}
                cookies = result.get("cookies") or []
                if cookies or resp.get("error"):
                    break
            if cookies:
                return cookies
        return []
    finally:
        ws.close()


def _chromium_douban_cookies(browser: str, timeout: float) -> tuple[list[dict], str]:
    exe, user_data = _find_chromium(browser)
    if not exe:
        return [], f"未找到 {browser} 的可执行文件"
    if not user_data:
        return [], f"未找到 {browser} 的用户数据目录"
    profiles = [
        d.name for d in user_data.iterdir()
        if d.is_dir() and (d.name == "Default" or re.fullmatch(r"Profile \d+", d.name))
    ]
    if not profiles:
        profiles = ["Default"]
    collected: list[dict] = []
    for prof in profiles:
        collected.extend(_cdp_douban_cookies(exe, user_data, prof, timeout))
        if any(c.get("name") == "dbcl2" for c in collected):
            break
    return collected, f"{browser} ({exe})"


def _firefox_douban_cookies() -> tuple[list[dict], str]:
    cookies: list[dict] = []
    profiles = _find_firefox_profiles()
    if not profiles:
        return [], "未找到 Firefox 配置"
    tmp = Path(tempfile.mkdtemp(prefix="dwb_ff_"))
    try:
        for prof in profiles:
            db = tmp / f"{prof.name}.sqlite"
            if not (_copy_retry(prof / "cookies.sqlite", db)
                    and db.stat().st_size > 0):
                _snapshot_sqlite(prof / "cookies.sqlite", db)
            if not db.exists():
                continue
            con = sqlite3.connect(str(db))
            try:
                rows = con.execute(
                    "SELECT name, value, host FROM moz_cookies "
                    "WHERE host LIKE '%douban.com'"
                ).fetchall()
            finally:
                con.close()
            cookies.extend({"name": n, "value": v, "domain": h} for n, v, h in rows)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return cookies, f"firefox ({profiles[0].name})"


# ------------------------------------------------------------------ 对外 API


def build_cookie_string(cookies: list[dict]) -> str:
    return "; ".join(
        f"{c['name']}={c['value']}" for c in cookies if c.get("name") and "value" in c
    )


def describe(cookie_str: str) -> str:
    """生成不含 cookie 值的摘要（用于日志打印）。"""
    names = [p.split("=", 1)[0] for p in cookie_str.split("; ") if "=" in p]
    has_login = "dbcl2" in names
    return f"{len(names)} 条 cookie（{', '.join(sorted(set(names)))[:120]}）；登录态(dbcl2): {'有' if has_login else '无'}"


def login_and_get_cookie(browser: str = "auto", timeout: float = 300.0) -> tuple[str, str]:
    """弹出登录窗口：用浏览器副本打开豆瓣登录页，轮询直到出现 dbcl2 登录态。

    返回 (cookie_header, 来源描述)。窗口被关闭或超时则抛 RuntimeError。
    """
    order = (browser,) if browser in _LOGIN_ORDER else _LOGIN_ORDER
    errors: list[str] = []
    for name in order:
        print(f"[login] 尝试用 {name} 打开登录窗口...", flush=True)
        try:
            return _login_flow(name, timeout), name
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            errors.append(f"{name}: {e}")
    raise RuntimeError("登录窗口流程失败。" + "；".join(errors))


def _login_flow(browser: str, timeout: float) -> str:
    exe, user_data = _find_chromium(browser)
    if not exe:
        raise RuntimeError(f"未找到 {browser} 的可执行文件")
    if not user_data:
        raise RuntimeError(f"未找到 {browser} 的用户数据目录")
    profiles = [
        d.name for d in user_data.iterdir()
        if d.is_dir() and (d.name == "Default" or re.fullmatch(r"Profile \d+", d.name))
    ]
    profile = "Default" if "Default" in profiles else (profiles[0] if profiles else "Default")

    tmp = Path(tempfile.mkdtemp(prefix="dwb_login_"))
    proc: subprocess.Popen | None = None
    try:
        _copy_profile_minimal(user_data, tmp)
        ws_url, port = None, None
        for port in random.sample(range(20000, 30000), 4):
            args = [
                str(exe),
                f"--remote-debugging-port={port}",
                f"--user-data-dir={tmp}",
                f"--profile-directory={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--remote-allow-origins=*",
                "--window-size=1100,850",
                DOUBAN_URL,
            ]
            try:
                proc = subprocess.Popen(args)
            except OSError as e:
                raise RuntimeError(f"无法启动浏览器窗口: {e}") from e
            ws_url = _poll_devtools(port, proc, timeout=30)
            if ws_url:
                break
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            proc = None
        if not ws_url:
            raise RuntimeError("无法开启调试端口（可能被安全软件拦截）")

        print(
            "[login] 登录窗口已打开。请在窗口里登录豆瓣（密码或扫码均可），\n"
            "        登录成功后本程序会自动检测并继续；窗口用的是临时副本，不影响你平时的浏览器。",
            flush=True,
        )
        deadline = time.monotonic() + timeout
        start = time.monotonic()
        last_report = 0.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("浏览器窗口被关闭，登录未完成")
            waited = time.monotonic() - start
            if waited - last_report >= 20:
                print(f"[login] 等待登录中... {int(waited)}s", flush=True)
                last_report = waited
            try:
                cookies = _cdp_query_cookies(ws_url, timeout=10)
            except (RuntimeError, OSError, json.JSONDecodeError):
                time.sleep(1.5)
                continue
            douban = [c for c in cookies
                      if c.get("domain", "").lstrip(".").endswith("douban.com")]
            if any(c.get("name") == "dbcl2" for c in douban):
                print(f"[login] 检测到登录成功（等待了 {int(waited)} 秒），窗口即将关闭", flush=True)
                return build_cookie_string(douban)
            time.sleep(2)
        raise RuntimeError(f"等待登录超时（{int(timeout)} 秒），可重跑并用 --timeout 加长")
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        time.sleep(0.3)
        shutil.rmtree(tmp, ignore_errors=True)


def extract_douban_cookie(browser: str = "auto", timeout: float = 60.0) -> tuple[str, str]:
    """提取豆瓣 Cookie 串。返回 (cookie_header, 来源描述)。失败抛 RuntimeError。"""
    order = (browser,) if browser in _AUTO_ORDER else _AUTO_ORDER
    errors: list[str] = []
    for name in order:
        print(f"[cookie] 尝试从 {name} 提取...", flush=True)
        try:
            if name == "firefox":
                cookies, where = _firefox_douban_cookies()
            else:
                cookies, where = _chromium_douban_cookies(name, timeout)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            errors.append(f"{name}: {e}")
            continue
        if cookies:
            cookie_str = build_cookie_string(cookies)
            if not any(c.get("name") == "dbcl2" for c in cookies):
                print(
                    f"[cookie] 从 {where} 找到 {len(cookies)} 条豆瓣 cookie，"
                    "但没有 dbcl2（该浏览器未登录豆瓣）",
                )
            return cookie_str, where
        errors.append(f"{name}: 无豆瓣 cookie")
    raise RuntimeError("未能提取到豆瓣 cookie。各浏览器情况：" + "；".join(errors))
