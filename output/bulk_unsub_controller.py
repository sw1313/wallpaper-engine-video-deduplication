# bulk_unsub_pool_controller.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, os, time, subprocess, webbrowser, json, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import List, Deque
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from collections import deque, Counter
import re, requests
from openpyxl import load_workbook

REQ_HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/124 Safari/537.36"}
REQ_TIMEOUT = 15
APP_RE = re.compile(r'/app/(\d+)\b', re.IGNORECASE)

# ========== 简单队列 ==========
class UrlQueue:
    def __init__(self, urls: List[str]):
        self.lock = threading.Lock()
        self.urls = deque(urls)
        self.assigned = 0
        self.done = 0
    def pop(self) -> str:
        with self.lock:
            if not self.urls: return ""
            self.assigned += 1
            return self.urls.popleft()
    def stats(self):
        with self.lock:
            return dict(left=len(self.urls), assigned=self.assigned, done=self.done)
    def mark_done(self):
        with self.lock:
            self.done += 1

QUEUE: UrlQueue|None = None

# ========== 回调服务（CORS） ==========
def _cors(h):
    h.send_header("Access-Control-Allow-Origin", "*")
    h.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    h.send_header("Access-Control-Allow-Headers", "content-type")
    h.send_header("Access-Control-Max-Age", "86400")

class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        if self.path.rstrip('/') != "/next":
            self.send_response(404); _cors(self); self.end_headers(); return
        self.send_response(204); _cors(self); self.end_headers()
    def do_POST(self):
        if self.path.rstrip('/') != "/next":
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
        url    = (data.get('url') or '')
        if QUEUE: QUEUE.mark_done()
        nxt = QUEUE.pop() if QUEUE else ""
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
def read_urls_from_xlsx(xlsx_path: Path) -> List[str]:
    wb = load_workbook(filename=str(xlsx_path), read_only=True, data_only=True)
    ws = wb.active
    out: List[str] = []
    header = True
    for row in ws.iter_rows(values_only=True):
        if header: header=False; continue
        for cell in row[2:]:
            if isinstance(cell, str):
                u = cell.strip()
                if u.startswith("http"): out.append(u)
    wb.close()
    return out

def add_or_update_query(url: str, key: str, value: str) -> str:
    p = urlparse(url); q = parse_qs(p.query); q[key] = [value]
    new_query = urlencode({k: v[-1] for k, v in q.items()})
    return urlunparse(p._replace(query=new_query))

def set_hash_flag(url: str, flag: str = "bulk_unsub=1") -> str:
    p = urlparse(url)
    return urlunparse(p._replace(fragment=flag))

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
    ap = argparse.ArgumentParser(description="Steam Workshop 批量取消订阅 控制器（固定池轮转，标签自拉取下一条）")
    ap.add_argument("--xlsx", type=Path, required=True)
    ap.add_argument("--batch-size", type=int, default=10, help="初始打开的标签数（固定池大小）")
    ap.add_argument("--add-appid", action="store_true", help="为每个链接补充 ?appid=XXXX")
    ap.add_argument("--notify-port", type=int, default=8787, help="本地回调端口（与脚本一致）")
    args = ap.parse_args()

    if not args.xlsx.exists():
        print(f"找不到文件：{args.xlsx}"); return
    raw = read_urls_from_xlsx(args.xlsx)
    if not raw:
        print("Excel 中未找到链接（从 C 列开始）。"); return

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
        final.append(set_hash_flag(u, "bulk_unsub=1"))

    # 建队列（先取出前 batch-size 个作“首批”，剩余进入队列）
    batch = max(1, args.batch_size)
    first = final[:batch]
    rest  = final[batch:]
    global QUEUE
    QUEUE = UrlQueue(rest)

    # 启动回调服务
    srv = start_server(args.notify_port)
    print(f"[SERVER] http://127.0.0.1:{args.notify_port}/next  （标签完成后会来这里拉取下一条）")
    print(f"[QUEUE] total={len(final)}  pool={batch}  remaining_in_queue={len(rest)}")

    # 打开首批标签（只开 batch 个）
    opened = 0
    for u in first:
        open_url(u); opened += 1
        print(f"[OPEN] {opened}/{batch}: {u}")

    print("[RUNNING] 等待各标签完成后自行拉取下一条…（控制台会打印 NEXT 分配信息）")

    try:
        while True:
            time.sleep(1)
            st = QUEUE.stats()
            # 你也可以在这里打印 st 进度
            # print("[STATS]", st)
            if st['left']==0:
                # 队列空了就继续等，直到所有标签停下（自愿结束即可）
                pass
    except KeyboardInterrupt:
        print("\n[EXIT] 停止。")
    finally:
        srv.shutdown(); srv.server_close()

if __name__ == "__main__":
    main()
