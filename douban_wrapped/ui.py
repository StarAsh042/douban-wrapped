"""傻瓜式控制台：python -m douban_wrapped ui

起一个本地 HTTP 服务（纯标准库），浏览器打开控制台页面：
    ① 登录豆瓣（当前浏览器打开，已登录则直接提取）→ ② 一键全流程 → ③ 打开报告页
后台任务跑在独立子进程，可随时中止，日志实时推送到页面。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

PKG = Path(__file__).resolve().parent
UI_FILE = PKG / "templates" / "ui.html"
OUT = Path("output")  # 相对运行目录
LOG = deque(maxlen=500)
STATE = {"job": None, "started": "", "ok": None, "cancel": False}
_PROC: subprocess.Popen | None = None
_PORT = 8767


class _Tee(io.TextIOBase):
    """把日志环形缓冲与真实流同步写入（供纯内存任务使用）。"""

    def __init__(self, ring: deque, real):
        self.ring = ring
        self.real = real

    def write(self, s):
        if s and s.strip():
            for line in s.rstrip("\n").splitlines():
                self.ring.append(line)
        try:
            self.real.write(s)
        except (OSError, ValueError):
            pass
        return len(s)

    def flush(self):
        try:
            self.real.flush()
        except (OSError, ValueError):
            pass
        return self


def _log(msg: str) -> None:
    LOG.append(str(msg))


def _pump(proc: subprocess.Popen) -> None:
    """逐行把子进程输出推入日志，避免缓冲区把日志憋到进程结束才出现。"""
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        for raw in iter(stream.readline, ""):
            line = raw.rstrip("\r\n")
            if line.strip():
                LOG.append(line)
        stream.close()


def _run_cmd(name: str, cmd: list[str]) -> None:
    """在子进程中执行命令行任务（互斥 + 可中止 + 实时日志）。"""
    global _PROC
    if STATE["job"] is not None:
        _log(f"⚠ 已有任务在跑（{STATE['job']}），如需中止请点「中止运行」")
        return

    def _worker():
        global _PROC
        STATE["job"] = name
        STATE["started"] = time.strftime("%H:%M:%S")
        STATE["ok"] = None
        STATE["cancel"] = False
        _PROC = None
        try:
            _log("$ " + " ".join(cmd))
            child_env = os.environ.copy()
            child_env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=child_env,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            _PROC = proc
            th = threading.Thread(target=_pump, args=(proc,), daemon=True)
            th.start()
            code = proc.wait()
            th.join(timeout=5)
            if STATE["cancel"]:
                _log(f"🛑 {name} 已中止")
                STATE["ok"] = False
            else:
                STATE["ok"] = (code == 0)
                _log(f"✅ {name} 结束（退出码 {code}）")
        except Exception as e:  # noqa: BLE001 —— 后台任务兜底，不能让线程静默死掉
            STATE["ok"] = False
            _log(f"❌ {name} 异常：{e}")
        finally:
            _PROC = None
            STATE["job"] = None
            STATE["cancel"] = False

    threading.Thread(target=_worker, daemon=True, name=f"job-{name}").start()


def _abort_job() -> None:
    """中止当前子进程任务（Windows 下终止整个进程树）。"""
    proc = _PROC
    if proc is None or proc.poll() is not None:
        if STATE["job"] is not None:
            # 纯 Python 任务（如登录）：协作式中止，由任务自身检查 cancel 标志
            STATE["cancel"] = True
            _log("正在中止任务…")
        else:
            _log("当前没有正在运行的任务")
        return
    STATE["cancel"] = True
    _log("正在中止任务…")
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception as e:  # noqa: BLE001
        _log(f"中止失败：{e}")
        return
    _log("🛑 已中止运行")


def _run_inproc(name: str, fn, *args) -> None:
    """在后台线程执行纯 Python 任务（互斥 + 日志捕获）。"""
    if STATE["job"] is not None:
        _log(f"⚠ 已有任务在跑（{STATE['job']}），如需中止请点「中止运行」")
        return

    def _worker():
        STATE["job"] = name
        STATE["started"] = time.strftime("%H:%M:%S")
        STATE["ok"] = None
        STATE["cancel"] = False
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _Tee(LOG, sys.__stdout__)
        try:
            code = fn(*args)
            STATE["ok"] = code == 0
            _log(f"✅ {name} 结束（退出码 {code}）")
        except Exception as e:  # noqa: BLE001
            STATE["ok"] = False
            _log(f"❌ {name} 异常：{e}")
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            STATE["job"] = None
            STATE["cancel"] = False

    threading.Thread(target=_worker, daemon=True, name=f"job-{name}").start()


def _uid_or_empty() -> str:
    import re
    f = Path("cookie.txt")
    if not f.exists():
        return ""
    m = re.search(r'dbcl2="?(\d+):', f.read_text(encoding="utf-8", errors="ignore"))
    return m.group(1) if m else ""


def _pair_job(csv2: str, name2: str) -> int:
    from . import pair
    return pair.run("data/movies.csv", csv2, name2, "data/pair.json")


def _pair_page_job(csv2: str, name2: str, open_page: bool = True) -> int:
    from . import pair, report
    rc = pair.run("data/movies.csv", csv2, name2, "data/pair.json")
    if rc != 0:
        return rc
    rc = report.run_pair()
    if rc != 0:
        return rc
    if open_page:
        webbrowser.open(f"http://127.0.0.1:{_PORT}/output/pair.html")
        _log("已在浏览器打开契合度页面")
    return 0


def _has_login(cookie: str) -> bool:
    """cookie 串里是否含 dbcl2 登录态。"""
    return any(p.strip().startswith("dbcl2=") for p in cookie.split(";"))


def _login_job(timeout: float = 180.0) -> int:
    """登录豆瓣：已登录则直接提取；否则在【当前默认浏览器】打开豆瓣登录页，
    登录成功后轮询从本机浏览器提取 cookie（不再启动独立浏览器窗口）。"""
    from . import browser_cookie as bc

    cookie_file = Path("cookie.txt")

    # 1) cookie.txt 已有登录态 → 直接复用
    if cookie_file.exists():
        stale = cookie_file.read_text(encoding="utf-8", errors="ignore").strip()
        if _has_login(stale):
            _log(f"cookie.txt 已有登录态，直接复用：{bc.describe(stale)}")
            return 0

    # 2) 本机浏览器已登录 → 直接提取，不弹登录页
    _log("正在检查本机浏览器是否已有豆瓣登录态…")
    try:
        cookie, where = bc.extract_douban_cookie("auto")
        if _has_login(cookie):
            cookie_file.write_text(cookie, encoding="utf-8")
            _log(f"已登录（{where}），直接提取：{bc.describe(cookie)}")
            return 0
    except (RuntimeError, OSError) as e:
        _log(f"浏览器提取检查：{e}")

    # 3) 未登录 → 在当前默认浏览器打开豆瓣登录页
    url = "https://accounts.douban.com/passport/login?source=movie"
    _log("未检测到登录态，已在当前浏览器打开豆瓣登录页")
    try:
        webbrowser.open(url)
    except Exception as e:  # noqa: BLE001
        _log(f"打开登录页失败：{e}")
        return 1
    _log("请在刚打开的标签里登录豆瓣（密码或扫码）；登录成功后本任务会自动检测并保存 Cookie。")

    deadline = time.monotonic() + timeout
    start = time.monotonic()
    last_report = 0.0
    while time.monotonic() < deadline:
        if STATE["cancel"]:
            _log("已取消登录等待")
            return 1
        time.sleep(3)
        waited = time.monotonic() - start
        try:
            cookie, where = bc.extract_douban_cookie("auto")
        except (RuntimeError, OSError):
            cookie, where = "", ""
        if cookie and _has_login(cookie):
            cookie_file.write_text(cookie, encoding="utf-8")
            _log(f"✅ 登录成功（{where}，等待 {int(waited)} 秒），已保存 cookie.txt：{bc.describe(cookie)}")
            return 0
        if waited - last_report >= 20:
            _log(f"等待登录中… {int(waited)}s（登录完成后会自动继续）")
            last_report = waited
    _log(f"登录等待超时（{int(timeout)} 秒）。若已登录，可再次点击「登录」直接提取。")
    return 1


def _actions() -> dict:
    return {
        "login": lambda: _run_inproc("登录豆瓣", _login_job, 180.0),
        "all": lambda: _run_cmd("一键全流程", [sys.executable, "-m", "douban_wrapped", "all"]),
        "export": lambda: _run_cmd("导出数据", [sys.executable, "-m", "douban_wrapped", "export", "--types", "watched,watching,wish"]),
        "analyze": lambda: _run_cmd("品味画像", [sys.executable, "-m", "douban_wrapped", "analyze", "--csv", "data/movies.csv", "--out", "data/profile.json", "--uid", _uid_or_empty()]),
        "report": lambda: _run_cmd("生成报告页", [sys.executable, "-m", "douban_wrapped", "report"]),
        "open": _open_report,
        "stop": _abort_job,
        "pair_page": lambda: _run_inproc("契合度页面", _pair_page_job, "data/other_movies.csv", "TA", True),
    }


def _open_report() -> None:
    f = OUT / "report.html"
    if f.exists():
        webbrowser.open(f"http://127.0.0.1:{_PORT}/output/report.html")
        _log("已用系统默认浏览器打开报告页")
    else:
        _log("报告页还不存在，先跑「一键全流程」或「生成报告页」")


def _status() -> dict:
    root = Path(".")
    return {
        "job": STATE["job"],
        "started": STATE["started"],
        "ok": STATE["ok"],
        "log": list(LOG)[-60:],
        "files": {
            "cookie": (root / "cookie.txt").exists(),
            "csv": (root / "data" / "movies.csv").exists(),
            "profile": (root / "data" / "profile.json").exists(),
            "report": (root / "output" / "report.html").exists(),
            "pair": (root / "output" / "pair.html").exists(),
        },
        "time": time.strftime("%H:%M:%S"),
    }


def _serve_static(path: Path) -> bytes | None:
    if not path.exists() or path.is_dir():
        return None
    return path.read_bytes()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    def _proxy_cover(self, url: str) -> None:
        """按需代理豆瓣封面图：带浏览器请求头与登录 Cookie，绕过 CDN 防盗链。"""
        if not url.startswith(("http://", "https://")) or "doubanio.com" not in url:
            self._send(404, b"bad url", "text/plain")
            return
        from . import report as _report
        data = _report._fetch_image(url, _report._image_headers())
        if data is None:
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, data, "image/jpeg")

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/output/cover":
            qs = parse_qs(urlparse(self.path).query)
            self._proxy_cover((qs.get("url") or [""])[0])
            return
        if route in ("/", "/index.html"):
            body = _serve_static(UI_FILE) or "<h1>ui.html 缺失</h1>".encode()
            self._send(200, body, "text/html; charset=utf-8")
        elif route == "/api/profile":
            f = Path("data") / "profile.json"
            if f.exists():
                self._send(200, f.read_bytes(), "application/json; charset=utf-8")
            else:
                self._send(404, b"{}", "application/json")
        elif route == "/api/status":
            self._send(200, json.dumps(_status(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif route.startswith("/output/"):
            rel = unquote(route[len("/output/"):])
            f = (OUT / rel).resolve()
            if OUT.resolve() not in f.parents or not f.exists():
                self._send(404, b"not found", "text/plain")
                return
            ctype = "text/html; charset=utf-8" if f.suffix == ".html" else (
                "audio/mpeg" if f.suffix == ".mp3" else (
                    "image/jpeg" if f.suffix in (".jpg", ".jpeg") else "application/octet-stream"))
            self._send(200, f.read_bytes(), ctype)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        route = urlparse(self.path).path
        if route != "/api/action":
            self._send(404, b"not found", "text/plain")
            return
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) or b"{}"
        text = None
        for enc in ("utf-8", "gbk"):
            try:
                text = raw.decode(enc)
                json.loads(text)
                break
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                text = None
        payload = json.loads(text) if text else {}
        action = payload.get("action", "")
        if action == "pair":
            csv2 = payload.get("csv2", "")
            name2 = payload.get("name2", "TA")
            if not csv2:
                self._send(400, b'{"ok": false, "error": "csv2 required"}', "application/json")
                return
            if not Path(csv2).exists():
                err = json.dumps({"ok": False, "error": "文件不存在: " + csv2}, ensure_ascii=False).encode("utf-8")
                self._send(400, err, "application/json")
                return
            _run_inproc("双人契合度", _pair_job, csv2, name2)
            self._send(200, json.dumps({"ok": True, "started": "pair"}).encode(), "application/json")
            return
        if action == "pair_page":
            csv2 = payload.get("csv2", "data/other_movies.csv")
            name2 = payload.get("name2", "TA")
            if not Path(csv2).exists():
                err = json.dumps({"ok": False, "error": "文件不存在: " + csv2}, ensure_ascii=False).encode("utf-8")
                self._send(400, err, "application/json")
                return
            _run_inproc("契合度页面", _pair_page_job, csv2, name2, True)
            self._send(200, json.dumps({"ok": True, "started": "pair_page"}).encode(), "application/json")
            return
        acts = _actions()
        if action == "open":
            acts["open"]()
            self._send(200, b'{"ok": true}', "application/json")
            return
        if action in acts:
            acts[action]()
            self._send(200, json.dumps({"ok": True, "started": action}).encode(), "application/json")
        else:
            self._send(400, b'{"ok": false, "error": "unknown action"}', "application/json")


def run(port: int = 8767) -> int:
    global _PORT
    from .export import _safe_stdio
    _safe_stdio()
    server = None
    used = port
    for cand in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", cand), Handler)
            used = cand
            break
        except OSError:
            continue
    if server is None:
        print(f"{port}~{port + 9} 端口均被占用，未能启动")
        return 1
    _PORT = used
    LOG.append(f"控制台就绪：http://127.0.0.1:{used}/")
    print(f"控制台运行中：http://127.0.0.1:{used}/  （Ctrl+C 停止）")
    threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{used}/")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n控制台已停止")
    return 0
