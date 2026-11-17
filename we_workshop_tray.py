"""Tray controls for the background Workshop downloader."""
from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import traceback

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QMenu,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QStyle, QSystemTrayIcon, QTabWidget,
    QVBoxLayout, QWidget,
)

from we_workshop_guard import ROOT, background_command, read_status, session_lock


STATE_LABELS = {
    'starting': '正在启动', 'waiting_for_steam': '等待 Steam 登录',
    'active': '后台逐项下载中', 'paused': '已暂停，已释放 Steam 占用',
    'yielding_to_we': 'WE 主界面运行中，已自动让出',
    'stopped': '已停止', 'steam_closed': 'Steam 已退出', 'error': '运行出错',
    'releasing': '正在释放 Steam 占用',
}


def format_bytes(value):
    value = float(value or 0)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if value < 1024 or unit == 'TB':
            return f'{value:.1f} {unit}'
        value /= 1024


def control_command(action, install, directory):
    return background_command(install or ROOT, directory, '', action == 'start', action=action)


class StatusWindow(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.setWindowTitle('WE 后台下载')
        self.setMinimumWidth(440)
        self.setWindowIcon(controller.icon)
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        self.status_tab = QWidget()
        self.tabs.addTab(self.status_tab, '运行状态')
        layout = QVBoxLayout(self.status_tab)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)
        self.state = QLabel('正在读取状态')
        font = self.state.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self.state.setFont(font)
        self.state.setWordWrap(True)
        layout.addWidget(self.state)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        self.item = QLabel('—')
        self.item.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.bytes = QLabel('—')
        self.completed = QLabel('0')
        self.deferred = QLabel('0')
        form.addRow('当前项目', self.item)
        form.addRow('下载进度', self.bytes)
        form.addRow('本次完成', self.completed)
        form.addRow('等待重试', self.deferred)
        self.result = QLabel('—')
        self.result.setWordWrap(True)
        form.addRow('最近结果', self.result)
        layout.addLayout(form)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        self.resume = QPushButton('启动 / 恢复')
        self.resume.clicked.connect(controller.start)
        self.pause = QPushButton('暂停')
        self.pause.clicked.connect(controller.pause)
        close = QPushButton('收起到托盘')
        close.clicked.connect(self.hide)
        buttons.addWidget(self.resume)
        buttons.addWidget(self.pause)
        buttons.addStretch()
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.log_tab = QWidget()
        log_layout = QVBoxLayout(self.log_tab)
        log_layout.addWidget(QLabel('显示最近 128 KB 的日志，每秒自动刷新。'))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumSize(520, 280)
        self.log.document().setMaximumBlockCount(2000)
        log_layout.addWidget(self.log)
        self.tabs.addTab(self.log_tab, '运行日志')
        self.log_signature = None
        self.tabs.currentChanged.connect(controller.refresh_log)

    def refresh_log(self, path):
        try:
            info = path.stat()
            signature = (info.st_mtime_ns, info.st_size)
            if signature == self.log_signature:
                return
            with path.open('rb') as source:
                offset = max(0, info.st_size - 128 * 1024)
                source.seek(offset)
                content = source.read(128 * 1024)
            if offset and b'\n' in content:
                content = content.partition(b'\n')[2]
            text = content.decode('utf-8', errors='replace') or '暂无运行日志。'
            self.log_signature = signature
        except FileNotFoundError:
            text = '暂无运行日志。'
            self.log_signature = None
        except OSError as error:
            text = f'读取日志失败\n{error}'
            self.log_signature = None
        scroll = self.log.verticalScrollBar()
        position = scroll.value()
        follow = position >= scroll.maximum()
        if self.log.toPlainText() != text:
            self.log.setPlainText(text)
            scroll.setValue(scroll.maximum() if follow else position)

    def closeEvent(self, event):
        self.hide()
        event.ignore()


class GuardTray:
    def __init__(self, app, directory, install=None):
        self.app = app
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.install = install or self.load_install()
        self.command = None
        self.command_output = None
        self.command_error = ''
        self.last_status = {}
        self.status_pending = False
        self.restart_after_stop = False
        self.exit_requested = False
        self.shutdown_sent = False
        self.icon = app.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown)
        self.tray = QSystemTrayIcon(self.icon)
        self.window = StatusWindow(self)
        menu = QMenu()
        menu.addAction('查看运行状态', self.queue_status)
        self.start_action = menu.addAction('启动 / 恢复', self.start)
        self.pause_action = menu.addAction('暂停（释放 Steam 占用）', self.pause)
        menu.addSeparator()
        menu.addAction('设置 WE 安装目录…', self.choose_install)
        menu.addAction('查看运行日志', self.queue_log)
        menu.addSeparator()
        menu.addAction('退出', self.exit)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.activated)
        self.tray.show()
        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.log_timer = QTimer()
        self.log_timer.timeout.connect(self.refresh_log)
        self.log_timer.start(1000)
        self.app.aboutToQuit.connect(self.request_stop)
        self.refresh()

    def load_install(self):
        settings = self.directory / 'workshop_guard_settings.json'
        try:
            if settings.exists():
                return Path(json.loads(settings.read_text(encoding='utf-8'))['install_dir'])
            config = tomllib.loads((ROOT / 'config.toml').read_text(encoding='utf-8-sig'))
            value = config.get('we_install_dir')
            return Path(value) if value else None
        except (OSError, ValueError, KeyError):
            return None

    def backend_status(self):
        with session_lock(self.directory) as acquired:
            data = read_status(self.directory, fallback=self.last_status)
            self.status_pending = data is self.last_status
            self.last_status = data
            return data, not acquired

    def launch(self, action):
        if self.command and self.command.poll() is None:
            return
        self.command_error = ''
        self.command_output = tempfile.TemporaryFile()
        try:
            self.command = subprocess.Popen(
                control_command(action, self.install, self.directory), cwd=ROOT,
                stdin=subprocess.DEVNULL, stdout=self.command_output, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except OSError as error:
            self.command_output.close()
            self.command_output = None
            self.command_error = str(error)
            self.restart_after_stop = False
            self.show_status()

    def start(self):
        if not self.install or not (self.install / 'bin' / 'steam_api64.dll').is_file():
            if not self.choose_install():
                return
        data, running = self.backend_status()
        if running and (data.get('protocol', 0) < 3 or data.get('mode') == 'session_only'):
            self.restart_after_stop = True
            self.launch('stop')
        else:
            self.launch('resume' if running else 'start')

    def pause(self):
        self.launch('pause')

    def request_stop(self):
        (self.directory / 'workshop_guard.stop').touch()

    def exit(self):
        self.exit_requested = True
        self.restart_after_stop = False
        self.shutdown_sent = False
        self.refresh()

    def show_window(self):
        self.window.showNormal()
        if sys.platform == 'win32':
            # Restore the native window even when the launcher requested a hidden startup.
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            handle = ctypes.c_void_p(int(self.window.winId()))
            user32.ShowWindow(handle, 9)
            user32.SetForegroundWindow(handle)
        self.window.raise_()
        self.window.activateWindow()

    def show_status(self):
        self.window.tabs.setCurrentWidget(self.window.status_tab)
        self.show_window()
        self.refresh()

    def queue_status(self):
        QTimer.singleShot(0, self.show_status)

    def queue_log(self):
        QTimer.singleShot(0, self.open_log)

    def activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.queue_status()

    def choose_install(self):
        selected = QFileDialog.getExistingDirectory(self.window, '选择 Wallpaper Engine 安装目录', str(self.install or ROOT))
        if not selected:
            return False
        install = Path(selected)
        if not (install / 'bin' / 'steam_api64.dll').is_file():
            QMessageBox.warning(self.window, '目录不正确', '请选择包含 bin/steam_api64.dll 的 Wallpaper Engine 安装目录。')
            return False
        self.install = install
        settings = self.directory / 'workshop_guard_settings.json'
        temporary = settings.with_suffix('.tmp')
        temporary.write_text(json.dumps({'install_dir': str(install)}, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(settings)
        return True

    def open_log(self):
        self.window.tabs.setCurrentWidget(self.window.log_tab)
        self.show_window()
        self.refresh_log()

    def refresh_log(self):
        if self.window.isVisible() and self.window.tabs.currentWidget() is self.window.log_tab:
            self.window.refresh_log(self.directory / 'workshop_guard.log')

    def refresh(self):
        show_request = self.directory / 'workshop_guard.show_status'
        if show_request.exists():
            show_request.unlink(missing_ok=True)
            self.queue_status()
        if self.command and self.command.poll() is not None:
            self.command_output.seek(0)
            output = self.command_output.read().decode('utf-8', errors='replace').strip()
            self.command_output.close()
            self.command_output = None
            if self.command.returncode:
                self.command_error = output
                self.restart_after_stop = False
                self.tray.showMessage('WE 后台下载', self.command_error or '操作失败，请查看日志。', QSystemTrayIcon.MessageIcon.Warning)
                self.exit_requested = False
            self.command = None
        data, running = self.backend_status()
        busy = self.command is not None
        if self.exit_requested and not busy:
            if not running:
                self.tray.hide()
                self.app.quit()
                return
            if not self.shutdown_sent:
                self.request_stop()
                self.shutdown_sent = True
        if self.restart_after_stop and not running and not busy and not self.exit_requested:
            self.restart_after_stop = False
            self.launch('start')
            busy = True
        state = data.get('state', 'stopped')
        if not running and state not in ('error', 'steam_closed'):
            state = 'stopped'
        label = STATE_LABELS.get(state, '等待状态更新')
        self.window.state.setText('正在退出…' if self.exit_requested else label)
        detail = self.command_error or data.get('message', '')
        if self.status_pending and running:
            detail = '正在重试读取状态，暂时显示上次结果。'
        if not detail:
            if state == 'paused':
                detail = '后台请求已暂停，可以从 Steam 启动 WE。点击恢复后继续自动接管。'
            elif state == 'yielding_to_we':
                detail = '下载由 WE 主界面处理。主界面关闭后，后台工具会自动接管。'
            else:
                detail = '自动模式会在 WE 主界面打开时让出下载调度。'
        self.window.detail.setText(detail)
        downloads = data.get('downloads') or {}
        self.window.item.setText(downloads.get('active_item') or '—')
        downloaded, total = downloads.get('downloaded_bytes', 0), downloads.get('total_bytes', 0)
        self.window.bytes.setText(f'{format_bytes(downloaded)} / {format_bytes(total)}' if total else '等待下载信息')
        self.window.completed.setText(str(downloads.get('completed', 0)))
        self.window.deferred.setText(f"{downloads.get('deferred', 0)} 项（失败后等待 5 分钟）")
        result = downloads.get('last_result') or {}
        code = result.get('result')
        descriptions = {1: '下载成功', 3: 'Steam 连接失败（No Connection）', 'request_rejected': 'Steam 未接受下载请求'}
        description = descriptions.get(code, f'Steam 错误码 {code}')
        self.window.result.setText(f"{result.get('item_id', '—')} · {description}" if result else '—')
        value = min(1000, int(downloaded / total * 1000)) if total else 0
        self.window.progress.setValue(value)
        self.window.progress.setFormat(f'{value / 10:.1f}%' if total else '—')
        self.start_action.setEnabled(not busy and not self.exit_requested)
        self.pause_action.setEnabled(running and state != 'paused' and not busy and not self.exit_requested)
        self.window.resume.setEnabled(self.start_action.isEnabled())
        self.window.pause.setEnabled(self.pause_action.isEnabled())
        self.tray.setToolTip(f'WE 后台下载\n{label}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-dir', type=Path)
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'output')
    parser.add_argument('--no-start', action='store_true')
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, 'WE 后台下载', '当前系统未提供通知区域，无法启动托盘工具。')
        return
    with session_lock(args.state_dir, 'workshop_guard_tray.lock') as acquired:
        if not acquired:
            (args.state_dir / 'workshop_guard.show_status').touch()
            return
        def log_exception(kind, error, trace):
            with (args.state_dir / 'workshop_guard.log').open('a', encoding='utf-8') as log:
                traceback.print_exception(kind, error, trace, file=log)

        previous_hook = sys.excepthook
        sys.excepthook = log_exception
        try:
            controller = GuardTray(app, args.state_dir, args.install_dir)
            if not args.no_start:
                QTimer.singleShot(0, controller.start)
            app.exec()
        finally:
            sys.excepthook = previous_hook


if __name__ == '__main__':
    main()
