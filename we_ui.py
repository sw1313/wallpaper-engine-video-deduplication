#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wallpaper Engine 视频筛重 / 批量取消订阅 / 下架归档 统一 UI（PySide6 / Qt 6）

- 筛重：编辑/加载/保存 config.toml 的全部控制参数，调用 we_duplicate_finder_readonly.py
- 取消订阅：映射 output/bulk_unsub_controller.py 的所有命令行参数
- 下架归档：调用 we_delisted_archiver.py 检测/归档，并可一键生成取消订阅 xlsx

运行:
  python we_ui.py
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QObject, QTimer, Signal
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyleFactory,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

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


def _prevent_sleep() -> None:
    """告知 Windows：当前有长时间任务，禁止自动休眠/挂起。"""
    if _is_windows():
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(
                _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
            )
        except Exception:
            pass


def _allow_sleep() -> None:
    """恢复 Windows 默认休眠策略。"""
    if _is_windows():
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        except Exception:
            pass


def _which(p: str) -> str:
    if not p:
        return p
    if os.path.sep in p or (os.path.altsep and os.path.altsep in p):
        return p
    hit = shutil.which(p)
    return hit or p


def _as_int(s: Any, default: int) -> int:
    try:
        return int(str(s).strip())
    except Exception:
        return default


def _as_float(s: Any, default: float) -> float:
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
    return f'"{s}"'


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


# ------------------- Log sink -------------------

class LogSink(QObject):
    """线程安全日志接收器：读线程 push 到 queue，UI 线程 QTimer 批量写入 QPlainTextEdit。"""

    def __init__(self, widget: QPlainTextEdit):
        super().__init__()
        self.widget = widget
        self.widget.setReadOnly(True)
        self.widget.setMaximumBlockCount(8000)  # 自动截断，防止日志无限增长
        # Windows 某些机器强制 monospace hint 会回退到 Fixedsys，触发 DirectWrite 警告。
        # 改成“按可用字体表优先选择”的方式，避免 Fixedsys 路径。
        preferred = [
            "Cascadia Mono", "Consolas", "JetBrains Mono",
            "Source Code Pro", "Courier New", "Microsoft YaHei UI",
        ]
        db = QFontDatabase()
        families = set(db.families())
        chosen = next((f for f in preferred if f in families), "Microsoft YaHei UI")
        mono = QFont(chosen, 9)
        self.widget.setFont(mono)

        self._q: "queue.Queue[str]" = queue.Queue()
        self._closed = False

        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._flush)
        self._timer.start()

    def write(self, s: str) -> None:
        if self._closed:
            return
        self._q.put(s)

    def clear(self) -> None:
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        self.widget.clear()

    def close(self) -> None:
        self._closed = True
        try:
            self._timer.stop()
        except Exception:
            pass

    def _flush(self) -> None:
        if self._closed:
            return
        batch: List[str] = []
        total = 0
        max_chars = 65536
        try:
            while total < max_chars:
                s = self._q.get_nowait()
                batch.append(s)
                total += len(s)
        except queue.Empty:
            pass
        if not batch:
            return
        text = "".join(batch)
        # QPlainTextEdit 按 \n 切 block，\r 和进度条的回车我们合并到最后一行更新
        # 这里简单追加并滚到末尾，足以应付 tqdm 刷屏（maximumBlockCount 会自动裁）
        self.widget.moveCursor(QTextCursor.MoveOperation.End)
        self.widget.insertPlainText(text)
        self.widget.moveCursor(QTextCursor.MoveOperation.End)


# ------------------- Main window -------------------

class App(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Wallpaper Engine 视频筛重 / 批量取消订阅 UI")

        # 自适应屏幕
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            w = max(900, min(1400, int(geo.width() * 0.92)))
            h = max(640, min(980, int(geo.height() * 0.90)))
        else:
            w, h = 1200, 800
        self.setMinimumSize(900, 620)
        self.resize(w, h)

        self.proc: Optional[ProcHandle] = None

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.setCentralWidget(self.tabs)

        # 字段 widget 集合：key -> widget
        self._dedup_fields: Dict[str, QWidget] = {}

        # 各 tab 占位
        self.tab_dedup = QWidget()
        self.tab_unsub = QWidget()
        self.tab_archive = QWidget()
        self.tabs.addTab(self.tab_dedup, "筛重 / 查重")
        self.tabs.addTab(self.tab_unsub, "取消订阅")
        self.tabs.addTab(self.tab_archive, "下架归档")

        self._build_dedup_tab(self.tab_dedup)
        self._build_unsub_tab(self.tab_unsub)
        self._build_archive_tab(self.tab_archive)

        # 默认加载根目录 config.toml
        self.dedup_config_path.setText(str((REPO_ROOT / "config.toml").resolve()))
        self._load_config_into_form(Path(self.dedup_config_path.text()))

    # ------------------- close / process mgmt -------------------
    def closeEvent(self, event) -> None:  # noqa: N802
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
        super().closeEvent(event)

    def _kill_process_tree(self, p: subprocess.Popen) -> None:
        if p.poll() is not None:
            return
        try:
            if _is_windows():
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
            QMessageBox.warning(self, "正在运行", "已有任务在运行，请先停止。")
            return

        sink.clear()
        sink.write("[CMD] " + " ".join(cmd) + "\n\n")

        creationflags = 0
        if _is_windows():
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )

        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            # 统一子进程输出编码，避免 Windows 下 cp936 字节被 UI 按 utf-8 解码后出现乱码。
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            # 注意：不要设置 TQDM_ASCII=1。tqdm 会把字符串 "1" 当成仅含单字符的 bar charset，
            # 在 transformers 内部的 "Loading weights" 等进度条里触发 ZeroDivisionError
            # （len("1")-1=0 → divmod by 0）。我们自己的 tqdm 调用已经显式传 ascii=True（bool）。
        }
        p = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=creationflags,
        )

        def _reader() -> None:
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

    def _stop_running(self) -> None:
        if not self.proc:
            return
        p = self.proc.popen
        if p.poll() is None:
            self._kill_process_tree(p)
        self.proc = None
        _allow_sleep()

    # ------------------- helpers: form widgets -------------------
    @staticmethod
    def _make_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setMinimumWidth(380)
        lbl.setMaximumWidth(440)
        lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        return lbl

    def _field_value(self, key: str) -> Any:
        w = self._dedup_fields.get(key)
        if w is None:
            return ""
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QComboBox):
            return w.currentText()
        if isinstance(w, QSpinBox):
            return w.value()
        if isinstance(w, QLineEdit):
            return w.text()
        return ""

    def _field_set(self, key: str, v: Any) -> None:
        w = self._dedup_fields.get(key)
        if w is None:
            return
        if isinstance(w, QCheckBox):
            w.setChecked(_as_bool(v))
        elif isinstance(w, QComboBox):
            w.setCurrentText("" if v is None else str(v))
        elif isinstance(w, QSpinBox):
            w.setValue(_as_int(v, 0))
        elif isinstance(w, QLineEdit):
            w.setText("" if v is None else str(v))

    # ------------------- dedup tab -------------------
    def _build_dedup_tab(self, root: QWidget) -> None:
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # 顶部：config.toml 路径
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(QLabel("config.toml："))
        self.dedup_config_path = QLineEdit()
        top.addWidget(self.dedup_config_path, 1)
        btn_pick = QPushButton("选择…")
        btn_pick.clicked.connect(self._pick_config)
        top.addWidget(btn_pick)
        btn_reload = QPushButton("加载")
        btn_reload.clicked.connect(self._reload_config)
        top.addWidget(btn_reload)
        btn_save = QPushButton("另存为…")
        btn_save.clicked.connect(self._save_config_as)
        top.addWidget(btn_save)
        outer.addLayout(top)

        # 中部：左右分栏 —— 左边表单（可滚动），右边日志
        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter, 1)

        # 左：滚动的配置表单
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        form_host = QWidget()
        scroll.setWidget(form_host)

        grid = QGridLayout(form_host)
        grid.setContentsMargins(6, 6, 12, 6)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(0, 0)  # label
        grid.setColumnStretch(1, 1)  # input
        grid.setColumnStretch(2, 0)  # pick button

        def add_section(row: int, text: str) -> int:
            lbl = QLabel(text)
            f = lbl.font()
            f.setBold(True)
            f.setPointSize(f.pointSize() + 1)
            lbl.setFont(f)
            grid.addWidget(lbl, row, 0, 1, 3)
            return row + 1

        def add_separator(row: int) -> int:
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setFrameShadow(QFrame.Shadow.Sunken)
            grid.addWidget(line, row, 0, 1, 3)
            return row + 1

        def add_line(row: int, label: str, key: str) -> int:
            grid.addWidget(self._make_label(label), row, 0)
            w = QLineEdit()
            w.setClearButtonEnabled(True)
            grid.addWidget(w, row, 1)
            self._dedup_fields[key] = w
            return row + 1

        def add_bool(row: int, label: str, key: str) -> int:
            grid.addWidget(self._make_label(label), row, 0)
            w = QCheckBox()
            grid.addWidget(w, row, 1, Qt.AlignmentFlag.AlignLeft)
            self._dedup_fields[key] = w
            return row + 1

        def add_choice(row: int, label: str, key: str, options: List[str]) -> int:
            grid.addWidget(self._make_label(label), row, 0)
            w = QComboBox()
            w.addItems(options)
            grid.addWidget(w, row, 1)
            self._dedup_fields[key] = w
            return row + 1

        def add_dir(row: int, label: str, key: str) -> int:
            grid.addWidget(self._make_label(label), row, 0)
            w = QLineEdit()
            w.setClearButtonEnabled(True)
            grid.addWidget(w, row, 1)
            self._dedup_fields[key] = w
            btn = QPushButton("…")
            btn.setFixedWidth(28)
            btn.clicked.connect(lambda _=False, ww=w: self._pick_dir_into(ww))
            grid.addWidget(btn, row, 2)
            return row + 1

        def add_file(row: int, label: str, key: str) -> int:
            grid.addWidget(self._make_label(label), row, 0)
            w = QLineEdit()
            w.setClearButtonEnabled(True)
            grid.addWidget(w, row, 1)
            self._dedup_fields[key] = w
            btn = QPushButton("…")
            btn.setFixedWidth(28)
            btn.clicked.connect(lambda _=False, ww=w: self._pick_file_into(ww))
            grid.addWidget(btn, row, 2)
            return row + 1

        r = 0
        r = add_section(r, "参数（对应 config.toml）")
        r = add_dir(r, "workshop_root（创意工坊目录）", "workshop_root")
        r = add_dir(r, "we_install_dir（WE 根目录，用于扫描 myprojects）", "we_install_dir")
        r = add_bool(r, "include_myprojects（筛重包含本地 myprojects）", "include_myprojects")
        r = add_dir(r, "output_dir（输出目录）", "output_dir")
        r = add_dir(r, "model_cache_dir（模型缓存目录，默认 models_cache）", "model_cache_dir")
        r = add_file(r, "ffmpeg_path", "ffmpeg_path")
        r = add_file(r, "ffprobe_path", "ffprobe_path")
        r = add_file(r, "fpcalc_path", "fpcalc_path")

        r = add_separator(r)
        r = add_line(r, "video_window_seconds（秒）", "video_window_seconds")
        r = add_line(r, "audio_window_seconds（秒）", "audio_window_seconds")
        r = add_line(r, "seek_ratio（0~1）", "seek_ratio")

        r = add_separator(r)
        r = add_line(r, "sample_frames（抽帧数）", "sample_frames")
        r = add_line(r, "phash_size", "phash_size")
        r = add_line(r, "phash_distance_threshold（组合距离分阈值，越小越严格；默认 1.5）", "phash_distance_threshold")
        r = add_line(r, "phash_trimmed_mean_cap（截尾均值上限，64 位基准；默认 12.0）", "phash_trimmed_mean_cap")
        r = add_line(r, "phash_trim_ratio（丢弃最高距离帧的比例；默认 0.2）", "phash_trim_ratio")
        r = add_line(r, 'phash_bimodal_gap_cap（双峰差上限；水印+不同码率也会造成双峰，默认放宽到 40.0 让语义闸把关）', "phash_bimodal_gap_cap")
        r = add_line(r, "color_hist_bins_h（HSV 色相 bin 数；默认 16）", "color_hist_bins_h")
        r = add_line(r, "color_hist_bins_s（HSV 饱和度 bin 数；默认 4）", "color_hist_bins_s")
        r = add_line(r, 'color_distance_threshold（颜色距离阈值 Bhattacharyya，挡"同场景不同角色/服饰"；默认 0.15）', "color_distance_threshold")
        r = add_bool(r, 'audio_merge_override_color（音频软闸：color 判拆时若音频几乎一致则豁免 color；首次开启会对所有视频补跑一遍 fpcalc）', "audio_merge_override_color")
        r = add_line(r, 'audio_merge_threshold（Chromaprint 归一化汉明距离上限；<=此值视为音频相同；默认 0.15）', "audio_merge_threshold")
        r = add_bool(r, '语义特征 semantic_feature_enabled（DINOv2 第三道闸，挡"同源差分"；需要 pip install torch）', "semantic_feature_enabled")
        r = add_line(r, 'semantic_feature_model（dinov2_s / dinov2_b / dinov2_l；默认 dinov2_s）', "semantic_feature_model")
        r = add_line(r, 'semantic_feature_device（auto / cuda / cpu；默认 auto）', "semantic_feature_device")
        r = add_line(r, 'semantic_sample_frames（语义专用：全片均匀抽帧数；默认 60）', "semantic_sample_frames")
        r = add_line(r, 'semantic_distance_threshold（per-frame cosine 距离 mean 上限；默认 0.015，抓整体性不同）', "semantic_distance_threshold")
        r = add_line(r, 'semantic_max_threshold（per-frame cosine 距离 max 上限；默认 0.040，绝对值兜底）', "semantic_max_threshold")
        r = add_line(r, 'semantic_peak_ratio_threshold（max/mean 上限=尖峰闸；默认 3.8，区分水印(平坦)与差分(尖峰)的核心）', "semantic_peak_ratio_threshold")
        r = add_line(r, 'semantic_peak_min_max（尖峰闸前置，max 先超过这个值才用 ratio；默认 0.015，防低 mean 时比率虚高）', "semantic_peak_min_max")
        r = add_line(r, 'semantic_drift_p90_exempt（平坦漂移例外：p90 <= 此值时豁免 max/ratio 尖峰闸；默认 0.005）', "semantic_drift_p90_exempt")
        r = add_line(r, 'semantic_drift_sparse_mid_count（稀疏超级尖峰例外：(0.5×max闸, max闸] 区间帧数 <= 此值时豁免；默认 2）', "semantic_drift_sparse_mid_count")
        r = add_bool(r, 'Patch 空间闸 semantic_patch_enabled（第四道闸：看高距 patch 在画面哪个区域，边角=水印→合并，中心=差分→拆；默认 True）', "semantic_patch_enabled")
        r = add_line(r, 'semantic_patch_grid（patch 网格大小，8=把 16×16 avgpool 到 8×8=64 patches/帧；默认 8）', "semantic_patch_grid")
        r = add_line(r, 'semantic_patch_hot_threshold（patch 距离"热点"下限，低于此值不算差异；默认 0.015）', "semantic_patch_hot_threshold")
        r = add_line(r, 'semantic_patch_min_hot_patches（跨帧热点总数下限，不足则 abstain；默认 12）', "semantic_patch_min_hot_patches")
        r = add_line(r, 'semantic_patch_center_margin（归一化距 <= 此值算"中心"区域；默认 0.4，8 网格下约中央 4×4）', "semantic_patch_center_margin")
        r = add_line(r, 'semantic_patch_edge_margin（归一化距 >= 此值算"边角"区域；默认 0.6）', "semantic_patch_edge_margin")
        r = add_line(r, 'semantic_patch_corner_merge_frac（边角热点占比 >= 此值→水印候选；默认 0.55）', "semantic_patch_corner_merge_frac")
        r = add_line(r, 'semantic_patch_center_split_frac（中心热点占比 >= 此值→判差分；默认 0.45）', "semantic_patch_center_split_frac")
        r = add_line(r, 'semantic_patch_persistent_frame_frac（≥此比例帧都热点→"持久热点"；默认 0.5）', "semantic_patch_persistent_frame_frac")
        r = add_line(r, 'semantic_patch_persistent_min（strong_wm 持久热点数下限；默认 2）', "semantic_patch_persistent_min")
        r = add_line(r, 'semantic_patch_persistent_max（strong_wm 持久热点数上限，超过→视为稳定内容差异而非水印；默认 8）', "semantic_patch_persistent_max")
        r = add_line(r, 'semantic_patch_persistent_corner_min（strong_wm 持久热点位于角落占比 >= 此值；默认 0.8）', "semantic_patch_persistent_corner_min")
        r = add_line(r, 'semantic_patch_weak_center_max（weak_wm 中心热点占比 < 此值；默认 0.12）', "semantic_patch_weak_center_max")
        r = add_line(r, 'semantic_patch_weak_hot_ratio_max（weak_wm 热点占全部 patch 比例 < 此值；默认 0.10）', "semantic_patch_weak_hot_ratio_max")
        r = add_line(r, 'semantic_patch_heavy_persistent_min（heavy 持久热点数 >= 此值；默认 10）', "semantic_patch_heavy_persistent_min")
        r = add_line(r, 'semantic_patch_heavy_hot_ratio_min（heavy 热点占比 >= 此值；默认 0.20）', "semantic_patch_heavy_hot_ratio_min")
        r = add_line(r, 'semantic_patch_heavy_pers_corner_max（heavy 持久热点不全在角落：pc < 此值；默认 0.85）', "semantic_patch_heavy_pers_corner_max")
        r = add_line(r, 'semantic_patch_heavy_min_ratio（heavy 需 max/mean >= 此值，否则视为重编码漂移保留合并；默认 2.5）', "semantic_patch_heavy_min_ratio")
        r = add_line(r, 'semantic_patch_center_persistent_corner_max（center_persistent：持久热点几乎不在角落 pc <= 此值；默认 0.20）', "semantic_patch_center_persistent_corner_max")
        r = add_line(r, 'semantic_patch_center_persistent_total_corner_max（center_persistent：总热点不在角落 corner <= 此值；默认 0.25）', "semantic_patch_center_persistent_total_corner_max")

        r = add_choice(r, "duration_rounding", "duration_rounding", ["nearest_1.0", "nearest_0.5", "int"])
        r = add_bool(r, "require_both_signatures（视频+音频都要）", "require_both_signatures")
        r = add_line(r, 'duration_cross_bucket_tolerance（跨相邻时长桶比较容差秒；>0 时相邻桶交叉比较，救"跨桶边界的同源对"；默认 0.6）', "duration_cross_bucket_tolerance")

        r = add_separator(r)
        r = add_line(r, "max_workers_stage1", "max_workers_stage1")
        r = add_line(r, "max_workers_stage2", "max_workers_stage2")
        r = add_line(r, "ffprobe_timeout", "ffprobe_timeout")
        r = add_line(r, "ffmpeg_timeout", "ffmpeg_timeout")
        r = add_line(r, "fpcalc_timeout", "fpcalc_timeout")

        r = add_separator(r)
        r = add_line(r, "log_file（空=仅控制台）", "log_file")
        r = add_bool(r, "progress（默认进度条）", "progress")

        # 命令行开关（不写入 toml）
        r = add_separator(r)
        r = add_section(r, "命令行开关（不写入 toml）")
        self.dedup_verbose = QCheckBox("--verbose（DEBUG 日志）")
        self.dedup_trace = QCheckBox("--trace（打印外部命令）")
        self.dedup_no_progress = QCheckBox("--no-progress（关闭 tqdm）")
        grid.addWidget(self.dedup_verbose, r, 0, 1, 3); r += 1
        grid.addWidget(self.dedup_trace, r, 0, 1, 3); r += 1
        grid.addWidget(self.dedup_no_progress, r, 0, 1, 3); r += 1

        # 表单底部拉伸
        grid.setRowStretch(r, 1)

        splitter.addWidget(scroll)

        # 右：日志面板。顶部一行：左侧三个动作按钮 + 右侧"日志输出"标签
        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(6, 6, 6, 6)
        rlay.setSpacing(6)

        log_header = QHBoxLayout()
        log_header.setContentsMargins(0, 0, 0, 0)
        log_header.setSpacing(6)
        btn_run = QPushButton("运行筛重")
        btn_run.clicked.connect(self._run_dedup)
        btn_stop = QPushButton("停止")
        btn_stop.clicked.connect(self._stop_running)
        btn_open = QPushButton("打开输出目录")
        btn_open.clicked.connect(self._open_output_dir)
        log_header.addWidget(btn_run)
        log_header.addWidget(btn_stop)
        log_header.addWidget(btn_open)
        log_header.addStretch(1)
        lbl_log = QLabel("日志输出")
        f = lbl_log.font()
        f.setBold(True)
        lbl_log.setFont(f)
        log_header.addWidget(lbl_log)
        rlay.addLayout(log_header)

        log_widget = QPlainTextEdit()
        rlay.addWidget(log_widget, 1)
        self.log_dedup = LogSink(log_widget)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([820, 520])

    # ------------------- dedup tab: actions -------------------
    def _pick_config(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self, "选择 config.toml", str(REPO_ROOT), "TOML (*.toml);;All (*.*)"
        )
        if not p:
            return
        self.dedup_config_path.setText(p)
        self._load_config_into_form(Path(p))

    def _reload_config(self) -> None:
        p = Path(self.dedup_config_path.text().strip())
        self._load_config_into_form(p)

    def _pick_dir_into(self, line: QLineEdit) -> None:
        p = QFileDialog.getExistingDirectory(self, "选择目录", line.text() or str(REPO_ROOT))
        if p:
            line.setText(p)

    def _pick_file_into(self, line: QLineEdit) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "选择文件", line.text() or str(REPO_ROOT), "All (*.*)")
        if p:
            line.setText(p)

    def _save_config_as(self) -> None:
        p, _ = QFileDialog.getSaveFileName(
            self, "另存为 config.toml", str(REPO_ROOT / "config.toml"), "TOML (*.toml);;All (*.*)"
        )
        if not p:
            return
        cfg = self._collect_dedup_config_dict()
        Path(p).write_text(dump_simple_toml(cfg), encoding="utf-8")
        QMessageBox.information(self, "已保存", f"已保存：{p}")
        self.dedup_config_path.setText(p)

    def _load_config_into_form(self, p: Path) -> None:
        data = load_toml(p)
        defaults = load_toml(REPO_ROOT / "config.toml")
        merged = dict(defaults)
        merged.update(data or {})
        for k in list(self._dedup_fields.keys()):
            if k in merged:
                self._field_set(k, merged.get(k))

        # 外部工具路径显示实际命中
        for key in ("ffmpeg_path", "ffprobe_path", "fpcalc_path"):
            if key in self._dedup_fields:
                vv = str(self._field_value(key)).strip()
                if vv:
                    self._field_set(key, _which(vv))

        # 下架归档 tab 共用
        if hasattr(self, "arc_steam_api_key"):
            self.arc_steam_api_key.setText(str(merged.get("steam_api_key", "")))

    def _collect_dedup_config_dict(self) -> Dict[str, Any]:
        g = self._field_value
        d: Dict[str, Any] = {}
        d["workshop_root"] = str(g("workshop_root")).strip()
        d["we_install_dir"] = str(g("we_install_dir")).strip()
        d["include_myprojects"] = bool(g("include_myprojects"))
        d["output_dir"] = str(g("output_dir")).strip() or "output"
        d["model_cache_dir"] = str(g("model_cache_dir")).strip() or "models_cache"
        d["ffmpeg_path"] = str(g("ffmpeg_path")).strip() or "ffmpeg"
        d["ffprobe_path"] = str(g("ffprobe_path")).strip() or "ffprobe"
        d["fpcalc_path"] = str(g("fpcalc_path")).strip() or "fpcalc"

        d["video_window_seconds"] = _as_int(g("video_window_seconds"), 15)
        d["audio_window_seconds"] = _as_int(g("audio_window_seconds"), 60)
        d["seek_ratio"] = _as_float(g("seek_ratio"), 0.5)

        d["sample_frames"] = _as_int(g("sample_frames"), 36)
        d["phash_size"] = _as_int(g("phash_size"), 12)
        d["phash_distance_threshold"] = _as_float(g("phash_distance_threshold"), 1.5)
        d["phash_trimmed_mean_cap"] = _as_float(g("phash_trimmed_mean_cap"), 12.0)
        d["phash_trim_ratio"] = _as_float(g("phash_trim_ratio"), 0.2)
        d["phash_bimodal_gap_cap"] = _as_float(g("phash_bimodal_gap_cap"), 40.0)
        d["color_hist_bins_h"] = _as_int(g("color_hist_bins_h"), 16)
        d["color_hist_bins_s"] = _as_int(g("color_hist_bins_s"), 4)
        d["color_distance_threshold"] = _as_float(g("color_distance_threshold"), 0.15)
        d["audio_merge_override_color"] = bool(g("audio_merge_override_color"))
        d["audio_merge_threshold"] = _as_float(g("audio_merge_threshold"), 0.15)

        d["semantic_feature_enabled"] = bool(g("semantic_feature_enabled"))
        d["semantic_feature_model"] = str(g("semantic_feature_model")).strip() or "dinov2_s"
        d["semantic_feature_device"] = str(g("semantic_feature_device")).strip() or "auto"
        d["semantic_sample_frames"] = _as_int(g("semantic_sample_frames"), 60)
        d["semantic_distance_threshold"] = _as_float(g("semantic_distance_threshold"), 0.015)
        d["semantic_max_threshold"] = _as_float(g("semantic_max_threshold"), 0.040)
        d["semantic_peak_ratio_threshold"] = _as_float(g("semantic_peak_ratio_threshold"), 3.8)
        d["semantic_peak_min_max"] = _as_float(g("semantic_peak_min_max"), 0.015)
        d["semantic_drift_p90_exempt"] = _as_float(g("semantic_drift_p90_exempt"), 0.005)
        d["semantic_drift_sparse_mid_count"] = _as_int(g("semantic_drift_sparse_mid_count"), 2)

        d["semantic_patch_enabled"] = bool(g("semantic_patch_enabled"))
        d["semantic_patch_grid"] = _as_int(g("semantic_patch_grid"), 8)
        d["semantic_patch_hot_threshold"] = _as_float(g("semantic_patch_hot_threshold"), 0.015)
        d["semantic_patch_min_hot_patches"] = _as_int(g("semantic_patch_min_hot_patches"), 12)
        d["semantic_patch_center_margin"] = _as_float(g("semantic_patch_center_margin"), 0.4)
        d["semantic_patch_edge_margin"] = _as_float(g("semantic_patch_edge_margin"), 0.6)
        d["semantic_patch_corner_merge_frac"] = _as_float(g("semantic_patch_corner_merge_frac"), 0.55)
        d["semantic_patch_center_split_frac"] = _as_float(g("semantic_patch_center_split_frac"), 0.45)
        d["semantic_patch_persistent_frame_frac"] = _as_float(g("semantic_patch_persistent_frame_frac"), 0.5)
        d["semantic_patch_persistent_min"] = _as_int(g("semantic_patch_persistent_min"), 2)
        d["semantic_patch_persistent_max"] = _as_int(g("semantic_patch_persistent_max"), 8)
        d["semantic_patch_persistent_corner_min"] = _as_float(g("semantic_patch_persistent_corner_min"), 0.8)
        d["semantic_patch_weak_center_max"] = _as_float(g("semantic_patch_weak_center_max"), 0.12)
        d["semantic_patch_weak_hot_ratio_max"] = _as_float(g("semantic_patch_weak_hot_ratio_max"), 0.10)
        d["semantic_patch_heavy_persistent_min"] = _as_int(g("semantic_patch_heavy_persistent_min"), 10)
        d["semantic_patch_heavy_hot_ratio_min"] = _as_float(g("semantic_patch_heavy_hot_ratio_min"), 0.20)
        d["semantic_patch_heavy_pers_corner_max"] = _as_float(g("semantic_patch_heavy_pers_corner_max"), 0.85)
        d["semantic_patch_heavy_min_ratio"] = _as_float(g("semantic_patch_heavy_min_ratio"), 2.5)
        d["semantic_patch_center_persistent_corner_max"] = _as_float(g("semantic_patch_center_persistent_corner_max"), 0.20)
        d["semantic_patch_center_persistent_total_corner_max"] = _as_float(g("semantic_patch_center_persistent_total_corner_max"), 0.25)

        d["duration_rounding"] = str(g("duration_rounding")).strip() or "nearest_0.5"
        d["require_both_signatures"] = bool(g("require_both_signatures"))
        d["duration_cross_bucket_tolerance"] = _as_float(g("duration_cross_bucket_tolerance"), 0.6)

        d["max_workers_stage1"] = _as_int(g("max_workers_stage1"), 8)
        d["max_workers_stage2"] = _as_int(g("max_workers_stage2"), 6)
        d["ffprobe_timeout"] = _as_int(g("ffprobe_timeout"), 60)
        d["ffmpeg_timeout"] = _as_int(g("ffmpeg_timeout"), 60)
        d["fpcalc_timeout"] = _as_int(g("fpcalc_timeout"), 60)

        log_file = str(g("log_file")).strip()
        d["log_file"] = log_file if log_file else ""
        d["progress"] = bool(g("progress"))
        return d

    def _open_output_dir(self) -> None:
        out_dir = Path(str(self._field_value("output_dir")).strip() or "output")
        if not out_dir.is_absolute():
            out_dir = (REPO_ROOT / out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            if _is_windows():
                os.startfile(str(out_dir))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(out_dir)])
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

    def _run_dedup(self) -> None:
        cfg = self._collect_dedup_config_dict()
        if not cfg.get("workshop_root"):
            QMessageBox.critical(self, "参数缺失", "请填写 workshop_root（创意工坊目录）。")
            return

        tmp_dir = Path(tempfile.gettempdir()) / "we_dedup_ui"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        tmp_cfg = tmp_dir / f"config_ui_{ts}.toml"
        tmp_cfg.write_text(dump_simple_toml(cfg), encoding="utf-8")

        cmd = [sys.executable, str((REPO_ROOT / "we_duplicate_finder_readonly.py").resolve()), "-c", str(tmp_cfg)]
        if self.dedup_verbose.isChecked():
            cmd.append("--verbose")
        if self.dedup_trace.isChecked():
            cmd.append("--trace")
        if self.dedup_no_progress.isChecked():
            cmd.append("--no-progress")
        self._start_process(cmd, cwd=REPO_ROOT, sink=self.log_dedup)

    # ------------------- unsub tab -------------------
    def _build_unsub_tab(self, root: QWidget) -> None:
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        box = QGroupBox("批量取消订阅")
        g = QGridLayout(box)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(6)

        g.addWidget(QLabel("xlsx（duplicates_*.xlsx）："), 0, 0)
        self.unsub_xlsx = QLineEdit()
        self.unsub_xlsx.setClearButtonEnabled(True)
        g.addWidget(self.unsub_xlsx, 0, 1, 1, 3)
        b1 = QPushButton("选择…"); b1.clicked.connect(self._pick_xlsx); g.addWidget(b1, 0, 4)
        b2 = QPushButton("选最新"); b2.clicked.connect(self._pick_latest_xlsx); g.addWidget(b2, 0, 5)
        b3 = QPushButton("打开所在目录"); b3.clicked.connect(self._open_xlsx_dir); g.addWidget(b3, 0, 6)

        self.unsub_single_page = QCheckBox("单页面模式（推荐）")
        self.unsub_single_page.setChecked(True)
        self.unsub_single_page.toggled.connect(self._refresh_unsub_controls)
        g.addWidget(self.unsub_single_page, 1, 0)

        g.addWidget(QLabel("batch-size："), 1, 1, Qt.AlignmentFlag.AlignRight)
        self.unsub_batch_size = QSpinBox()
        self.unsub_batch_size.setRange(1, 50)
        self.unsub_batch_size.setValue(1)
        g.addWidget(self.unsub_batch_size, 1, 2)

        self.unsub_add_appid = QCheckBox("add-appid（可选）")
        g.addWidget(self.unsub_add_appid, 1, 3)

        g.addWidget(QLabel("notify-port："), 1, 4, Qt.AlignmentFlag.AlignRight)
        self.unsub_notify_port = QLineEdit("8787")
        self.unsub_notify_port.setFixedWidth(80)
        g.addWidget(self.unsub_notify_port, 1, 5)

        g.addWidget(QLabel("single-page-url（可选）："), 2, 0)
        self.unsub_single_page_url = QLineEdit()
        self.unsub_single_page_url.setClearButtonEnabled(True)
        g.addWidget(self.unsub_single_page_url, 2, 1, 1, 5)
        b4 = QPushButton("清空")
        b4.clicked.connect(lambda: self.unsub_single_page_url.clear())
        g.addWidget(b4, 2, 6)

        note = QLabel("提示：筛重表每行第一列为保留项；其余列 Steam 链接由油猴退订，myprojects 的 file:/// 会先本地删夹。需已登录 Steam。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        g.addWidget(note, 3, 0, 1, 7)

        btns = QHBoxLayout()
        brun = QPushButton("运行取消订阅"); brun.clicked.connect(self._run_unsub); btns.addWidget(brun)
        bstop = QPushButton("停止"); bstop.clicked.connect(self._stop_running); btns.addWidget(bstop)
        btns.addStretch(1)
        btn_wrap = QWidget(); btn_wrap.setLayout(btns)
        g.addWidget(btn_wrap, 4, 0, 1, 7)

        outer.addWidget(box)

        # 日志
        outer.addWidget(QLabel("日志输出"))
        log_widget = QPlainTextEdit()
        outer.addWidget(log_widget, 1)
        self.log_unsub = LogSink(log_widget)

        self._refresh_unsub_controls()

    def _refresh_unsub_controls(self) -> None:
        self.unsub_single_page_url.setEnabled(self.unsub_single_page.isChecked())

    def _pick_xlsx(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self, "选择 duplicates_*.xlsx", str(REPO_ROOT / "output"), "Excel (*.xlsx);;All (*.*)"
        )
        if p:
            self.unsub_xlsx.setText(p)

    def _pick_latest_xlsx(self) -> None:
        out_dir = Path(str(self._field_value("output_dir")).strip() or "output")
        if not out_dir.is_absolute():
            out_dir = (REPO_ROOT / out_dir).resolve()
        latest = find_latest_duplicates_xlsx(out_dir)
        if not latest:
            QMessageBox.warning(self, "未找到", f"在 {out_dir} 未找到 duplicates_*.xlsx")
            return
        self.unsub_xlsx.setText(str(latest))

    def _open_xlsx_dir(self) -> None:
        p = Path(self.unsub_xlsx.text().strip() or "")
        if not p.exists():
            return
        d = p.parent
        try:
            if _is_windows():
                os.startfile(str(d))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(d)])
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

    def _run_unsub(self) -> None:
        xlsx = Path(self.unsub_xlsx.text().strip() or "")
        if not xlsx.exists():
            QMessageBox.critical(self, "参数缺失", "请选择有效的 xlsx 文件。")
            return

        cmd = [sys.executable, str((REPO_ROOT / "output" / "bulk_unsub_controller.py").resolve()), "--xlsx", str(xlsx)]
        cmd += ["--batch-size", str(int(self.unsub_batch_size.value()))]
        cmd += ["--notify-port", str(_as_int(self.unsub_notify_port.text(), 8787))]
        if self.unsub_add_appid.isChecked():
            cmd.append("--add-appid")
        if self.unsub_single_page.isChecked():
            cmd.append("--single-page")
            url = self.unsub_single_page_url.text().strip()
            if url:
                cmd += ["--single-page-url", url]
        self._start_process(cmd, cwd=REPO_ROOT / "output", sink=self.log_unsub)

    # ------------------- archive tab -------------------
    def _build_archive_tab(self, root: QWidget) -> None:
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        box = QGroupBox("下架归档")
        g = QGridLayout(box)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(6)

        # WE 安装目录 —— 与筛重 tab 的 we_install_dir 共享同一字段值（非双向绑定，主要以筛重 tab 为源）
        g.addWidget(QLabel("WE 安装目录："), 0, 0)
        self.arc_we_install_dir = QLineEdit()
        self.arc_we_install_dir.setClearButtonEnabled(True)
        g.addWidget(self.arc_we_install_dir, 0, 1, 1, 2)
        bd = QPushButton("选择…")
        bd.clicked.connect(lambda: self._pick_dir_into(self.arc_we_install_dir))
        g.addWidget(bd, 0, 3)

        g.addWidget(QLabel("Steam API Key（推荐）："), 1, 0)
        self.arc_steam_api_key = QLineEdit()
        self.arc_steam_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.arc_steam_api_key.setClearButtonEnabled(True)
        g.addWidget(self.arc_steam_api_key, 1, 1, 1, 2)
        bk = QPushButton("申请")
        bk.clicked.connect(lambda: __import__("webbrowser").open("https://steamcommunity.com/dev/apikey"))
        g.addWidget(bk, 1, 3)

        note = QLabel("提示：有 API Key 时使用 IPublishedFileService（检测更准确）；无 Key 回退到旧 API。取消订阅会打开 steamcommunity.com/my/…（已登录即当前账号）。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        g.addWidget(note, 2, 0, 1, 4)

        btns = QHBoxLayout()
        b1 = QPushButton("检测下架物品"); b1.clicked.connect(self._run_arc_detect); btns.addWidget(b1)
        b2 = QPushButton("归档到本地");  b2.clicked.connect(self._run_arc_archive); btns.addWidget(b2)
        b3 = QPushButton("取消订阅已下架"); b3.clicked.connect(self._run_arc_unsub); btns.addWidget(b3)
        b4 = QPushButton("停止"); b4.clicked.connect(self._stop_running); btns.addWidget(b4)
        btns.addStretch(1)
        wrap = QWidget(); wrap.setLayout(btns)
        g.addWidget(wrap, 3, 0, 1, 4)

        outer.addWidget(box)

        outer.addWidget(QLabel("日志输出"))
        log_widget = QPlainTextEdit()
        outer.addWidget(log_widget, 1)
        self.log_archive = LogSink(log_widget)

    # ------------------- archive tab: actions -------------------
    def _arc_get_config_path(self) -> Path:
        return Path(self.dedup_config_path.text().strip() or str(REPO_ROOT / "config.toml"))

    def _arc_workshop_root(self) -> str:
        return str(self._field_value("workshop_root")).strip()

    def _arc_we_install_dir_effective(self) -> str:
        """优先取归档 tab 自己的输入，未填则回退到筛重 tab 的 we_install_dir。"""
        v = self.arc_we_install_dir.text().strip()
        if v:
            return v
        return str(self._field_value("we_install_dir")).strip()

    def _run_arc_detect(self) -> None:
        wr = self._arc_workshop_root()
        if not wr:
            QMessageBox.critical(self, "参数缺失", "请在「筛重」标签页填写 workshop_root。")
            return
        cfg_path = self._arc_get_config_path()
        cmd = [
            sys.executable,
            str((REPO_ROOT / "we_delisted_archiver.py").resolve()),
            "-c", str(cfg_path),
            "--detect",
            "--workshop-root", wr,
        ]
        api_key = self.arc_steam_api_key.text().strip()
        if api_key:
            cmd += ["--steam-api-key", api_key]
        self._start_process(cmd, cwd=REPO_ROOT, sink=self.log_archive)

    def _run_arc_archive(self) -> None:
        we_dir = self._arc_we_install_dir_effective()
        if not we_dir:
            QMessageBox.critical(self, "参数缺失", "请填写 WE 安装目录。")
            return
        wr = self._arc_workshop_root()
        if not wr:
            QMessageBox.critical(self, "参数缺失", "请在「筛重」标签页填写 workshop_root。")
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

    def _run_arc_unsub(self) -> None:
        """生成 xlsx → 调用 bulk_unsub_controller.py 取消订阅已下架物品。"""
        out_dir = Path(str(self._field_value("output_dir")).strip() or "output")
        if not out_dir.is_absolute():
            out_dir = (REPO_ROOT / out_dir).resolve()

        delisted_path = out_dir / "delisted_items.json"
        if not delisted_path.exists():
            QMessageBox.critical(self, "未找到", f"请先运行「检测下架物品」\n{delisted_path}")
            return

        try:
            with delisted_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items", [])
            if not items:
                QMessageBox.information(self, "无需操作", "没有需要取消订阅的下架物品。")
                return
        except Exception as e:
            QMessageBox.critical(self, "读取失败", str(e))
            return

        try:
            from openpyxl import Workbook
        except ImportError:
            QMessageBox.critical(self, "缺少依赖", "需要 openpyxl: pip install openpyxl")
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


# ------------------- entry -------------------

def _apply_windows11_style(app: QApplication) -> None:
    """优先使用 Windows 11 原生风格（Qt 6.8+），回退到 windowsvista / Fusion。"""
    if not _is_windows():
        return
    keys = [k.lower() for k in QStyleFactory.keys()]
    for preferred in ("windows11", "windowsvista", "fusion"):
        if preferred in keys:
            for real in QStyleFactory.keys():
                if real.lower() == preferred:
                    app.setStyle(real)
                    return


def main() -> None:
    # 抑制 Qt 在 Windows 下偶发的字体探测噪声（不影响实际渲染）。
    os.environ.setdefault(
        "QT_LOGGING_RULES",
        "qt.qpa.fonts.warning=false;qt.text.font.db.warning=false",
    )
    # 高 DPI：Qt6 默认启用 HighDPI 缩放，这里显式设置取整策略，避免在 125%/150% 缩放下字体虚糊
    if hasattr(Qt, "HighDpiScaleFactorRoundingPolicy"):
        try:
            QApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
            )
        except Exception:
            pass

    app = QApplication(sys.argv)
    _apply_windows11_style(app)

    # 全局字体：Windows 下优先用 Segoe UI / Microsoft YaHei UI
    if _is_windows():
        f = QFont("Microsoft YaHei UI", 9)
        app.setFont(f)

    w = App()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
