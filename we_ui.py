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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QObject, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

try:
    import tomllib  # py311+
except Exception:  # pragma: no cover
    import tomli as tomllib  # py310


REPO_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def _worker_command(cmd: List[str]) -> List[str]:
    if getattr(sys, "frozen", False) and len(cmd) > 1 and cmd[0] == sys.executable:
        return [str(REPO_ROOT / "WEWorker.exe"), "--worker", Path(cmd[1]).name, *cmd[2:]]
    return cmd


def _write_toml_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(dump_simple_toml(data))
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

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
        if Path(p).exists():
            return p
        bundled = Path(getattr(sys, "_MEIPASS", REPO_ROOT)) / "tools" / Path(p).name
        return str(bundled) if bundled.exists() else p
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
    return json.dumps(str(s), ensure_ascii=False)


def dump_simple_toml(d: Dict[str, Any]) -> str:
    """只覆盖本项目 config.toml 用到的原始类型（str/int/float/bool/list[str]）。"""
    def _fmt_scalar(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, float):
            return repr(float(v))
        return _toml_quote(str(v))

    lines: List[str] = []
    for k, v in d.items():
        if isinstance(v, list):
            parts = [_fmt_scalar(x) for x in v]
            lines.append(f"{k} = [{', '.join(parts)}]")
        else:
            lines.append(f"{k} = {_fmt_scalar(v)}")
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
    cancelled: bool = False


# ------------------- archive pipeline state -------------------

# 两条独立的"归档→退订"流水线，每步完成推进到下一步；最后一步完成后回到
# 起点。亮态 = 当前 stage 对应的按钮；其它按钮视觉 dim + 点击拦截（弹
# "是否继续"）。
#
#   delisted_stage: detect → archive → unsub → (detect)
#   folder_stage:   archive → unsub → (archive)

_DELISTED_STAGES = ("detect", "archive", "unsub")
_DELISTED_NEXT = {"detect": "archive", "archive": "unsub", "unsub": "detect"}

_FOLDER_STAGES = ("archive", "unsub")
_FOLDER_NEXT = {"archive": "unsub", "unsub": "archive"}


@dataclass
class ArchivePipelineState:
    delisted_stage: str = "detect"
    folder_stage: str = "archive"

    def to_dict(self) -> Dict[str, str]:
        return {
            "delisted_stage": self.delisted_stage,
            "folder_stage": self.folder_stage,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ArchivePipelineState":
        st = cls()
        v = str(d.get("delisted_stage", "") or "").strip()
        if v in _DELISTED_STAGES:
            st.delisted_stage = v
        v = str(d.get("folder_stage", "") or "").strip()
        if v in _FOLDER_STAGES:
            st.folder_stage = v
        return st


# ------------------- custom widgets -------------------

class NoWheelComboBox(QComboBox):
    """禁止滚轮改动选择项——下拉框里滚轮误触发会把参数改掉。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: Any) -> None:  # type: ignore[override]
        event.ignore()


class NoWheelSpinBox(QSpinBox):
    """同理：滚轮不要改数值，防止在滚动表单时误改配置。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: Any) -> None:  # type: ignore[override]
        event.ignore()


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
        families = set(QFontDatabase.families())
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
    _ui_call = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._ui_call.connect(self._invoke_ui_call)
        self.setWindowTitle("Wallpaper Engine · 视频库管理")

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
        self._archive_query_token: Optional[object] = None
        self._task_result_message: Optional[str] = None

        # 归档流水线状态：跨 UI 会话持久化到 output/ui_state.json
        self._arc_state: ArchivePipelineState = self._arc_load_state()
        # 指定文件夹归档支持多选，按顺序串行执行；中途 rc!=0 会中止队列。
        self._arc_folder_queue: List[Dict[str, Any]] = []

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().hide()
        central = QWidget()
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(190)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(20, 30, 20, 22)
        brand = QLabel("WALLPAPER\n视频库管理")
        brand.setObjectName("brand")
        side.addWidget(brand)
        side.addSpacing(28)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setSpacing(7)
        self.navigation.addItems(["视频查重", "批量取消订阅", "下架与归档"])
        self.navigation.setCurrentRow(0)
        self.navigation.currentRowChanged.connect(self.tabs.setCurrentIndex)
        side.addWidget(self.navigation, 1)
        output_button = QPushButton("打开输出目录")
        output_button.clicked.connect(self._open_output_dir)
        side.addWidget(output_button)
        side.addWidget(QLabel("Wallpaper Engine\nWorkshop / 本地项目"))
        shell.addWidget(sidebar)
        content = QVBoxLayout()
        content.setContentsMargins(24, 24, 24, 18)
        content.setSpacing(16)
        heading = QHBoxLayout()
        self.page_heading = QLabel("视频查重")
        self.page_heading.setObjectName("pageHeading")
        heading.addWidget(self.page_heading, 1)
        self.task_status = QLabel("就绪")
        self.task_status.setObjectName("taskStatus")
        heading.addWidget(self.task_status)
        content.addLayout(heading)
        self.task_progress = QProgressBar()
        self.task_progress.setMaximumHeight(3)
        self.task_progress.setTextVisible(False)
        self.task_progress.hide()
        content.addWidget(self.task_progress)
        content.addWidget(self.tabs, 1)
        shell.addLayout(content, 1)
        self.setCentralWidget(central)
        self.navigation.currentTextChanged.connect(self.page_heading.setText)
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_task_status)
        self._status_timer.start(300)

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

        for button in self.findChildren(QPushButton):
            if button.text().startswith(("运行", "检测", "下架物品归档", "指定文件夹归档", "取消订阅")):
                button.setProperty("taskAction", True)
            if button.text().startswith(("运行筛重", "检测下架物品")):
                button.setProperty("primary", True)

        # 默认加载根目录 config.toml
        self.dedup_config_path.setText(str((REPO_ROOT / "config.toml").resolve()))
        self._load_config_into_form(Path(self.dedup_config_path.text()))
        self._dedup_fields["we_install_dir"].textChanged.connect(self.arc_we_install_dir.setText)
        self.arc_we_install_dir.textChanged.connect(self._dedup_fields["we_install_dir"].setText)

    def _refresh_task_status(self) -> None:
        running = bool(self._archive_query_token is not None or (self.proc and self.proc.popen.poll() is None))
        self.task_progress.setVisible(running)
        if running:
            self.task_progress.setRange(0, 0)
            self.task_status.setText("任务运行中")
        elif self._task_result_message:
            self.task_status.setText(self._task_result_message)
        elif self.proc:
            code = self.proc.popen.returncode
            self.task_status.setText("任务完成" if code == 0 else f"任务已结束 · {code}")
        else:
            self.task_status.setText("就绪")
        for button in self.findChildren(QPushButton):
            if button.property("taskAction"):
                button.setEnabled(not running)

    def _invoke_ui_call(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:
            if hasattr(self, "log_archive"):
                self.log_archive.write(f"[UI] 主线程回调失败: {e}\n")

    def _call_on_ui_thread(self, fn: Callable[[], None]) -> None:
        self._ui_call.emit(fn)

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

    def _start_process(
        self,
        cmd: List[str],
        cwd: Path,
        sink: LogSink,
        on_finish: Optional[Callable[[Optional[int]], None]] = None,
        keep_log: bool = False,
        on_output: Optional[Callable[[str], None]] = None,
    ) -> bool:
        if self.proc and self.proc.popen.poll() is None:
            QMessageBox.warning(self, "正在运行", "已有任务在运行，请先停止。")
            return False

        # 批量串行场景（例如多选文件夹归档）需要 keep_log=True 保留前几次子进程的
        # 输出，否则每开一个子进程就清屏，用户看不到累计进度。
        if not keep_log:
            sink.clear()
        self._task_result_message = None
        display_cmd = list(cmd)
        if "--steam-api-key" in display_cmd:
            key_index = display_cmd.index("--steam-api-key") + 1
            if key_index < len(display_cmd):
                display_cmd[key_index] = "***"
        sink.write("[CMD] " + " ".join(display_cmd) + "\n\n")

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
        try:
            p = subprocess.Popen(
                _worker_command(cmd),
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=creationflags,
            )
        except OSError as exc:
            sink.write(f"[ERROR] 无法启动任务：{exc}\n")
            QMessageBox.critical(self, "启动失败", str(exc))
            return False

        def _reader() -> None:
            assert p.stdout is not None
            try:
                for raw in p.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    if on_output is not None:
                        on_output(line)
                    else:
                        sink.write(line)
            except Exception as e:
                sink.write(f"\n[UI] 读取输出失败：{e}\n")
            finally:
                # 子进程刚把 stdout 关掉时，OS 可能还没回收它，poll() 会返回 None。
                # 这里用 wait(timeout=...) 等一下，最多 5 秒，避免 rc=None 让 on_finish
                # 判定成"异常"从而漏掉清理（例如退订完成后不删 pending xlsx）。
                code: Optional[int]
                try:
                    code = p.wait(timeout=5)
                except Exception:
                    code = p.poll()
                sink.write(f"\n[EXIT] rc={code}\n")
                self._call_on_ui_thread(_allow_sleep)
                try:
                    p.stdout.close()
                except Exception:
                    pass
                if on_finish is not None and not handle.cancelled:
                    try:
                        on_finish(code)
                    except Exception as cb_e:
                        sink.write(f"[UI] on_finish 回调失败: {cb_e}\n")

        t = threading.Thread(target=_reader, daemon=True)
        handle = ProcHandle(popen=p, reader_thread=t)
        self.proc = handle
        _prevent_sleep()
        t.start()
        return True

    @staticmethod
    def _safe_unlink(path: Path) -> bool:
        try:
            if path.exists():
                path.unlink()
                return True
        except Exception:
            pass
        return False

    def _cleanup_unsub_xlsx(self, rc: Optional[int], xlsx_path: Path, sink: "LogSink") -> None:
        """退订子进程结束回调：rc==0 时删掉 pending xlsx，其它情况保留以便复盘 / 重试。"""
        if rc == 0:
            if self._safe_unlink(xlsx_path):
                sink.write(f"[INFO] 已清理退订清单文件: {xlsx_path.name}\n")
        else:
            sink.write(
                f"[INFO] 退订未正常完成 (rc={rc})，保留 {xlsx_path.name} 以便复查 / 重跑。\n"
            )

    def _remove_wids_from_config_sync(self, wids_file: Path, sink: "LogSink") -> None:
        """同步调 archiver --remove-workshop-wids-from-config，按显式 wid 列表
        把 config.json 里对应的 workshop 分类条目删掉。

        调用时机：bulk_unsub_controller 正常跑完（rc==0）后。按 wid 列表
        而不是按磁盘存在与否来删——Steam 客户端删本地 workshop 目录有
        延迟，磁盘探测可能漏。
        """
        we_dir = self._arc_we_install_dir_effective()
        if not we_dir:
            sink.write("[INFO] 跳过 config.json 分类条目清理（缺 we_install_dir）。\n")
            return
        if not wids_file.exists():
            sink.write(f"[INFO] 跳过 config.json 分类条目清理（wids 文件不存在: {wids_file.name}）。\n")
            return
        cfg_path = self._arc_get_config_path()
        cmd = [
            sys.executable,
            str((REPO_ROOT / "we_delisted_archiver.py").resolve()),
            "-c", str(cfg_path),
            "--remove-workshop-wids-from-config",
            "--wids-file", str(wids_file),
            "--we-install-dir", we_dir,
        ]
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _is_windows() else 0
        try:
            r = subprocess.run(
                _worker_command(cmd), cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, timeout=60, creationflags=creationflags,
            )
            out = r.stdout.decode("utf-8", errors="replace")
            if out.strip():
                sink.write(out if out.endswith("\n") else out + "\n")
        except Exception as e:
            sink.write(f"[WARN] 分类条目清理失败：{e}\n")

    def _after_unsub_cleanup(
        self,
        rc: Optional[int],
        xlsx_path: Path,
        wids_file: Optional[Path],
        sink: "LogSink",
    ) -> None:
        """bulk_unsub 子进程结束后的统一回调：先删 pending xlsx，rc==0 时再
        按本次退订的 wid 列表清理 config.json 里的分类条目。

        只有 rc==0 才跑清理——rc!=0 意味着退订被中断 / 报错，这种情况
        可能只有一部分 wid 真的在 Steam 端退订成功了，贸然清 config 会把
        那些"本次没成功退订、下次重跑仍要处理"的条目也干掉。
        """
        self._cleanup_unsub_xlsx(rc, xlsx_path, sink)
        if rc == 0 and wids_file is not None:
            self._remove_wids_from_config_sync(wids_file, sink)
            self._safe_unlink(wids_file)

    def _stop_running(self) -> None:
        self._archive_query_token = None
        self._arc_folder_queue = []
        self._task_result_message = "任务已停止"
        if not self.proc:
            return
        self.proc.cancelled = True
        p = self.proc.popen
        if p.poll() is None:
            self._kill_process_tree(p)
        self.proc = None
        _allow_sleep()

    # ------------------- helpers: form widgets -------------------
    @staticmethod
    def _make_label(text: str) -> QLabel:
        names = {
            "workshop_root": "创意工坊目录", "we_install_dir": "Wallpaper Engine 目录",
            "include_myprojects": "包含本地项目", "output_dir": "输出目录",
            "model_cache_dir": "模型缓存目录", "sample_frames": "视频采样帧数",
            "video_window_seconds": "视频采样时长（秒）", "audio_window_seconds": "音频采样时长（秒）",
            "seek_ratio": "采样起点比例", "phash_size": "感知哈希大小",
            "phash_distance_threshold": "视频相似度阈值", "phash_trimmed_mean_cap": "截尾均值上限",
            "phash_trim_ratio": "异常帧丢弃比例", "phash_bimodal_gap_cap": "双峰差上限",
            "color_hist_bins_h": "色相直方图精度", "color_hist_bins_s": "饱和度直方图精度",
            "color_distance_threshold": "颜色距离阈值", "audio_merge_override_color": "音频匹配时豁免颜色差异",
            "audio_merge_threshold": "音频匹配阈值", "semantic_feature_model": "语义模型",
            "semantic_feature_device": "推理设备", "semantic_sample_frames": "语义采样帧数",
            "max_workers_stage1": "时长扫描并发数", "max_workers_stage2": "特征提取并发数",
            "duration_rounding": "时长分桶精度", "require_both_signatures": "同时要求音视频匹配",
            "duration_cross_bucket_tolerance": "跨时长桶容差（秒）", "log_file": "日志文件", "progress": "显示进度",
        }
        key = text.split("（", 1)[0]
        display = names.get(key, key)
        if "semantic_feature_enabled" in text:
            display = "启用视觉语义识别"
        elif "semantic_patch_enabled" in text:
            display = "启用 Patch 空间判定"
        lbl = QLabel(display)
        lbl.setWordWrap(True)
        lbl.setToolTip(text)
        lbl.setMinimumWidth(170)
        lbl.setMaximumWidth(240)
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
        btn_save = QPushButton("保存")
        btn_save.setToolTip("保存到当前选中的 config 路径")
        btn_save.clicked.connect(self._save_config)
        top.addWidget(btn_save)
        btn_save_as = QPushButton("另存为…")
        btn_save_as.clicked.connect(self._save_config_as)
        top.addWidget(btn_save_as)
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

        form_layout = QVBoxLayout(form_host)
        form_layout.setContentsMargins(0, 0, 12, 0)
        form_layout.setSpacing(14)
        search = QLineEdit()
        search.setPlaceholderText("搜索参数或配置项…")
        search.setClearButtonEnabled(True)
        form_layout.addWidget(search)
        sections: List[QGroupBox] = []
        grid: QGridLayout

        def add_section(row: int, text: str) -> int:
            nonlocal grid
            box = QGroupBox(text)
            grid = QGridLayout(box)
            grid.setContentsMargins(16, 28, 16, 16)
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(10)
            grid.setColumnStretch(1, 1)
            sections.append(box)
            form_layout.addWidget(box)
            return 0

        def add_separator(row: int) -> int:
            titles = {1: "采样设置", 2: "视频与音频匹配", 4: "运行与日志"}
            return add_section(row, titles.get(len(sections), "运行选项"))

        def add_line(row: int, label: str, key: str) -> int:
            grid.addWidget(self._make_label(label), row, 0)
            w = QLineEdit()
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
            w = NoWheelComboBox()
            w.addItems(options)
            grid.addWidget(w, row, 1)
            self._dedup_fields[key] = w
            return row + 1

        def add_dir(row: int, label: str, key: str) -> int:
            grid.addWidget(self._make_label(label), row, 0)
            w = QLineEdit()
            grid.addWidget(w, row, 1)
            self._dedup_fields[key] = w
            btn = QPushButton("…")
            btn.setFixedWidth(36)
            btn.setProperty("compact", True)
            btn.clicked.connect(lambda _=False, ww=w: self._pick_dir_into(ww))
            grid.addWidget(btn, row, 2)
            return row + 1

        def add_file(row: int, label: str, key: str) -> int:
            grid.addWidget(self._make_label(label), row, 0)
            w = QLineEdit()
            grid.addWidget(w, row, 1)
            self._dedup_fields[key] = w
            btn = QPushButton("…")
            btn.setFixedWidth(36)
            btn.setProperty("compact", True)
            btn.clicked.connect(lambda _=False, ww=w: self._pick_file_into(ww))
            grid.addWidget(btn, row, 2)
            return row + 1

        r = 0
        r = add_section(r, "文件与目录")
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
        r = add_section(r, "视觉语义与空间判定")
        r = add_bool(r, '语义特征 semantic_feature_enabled（DINOv2 / DINOv3，需要 torch）', "semantic_feature_enabled")
        r = add_choice(r, 'semantic_feature_model（DINOv2 / DINOv3）', "semantic_feature_model", ["dinov2_s", "dinov2_b", "dinov2_l", "dinov3_s", "dinov3_b", "dinov3_l"])
        r = add_choice(r, 'semantic_feature_device（自动 / CUDA / CPU）', "semantic_feature_device", ["auto", "cuda", "cpu"])
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

        r = add_line(r, "log_file（空=仅控制台）", "log_file")
        r = add_bool(r, "progress（默认进度条）", "progress")

        # 命令行开关（不写入 toml）
        r = add_section(r, "命令行开关（不写入 toml）")
        self.dedup_verbose = QCheckBox("--verbose（DEBUG 日志）")
        self.dedup_trace = QCheckBox("--trace（打印外部命令）")
        self.dedup_no_progress = QCheckBox("--no-progress（关闭 tqdm）")
        grid.addWidget(self.dedup_verbose, r, 0, 1, 3); r += 1
        grid.addWidget(self.dedup_trace, r, 0, 1, 3); r += 1
        grid.addWidget(self.dedup_no_progress, r, 0, 1, 3); r += 1

        # 表单底部拉伸
        form_layout.addStretch(1)

        def filter_parameters(query: str) -> None:
            query = query.strip().casefold()
            for box in sections:
                section_grid = box.layout()
                any_visible = False
                for row in range(section_grid.rowCount()):
                    item = section_grid.itemAtPosition(row, 0)
                    if item is None or item.widget() is None:
                        continue
                    label = item.widget()
                    visible = not query or query in (box.title() + label.toolTip() + label.text()).casefold()
                    any_visible |= visible
                    for column in range(section_grid.columnCount()):
                        cell = section_grid.itemAtPosition(row, column)
                        if cell is not None and cell.widget() is not None:
                            cell.widget().setVisible(visible)
                box.setVisible(any_visible)

        search.textChanged.connect(filter_parameters)

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
        btn_run.setProperty("primary", True)
        btn_run.setProperty("taskAction", True)
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
        splitter.setSizes([620, 500])

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

    def _save_config(self) -> None:
        p = Path(self.dedup_config_path.text().strip() or str(REPO_ROOT / "config.toml"))
        self._write_config_to(p)

    def _save_config_as(self) -> None:
        current = self.dedup_config_path.text().strip() or str(REPO_ROOT / "config.toml")
        p, _ = QFileDialog.getSaveFileName(
            self, "另存为 config.toml", current, "TOML (*.toml);;All (*.*)"
        )
        if not p:
            return
        self._write_config_to(Path(p))
        self.dedup_config_path.setText(p)

    def _write_config_to(self, p: Path) -> None:
        """合并：
           1) 当前磁盘上的文件作为基底（保留 force_merge_pairs / force_split_pairs 等 UI 没管的键）
           2) 表单字段覆盖
           3) 下架归档 tab 的两个字段覆盖（WE 安装目录与 Steam API Key）
        """
        base: Dict[str, Any] = {}
        try:
            if p.exists():
                base = load_toml(p) or {}
        except Exception as exc:
            QMessageBox.critical(self, "读取配置失败", str(exc))
            return

        form = self._collect_dedup_config_dict()
        merged: Dict[str, Any] = dict(base)
        merged.update(form)

        # 下架归档 tab 里输入的值优先于表单里 we_install_dir（若 arc 留空则沿用 form）
        if hasattr(self, "arc_we_install_dir"):
            arc_v = self.arc_we_install_dir.text().strip()
            if arc_v:
                merged["we_install_dir"] = arc_v
        if hasattr(self, "arc_steam_api_key"):
            merged["steam_api_key"] = self.arc_steam_api_key.text()

        try:
            _write_toml_atomic(p, merged)
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))
            return
        QMessageBox.information(self, "已保存", f"已保存：{p}")

    def _load_config_into_form(self, p: Path) -> None:
        try:
            from we_duplicate_finder_readonly import Config
            data = load_toml(p)
            defaults = asdict(Config(workshop_root=""))
            defaults.update(load_toml(REPO_ROOT / "config.example.toml"))
        except Exception as exc:
            QMessageBox.critical(self, "读取配置失败", str(exc))
            return
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

        # 下架归档 tab 共用：WE 安装目录 + API Key 都从同一个 config 读
        if hasattr(self, "arc_we_install_dir"):
            self.arc_we_install_dir.setText(str(merged.get("we_install_dir", "")))
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
        try:
            cfg = load_toml(Path(self.dedup_config_path.text()))
        except Exception as exc:
            QMessageBox.critical(self, "读取配置失败", str(exc))
            return
        cfg.update(self._collect_dedup_config_dict())
        if not cfg.get("workshop_root"):
            QMessageBox.critical(self, "参数缺失", "请填写 workshop_root（创意工坊目录）。")
            return

        tmp_dir = Path(tempfile.gettempdir()) / "we_dedup_ui"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="config_ui_", suffix=".toml", dir=tmp_dir)
        tmp_cfg = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(dump_simple_toml(cfg))

        cmd = [sys.executable, str((REPO_ROOT / "we_duplicate_finder_readonly.py").resolve()), "-c", str(tmp_cfg)]
        if self.dedup_verbose.isChecked():
            cmd.append("--verbose")
        if self.dedup_trace.isChecked():
            cmd.append("--trace")
        if self.dedup_no_progress.isChecked():
            cmd.append("--no-progress")
        self._start_process(cmd, cwd=REPO_ROOT, sink=self.log_dedup,
                            on_finish=lambda rc: tmp_cfg.unlink(missing_ok=True))

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
        g.addWidget(self.unsub_xlsx, 0, 1, 1, 3)
        b1 = QPushButton("选择…"); b1.clicked.connect(self._pick_xlsx); g.addWidget(b1, 0, 4)
        b2 = QPushButton("选最新"); b2.clicked.connect(self._pick_latest_xlsx); g.addWidget(b2, 0, 5)
        b3 = QPushButton("打开所在目录"); b3.clicked.connect(self._open_xlsx_dir); g.addWidget(b3, 0, 6)

        self.unsub_single_page = QCheckBox("单页面模式（推荐）")
        self.unsub_single_page.setChecked(True)
        self.unsub_single_page.toggled.connect(self._refresh_unsub_controls)
        g.addWidget(self.unsub_single_page, 1, 0)

        g.addWidget(QLabel("浏览器标签数："), 1, 1, Qt.AlignmentFlag.AlignRight)
        self.unsub_batch_size = NoWheelSpinBox()
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
        g.addWidget(self.unsub_single_page_url, 2, 1, 1, 6)

        note = QLabel("提示：筛重表每行第一列为保留项；其余列 Steam 链接由油猴退订，myprojects 的 file:/// 会先本地删夹。需已登录 Steam。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        g.addWidget(note, 3, 0, 1, 7)

        btns = QHBoxLayout()
        brun = QPushButton("运行 / 继续退订"); brun.clicked.connect(self._run_unsub); btns.addWidget(brun)
        brun.setToolTip("每项自动尝试最多 3 次。再次运行同一清单时，会跳过已确认成功的项目。")
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
        cmd += self._unsub_progress_args()
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

    def _unsub_progress_args(self) -> List[str]:
        directory = Path(str(self._field_value("output_dir")).strip() or "output")
        if not directory.is_absolute():
            directory = REPO_ROOT / directory
        return ["--state-db", str(directory / "unsub_progress.sqlite3")]

    # ------------------- archive tab: pipeline state + visuals -------------------
    def _arc_state_path(self) -> Path:
        """ui_state.json 固定放在仓库的 output/ 下——和 delisted_items.json 等同目录，
        不跟随 dedup tab 里可改的 output_dir（那个是扫描结果输出路径）。
        """
        return (REPO_ROOT / "output" / "ui_state.json").resolve()

    def _arc_load_state(self) -> "ArchivePipelineState":
        p = self._arc_state_path()
        if not p.exists():
            return ArchivePipelineState()
        try:
            with p.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            return ArchivePipelineState.from_dict(raw or {})
        except Exception:
            # 文件坏了就回到初始——不弹窗，UI 起不来就别拦用户。
            return ArchivePipelineState()

    def _arc_save_state(self) -> None:
        p = self._arc_state_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._arc_state.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(p))
        except Exception as e:
            if hasattr(self, "log_archive"):
                self.log_archive.write(f"[WARN] 保存 UI 状态失败：{e}\n")

    def _arc_apply_dim(self, btn: QPushButton, dim: bool) -> None:
        """视觉 dim：用 QGraphicsOpacityEffect，比 stylesheet 兼容性好；按钮仍可点。"""
        eff = btn.graphicsEffect()
        if dim:
            if not isinstance(eff, QGraphicsOpacityEffect):
                eff = QGraphicsOpacityEffect(btn)
                btn.setGraphicsEffect(eff)
            eff.setOpacity(0.45)
        else:
            if isinstance(eff, QGraphicsOpacityEffect):
                eff.setOpacity(1.0)

    def _arc_refresh_buttons(self) -> None:
        """根据 self._arc_state 刷新两条 pipeline 按钮的亮/dim 态。"""
        state = self._arc_state
        pairs = [
            ("detect", getattr(self, "btn_arc_detect", None)),
            ("archive", getattr(self, "btn_arc_archive", None)),
            ("unsub", getattr(self, "btn_arc_unsub_delisted", None)),
        ]
        for stage, btn in pairs:
            if btn is not None:
                self._arc_apply_dim(btn, stage != state.delisted_stage)

        pairs2 = [
            ("archive", getattr(self, "btn_arc_archive_folder", None)),
            ("unsub", getattr(self, "btn_arc_unsub_folder", None)),
        ]
        for stage, btn in pairs2:
            if btn is not None:
                self._arc_apply_dim(btn, stage != state.folder_stage)

    def _arc_advance(self, pipeline: str, completed_stage: str) -> None:
        """当前 stage 完成后推进到下一个；若完成的不是当前 stage（= 用户强制跑了
        dim 按钮），不改 stage。"""
        state = self._arc_state
        current = getattr(state, f"{pipeline}_stage", None)
        if current != completed_stage:
            return
        if pipeline == "delisted":
            nxt = _DELISTED_NEXT.get(completed_stage)
        elif pipeline == "folder":
            nxt = _FOLDER_NEXT.get(completed_stage)
        else:
            nxt = None
        if nxt is None:
            return
        setattr(state, f"{pipeline}_stage", nxt)
        self._arc_save_state()
        self._arc_refresh_buttons()

    def _arc_guarded_click(
        self,
        pipeline: str,
        stage: str,
        action_label: str,
        handler: Callable[[bool], None],
    ) -> None:
        """按钮点击入口：active 态直接跑；dim 态弹"已整理过一次，是否继续"确认。

        handler(advance_on_success) —— advance_on_success=True 表示这次点击来自
        active 态，跑完需要推进 stage；False 表示 dim 态强制重跑，不动 stage。
        """
        current = getattr(self._arc_state, f"{pipeline}_stage", None)
        is_active = (current == stage)
        if not is_active:
            ret = QMessageBox.question(
                self, "确认再执行一次",
                f"已整理过一次「{action_label}」，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        handler(is_active)

    @staticmethod
    def _arc_compose_on_finish(
        *fns: Optional[Callable[[Optional[int]], None]],
    ) -> Callable[[Optional[int]], None]:
        """把多个 on_finish 串起来；单个失败不影响后续。None 直接跳过。"""
        real = [f for f in fns if f is not None]

        def _combined(rc: Optional[int]) -> None:
            for f in real:
                try:
                    f(rc)
                except Exception:
                    pass

        return _combined

    def _arc_folder_label(self, f: Dict[str, Any]) -> str:
        """把 folders 条目格式化成下拉/列表里的展示文本（与旧逻辑一致）。"""
        ws_alive = f.get("workshop_alive_count")
        ws_total = f.get("workshop_count", 0)
        if ws_alive is not None and ws_alive != ws_total:
            ws_part = f"workshop={ws_alive}/{ws_total}"
        else:
            ws_part = f"workshop={ws_total}"
        return (
            f"[{f.get('index', '?')}] {f.get('title', '?')}  "
            f"({ws_part}, 已归档={f.get('myprojects_count', 0)})"
        )

    def _arc_pick_folders_multi(
        self,
        folders: List[Dict[str, Any]],
        prompt: str,
        tip_text: str = "按住 Ctrl / Shift 可多选；任务将按勾选顺序依次串行执行。",
    ) -> List[Dict[str, Any]]:
        """多选版文件夹选择对话框：返回用户勾选的 folders 子集（按原顺序）。"""
        if not folders:
            QMessageBox.information(self, "无文件夹", "config.json 里没有任何文件夹。")
            return []
        dlg = QDialog(self)
        dlg.setWindowTitle("选择 WE 文件夹（支持多选）")
        dlg.resize(620, 520)

        v = QVBoxLayout(dlg)
        lbl = QLabel(prompt)
        lbl.setWordWrap(True)
        v.addWidget(lbl)

        listw = QListWidget()
        listw.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for idx, f in enumerate(folders):
            it = QListWidgetItem(self._arc_folder_label(f))
            it.setData(Qt.ItemDataRole.UserRole, idx)
            listw.addItem(it)
        v.addWidget(listw, 1)

        tip = QLabel(tip_text)
        tip.setStyleSheet("color:#666;")
        v.addWidget(tip)

        row = QHBoxLayout()
        b_all = QPushButton("全选")
        b_all.clicked.connect(listw.selectAll)
        row.addWidget(b_all)
        b_none = QPushButton("全不选")
        b_none.clicked.connect(listw.clearSelection)
        row.addWidget(b_none)
        row.addStretch(1)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        row.addWidget(bb)
        v.addLayout(row)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return []
        chosen_rows = sorted({listw.row(it) for it in listw.selectedItems()})
        return [folders[r] for r in chosen_rows]

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
        # 失焦或回车时原子写入 config.toml（只覆盖 we_install_dir 一项，其余键不动）
        self.arc_we_install_dir.editingFinished.connect(self._arc_autosave)
        g.addWidget(self.arc_we_install_dir, 0, 1, 1, 2)
        bd = QPushButton("选择…")
        bd.clicked.connect(lambda: (self._pick_dir_into(self.arc_we_install_dir), self._arc_autosave()))
        g.addWidget(bd, 0, 3)

        g.addWidget(QLabel("Steam API Key（推荐）："), 1, 0)
        self.arc_steam_api_key = QLineEdit()
        self.arc_steam_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.arc_steam_api_key.editingFinished.connect(self._arc_autosave)
        g.addWidget(self.arc_steam_api_key, 1, 1, 1, 2)
        bk = QPushButton("申请")
        bk.clicked.connect(lambda: __import__("webbrowser").open("https://steamcommunity.com/dev/apikey"))
        g.addWidget(bk, 1, 3)

        note = QLabel("提示：有 API Key 时使用 IPublishedFileService（检测更准确）；无 Key 回退到旧 API。取消订阅会打开 steamcommunity.com/my/…（已登录即当前账号）。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        g.addWidget(note, 2, 0, 1, 4)

        # ----- Row 1：下架系列 —— 检测 → 归档 → 退订（一条链） -----
        row1 = QHBoxLayout()
        self.btn_arc_detect = QPushButton("检测下架物品")
        self.btn_arc_detect.clicked.connect(
            lambda: self._arc_guarded_click(
                "delisted", "detect", "检测下架物品",
                lambda active: self._run_arc_detect(active),
            )
        )
        row1.addWidget(self.btn_arc_detect)

        self.btn_arc_archive = QPushButton("下架物品归档到本地")
        self.btn_arc_archive.clicked.connect(
            lambda: self._arc_guarded_click(
                "delisted", "archive", "下架物品归档到本地",
                lambda active: self._run_arc_archive(active),
            )
        )
        row1.addWidget(self.btn_arc_archive)

        self.btn_arc_unsub_delisted = QPushButton("取消订阅已下架")
        self.btn_arc_unsub_delisted.clicked.connect(
            lambda: self._arc_guarded_click(
                "delisted", "unsub", "取消订阅已下架",
                lambda active: self._run_arc_unsub(active),
            )
        )
        row1.addWidget(self.btn_arc_unsub_delisted)

        row1.addStretch(1)
        btn_stop = QPushButton("停止")
        btn_stop.clicked.connect(self._stop_running)
        row1.addWidget(btn_stop)
        wrap1 = QWidget(); wrap1.setLayout(row1)
        g.addWidget(wrap1, 3, 0, 1, 4)

        # ----- Row 2：手动归档系列 —— 指定文件夹归档（多选） → 对应退订 -----
        row2 = QHBoxLayout()
        self.btn_arc_archive_folder = QPushButton("指定文件夹归档到本地")
        self.btn_arc_archive_folder.clicked.connect(
            lambda: self._arc_guarded_click(
                "folder", "archive", "指定文件夹归档到本地",
                lambda active: self._run_arc_archive_folder(active),
            )
        )
        row2.addWidget(self.btn_arc_archive_folder)

        self.btn_arc_unsub_folder = QPushButton("取消订阅手动归档")
        self.btn_arc_unsub_folder.clicked.connect(
            lambda: self._arc_guarded_click(
                "folder", "unsub", "取消订阅手动归档",
                lambda active: self._run_arc_unsub_folder(active),
            )
        )
        row2.addWidget(self.btn_arc_unsub_folder)

        row2.addStretch(1)
        wrap2 = QWidget(); wrap2.setLayout(row2)
        g.addWidget(wrap2, 4, 0, 1, 4)

        outer.addWidget(box)

        outer.addWidget(QLabel("日志输出"))
        log_widget = QPlainTextEdit()
        outer.addWidget(log_widget, 1)
        self.log_archive = LogSink(log_widget)

        # 日志沉降准备好后，立刻刷一次按钮状态（dim/亮）
        self._arc_refresh_buttons()

    # ------------------- archive tab: actions -------------------
    def _arc_get_config_path(self) -> Path:
        return Path(self.dedup_config_path.text().strip() or str(REPO_ROOT / "config.toml"))

    def _arc_autosave(self) -> None:
        """只把下架归档 tab 的两个字段写回 config.toml，其他键原样保留。

        触发时机：两个 QLineEdit 的 editingFinished（失焦或回车），以及"选择…"
        按钮选完目录后。失败静默（不弹窗打断用户），仅在日志面板里提示一行。
        """
        if not hasattr(self, "arc_we_install_dir") or not hasattr(self, "arc_steam_api_key"):
            return
        # 读两个值（we_install_dir 允许写空，相当于让归档流程回退到 form 里的 we_install_dir）
        new_we = self.arc_we_install_dir.text().strip()
        new_key = self.arc_steam_api_key.text()

        p = self._arc_get_config_path()
        try:
            base: Dict[str, Any] = load_toml(p) if p.exists() else {}
        except Exception as e:
            if hasattr(self, "log_archive"):
                self.log_archive.write(f"[WARN] 自动保存失败（读取旧配置）：{e}\n")
            return

        # 只有值真正变了才写盘，避免每次 tab 切换 / focus 抖动都重写
        changed = False
        if str(base.get("we_install_dir", "")) != new_we:
            base["we_install_dir"] = new_we
            changed = True
        if str(base.get("steam_api_key", "")) != new_key:
            base["steam_api_key"] = new_key
            changed = True
        if not changed:
            return

        try:
            _write_toml_atomic(p, base)
        except Exception as e:
            if hasattr(self, "log_archive"):
                self.log_archive.write(f"[WARN] 自动保存失败（写入 {p}）：{e}\n")
            return
        if hasattr(self, "log_archive"):
            self.log_archive.write(f"[INFO] 已自动保存到 {p}\n")

    def _arc_workshop_root(self) -> str:
        return str(self._field_value("workshop_root")).strip()

    def _arc_we_install_dir_effective(self) -> str:
        """优先取归档 tab 自己的输入，未填则回退到筛重 tab 的 we_install_dir。"""
        v = self.arc_we_install_dir.text().strip()
        if v:
            return v
        return str(self._field_value("we_install_dir")).strip()

    def _run_arc_detect(self, advance: bool = True) -> None:
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
        on_fin = None
        if advance:
            on_fin = lambda rc: (
                rc == 0 and self._call_on_ui_thread(lambda: self._arc_advance("delisted", "detect"))
            )
        self._start_process(cmd, cwd=REPO_ROOT, sink=self.log_archive, on_finish=on_fin)

    def _run_arc_archive(self, advance: bool = True) -> None:
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
            "--copy-backend", "auto",
        ]
        on_fin = None
        if advance:
            on_fin = lambda rc: (
                rc == 0 and self._call_on_ui_thread(lambda: self._arc_advance("delisted", "archive"))
            )
        self._start_process(cmd, cwd=REPO_ROOT, sink=self.log_archive, on_finish=on_fin)

    def _run_arc_unsub(self, advance: bool = True) -> None:
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

        xlsx_path = out_dir / "delisted_unsub_pending.xlsx"
        # 每次点按钮都重新生成——xlsx 只反映"当下这一次"的状态，避免历史文件混用。
        self._safe_unlink(xlsx_path)
        wb = Workbook()
        ws = wb.active
        ws.title = "delisted"
        ws.append(["url"])
        wids_for_cleanup: List[str] = []
        for item in items:
            wid = str(item.get("id", "")).strip()
            if wid:
                wids_for_cleanup.append(wid)
            ws.append([f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}"])
        xlsx_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(str(xlsx_path))

        wids_file = out_dir / "delisted_unsub_wids.json"
        self._safe_unlink(wids_file)
        try:
            wids_file.write_text(
                json.dumps({"archived_wids": wids_for_cleanup}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            self.log_archive.write(f"[WARN] 写入 wids 清理清单失败，跳过 config.json 清理：{e}\n")
            wids_file = None  # type: ignore[assignment]

        cmd = [
            sys.executable,
            str((REPO_ROOT / "output" / "bulk_unsub_controller.py").resolve()),
            "--xlsx", str(xlsx_path),
            "--batch-size", "1",
            "--single-page",
            "--single-page-url", STEAM_MY_SUBS_UNSUB2,
        ]
        cmd += self._unsub_progress_args()

        self.log_archive.write(f"[INFO] 已生成 {len(items)} 条取消订阅链接: {xlsx_path}\n")

        on_cleanup = lambda rc: self._after_unsub_cleanup(
            rc, xlsx_path, wids_file, self.log_archive,
        )
        on_advance = None
        if advance:
            on_advance = lambda rc: (
                rc == 0 and self._call_on_ui_thread(lambda: self._arc_advance("delisted", "unsub"))
            )
        self._start_process(
            cmd,
            cwd=REPO_ROOT / "output",
            sink=self.log_archive,
            on_finish=self._arc_compose_on_finish(on_cleanup, on_advance),
        )

    # ---- 按指定 WE 文件夹归档 / 退订 ----
    def _query_archive_json(
        self,
        cmd: List[str],
        description: str,
        on_result: Callable[[Dict[str, Any]], None],
    ) -> None:
        if self._archive_query_token is not None or (self.proc and self.proc.popen.poll() is None):
            QMessageBox.warning(self, "正在运行", "已有任务在运行，请先停止。")
            return
        token = object()
        self._archive_query_token = token
        payload: Optional[Dict[str, Any]] = None
        self.log_archive.write(f"[INFO] {description}，正在校验本地归档，可点击停止。\n")

        def read_line(line: str) -> None:
            nonlocal payload
            try:
                data = json.loads(line)
            except ValueError:
                self.log_archive.write(line)
                return
            if isinstance(data, dict):
                payload = data

        def finished(rc: Optional[int]) -> None:
            def apply_result() -> None:
                if self._archive_query_token is not token:
                    return
                self._archive_query_token = None
                if rc != 0 or payload is None or payload.get("error"):
                    self._task_result_message = "归档查询失败"
                    message = str(payload.get("error")) if payload and payload.get("error") else "请查看日志中的错误详情。"
                    QMessageBox.critical(self, "查询失败", f"{description}失败。\n{message}")
                    return
                on_result(payload)
            self._call_on_ui_thread(apply_result)

        started = self._start_process(cmd, cwd=REPO_ROOT, sink=self.log_archive,
                                      on_finish=finished, keep_log=True, on_output=read_line)
        if started is False:
            self._archive_query_token = None

    def _arc_fetch_folders(
        self,
        we_dir: str,
        cfg_path: Path,
        workshop_root: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """同步调用 archiver --list-folders，解析 stdout JSON。失败返回 None。

        给 workshop_root 时，返回的每个 folder 会多一个 `workshop_alive_count`
        字段——即 config.json 里记录的 workshop wid 中、磁盘目录仍在的那部分。
        """
        cmd = [
            sys.executable,
            str((REPO_ROOT / "we_delisted_archiver.py").resolve()),
            "-c", str(cfg_path),
            "--list-folders",
            "--we-install-dir", we_dir,
        ]
        if workshop_root:
            cmd += ["--workshop-root", workshop_root]
        try:
            env = {
                **os.environ,
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            }
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _is_windows() else 0
            r = subprocess.run(
                _worker_command(cmd), cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, timeout=15, creationflags=creationflags,
            )
            out = r.stdout.decode("utf-8")
            data = json.loads(out)
        except subprocess.TimeoutExpired:
            QMessageBox.critical(self, "超时", "读取 WE 文件夹列表超时。")
            return None
        except Exception as e:
            QMessageBox.critical(self, "读取失败", f"无法解析 WE 文件夹列表：{e}")
            return None
        if data.get("error"):
            QMessageBox.critical(self, "出错", f"WE 文件夹列表错误：{data.get('error')}")
            return None
        return data.get("folders") or []

    def _arc_pick_folder(self, folders: List[Dict[str, Any]], prompt: str) -> Optional[Dict[str, Any]]:
        if not folders:
            QMessageBox.information(self, "无文件夹", "config.json 里没有任何文件夹。")
            return None
        labels: List[str] = []
        for f in folders:
            # 给了 workshop_root 时，`workshop_alive_count` = 磁盘上仍订阅的；
            # workshop_count 保留给 config.json 里记录到的总数。
            ws_alive = f.get("workshop_alive_count")
            ws_total = f["workshop_count"]
            if ws_alive is not None and ws_alive != ws_total:
                ws_part = f"workshop={ws_alive}/{ws_total}"
            else:
                ws_part = f"workshop={ws_total}"
            labels.append(
                f"[{f['index']}] {f['title']}  ({ws_part}, 已归档={f['myprojects_count']})"
            )
        choice, ok = QInputDialog.getItem(self, "选择 WE 文件夹", prompt, labels, 0, False)
        if not ok or not choice:
            return None
        idx = labels.index(choice)
        return folders[idx]

    def _run_arc_archive_folder(self, advance: bool = True) -> None:
        """把选中的一个或多个 WE 文件夹里的 workshop 项批量归档到 myprojects。

        支持多选：依次串行跑，避免多个子进程并发改 config.json。队列全部跑完
        （且至少有一个 rc==0）时才推进 folder pipeline stage。
        """
        we_dir = self._arc_we_install_dir_effective()
        if not we_dir:
            QMessageBox.critical(self, "参数缺失", "请填写 WE 安装目录。")
            return
        wr = self._arc_workshop_root()
        if not wr:
            QMessageBox.critical(self, "参数缺失", "请在「筛重」标签页填写 workshop_root。")
            return
        cfg_path = self._arc_get_config_path()

        folders = self._arc_fetch_folders(we_dir, cfg_path, workshop_root=wr)
        if folders is None:
            return
        picked_list = self._arc_pick_folders_multi(
            folders,
            "勾选要归档的文件夹（可多选；会按顺序串行把 workshop 项归档到 myprojects）：",
        )
        if not picked_list:
            return

        # 只保留"磁盘上仍有 workshop 项"的，纯幽灵文件夹跳过并提示。
        runnable: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        for p in picked_list:
            alive = p.get("workshop_alive_count", p.get("workshop_count", 0))
            if alive <= 0:
                skipped.append(p)
            else:
                runnable.append(p)
        if skipped:
            self.log_archive.write(
                f"[INFO] 跳过 {len(skipped)} 个文件夹（没有仍在盘上的 workshop 项）：" +
                ", ".join(p.get("title", "?") for p in skipped) + "\n"
            )
        if not runnable:
            QMessageBox.information(
                self, "无需归档",
                "所选文件夹里都没有仍在盘上的 workshop 项。",
            )
            return

        if self._arc_folder_queue:
            QMessageBox.warning(self, "队列非空", "上一次归档队列尚未处理完，请等它跑完或先点停止。")
            return
        self._arc_folder_queue = list(runnable)
        total = len(self._arc_folder_queue)
        # 先手动清一次日志，后续子进程复用同一个日志面板（不再清）
        self.log_archive.clear()
        self.log_archive.write(f"[INFO] 开始批量归档 {total} 个文件夹（串行）\n")
        self._arc_run_next_archive_folder(advance, total_planned=total, total_ok=0)

    def _arc_run_next_archive_folder(
        self,
        advance: bool,
        total_planned: int,
        total_ok: int,
    ) -> None:
        """串行跑 `self._arc_folder_queue` 里下一个文件夹；全部处理完才推进 stage。"""
        if not self._arc_folder_queue:
            self.log_archive.write(
                f"[INFO] 批量归档结束：成功 {total_ok}/{total_planned} 个文件夹\n"
            )
            self._task_result_message = "归档完成" if total_ok == total_planned else f"部分归档失败 · {total_planned - total_ok}/{total_planned}"
            if advance and total_ok == total_planned:
                self._arc_advance("folder", "archive")
            return

        we_dir = self._arc_we_install_dir_effective()
        wr = self._arc_workshop_root()
        cfg_path = self._arc_get_config_path()
        if not we_dir or not wr:
            # 理论上进不到这里（前置校验已挡），出了也安全退队列
            self._arc_folder_queue.clear()
            return

        picked = self._arc_folder_queue.pop(0)
        archive_queue = self._arc_folder_queue
        cmd = [
            sys.executable,
            str((REPO_ROOT / "we_delisted_archiver.py").resolve()),
            "-c", str(cfg_path),
            "--archive-folder-index", str(picked["index"]),
            "--workshop-root", wr,
            "--we-install-dir", we_dir,
            "--copy-backend", "auto",
        ]
        remaining = len(self._arc_folder_queue)
        self.log_archive.write(
            f"[INFO] 归档文件夹 '{picked['title']}' → myprojects（workshop={picked.get('workshop_count', 0)}, "
            f"已归档跳过={picked.get('myprojects_count', 0)}，剩 {remaining} 个待处理）\n"
        )

        def _after(rc: Optional[int]) -> None:
            new_ok = total_ok + (1 if rc == 0 else 0)
            # 子进程回调在 reader 线程，切回主线程再起下一个（start_process 要动 UI）
            self._call_on_ui_thread(
                lambda: self._arc_run_next_archive_folder(advance, total_planned, new_ok)
                if self._arc_folder_queue is archive_queue else None
            )

        self._start_process(
            cmd, cwd=REPO_ROOT, sink=self.log_archive, on_finish=_after, keep_log=True,
        )

    def _run_arc_unsub_folder(self, advance: bool = True) -> None:
        """取消订阅手动归档：可多选 WE 文件夹，合并已归档 wid 后一次退订。"""
        we_dir = self._arc_we_install_dir_effective()
        if not we_dir:
            QMessageBox.critical(self, "参数缺失", "请填写 WE 安装目录。")
            return
        wr = self._arc_workshop_root()
        if not wr:
            QMessageBox.critical(self, "参数缺失", "请在「筛重」标签页填写 workshop_root。")
            return
        cfg_path = self._arc_get_config_path()

        folders = self._arc_fetch_folders(we_dir, cfg_path, workshop_root=wr)
        if folders is None:
            return
        picked_list = self._arc_pick_folders_multi(
            folders,
            "勾选要取消订阅手动归档项的文件夹（可多选；会合并已归档 wid 后一次执行）：",
            tip_text="按住 Ctrl / Shift 可多选；会合并去重后生成一次退订任务。",
        )
        if not picked_list:
            return

        all_wids: List[str] = []
        all_orphan_wids: List[str] = []
        empty_titles: List[str] = []

        def query_next(position: int) -> None:
            if position == len(picked_list):
                self._arc_prepare_unsub_folder(picked_list, all_wids, all_orphan_wids, empty_titles, advance)
                return
            picked = picked_list[position]
            cmd_query = [
                sys.executable,
                str((REPO_ROOT / "we_delisted_archiver.py").resolve()),
                "-c", str(cfg_path),
                "--list-archived-in-folder-index", str(picked["index"]),
                "--we-install-dir", we_dir,
                "--workshop-root", wr,
            ]

            def queried(data: Dict[str, Any]) -> None:
                wids = [str(x).strip() for x in (data.get("archived_wids") or []) if str(x).strip()]
                orphan_wids = [str(x).strip() for x in (data.get("orphan_wids") or []) if str(x).strip()]
                if not wids and not orphan_wids:
                    empty_titles.append(str(picked.get("title", "?")))
                all_wids.extend(wids)
                all_orphan_wids.extend(orphan_wids)
                incomplete = len(data.get("incomplete_wids") or [])
                self.log_archive.write(
                    f"[INFO] 文件夹 '{picked['title']}': 可退订 {len(wids)}，残留清理 {len(orphan_wids)}，"
                    f"归档不完整 {incomplete}（保留订阅）\n"
                )
                query_next(position + 1)

            self._query_archive_json(cmd_query, f"读取文件夹 '{picked['title']}' 的已归档项目", queried)

        query_next(0)

    def _arc_prepare_unsub_folder(
        self,
        picked_list: List[Dict[str, Any]],
        all_wids: List[str],
        all_orphan_wids: List[str],
        empty_titles: List[str],
        advance: bool,
    ) -> None:
        wids = list(dict.fromkeys(all_wids))
        orphan_wids = list(dict.fromkeys(all_orphan_wids))

        # 情况 A：既没有还在订阅的归档项，也没有历史遗留幽灵条目——真的没事可做。
        if not wids and not orphan_wids:
            QMessageBox.information(
                self, "无可退订",
                f"所选 {len(picked_list)} 个文件夹里没有需要退订或清理的条目。\n"
                f"（如果还没归档，请先跑「指定文件夹归档到本地」。）",
            )
            return

        out_dir = Path(str(self._field_value("output_dir")).strip() or "output")
        if not out_dir.is_absolute():
            out_dir = (REPO_ROOT / out_dir).resolve()

        skipped_note = ""
        if empty_titles:
            skipped_note = f"\n其中 {len(empty_titles)} 个文件夹没有可处理条目，已跳过。"

        # 情况 B：已经没有活着的订阅要退了，但有老版本残留的幽灵条目——直接清理。
        if not wids and orphan_wids:
            ret = QMessageBox.question(
                self, "清理残留条目",
                f"所选 {len(picked_list)} 个文件夹里没有仍在订阅的已归档项，"
                f"但检测到 {len(orphan_wids)} 条 workshop 已被退订、config.json 里仍挂着的残留条目。"
                f"{skipped_note}\n\n"
                f"直接从 config.json 清掉这些残留吗？（不会弹浏览器，也不会再动 Steam。）\n"
                f"注意：请确保 Wallpaper Engine 已关闭，否则 WE 退出时会覆盖本次改动。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
            wids_file = out_dir / "archived_orphan_wids.json"
            self._safe_unlink(wids_file)
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                wids_file.write_text(
                    json.dumps({"archived_wids": orphan_wids}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as e:
                QMessageBox.critical(self, "写入失败", f"无法写入 wids 清理清单：{e}")
                return
            self._remove_wids_from_config_sync(wids_file, self.log_archive)
            self._safe_unlink(wids_file)
            if advance:
                self._arc_advance("folder", "unsub")
            return

        # 情况 C：有活着的订阅要退，可能顺带还有幽灵——两者一起清理。
        confirm_lines = [
            f"将对所选 {len(picked_list)} 个文件夹中 {len(wids)} 个已归档 wid 发起取消订阅。",
            "（只退订已经归档到 myprojects 的那些，未归档的保持订阅。）",
        ]
        if orphan_wids:
            confirm_lines.append(
                f"另检测到 {len(orphan_wids)} 条以前已退订、config.json 里仍挂着的"
                f"残留条目，将在退订成功后一起清理。"
            )
        if skipped_note:
            confirm_lines.append(skipped_note.strip())
        confirm_lines.append("\n确认继续？")
        ret = QMessageBox.question(
            self, "确认取消订阅",
            "\n".join(confirm_lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        try:
            from openpyxl import Workbook
        except ImportError:
            QMessageBox.critical(self, "缺少依赖", "需要 openpyxl: pip install openpyxl")
            return

        xlsx_path = out_dir / "archived_unsub_pending.xlsx"
        # 每次点按钮都重新生成——xlsx 只反映"当下这一次"从 config.json 查到的状态。
        self._safe_unlink(xlsx_path)
        wb = Workbook()
        ws = wb.active
        ws.title = "archived_unsub"
        ws.append(["url"])
        for wid in wids:
            ws.append([f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}"])
        xlsx_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(str(xlsx_path))

        # wids 清理清单 = 本次刚退订的 wids + 历史残留的 orphan_wids，一锅端。
        wids_file: Optional[Path] = out_dir / "archived_unsub_wids.json"
        self._safe_unlink(wids_file)
        try:
            merged_wids = list(dict.fromkeys([*wids, *orphan_wids]))
            wids_file.write_text(
                json.dumps({"archived_wids": merged_wids}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            self.log_archive.write(f"[WARN] 写入 wids 清理清单失败，跳过 config.json 清理：{e}\n")
            wids_file = None

        cmd = [
            sys.executable,
            str((REPO_ROOT / "output" / "bulk_unsub_controller.py").resolve()),
            "--xlsx", str(xlsx_path),
            "--batch-size", "1",
            "--single-page",
            "--single-page-url", STEAM_MY_SUBS_UNSUB2,
        ]
        cmd += self._unsub_progress_args()
        self.log_archive.write(
            f"[INFO] 已生成 {len(wids)} 条手动归档退订链接（来自 {len(picked_list)} 个文件夹）: {xlsx_path}\n"
        )

        on_cleanup = lambda rc: self._after_unsub_cleanup(
            rc, xlsx_path, wids_file, self.log_archive,
        )
        on_advance = None
        if advance:
            on_advance = lambda rc: (
                rc == 0 and self._call_on_ui_thread(lambda: self._arc_advance("folder", "unsub"))
            )
        self._start_process(
            cmd,
            cwd=REPO_ROOT / "output",
            sink=self.log_archive,
            on_finish=self._arc_compose_on_finish(on_cleanup, on_advance),
        )


# ------------------- entry -------------------

def _apply_windows11_style(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#f3f5f8"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#202b3b"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#202b3b"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#202b3b"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#167d68"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
    app.setStyleSheet("""
        QWidget { color: #263447; font-size: 13px; }
        QMainWindow, QTabWidget::pane { background: #f3f5f8; border: none; }
        QFrame#sidebar { background: #152536; border: none; }
        QFrame#sidebar QLabel { color: #a6b7c9; line-height: 1.5; }
        QLabel#brand { color: #f4f8fc; font-size: 20px; font-weight: 700; }
        QLabel#pageHeading { font-size: 25px; font-weight: 700; color: #172c3c; }
        QLabel#taskStatus { background: #e0eee8; color: #17634f; padding: 7px 14px; border-radius: 14px; }
        QListWidget#navigation { background: transparent; border: none; outline: none; color: #a6b7c9; }
        QListWidget#navigation::item { padding: 13px 10px; border-radius: 8px; }
        QListWidget#navigation::item:selected { color: #ffffff; background: #276d65; }
        QListWidget#navigation::item:hover:!selected { color: #ffffff; background: #20384b; }
        QFrame#sidebar QPushButton { color: #d4e3ed; background: #20384b; border: 1px solid #355066; }
        QGroupBox { background: #ffffff; border: 1px solid #dde4eb; border-radius: 10px;
                    margin-top: 15px; padding-top: 14px; font-weight: 600; }
        QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left;
                         left: 14px; padding: 0 5px; color: #1e594f; }
        QLineEdit, QComboBox, QSpinBox { background: #ffffff; border: 1px solid #d4dde6;
                                      border-radius: 6px; padding: 7px 9px; min-height: 18px;
                                      selection-background-color: #cbe9e1; selection-color: #153d33; }
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #218b75; }
        QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: #8f9ca9; background: #edf0f3; }
        QComboBox::drop-down { border: none; width: 23px; }
        QComboBox QAbstractItemView { background: #ffffff; selection-background-color: #dfefe9; color: #263447; }
        QPushButton { background: #ffffff; border: 1px solid #d1dce5; border-radius: 6px;
                      padding: 8px 13px; min-height: 18px; font-weight: 500; }
        QPushButton:hover { background: #edf5f2; border-color: #7eaa9d; }
        QPushButton:pressed { background: #d9eae3; }
        QPushButton[compact="true"] { padding: 4px; }
        QPushButton[primary="true"] { background: #167d68; color: #ffffff; border-color: #167d68; font-weight: 600; }
        QPushButton[primary="true"]:hover { background: #116a58; }
        QPushButton:disabled { background: #e9edf1; color: #9aa7b4; border-color: #e0e5eb; }
        QPlainTextEdit { background: #182736; color: #cfdfeb; border: 1px solid #263d50;
                         border-radius: 9px; padding: 12px; selection-background-color: #375769; }
        QScrollArea { background: transparent; border: none; }
        QScrollArea > QWidget > QWidget { background: #f3f5f8; }
        QScrollBar:vertical { background: transparent; width: 9px; margin: 0; }
        QScrollBar::handle:vertical { background: #bdcbd4; min-height: 32px; border-radius: 4px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        QSplitter::handle { background: #e1e7ee; width: 2px; margin: 7px 0; }
        QCheckBox { spacing: 8px; padding: 3px 0; }
        QCheckBox::indicator { width: 17px; height: 17px; }
        QProgressBar { border: none; background: #e0e7ee; }
        QProgressBar::chunk { background: #218b75; }
        QListWidget { background: #ffffff; border: 1px solid #d4dde6; border-radius: 6px; padding: 5px; }
        QToolTip { background: #ffffff; color: #263447; border: 1px solid #bdcbd4; padding: 8px; }
    """)


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
