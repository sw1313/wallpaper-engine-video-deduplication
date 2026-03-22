#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简易 UI（Tkinter）：
- 筛重：编辑/加载/保存 config.toml 的全部控制参数，调用 we_duplicate_finder_readonly.py
- 取消订阅：映射 output/bulk_unsub_controller.py 的所有命令行参数

运行：
  python we_ui.py
"""

from __future__ import annotations

import os
import sys
import time
import json
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import tomllib  # py311+
except Exception:  # pragma: no cover
    import tomli as tomllib  # py310


REPO_ROOT = Path(__file__).resolve().parent

# 登录 Steam 后 /my/ 自动指向当前账号，无需配置个人资料 URL
STEAM_MY_SUBS_UNSUB2 = (
    "https://steamcommunity.com/my/myworkshopfiles"
    "?browsesort=mysubscriptions&browsefilter=mysubscriptions&appid=431960&p=1#bulk_unsub=2"
)


def _is_windows() -> bool:
    return os.name == "nt"


# ---- Windows 防休眠 ----
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

def _prevent_sleep():
    """告知 Windows：当前有长时间任务，禁止自动休眠/挂起。"""
    if _is_windows():
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(
                _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
            )
        except Exception:
            pass

def _allow_sleep():
    """恢复 Windows 默认休眠策略。"""
    if _is_windows():
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        except Exception:
            pass


def _which(p: str) -> str:
    """给 UI 的一个小提示：如果是命令名，尝试找到绝对路径。"""
    if not p:
        return p
    if os.path.sep in p or (os.path.altsep and os.path.altsep in p):
        return p
    hit = shutil.which(p)
    return hit or p


def _as_int(s: str, default: int) -> int:
    try:
        return int(str(s).strip())
    except Exception:
        return default


def _as_float(s: str, default: float) -> float:
    try:
        return float(str(s).strip())
    except Exception:
        return default


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


def _toml_quote(s: str) -> str:
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return f"\"{s}\""


def dump_simple_toml(d: Dict[str, Any]) -> str:
    """只覆盖本项目 config.toml 用到的原始类型（str/int/float/bool）。"""
    lines: List[str] = []
    for k, v in d.items():
        if isinstance(v, bool):
            vv = "true" if v else "false"
        elif isinstance(v, int):
            vv = str(v)
        elif isinstance(v, float):
            vv = repr(float(v))
        else:
            vv = _toml_quote(str(v))
        lines.append(f"{k} = {vv}")
    return "\n".join(lines) + "\n"


def load_toml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f) or {}


def find_latest_duplicates_xlsx(out_dir: Path) -> Optional[Path]:
    if not out_dir.exists():
        return None
    cands = list(out_dir.glob("duplicates_*.xlsx"))
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0]


@dataclass
class ProcHandle:
    popen: subprocess.Popen
    reader_thread: threading.Thread


class LogSink:
    def __init__(self, text: tk.Text):
        self.text = text
        self.q: "queue.Queue[str]" = queue.Queue()
        self._closed = False
        self._after_id: Optional[str] = None
        self.text.configure(state="disabled")
        # 防止长时间运行日志无限膨胀导致卡顿/崩溃（按行数截断）
        self.max_lines = 8000
        self.trim_to_lines = 6000

    def write(self, s: str):
        if self._closed:
            return
        self.q.put(s)

    def close(self):
        self._closed = True
        # 关闭时尽量取消 after 回调，避免窗口销毁后继续回调导致 TclError
        try:
            if self._after_id:
                self.text.after_cancel(self._after_id)
        except Exception:
            pass
        self._after_id = None

    def pump(self):
        """把队列内容刷进 Text。单次只做一次批量 insert，避免大量小 insert 导致 UI 卡死。"""
        if self._closed:
            return
        batch: List[str] = []
        batch_size = 0
        max_batch_chars = 65536  # 单次最多写入字符数，避免一次 insert 过大
        try:
            while True:
                s = self.q.get_nowait()
                batch.append(s)
                batch_size += len(s)
                if batch_size >= max_batch_chars:
                    break
        except queue.Empty:
            pass
        except tk.TclError:
            self._closed = True
            return

        if batch:
            try:
                self.text.configure(state="normal")
                self.text.insert("end", "".join(batch))
                self.text.see("end")
                self.text.configure(state="disabled")
                # 截断：只保留最后 N 行（每批最多做一次）
                try:
                    end_line = int(self.text.index("end-1c").split(".")[0])
                    if end_line > self.max_lines:
                        del_to = max(1, end_line - self.trim_to_lines)
                        self.text.configure(state="normal")
                        self.text.delete("1.0", f"{del_to}.0")
                        self.text.configure(state="disabled")
                except Exception:
                    pass
            except tk.TclError:
                self._closed = True
                return

        try:
            self._after_id = self.text.after(150, self.pump)
        except tk.TclError:
            self._closed = True
            self._after_id = None

    def clear(self):
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.configure(state="disabled")
        except tk.TclError:
            self._closed = True


class ScrollableFrame(ttk.Frame):
    """ttk 可滚动容器（仅竖向）。"""

    def __init__(self, master, *, padding=(0, 0, 0, 0)):
        super().__init__(master, padding=padding)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")

        self.frame = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.frame, anchor="nw")

        def _on_frame_config(_evt=None):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        def _on_canvas_config(_evt=None):
            # 让内部 frame 宽度跟着 canvas 变化，避免横向挤压
            self.canvas.itemconfigure(self._win, width=self.canvas.winfo_width())

        self.frame.bind("<Configure>", _on_frame_config)
        self.canvas.bind("<Configure>", _on_canvas_config)

        # 鼠标滚轮支持（Windows / macOS / X11）
        self._bind_mousewheel(self.canvas)
        self._bind_mousewheel(self.frame)

    def _bind_mousewheel(self, widget):
        widget.bind("<Enter>", lambda _e: self._set_wheel_target(True))
        widget.bind("<Leave>", lambda _e: self._set_wheel_target(False))

    def _set_wheel_target(self, active: bool):
        if active:
            self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)      # Windows/macOS
            self.canvas.bind_all("<Button-4>", self._on_mousewheel_x11)    # Linux up
            self.canvas.bind_all("<Button-5>", self._on_mousewheel_x11)    # Linux down
        else:
            self.canvas.unbind_all("<MouseWheel>")
            self.canvas.unbind_all("<Button-4>")
            self.canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, e):
        # Windows: e.delta=120/-120；macOS: 可能更小
        delta = -1 * int(e.delta / 120) if e.delta else 0
        if delta:
            self.canvas.yview_scroll(delta, "units")

    def _on_mousewheel_x11(self, e):
        self.canvas.yview_scroll(-1 if e.num == 4 else 1, "units")


class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master)
        self.master = master
        self.grid(sticky="nsew")
        self.master.title("Wallpaper Engine 视频筛重 / 批量取消订阅 UI")
        # 自适应屏幕（避免高 DPI / 小屏时“显示不全”）
        sw = self.master.winfo_screenwidth()
        sh = self.master.winfo_screenheight()
        w = max(900, min(1400, int(sw * 0.92)))
        h = max(620, min(980, int(sh * 0.90)))
        self.master.minsize(860, 600)
        self.master.geometry(f"{w}x{h}")

        # state
        self.proc: Optional[ProcHandle] = None
        self.current_tab: str = "dedup"

        # layout
        self.master.rowconfigure(0, weight=1)
        self.master.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.nb = ttk.Notebook(self)
        self.nb.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.tab_dedup = ttk.Frame(self.nb)
        self.tab_unsub = ttk.Frame(self.nb)
        self.tab_archive = ttk.Frame(self.nb)
        self.nb.add(self.tab_dedup, text="筛重 / 查重")
        self.nb.add(self.tab_unsub, text="取消订阅")
        self.nb.add(self.tab_archive, text="下架归档")

        self._build_dedup_tab(self.tab_dedup)
        self._build_unsub_tab(self.tab_unsub)
        self._build_archive_tab(self.tab_archive)

        # 默认加载根目录 config.toml
        self.dedup_config_path.set(str((REPO_ROOT / "config.toml").resolve()))
        self._load_config_into_form(Path(self.dedup_config_path.get()))

        # log pumps
        self.log_dedup.pump()
        self.log_unsub.pump()
        self.log_archive.pump()

        # 关闭窗口时，确保把子进程杀干净（否则 log 文件会一直被占用）
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------- common process mgmt -------------------
    def _kill_process_tree(self, p: subprocess.Popen) -> None:
        if p.poll() is not None:
            return
        try:
            if _is_windows():
                # 强制杀掉整个进程树（含 ffmpeg/fpcalc）
                subprocess.run(
                    ["taskkill", "/PID", str(p.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                p.terminate()
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def _start_process(self, cmd: List[str], cwd: Path, sink: LogSink) -> None:
        if self.proc and self.proc.popen.poll() is None:
            messagebox.showwarning("正在运行", "已有任务在运行，请先停止。")
            return

        sink.clear()
        sink.write("[CMD] " + " ".join(cmd) + "\n\n")

        creationflags = 0
        if _is_windows():
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        p = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=creationflags,
        )

        def _reader():
            assert p.stdout is not None
            try:
                for raw in p.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    sink.write(line)
            except Exception as e:
                sink.write(f"\n[UI] 读取输出失败：{e}\n")
            finally:
                code = p.poll()
                sink.write(f"\n[EXIT] rc={code}\n")
                _allow_sleep()
                try:
                    p.stdout.close()
                except Exception:
                    pass

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        self.proc = ProcHandle(popen=p, reader_thread=t)
        _prevent_sleep()

    def _stop_running(self):
        if not self.proc:
            return
        p = self.proc.popen
        if p.poll() is None:
            self._kill_process_tree(p)
        self.proc = None
        _allow_sleep()

    def _on_close(self):
        try:
            self._stop_running()
        except Exception:
            pass
        _allow_sleep()
        try:
            self.log_dedup.close()
            self.log_unsub.close()
            self.log_archive.close()
        except Exception:
            pass
        try:
            self.master.destroy()
        except Exception:
            pass

    # ------------------- tabs -------------------
    def _on_tab_changed(self, _evt):
        idx = self.nb.index(self.nb.select())
        self.current_tab = ("dedup", "unsub", "archive")[idx] if idx < 3 else "dedup"

    # ------------------- dedup tab -------------------
    def _build_dedup_tab(self, root: ttk.Frame):
        root.rowconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)

        # top controls
        frm = ttk.Frame(root)
        frm.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        frm.columnconfigure(3, weight=1)

        self.dedup_config_path = tk.StringVar()
        ttk.Label(frm, text="config.toml：").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.dedup_config_path).grid(row=0, column=1, columnspan=3, sticky="ew", padx=(6, 6))
        ttk.Button(frm, text="选择…", command=self._pick_config).grid(row=0, column=4, padx=(0, 6))
        ttk.Button(frm, text="加载", command=self._reload_config).grid(row=0, column=5, padx=(0, 6))
        ttk.Button(frm, text="另存为…", command=self._save_config_as).grid(row=0, column=6)

        # form + log (paned)
        pan = ttk.Panedwindow(root, orient="horizontal")
        pan.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        left = ttk.Frame(pan)
        right = ttk.Frame(pan)
        pan.add(left, weight=3)
        pan.add(right, weight=2)

        # left: scrollable config form（字段较多，必须可滚动）
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        sf = ScrollableFrame(left)
        sf.grid(row=0, column=0, sticky="nsew")
        form = sf.frame
        form.columnconfigure(1, weight=1)

        self._dedup_vars: Dict[str, tk.Variable] = {}

        def add_entry(row: int, label: str, key: str, kind: str = "str", width: int = 22):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=2)
            if kind == "bool":
                var = tk.BooleanVar()
                ttk.Checkbutton(form, variable=var).grid(row=row, column=1, sticky="w", pady=2)
            elif kind == "choice":
                var = tk.StringVar()
                cb = ttk.Combobox(form, textvariable=var, values=["nearest_1.0", "nearest_0.5", "int"], width=width, state="readonly")
                cb.grid(row=row, column=1, sticky="ew", pady=2)
            else:
                var = tk.StringVar()
                ttk.Entry(form, textvariable=var, width=width).grid(row=row, column=1, sticky="ew", pady=2)
            self._dedup_vars[key] = var

        def add_path_row(row: int, label: str, key: str, pick: str):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar()
            ent = ttk.Entry(form, textvariable=var)
            ent.grid(row=row, column=1, sticky="ew", pady=2)
            self._dedup_vars[key] = var
            if pick == "dir":
                ttk.Button(form, text="…", width=3, command=lambda: self._pick_dir(var)).grid(row=row, column=2, sticky="w", padx=6)
            else:
                ttk.Button(form, text="…", width=3, command=lambda: self._pick_file(var)).grid(row=row, column=2, sticky="w", padx=6)

        r = 0
        ttk.Label(form, text="参数（对应 config.toml）", font=("Microsoft YaHei UI", 10, "bold")).grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, 6))
        r += 1

        add_path_row(r, "workshop_root（创意工坊目录）", "workshop_root", "dir"); r += 1
        add_path_row(r, "output_dir（输出目录）", "output_dir", "dir"); r += 1
        add_entry(r, "ffmpeg_path", "ffmpeg_path"); ttk.Button(form, text="…", width=3, command=lambda: self._pick_file(self._dedup_vars["ffmpeg_path"])).grid(row=r, column=2, sticky="w", padx=6); r += 1
        add_entry(r, "ffprobe_path", "ffprobe_path"); ttk.Button(form, text="…", width=3, command=lambda: self._pick_file(self._dedup_vars["ffprobe_path"])).grid(row=r, column=2, sticky="w", padx=6); r += 1
        add_entry(r, "fpcalc_path", "fpcalc_path"); ttk.Button(form, text="…", width=3, command=lambda: self._pick_file(self._dedup_vars["fpcalc_path"])).grid(row=r, column=2, sticky="w", padx=6); r += 1

        ttk.Separator(form).grid(row=r, column=0, columnspan=3, sticky="ew", pady=8); r += 1
        add_entry(r, "video_window_seconds（秒）", "video_window_seconds"); r += 1
        add_entry(r, "audio_window_seconds（秒）", "audio_window_seconds"); r += 1
        add_entry(r, "seek_ratio（0~1）", "seek_ratio"); r += 1

        ttk.Separator(form).grid(row=r, column=0, columnspan=3, sticky="ew", pady=8); r += 1
        add_entry(r, "sample_frames（抽帧数）", "sample_frames"); r += 1
        add_entry(r, "phash_size", "phash_size"); r += 1
        add_entry(r, "phash_distance_threshold（组合分，推荐 0.5~0.7）", "phash_distance_threshold"); r += 1
        add_entry(r, "duration_rounding", "duration_rounding", kind="choice"); r += 1
        add_entry(r, "require_both_signatures（视频+音频都要）", "require_both_signatures", kind="bool"); r += 1

        ttk.Separator(form).grid(row=r, column=0, columnspan=3, sticky="ew", pady=8); r += 1
        add_entry(r, "max_workers_stage1", "max_workers_stage1"); r += 1
        add_entry(r, "max_workers_stage2", "max_workers_stage2"); r += 1
        add_entry(r, "ffprobe_timeout", "ffprobe_timeout"); r += 1
        add_entry(r, "ffmpeg_timeout", "ffmpeg_timeout"); r += 1
        add_entry(r, "fpcalc_timeout", "fpcalc_timeout"); r += 1

        ttk.Separator(form).grid(row=r, column=0, columnspan=3, sticky="ew", pady=8); r += 1
        add_entry(r, "log_file（空=仅控制台）", "log_file"); r += 1
        add_entry(r, "progress（默认进度条）", "progress", kind="bool"); r += 1

        # cli flags
        ttk.Separator(form).grid(row=r, column=0, columnspan=3, sticky="ew", pady=8); r += 1
        ttk.Label(form, text="命令行开关（不写入 toml）", font=("Microsoft YaHei UI", 9, "bold")).grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, 6)); r += 1
        self.dedup_verbose = tk.BooleanVar(value=False)
        self.dedup_trace = tk.BooleanVar(value=False)
        self.dedup_no_progress = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="--verbose（DEBUG 日志）", variable=self.dedup_verbose).grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
        ttk.Checkbutton(form, text="--trace（打印外部命令）", variable=self.dedup_trace).grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
        ttk.Checkbutton(form, text="--no-progress（关闭 tqdm）", variable=self.dedup_no_progress).grid(row=r, column=0, columnspan=3, sticky="w"); r += 1

        # action buttons
        btns = ttk.Frame(form)
        btns.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text="运行筛重", command=self._run_dedup).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(btns, text="停止", command=self._stop_running).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(btns, text="打开输出目录", command=self._open_output_dir).grid(row=0, column=2)

        # right: log
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        ttk.Label(right, text="日志输出", font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 6))
        txt = tk.Text(right, wrap="word")
        txt.grid(row=1, column=0, sticky="nsew")
        self.log_dedup = LogSink(txt)

    def _pick_config(self):
        p = filedialog.askopenfilename(
            title="选择 config.toml",
            initialdir=str(REPO_ROOT),
            filetypes=[("TOML", "*.toml"), ("All", "*.*")],
        )
        if not p:
            return
        self.dedup_config_path.set(p)
        self._load_config_into_form(Path(p))

    def _reload_config(self):
        p = Path(self.dedup_config_path.get().strip())
        self._load_config_into_form(p)

    def _pick_dir(self, var: tk.StringVar):
        p = filedialog.askdirectory(title="选择目录")
        if p:
            var.set(p)

    def _pick_file(self, var: tk.StringVar):
        p = filedialog.askopenfilename(title="选择文件", filetypes=[("All", "*.*")])
        if p:
            var.set(p)

    def _save_config_as(self):
        p = filedialog.asksaveasfilename(
            title="另存为 config.toml",
            defaultextension=".toml",
            filetypes=[("TOML", "*.toml"), ("All", "*.*")],
            initialdir=str(REPO_ROOT),
        )
        if not p:
            return
        cfg = self._collect_dedup_config_dict()
        Path(p).write_text(dump_simple_toml(cfg), encoding="utf-8")
        messagebox.showinfo("已保存", f"已保存：{p}")
        self.dedup_config_path.set(p)

    def _load_config_into_form(self, p: Path):
        data = load_toml(p)
        # 用现有 config.toml 的 key 做兜底
        defaults = load_toml(REPO_ROOT / "config.toml")
        merged = dict(defaults)
        merged.update(data or {})
        for k, var in self._dedup_vars.items():
            if k not in merged:
                continue
            v = merged.get(k)
            if isinstance(var, tk.BooleanVar):
                var.set(_as_bool(v))
            else:
                var.set("" if v is None else str(v))

        # 小提示：外部工具路径显示实际命中
        for key in ("ffmpeg_path", "ffprobe_path", "fpcalc_path"):
            if key in self._dedup_vars:
                vv = str(self._dedup_vars[key].get()).strip()
                if vv:
                    self._dedup_vars[key].set(_which(vv))

        # 下架归档 tab 也从同一 config 加载
        if hasattr(self, "arc_we_install_dir"):
            self.arc_we_install_dir.set(str(merged.get("we_install_dir", "")))
        if hasattr(self, "arc_steam_api_key"):
            self.arc_steam_api_key.set(str(merged.get("steam_api_key", "")))

    def _collect_dedup_config_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        # 这些 key 与 config.toml 对齐
        d["workshop_root"] = str(self._dedup_vars["workshop_root"].get()).strip()
        d["output_dir"] = str(self._dedup_vars["output_dir"].get()).strip() or "output"
        d["ffmpeg_path"] = str(self._dedup_vars["ffmpeg_path"].get()).strip() or "ffmpeg"
        d["ffprobe_path"] = str(self._dedup_vars["ffprobe_path"].get()).strip() or "ffprobe"
        d["fpcalc_path"] = str(self._dedup_vars["fpcalc_path"].get()).strip() or "fpcalc"

        d["video_window_seconds"] = _as_int(self._dedup_vars["video_window_seconds"].get(), 20)
        d["audio_window_seconds"] = _as_int(self._dedup_vars["audio_window_seconds"].get(), 120)
        d["seek_ratio"] = _as_float(self._dedup_vars["seek_ratio"].get(), 0.5)

        d["sample_frames"] = _as_int(self._dedup_vars["sample_frames"].get(), 12)
        d["phash_size"] = _as_int(self._dedup_vars["phash_size"].get(), 8)
        d["phash_distance_threshold"] = _as_float(self._dedup_vars["phash_distance_threshold"].get(), 0.6)
        d["duration_rounding"] = str(self._dedup_vars["duration_rounding"].get()).strip() or "int"
        d["require_both_signatures"] = bool(self._dedup_vars["require_both_signatures"].get())

        d["max_workers_stage1"] = _as_int(self._dedup_vars["max_workers_stage1"].get(), 8)
        d["max_workers_stage2"] = _as_int(self._dedup_vars["max_workers_stage2"].get(), 6)
        d["ffprobe_timeout"] = _as_int(self._dedup_vars["ffprobe_timeout"].get(), 25)
        d["ffmpeg_timeout"] = _as_int(self._dedup_vars["ffmpeg_timeout"].get(), 45)
        d["fpcalc_timeout"] = _as_int(self._dedup_vars["fpcalc_timeout"].get(), 35)

        log_file = str(self._dedup_vars["log_file"].get()).strip()
        d["log_file"] = log_file if log_file else ""
        d["progress"] = bool(self._dedup_vars["progress"].get())
        return d

    def _open_output_dir(self):
        out_dir = Path(str(self._dedup_vars["output_dir"].get()).strip() or "output")
        if not out_dir.is_absolute():
            out_dir = (REPO_ROOT / out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            if _is_windows():
                os.startfile(str(out_dir))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(out_dir)])
        except Exception as e:
            messagebox.showwarning("打开失败", str(e))

    def _run_dedup(self):
        cfg = self._collect_dedup_config_dict()
        if not cfg.get("workshop_root"):
            messagebox.showerror("参数缺失", "请填写 workshop_root（创意工坊目录）。")
            return

        # 写临时 config（避免用户原配置被覆盖）
        tmp_dir = Path(tempfile.gettempdir()) / "we_dedup_ui"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        tmp_cfg = tmp_dir / f"config_ui_{ts}.toml"
        tmp_cfg.write_text(dump_simple_toml(cfg), encoding="utf-8")

        cmd = [sys.executable, str((REPO_ROOT / "we_duplicate_finder_readonly.py").resolve()), "-c", str(tmp_cfg)]
        if self.dedup_verbose.get():
            cmd.append("--verbose")
        if self.dedup_trace.get():
            cmd.append("--trace")
        if self.dedup_no_progress.get():
            cmd.append("--no-progress")
        self._start_process(cmd, cwd=REPO_ROOT, sink=self.log_dedup)

    # ------------------- unsub tab -------------------
    def _build_unsub_tab(self, root: ttk.Frame):
        root.rowconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)

        frm = ttk.Frame(root)
        frm.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        for i in range(5):
            frm.columnconfigure(i, weight=1 if i == 1 else 0)

        self.unsub_xlsx = tk.StringVar()
        self.unsub_batch_size = tk.StringVar(value="1")
        self.unsub_add_appid = tk.BooleanVar(value=False)
        self.unsub_notify_port = tk.StringVar(value="8787")
        self.unsub_single_page = tk.BooleanVar(value=True)
        self.unsub_single_page_url = tk.StringVar(value="")
        self.unsub_keep_largest = tk.BooleanVar(value=True)

        ttk.Label(frm, text="xlsx（duplicates_*.xlsx）：").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.unsub_xlsx).grid(row=0, column=1, sticky="ew", padx=(6, 6))
        ttk.Button(frm, text="选择…", command=self._pick_xlsx).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(frm, text="选最新", command=self._pick_latest_xlsx).grid(row=0, column=3, padx=(0, 6))
        ttk.Button(frm, text="打开所在目录", command=self._open_xlsx_dir).grid(row=0, column=4)

        ttk.Checkbutton(frm, text="单页面模式（推荐）", variable=self.unsub_single_page, command=self._refresh_unsub_controls).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(frm, text="batch-size：").grid(row=1, column=1, sticky="e", pady=(8, 0))
        ttk.Spinbox(frm, from_=1, to=50, textvariable=self.unsub_batch_size, width=8).grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(frm, text="add-appid（可选）", variable=self.unsub_add_appid).grid(row=1, column=3, sticky="w", pady=(8, 0))
        ttk.Label(frm, text="notify-port：").grid(row=1, column=4, sticky="e", pady=(8, 0))
        ttk.Entry(frm, textvariable=self.unsub_notify_port, width=8).grid(row=1, column=5, sticky="w", pady=(8, 0))

        ttk.Label(frm, text="single-page-url（可选）：").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self._ent_single_url = ttk.Entry(frm, textvariable=self.unsub_single_page_url)
        self._ent_single_url.grid(row=2, column=1, columnspan=4, sticky="ew", padx=(6, 6), pady=(8, 0))
        ttk.Button(frm, text="清空", command=lambda: self.unsub_single_page_url.set("")).grid(row=2, column=5, pady=(8, 0))

        ttk.Checkbutton(
            frm,
            text="每组保留最大（跳过每行第一个链接）",
            variable=self.unsub_keep_largest,
        ).grid(row=3, column=0, columnspan=6, sticky="w", pady=(8, 0))

        note = ttk.Label(
            frm,
            text="提示：取消订阅需要配合浏览器油猴脚本（URL 带 #bulk_unsub=1），并确保已登录 Steam。",
            foreground="#666666",
        )
        note.grid(row=4, column=0, columnspan=6, sticky="w", pady=(8, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=6, sticky="w", pady=(10, 0))
        ttk.Button(btns, text="运行取消订阅", command=self._run_unsub).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(btns, text="停止", command=self._stop_running).grid(row=0, column=1)

        # log
        logfrm = ttk.Frame(root)
        logfrm.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        logfrm.rowconfigure(1, weight=1)
        logfrm.columnconfigure(0, weight=1)
        ttk.Label(logfrm, text="日志输出", font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 6))
        txt = tk.Text(logfrm, wrap="word")
        txt.grid(row=1, column=0, sticky="nsew")
        self.log_unsub = LogSink(txt)

        self._refresh_unsub_controls()

    def _refresh_unsub_controls(self):
        sp = bool(self.unsub_single_page.get())
        self._ent_single_url.configure(state="normal" if sp else "disabled")

    def _pick_xlsx(self):
        p = filedialog.askopenfilename(
            title="选择 duplicates_*.xlsx",
            initialdir=str(REPO_ROOT / "output"),
            filetypes=[("Excel", "*.xlsx"), ("All", "*.*")],
        )
        if p:
            self.unsub_xlsx.set(p)

    def _pick_latest_xlsx(self):
        out_dir = Path(str(self._dedup_vars.get("output_dir", tk.StringVar(value="output")).get()).strip() or "output")
        if not out_dir.is_absolute():
            out_dir = (REPO_ROOT / out_dir).resolve()
        latest = find_latest_duplicates_xlsx(out_dir)
        if not latest:
            messagebox.showwarning("未找到", f"在 {out_dir} 未找到 duplicates_*.xlsx")
            return
        self.unsub_xlsx.set(str(latest))

    def _open_xlsx_dir(self):
        p = Path(self.unsub_xlsx.get().strip() or "")
        if not p.exists():
            return
        d = p.parent
        try:
            if _is_windows():
                os.startfile(str(d))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(d)])
        except Exception as e:
            messagebox.showwarning("打开失败", str(e))

    def _run_unsub(self):
        xlsx = Path(self.unsub_xlsx.get().strip() or "")
        if not xlsx.exists():
            messagebox.showerror("参数缺失", "请选择有效的 xlsx 文件。")
            return

        cmd = [sys.executable, str((REPO_ROOT / "output" / "bulk_unsub_controller.py").resolve()), "--xlsx", str(xlsx)]
        cmd += ["--batch-size", str(_as_int(self.unsub_batch_size.get(), 1))]
        cmd += ["--notify-port", str(_as_int(self.unsub_notify_port.get(), 8787))]
        if self.unsub_add_appid.get():
            cmd.append("--add-appid")
        if self.unsub_keep_largest.get():
            cmd.append("--keep-largest-in-group")
        if self.unsub_single_page.get():
            cmd.append("--single-page")
            url = str(self.unsub_single_page_url.get()).strip()
            if url:
                cmd += ["--single-page-url", url]
        self._start_process(cmd, cwd=REPO_ROOT / "output", sink=self.log_unsub)


    # ------------------- archive tab -------------------
    def _build_archive_tab(self, root: ttk.Frame):
        root.rowconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)

        frm = ttk.Frame(root)
        frm.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        frm.columnconfigure(1, weight=1)

        self.arc_we_install_dir = tk.StringVar()
        self.arc_steam_api_key = tk.StringVar()

        ttk.Label(frm, text="WE 安装目录：").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.arc_we_install_dir).grid(row=0, column=1, sticky="ew", padx=(6, 6))
        ttk.Button(frm, text="选择…", command=lambda: self._pick_dir(self.arc_we_install_dir)).grid(row=0, column=2)

        ttk.Label(frm, text="Steam API Key（推荐）：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frm, textvariable=self.arc_steam_api_key, show="*").grid(row=1, column=1, sticky="ew", padx=(6, 6), pady=(6, 0))
        ttk.Button(frm, text="申请", command=lambda: __import__("webbrowser").open("https://steamcommunity.com/dev/apikey")).grid(row=1, column=2, pady=(6, 0))

        note = ttk.Label(
            frm,
            text="提示：有 API Key 时使用 IPublishedFileService（检测更准确）；无 Key 回退到旧 API。取消订阅会打开 steamcommunity.com/my/…（已登录即当前账号）。",
            foreground="#666666",
            wraplength=700,
        )
        note.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(btns, text="检测下架物品", command=self._run_arc_detect).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(btns, text="归档到本地", command=self._run_arc_archive).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(btns, text="取消订阅已下架", command=self._run_arc_unsub).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(btns, text="停止", command=self._stop_running).grid(row=0, column=3)

        # log
        logfrm = ttk.Frame(root)
        logfrm.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        logfrm.rowconfigure(1, weight=1)
        logfrm.columnconfigure(0, weight=1)
        ttk.Label(logfrm, text="日志输出", font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 6))
        txt = tk.Text(logfrm, wrap="word")
        txt.grid(row=1, column=0, sticky="nsew")
        self.log_archive = LogSink(txt)

        # 从 config 加载初始值
        defaults = load_toml(REPO_ROOT / "config.toml")
        self.arc_we_install_dir.set(str(defaults.get("we_install_dir", "")))
        self.arc_steam_api_key.set(str(defaults.get("steam_api_key", "")))

    def _arc_get_config_path(self) -> Path:
        return Path(self.dedup_config_path.get().strip() or str(REPO_ROOT / "config.toml"))

    def _arc_workshop_root(self) -> str:
        return str(self._dedup_vars.get("workshop_root", tk.StringVar(value="")).get()).strip()

    def _run_arc_detect(self):
        wr = self._arc_workshop_root()
        if not wr:
            messagebox.showerror("参数缺失", "请在「筛重」标签页填写 workshop_root。")
            return

        cfg_path = self._arc_get_config_path()
        cmd = [
            sys.executable,
            str((REPO_ROOT / "we_delisted_archiver.py").resolve()),
            "-c", str(cfg_path),
            "--detect",
            "--workshop-root", wr,
        ]
        api_key = self.arc_steam_api_key.get().strip()
        if api_key:
            cmd += ["--steam-api-key", api_key]
        self._start_process(cmd, cwd=REPO_ROOT, sink=self.log_archive)

    def _run_arc_archive(self):
        we_dir = self.arc_we_install_dir.get().strip()
        if not we_dir:
            messagebox.showerror("参数缺失", "请填写 WE 安装目录。")
            return
        wr = self._arc_workshop_root()
        if not wr:
            messagebox.showerror("参数缺失", "请在「筛重」标签页填写 workshop_root。")
            return

        cfg_path = self._arc_get_config_path()
        cmd = [
            sys.executable,
            str((REPO_ROOT / "we_delisted_archiver.py").resolve()),
            "-c", str(cfg_path),
            "--archive",
            "--workshop-root", wr,
            "--we-install-dir", we_dir,
        ]
        self._start_process(cmd, cwd=REPO_ROOT, sink=self.log_archive)

    def _run_arc_unsub(self):
        """生成 xlsx → 调用 bulk_unsub_controller.py 取消订阅已下架物品。"""
        out_dir = Path(str(self._dedup_vars.get("output_dir", tk.StringVar(value="output")).get()).strip() or "output")
        if not out_dir.is_absolute():
            out_dir = (REPO_ROOT / out_dir).resolve()

        delisted_path = out_dir / "delisted_items.json"
        if not delisted_path.exists():
            messagebox.showerror("未找到", f"请先运行「检测下架物品」\n{delisted_path}")
            return

        try:
            with delisted_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items", [])
            if not items:
                messagebox.showinfo("无需操作", "没有需要取消订阅的下架物品。")
                return
        except Exception as e:
            messagebox.showerror("读取失败", str(e))
            return

        # 生成临时 xlsx
        try:
            from openpyxl import Workbook
        except ImportError:
            messagebox.showerror("缺少依赖", "需要 openpyxl: pip install openpyxl")
            return

        ts = time.strftime("%Y%m%d_%H%M%S")
        xlsx_path = out_dir / f"delisted_unsub_{ts}.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "delisted"
        ws.append(["url"])
        for item in items:
            ws.append([f"https://steamcommunity.com/sharedfiles/filedetails/?id={item['id']}"])
        xlsx_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(str(xlsx_path))

        # 使用 /my/ 订阅页，浏览器已登录 Steam 时自动对应当前账号
        cmd = [
            sys.executable,
            str((REPO_ROOT / "output" / "bulk_unsub_controller.py").resolve()),
            "--xlsx", str(xlsx_path),
            "--batch-size", "1",
            "--single-page",
            "--single-page-url", STEAM_MY_SUBS_UNSUB2,
        ]

        self.log_archive.write(f"[INFO] 已生成 {len(items)} 条取消订阅链接: {xlsx_path}\n")
        self._start_process(cmd, cwd=REPO_ROOT / "output", sink=self.log_archive)


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if _is_windows():
            style.theme_use("vista")
    except Exception:
        pass
    root.rowconfigure(0, weight=1)
    root.columnconfigure(0, weight=1)

    # 全局 Tk 异常回调：捕获 after() / event 回调中未处理的异常，防止息屏等场景直接闪退
    def _tk_exception_handler(exc_type, exc_value, exc_tb):
        import traceback
        if issubclass(exc_type, tk.TclError):
            return
        try:
            traceback.print_exception(exc_type, exc_value, exc_tb)
        except Exception:
            pass

    root.report_callback_exception = _tk_exception_handler

    App(root)
    root.mainloop()
    _allow_sleep()


if __name__ == "__main__":
    main()

