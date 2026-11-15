# bulk_unsub_pool_controller.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, os, shutil, time, subprocess, webbrowser, json, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import List, Deque, Optional, Set, Tuple
from urllib.parse import urlparse, unquote, urlunparse, parse_qs, urlencode
from urllib.request import url2pathname
from collections import deque, Counter
import re, requests
import hashlib
import sqlite3
import uuid
import sys
import math
from filelock import FileLock, Timeout
from openpyxl import load_workbook

REQ_HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/124 Safari/537.36"}
REQ_TIMEOUT = 15
APP_RE = re.compile(r'/app/(\d+)\b', re.IGNORECASE)

# ========== 简单队列 ==========
class UrlQueue:
    def __init__(self, urls: List[str], single_page_mode: bool = False,
                 state_path: Optional[Path] = None, max_attempts: int = 1,
                 retry_delay: float = 2.0, lease_seconds: float = 180.0, restart: bool = False):
        self.lock = threading.Lock()
        self.single_page_mode = single_page_mode
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self.lease_seconds = lease_seconds
        targets = {}
        for url in urls:
            wid = extract_workshop_id(url)
            if not wid.isdigit():
                raise ValueError(f"无法识别退订物品 ID：{url}")
            targets.setdefault(wid, url)
        self.job = hashlib.sha256(json.dumps(sorted(targets), separators=(",", ":")).encode()).hexdigest()
        self._file_lock = None
        if state_path is not None:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            self._file_lock = FileLock(str(state_path) + f".{self.job[:16]}.lock")
            try:
                self._file_lock.acquire(timeout=0)
            except Timeout as exc:
                raise RuntimeError("同一退订任务正在运行，请先停止已有任务。") from exc
        try:
            self.conn = sqlite3.connect(str(state_path) if state_path is not None else ":memory:", timeout=30, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            with self.conn:
                self.conn.execute("CREATE TABLE IF NOT EXISTS jobs (job TEXT PRIMARY KEY, cooldown REAL NOT NULL DEFAULT 0)")
                self.conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
                    job TEXT NOT NULL, wid TEXT NOT NULL, url TEXT NOT NULL, position INTEGER NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_ts REAL NOT NULL DEFAULT 0,
                    token TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
                    PRIMARY KEY (job, wid))""")
                self.conn.execute("CREATE INDEX IF NOT EXISTS task_ready ON tasks(job, status, next_ts)")
                self.conn.execute("INSERT OR IGNORE INTO jobs(job) VALUES (?)", (self.job,))
                if restart:
                    self.conn.execute("DELETE FROM tasks WHERE job=?", (self.job,))
                for position, (wid, url) in enumerate(targets.items()):
                    self.conn.execute("""INSERT INTO tasks(job,wid,url,position,status,updated) VALUES (?,?,?,?,?,?)
                                      ON CONFLICT(job,wid) DO UPDATE SET url=excluded.url,position=excluded.position""",
                                      (self.job, wid, url, position, "queued", time.time()))
                # A new run resumes every unconfirmed item. Confirmed successes remain committed.
                self.conn.execute("UPDATE tasks SET status='queued',attempts=0,next_ts=0,token='' WHERE job=? AND status!='success'", (self.job,))
                self.conn.execute("UPDATE jobs SET cooldown=0 WHERE job=?", (self.job,))
        except Exception:
            if hasattr(self, "conn"):
                self.conn.close()
            if self._file_lock is not None:
                self._file_lock.release()
            raise

    def _expire(self, now: float) -> None:
        self.conn.execute("""UPDATE tasks SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'queued' END,
                          next_ts=?, token='', error='result_unconfirmed'
                          WHERE job=? AND status='inflight' AND next_ts<=?""",
                          (self.max_attempts, now + self.retry_delay, self.job, now))

    def lease_batch(self, size: int) -> List[dict]:
        now = time.time()
        with self.lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self._expire(now)
            cooldown = self.conn.execute("SELECT cooldown FROM jobs WHERE job=?", (self.job,)).fetchone()[0]
            if cooldown > now:
                return []
            rows = self.conn.execute("""SELECT * FROM tasks WHERE job=? AND status='queued' AND next_ts<=?
                                      ORDER BY attempts,position LIMIT ?""", (self.job, now, size)).fetchall()
            tasks = []
            for row in rows:
                token = uuid.uuid4().hex
                self.conn.execute("UPDATE tasks SET status='inflight',attempts=attempts+1,next_ts=?,token=?,updated=? WHERE job=? AND wid=?",
                                  (now + self.lease_seconds, token, now, self.job, row["wid"]))
                tasks.append({"id": row["wid"], "url": row["url"], "token": token})
            return tasks

    def pop(self) -> str:
        tasks = self.lease_batch(1)
        if not tasks:
            return ""
        task = tasks[0]
        parsed = urlparse(task["url"])
        fragment = parse_qs(parsed.fragment)
        fragment.update(job=[self.job], attempt=[task["token"]])
        return urlunparse(parsed._replace(fragment=urlencode(fragment, doseq=True)))

    def stats(self):
        now = time.time()
        with self.lock, self.conn:
            self._expire(now)
            rows = self.conn.execute("SELECT status,COUNT(*) AS n FROM tasks WHERE job=? GROUP BY status", (self.job,)).fetchall()
            counts = {row["status"]: row["n"] for row in rows}
            totals = self.conn.execute("SELECT COUNT(*) AS total,SUM(attempts>0) AS assigned,SUM(attempts) AS attempts,MIN(CASE WHEN status IN ('queued','inflight') THEN next_ts END) AS next_ts FROM tasks WHERE job=?", (self.job,)).fetchone()
            cooldown = self.conn.execute("SELECT cooldown FROM jobs WHERE job=?", (self.job,)).fetchone()[0]
            waiting_until = max(cooldown, totals["next_ts"] or now)
            return dict(left=counts.get("queued", 0), pending=counts.get("inflight", 0),
                        assigned=totals["assigned"] or 0, done=counts.get("success", 0)+counts.get("failed", 0),
                        ok=counts.get("success", 0), fail=counts.get("failed", 0), total=totals["total"],
                        attempts=totals["attempts"] or 0, retry_after_ms=max(250, min(5000, int((waiting_until-now)*1000))))

    def mark_done(self, ok: bool|None = None, wid: str = "", reason: str = "", token: str = ""):
        now = time.time()
        with self.lock, self.conn:
            row = self.conn.execute("SELECT * FROM tasks WHERE job=? AND wid=?", (self.job, wid)).fetchone()
            if row is None or row["status"] != "inflight" or (token and token != row["token"]):
                return False
            if ok is True:
                status, due = "success", 0
            else:
                status = "failed" if row["attempts"] >= self.max_attempts else "queued"
                delay = min(120.0, self.retry_delay * 2 ** (row["attempts"] - 1))
                if "429" in reason:
                    delay = max(delay, min(120.0, 10.0 * 2 ** (row["attempts"] - 1)))
                    self.conn.execute("UPDATE jobs SET cooldown=MAX(cooldown,?) WHERE job=?", (now+delay, self.job))
                due = now + delay
            self.conn.execute("UPDATE tasks SET status=?,next_ts=?,error=?,updated=? WHERE job=? AND wid=?",
                              (status, due, "" if ok is True else reason or "unconfirmed_failure", now, self.job, wid))
            return True

    def close(self):
        with self.lock:
            self.conn.close()
            if self._file_lock is not None:
                self._file_lock.release()
                self._file_lock = None

QUEUE: UrlQueue|None = None

# 浏览器结果回报与取任务都会刷新活动时间，缺少结果时超时返回非零状态。
LAST_BROWSER_ACTIVITY_TS: float = 0.0
_ACT_LOCK = threading.Lock()

def _touch_activity() -> None:
    global LAST_BROWSER_ACTIVITY_TS
    with _ACT_LOCK:
        LAST_BROWSER_ACTIVITY_TS = time.time()

def _seconds_since_activity() -> float:
    with _ACT_LOCK:
        if LAST_BROWSER_ACTIVITY_TS <= 0:
            return 0.0
        return time.time() - LAST_BROWSER_ACTIVITY_TS

# ========== 回调服务（CORS） ==========
def _cors(h):
    h.send_header("Access-Control-Allow-Origin", "*")
    h.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    h.send_header("Access-Control-Allow-Headers", "content-type")
    h.send_header("Access-Control-Max-Age", "86400")

class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        path = urlparse(self.path).path.rstrip('/')
        if path not in ["/next", "/batch", "/stats", "/report"]:
            self.send_response(404); _cors(self); self.end_headers(); return
        self.send_response(204); _cors(self); self.end_headers()
    
    def do_GET(self):
        path = urlparse(self.path).path.rstrip('/')
        # 获取统计信息
        if path == "/stats":
            stats = QUEUE.stats() if QUEUE else {"left": 0, "assigned": 0, "done": 0, "ok": 0, "fail": 0}
            self.send_response(200); _cors(self)
            self.send_header("Content-Type","application/json"); self.end_headers()
            self.wfile.write(json.dumps(stats).encode('utf-8'))
            return
        # 批量获取 ID（用于单页面模式）
        elif path.startswith("/batch"):
            # 解析批次大小
            query = parse_qs(urlparse(self.path).query)
            try:
                size = max(0, min(500, int(query.get('size', ['10'])[0])))
            except ValueError:
                self.send_response(400); _cors(self); self.end_headers(); return
            requested_job = query.get("job", [""])[0]
            if QUEUE and requested_job and requested_job != QUEUE.job:
                self.send_response(409); _cors(self); self.end_headers(); return
            tasks = []
            if QUEUE and QUEUE.single_page_mode:
                tasks = QUEUE.lease_batch(size)
            ids = [task["id"] for task in tasks]
            _touch_activity()
            print(f"[BATCH] 返回 {len(ids)} 个 workshop ID")
            self.send_response(200); _cors(self)
            self.send_header("Content-Type","application/json"); self.end_headers()
            progress = QUEUE.stats() if QUEUE else {}
            complete = progress.get("left", 0) == 0 and progress.get("pending", 0) == 0
            single = bool(QUEUE and QUEUE.single_page_mode)
            self.wfile.write(json.dumps({"ids": ids if single else None, "mode": "single" if single else "multi",
                                        "tasks": tasks, "durable": single,
                                        "job": QUEUE.job if QUEUE else "", "complete": complete,
                                        "retry_after_ms": progress.get("retry_after_ms", 1000),
                                        "progress": progress}).encode('utf-8'))
            return
        else:
            self.send_response(404); _cors(self); self.end_headers()
    
    def do_POST(self):
        path = urlparse(self.path).path.rstrip('/')
        if path not in ["/next", "/report"]:
            self.send_response(404); _cors(self); self.end_headers(); return
        length = int(self.headers.get('Content-Length','0') or 0)
        raw = self.rfile.read(length) if length else b'{}'
        try:
            data = json.loads(raw.decode('utf-8','ignore'))
        except Exception:
            data = {}
        slot   = (data.get('slot') or '')
        wid    = (data.get('id')   or '')
        status = (data.get('status') or '')
        ok_flag = data.get('ok', None)
        if QUEUE and data.get("job") and data["job"] != QUEUE.job:
            self.send_response(409); _cors(self); self.end_headers(); return

        # 推断成功/失败（允许前端直接传 ok=true/false；否则按 status 粗略判断）
        ok: bool|None = None
        if isinstance(ok_flag, bool):
            ok = ok_flag
        else:
            s = str(status or '').lower()
            if 'fail' in s or 'error' in s:
                ok = False
            elif s:
                ok = True

        if QUEUE:
            QUEUE.mark_done(ok, str(wid), str(data.get("reason") or status), str(data.get("token") or ""))

        _touch_activity()
        if path == "/report":
            # 单页面模式：只上报结果，不分配下一条，避免“吞队列”
            print(f"[REPORT] id={wid} status={status} ok={ok}")
            self.send_response(200); _cors(self); self.send_header("Content-Type","application/json"); self.end_headers()
            progress = QUEUE.stats() if QUEUE else {}
            complete = progress.get("left", 0) == 0 and progress.get("pending", 0) == 0
            self.wfile.write(json.dumps({"ok": True, "progress": progress, "complete": complete}).encode('utf-8'))
            return

        # /next：多页面模式继续分配下一条
        nxt = QUEUE.pop() if QUEUE else ""
        if QUEUE and QUEUE.single_page_mode and nxt:
            nxt_id = extract_workshop_id(nxt)
            print(f"[NEXT] slot={slot} id={wid} status={status} -> {'ASSIGN ID ' + nxt_id if nxt_id else 'EMPTY'}")
            self.send_response(200); _cors(self); self.send_header("Content-Type","application/json"); self.end_headers()
            self.wfile.write(json.dumps({"id": nxt_id, "url": ""}).encode('utf-8'))
        else:
            print(f"[NEXT] slot={slot} id={wid} status={status} -> {'ASSIGN ' + nxt if nxt else 'EMPTY'}")
            self.send_response(200); _cors(self); self.send_header("Content-Type","application/json"); self.end_headers()
            self.wfile.write(json.dumps({"url": nxt}).encode('utf-8'))

def start_server(port: int):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv

# ========== 打开 URL ==========
def open_url(url: str):
    try:
        if os.name == "nt":
            try: os.startfile(url); return
            except Exception: pass
            try:
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                     "-Command", "Start-Process", url],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                ); return
            except Exception: pass
            subprocess.Popen(f'start "" "{url}"', shell=True); return
        else:
            webbrowser.open_new_tab(url)
    except Exception:
        webbrowser.open_new_tab(url)

# ========== Excel & URL 工具 ==========

def _is_link_cell(s: str) -> bool:
    t = s.strip()
    return t.startswith("http") or t.lower().startswith("file:")


def _file_uri_to_path(uri: str) -> Optional[Path]:
    u = uri.strip()
    p = urlparse(u)
    if p.scheme != "file":
        return None
    try:
        raw = p.path or ""
        if p.netloc:
            # UNC: file://SERVER/share/path -> \\SERVER\share\path
            share_path = url2pathname(raw).lstrip("/\\")
            return Path(f"//{p.netloc}/{share_path}")
        # Windows: file:///C:/x -> path /C:/x
        local = url2pathname(raw)
        return Path(local)
    except Exception:
        return None


def _parse_myprojects_video_path(cell: str) -> Optional[Path]:
    """将单元格解析为视频文件路径（file: 或含盘符的绝对路径）。"""
    s = (cell or "").strip()
    if not s:
        return None
    if s.lower().startswith("file:"):
        return _file_uri_to_path(s)
    # 裸 Windows/UNC 路径（筛重导出一般为 file:，兼容手动粘贴）
    if len(s) > 2 and s[1] == ":" and "myprojects" in s.replace("\\", "/").lower():
        try:
            return Path(s)
        except Exception:
            return None
    if s.startswith("\\\\") and "myprojects" in s.replace("\\", "/").lower():
        try:
            return Path(s)
        except Exception:
            return None
    return None


def _myprojects_item_root(video_path: Path) -> Optional[Path]:
    """.../projects/myprojects/<子文件夹>/xxx.mp4 -> .../myprojects/<子文件夹>"""
    try:
        p = video_path.resolve()
    except Exception:
        p = video_path
    parts = p.parts
    lower = [x.lower() for x in parts]
    for i, seg in enumerate(lower):
        if seg == "myprojects" and i + 1 < len(parts):
            return Path(*parts[: i + 2])
    return None


def _safe_rmtree_myprojects_item(root: Path, deleted: Set[str]) -> bool:
    """删除 myprojects 下的单个项目文件夹（整夹）。同一任务内去重。"""
    try:
        root = root.resolve()
    except Exception:
        pass
    key = str(root)
    if key in deleted:
        return True
    if not root.is_dir():
        print(f"[DELETE-SKIP] 不是目录或不存在: {root}")
        return False
    parts_lower = [p.lower() for p in root.parts]
    if "myprojects" not in parts_lower:
        print(f"[DELETE-SKIP] 路径不在 myprojects 下，拒绝删除: {root}")
        return False
    # 必须是 .../myprojects/<一层子目录>，防止误删整个 myprojects
    try:
        mp_idx = parts_lower.index("myprojects")
    except ValueError:
        return False
    if len(root.parts) != mp_idx + 2:
        print(f"[DELETE-SKIP] 只删除 myprojects 下直接子文件夹，跳过: {root}")
        return False
    try:
        shutil.rmtree(root)
        deleted.add(key)
        print(f"[DELETE] 已删除本地 myprojects 项目夹: {root}")
        return True
    except Exception as e:
        print(f"[DELETE-FAIL] {root}: {e}")
        return False


def read_urls_from_xlsx(xlsx_path: Path) -> Tuple[List[str], int]:
    """
    兼容两类 xlsx：
      1) 筛重 duplicates_*.xlsx：每行 >=2 个链接，第一个保留，从第二列起退订 /
         删除 myprojects 项目夹；
      2) delisted_unsub_*.xlsx / archived_unsub_*.xlsx：每行只有 1 个链接，
         全部作为退订目标（没有"保留列"概念）。

    url 单列表为退订清单，其余表格保留每行首个链接。
    返回 (steam_urls, deleted_folder_count)。
    """
    wb = load_workbook(filename=str(xlsx_path), read_only=True, data_only=True)
    ws = wb.active
    out: List[str] = []
    deleted_roots: Set[str] = set()
    delete_ops = 0
    it = ws.iter_rows(values_only=True)
    header = next(it, None)  # noqa: F841
    single_target_list = bool(header and str(header[0]).strip().lower() == "url")
    first = next(it, None)
    if not first:
        wb.close()
        return out, 0

    def first_link_col_idx(r):
        for i, cell in enumerate(r):
            if isinstance(cell, str) and _is_link_cell(cell):
                return i
        return None

    start_idx = first_link_col_idx(first)
    if start_idx is None:
        start_idx = 1  # 默认从 B 列（筛重 duplicates_*.xlsx）

    def collect_link_cells(row) -> List[str]:
        urls: List[str] = []
        for cell in row[start_idx:]:
            if isinstance(cell, str):
                u = cell.strip()
                if _is_link_cell(u):
                    urls.append(u)
        return urls

    def consume_row(row):
        nonlocal delete_ops
        links = collect_link_cells(row)
        if not links:
            return
        targets = links if single_target_list else links[1:]
        for u in targets:
            vp = _parse_myprojects_video_path(u)
            if vp is not None:
                root = _myprojects_item_root(vp)
                if root and _safe_rmtree_myprojects_item(root, deleted_roots):
                    delete_ops += 1
                elif root is None:
                    print(f"[DELETE-SKIP] 无法定位 myprojects 项目目录: {u[:120]}")
                elif u.strip().lower().startswith("file:"):
                    print(f"[DELETE-SKIP] file: myprojects 项目目录不存在: {root}")
            elif u.strip().lower().startswith("file:"):
                print(f"[DELETE-SKIP] file: 链接无法解析或文件已不存在: {u[:120]}")
            elif u.strip().startswith("http"):
                out.append(u.strip())

    consume_row(first)
    for row in it:
        if not row:
            continue
        consume_row(row)
    wb.close()
    return out, delete_ops

def add_or_update_query(url: str, key: str, value: str) -> str:
    p = urlparse(url); q = parse_qs(p.query); q[key] = [value]
    new_query = urlencode({k: v[-1] for k, v in q.items()})
    return urlunparse(p._replace(query=new_query))

def set_hash_flag(url: str, flag: str = "bulk_unsub=1", notify_port: int = 8787) -> str:
    p = urlparse(url)
    fragment = parse_qs(p.fragment)
    fragment.update(parse_qs(flag))
    fragment["cb"] = [f"http://127.0.0.1:{notify_port}/next"]
    return urlunparse(p._replace(fragment=urlencode(fragment, doseq=True)))

def resolve_appid_once(workshop_id: str) -> str:
    url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"
    r = requests.get(url, headers=REQ_HEADERS, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    html = r.text
    hrefs = re.findall(r'href="([^"]+)"', html, flags=re.IGNORECASE)
    hits = []
    for h in hrefs:
        m = APP_RE.search(h)
        if m: hits.append(m.group(1))
    if not hits:
        hits = APP_RE.findall(html)
    if not hits:
        return ""
    return Counter(hits).most_common(1)[0][0]

def resolve_appid(workshop_id: str, try_times: int = 2, cooldown_sec: float = 0.2) -> str:
    for _ in range(try_times):
        try:
            appid = resolve_appid_once(workshop_id)
            if appid: return appid
        except Exception:
            pass
        time.sleep(cooldown_sec)
    return ""

def extract_workshop_id(url: str) -> str:
    p = urlparse(url); q = parse_qs(p.query)
    return (q.get("id", [""])[0] or "").strip()

# ========== 主程序 ==========
def main():
    global QUEUE
    
    ap = argparse.ArgumentParser(description="Steam Workshop 批量取消订阅 控制器（固定池轮转，标签自拉取下一条）")
    ap.add_argument("--xlsx", type=Path, required=True)
    ap.add_argument("--batch-size", type=int, default=None, help="浏览器标签数，单页面模式默认 1，多页面模式默认 10")
    ap.add_argument("--idle-timeout", type=int, default=60,
                    help="队列排空后，浏览器若 N 秒内没有任何 /batch 或 /report 活动，则自动退出（秒）。0 = 永不超时（需要手动 Ctrl+C）。")
    ap.add_argument("--add-appid", action="store_true", help="为每个链接补充 ?appid=XXXX")
    ap.add_argument("--notify-port", type=int, default=8787, help="本地回调端口（与脚本一致）")
    ap.add_argument("--single-page", action="store_true", help="单页面模式：在固定页面上批量取消订阅，避免触发速率限制")
    ap.add_argument("--single-page-url", type=str, default="", help="单页面模式使用的固定 URL（默认使用列表中的第一个）")
    ap.add_argument("--state-db", type=Path, help="退订进度数据库，默认程序目录 output/unsub_progress.sqlite3")
    ap.add_argument("--max-attempts", type=int, default=3, help="本轮每项最多尝试次数，默认 3")
    ap.add_argument("--retry-delay", type=float, default=2.0, help="首次失败的重试等待秒数，随后指数退避")
    ap.add_argument("--restart", action="store_true", help="重新处理本清单全部项目，重置该清单的已成功记录")
    ap.add_argument("--no-browser", action="store_true", help="启动回调服务并等待浏览器连接，跳过自动打开标签页")
    args = ap.parse_args()
    if args.batch_size is None:
        args.batch_size = 1 if args.single_page else 10

    if not args.xlsx.exists():
        raise SystemExit(f"找不到文件：{args.xlsx}")
    if args.batch_size < 1 or not 1 <= args.notify_port <= 65535:
        ap.error("batch-size 必须 >= 1，notify-port 必须在 1..65535")
    if not 1 <= args.max_attempts <= 20 or not math.isfinite(args.retry_delay) or args.retry_delay < 0:
        ap.error("max-attempts 必须在 1..20，retry-delay 必须 >= 0")
    raw, n_local_deleted = read_urls_from_xlsx(args.xlsx)
    if n_local_deleted:
        print(f"[LOCAL] 已直接删除 {n_local_deleted} 个 myprojects 项目文件夹（每行非首列）")
    if not raw:
        print("Excel 中无待取消订阅的 Steam 链接（本地 myprojects 已按上表处理）。")
        if n_local_deleted:
            return
        print("（也未执行任何本地删除。）")
        return

    # 去重
    seen=set(); urls=[]
    for u in raw:
        if u not in seen:
            seen.add(u); urls.append(u)

    # 预处理（可选 appid，统一 hash 触发）
    final = []
    for u in urls:
        if args.add_appid:
            wid = extract_workshop_id(u)
            if wid:
                appid = resolve_appid(wid)
                if appid:
                    u = add_or_update_query(u, "appid", appid)
        final.append(set_hash_flag(u, "bulk_unsub=1", args.notify_port))

    app_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    state_db = args.state_db or app_dir / "output" / "unsub_progress.sqlite3"
    QUEUE = UrlQueue(final, single_page_mode=args.single_page, state_path=state_db,
                     max_attempts=args.max_attempts, retry_delay=args.retry_delay, restart=args.restart)
    initial = QUEUE.stats()
    print(f"[STATE] 进度保存到 {state_db}；已成功 {initial['ok']}/{initial['total']}，本轮每项最多尝试 {args.max_attempts} 次")
    if initial["ok"] == initial["total"]:
        print("[完成] 本清单已全部成功。需要重新执行时可使用 --restart。")
        return

    # 单页面模式
    if args.single_page:
        print("[MODE] 单页面模式：所有取消订阅操作将在固定页面上运行")
        
        # 确定要打开的固定页面
        # 默认改为“已订阅物品主页”，避免依赖某个具体创意工坊详情页
        # - /my/ 会自动指向当前登录账号
        # - appid=431960：Wallpaper Engine（如你需要全局订阅列表，可手动去掉该参数）
        default_subs_url = "https://steamcommunity.com/my/myworkshopfiles/?browsesort=mysubscriptions&browsefilter=mysubscriptions&appid=431960&p=1"
        if args.single_page_url:
            base_url = args.single_page_url
        else:
            base_url = default_subs_url
        
        # 确保 URL 带有 bulk_unsub=1 标记
        base_url = set_hash_flag(base_url, "bulk_unsub=1", args.notify_port)
        
        # 启动回调服务
        srv = start_server(args.notify_port)
        print(f"[SERVER] http://127.0.0.1:{args.notify_port}")
        print(f"[SERVER] - /next   : 获取下一个 workshop ID")
        print(f"[SERVER] - /batch  : 批量获取 workshop ID")
        print(f"[SERVER] - /stats  : 查看队列统计")
        print(f"[QUEUE] total={len(final)} 个项目待取消订阅")
        
        # 打开固定数量的标签（避免打开太多）
        batch = min(args.batch_size, 5)  # 单页面模式最多打开 5 个标签
        print(f"[OPEN] 打开 {batch} 个固定页面标签...")
        for i in range(batch):
            if not args.no_browser:
                open_url(base_url)
            print(f"[OPEN] {i+1}/{batch}: {base_url}")
            time.sleep(0.5)  # 稍微延迟，避免同时打开太多
        
        print("[RUNNING] 单页面模式运行中...（控制台会打印取消订阅进度）")
        
    # 多页面模式（原有逻辑）
    else:
        print("[MODE] 多页面模式：每个项目打开一个单独的页面")
        batch = max(1, args.batch_size)
        first = [url for _ in range(batch) if (url := QUEUE.pop())]
        rest = QUEUE.stats()["left"]

        # 启动回调服务
        srv = start_server(args.notify_port)
        print(f"[SERVER] http://127.0.0.1:{args.notify_port}/next  （标签完成后会来这里拉取下一条）")
        print(f"[QUEUE] total={len(final)}  pool={batch}  remaining_in_queue={rest}")

        # 打开首批标签（只开 batch 个）
        opened = 0
        for u in first:
            if not args.no_browser:
                open_url(u)
            opened += 1
            print(f"[OPEN] {opened}/{batch}: {u}")

        print("[RUNNING] 等待各标签完成后自行拉取下一条…（控制台会打印 NEXT 分配信息）")

    print("\n[提示] 打开浏览器控制台(F12)可以查看详细执行日志")
    print("[提示] 按 Ctrl+C 可以随时停止")

    exit_code = 0
    try:
        last_done = 0
        check_interval = 0
        idle_notice_printed = False
        idle_timeout = max(0, int(args.idle_timeout))
        while True:
            time.sleep(1)
            check_interval += 1
            st = QUEUE.stats()

            # 每5秒打印一次进度
            if check_interval >= 5:
                check_interval = 0
                if st['done'] > last_done:
                    print(f"[进度] 已完成: {st['done']}（成功:{st.get('ok',0)} 失败:{st.get('fail',0)}） | 队列剩余: {st['left']} | 已分配: {st['assigned']}")
                    last_done = st['done']

            if st['left'] == 0 and st['assigned'] == st['done'] and st['done'] > 0:
                print(f"\n[完成] 所有任务已处理完毕（扩展已逐条回报）。")
                print(f"[统计] 总计处理: {st['done']} 个项目（成功:{st.get('ok',0)} 失败:{st.get('fail',0)}）")
                print(f"[提示] 请查看浏览器控制台确认实际成功/失败数量")
                exit_code = 1 if st['fail'] else 0
                if st['fail']:
                    print(f"[RETRY] {st['fail']} 项失败已保存，再次运行同一清单将只重试未成功项目。")
                break

            if not args.single_page and not args.no_browser and st["left"] and not st["pending"]:
                for _ in range(batch):
                    retry_url = QUEUE.pop()
                    if not retry_url:
                        break
                    open_url(retry_url)

            if st['left'] == 0:
                # 已分配的任务必须逐条收到结果，空队列本身不会触发成功清理。
                if idle_timeout > 0 and LAST_BROWSER_ACTIVITY_TS > 0:
                    idle_sec = _seconds_since_activity()
                    if not idle_notice_printed:
                        print(f"[IDLE] 队列已清空，等待浏览器再轮询一次（{idle_timeout}s 无活动则自动退出）…")
                        idle_notice_printed = True
                    if idle_sec >= idle_timeout:
                        print(f"\n[未确认] 浏览器 {int(idle_sec)}s 无新活动，未确认项目已保存，再次运行可以继续。")
                        print(f"[统计] 分配: {st['assigned']}（浏览器未逐条回报）")
                        exit_code = 2
                        break
    except KeyboardInterrupt:
        exit_code = 130
        st = QUEUE.stats()
        print(f"\n[EXIT] 用户中断")
        print(f"[统计] 已完成: {st['done']}（成功:{st.get('ok',0)} 失败:{st.get('fail',0)}） | 剩余: {st['left']}")
    finally:
        srv.shutdown(); srv.server_close()
    if exit_code:
        raise SystemExit(exit_code)

if __name__ == "__main__":
    main()
