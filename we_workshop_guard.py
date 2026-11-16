"""Keep Wallpaper Engine's licensed Steam session active without opening its browser.

Run with ``start``, ``status``, ``pause``, ``resume`` or ``stop``. Pausing releases the Steam session,
so Steam may resume its normal background Workshop scheduling.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib
import uuid
from ctypes import wintypes


APP_ID = 431960
ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


class CallbackMessage(ctypes.Structure):
    _fields_ = [('user', ctypes.c_int32), ('callback', ctypes.c_int32),
                ('data', ctypes.c_void_p), ('size', ctypes.c_int32)]


class DownloadResult(ctypes.Structure):
    _fields_ = [('app_id', ctypes.c_uint32), ('item_id', ctypes.c_uint64),
                ('result', ctypes.c_int32)]


@contextmanager
def session_lock(directory: Path, filename: str = 'workshop_guard.lock'):
    import msvcrt

    directory.mkdir(parents=True, exist_ok=True)
    with (directory / filename).open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def read_status(directory: Path) -> dict:
    try:
        return json.loads((directory / 'workshop_guard.json').read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def steam_directory() -> Path:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
        return Path(winreg.QueryValueEx(key, 'SteamPath')[0])


def we_ui_running(install: Path) -> bool:
    """Include browser/editor cleanup processes so Steam retains ownership during handoff."""
    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
            ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
            ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
            ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
            ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    expected = os.path.normcase(os.path.realpath(install / 'bin' / 'wallpaperui.exe'))
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.szExeFile.lower() == 'wallpaperui.exe':
                process = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if process:
                    try:
                        buffer = ctypes.create_unicode_buffer(32768)
                        length = wintypes.DWORD(len(buffer))
                        if kernel.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(length)):
                            if os.path.normcase(os.path.realpath(buffer.value)) == expected:
                                return True
                    finally:
                        kernel.CloseHandle(process)
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        return False
    finally:
        kernel.CloseHandle(snapshot)


class SteamSession:
    def __init__(self, install: Path):
        os.environ['SteamAppId'] = str(APP_ID)
        os.environ['SteamGameId'] = str(APP_ID)
        self.api = ctypes.CDLL(str((install / 'bin' / 'steam_api64.dll').resolve()))
        self.initialized = False
        self.bind('SteamAPI_InitFlat', [ctypes.c_void_p], ctypes.c_int)
        self.bind('SteamAPI_Shutdown', [], None)
        self.bind('SteamAPI_IsSteamRunning', [], ctypes.c_bool)
        self.bind('SteamAPI_GetHSteamPipe', [], ctypes.c_int32)
        self.bind('SteamAPI_ManualDispatch_Init', [], None)
        self.bind('SteamAPI_ManualDispatch_RunFrame', [ctypes.c_int32], None)
        self.bind('SteamAPI_ManualDispatch_GetNextCallback', [ctypes.c_int32, ctypes.POINTER(CallbackMessage)], ctypes.c_bool)
        self.bind('SteamAPI_ManualDispatch_FreeLastCallback', [ctypes.c_int32], None)
        for name in ['SteamAPI_SteamApps_v008', 'SteamAPI_SteamUser_v023', 'SteamAPI_SteamUtils_v010']:
            self.bind(name, [], ctypes.c_void_p)
        self.bind('SteamAPI_ISteamApps_BIsSubscribedApp', [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_bool)
        self.bind('SteamAPI_ISteamUser_BLoggedOn', [ctypes.c_void_p], ctypes.c_bool)
        self.bind('SteamAPI_ISteamUtils_GetAppID', [ctypes.c_void_p], ctypes.c_uint32)
        self.bind('SteamAPI_SteamUGC_v020', [], ctypes.c_void_p)
        self.bind('SteamAPI_ISteamUGC_GetNumSubscribedItems', [ctypes.c_void_p, ctypes.c_bool], ctypes.c_uint32)
        self.bind('SteamAPI_ISteamUGC_GetSubscribedItems', [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64), ctypes.c_uint32, ctypes.c_bool], ctypes.c_uint32)
        self.bind('SteamAPI_ISteamUGC_GetItemState', [ctypes.c_void_p, ctypes.c_uint64], ctypes.c_uint32)
        self.bind('SteamAPI_ISteamUGC_DownloadItem', [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_bool], ctypes.c_bool)
        self.bind('SteamAPI_ISteamUGC_SuspendDownloads', [ctypes.c_void_p, ctypes.c_bool], None)
        self.bind('SteamAPI_ISteamUGC_GetItemDownloadInfo', [ctypes.c_void_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64)], ctypes.c_bool)

    def bind(self, name, arguments, result):
        function = getattr(self.api, name)
        function.argtypes = arguments
        function.restype = result

    def connect(self):
        error = ctypes.create_string_buffer(1024)
        result = self.api.SteamAPI_InitFlat(error)
        if result:
            return error.value.decode('utf-8', errors='replace')
        self.initialized = True
        self.user = self.api.SteamAPI_SteamUser_v023()
        apps = self.api.SteamAPI_SteamApps_v008()
        utils = self.api.SteamAPI_SteamUtils_v010()
        if not self.user or not apps or not utils:
            raise RuntimeError('Steam did not provide the required interfaces.')
        if self.api.SteamAPI_ISteamUtils_GetAppID(utils) != APP_ID:
            raise RuntimeError('Steam returned a different application ID.')
        if not self.api.SteamAPI_ISteamApps_BIsSubscribedApp(apps, APP_ID):
            raise RuntimeError('This Steam account must own Wallpaper Engine.')
        self.ugc = self.api.SteamAPI_SteamUGC_v020()
        if not self.ugc:
            raise RuntimeError('Steam did not provide its Workshop interface.')
        self.api.SteamAPI_ManualDispatch_Init()
        self.pipe = self.api.SteamAPI_GetHSteamPipe()
        return None

    def callbacks(self):
        results = []
        self.api.SteamAPI_ManualDispatch_RunFrame(self.pipe)
        message = CallbackMessage()
        while self.api.SteamAPI_ManualDispatch_GetNextCallback(self.pipe, ctypes.byref(message)):
            try:
                if message.callback == 3406 and message.size >= ctypes.sizeof(DownloadResult):
                    result = DownloadResult.from_buffer_copy(ctypes.string_at(message.data, ctypes.sizeof(DownloadResult)))
                    if result.app_id == APP_ID:
                        results.append((result.item_id, result.result))
            finally:
                self.api.SteamAPI_ManualDispatch_FreeLastCallback(self.pipe)
        return results

    def subscribed_items(self):
        count = self.api.SteamAPI_ISteamUGC_GetNumSubscribedItems(self.ugc, False)
        items = (ctypes.c_uint64 * count)()
        actual = self.api.SteamAPI_ISteamUGC_GetSubscribedItems(self.ugc, items, count, False)
        return list(items[:actual])

    def item_state(self, item):
        return self.api.SteamAPI_ISteamUGC_GetItemState(self.ugc, item)

    def progress(self, item):
        downloaded, total = ctypes.c_uint64(), ctypes.c_uint64()
        self.api.SteamAPI_ISteamUGC_GetItemDownloadInfo(self.ugc, item, ctypes.byref(downloaded), ctypes.byref(total))
        return downloaded.value, total.value

    def close(self):
        if self.initialized:
            self.api.SteamAPI_Shutdown()
            self.initialized = False


class DownloadQueue:
    def __init__(self, session):
        self.session = session
        self.active = None
        self.items = []
        self.refresh_at = 0
        self.retry_at = {}
        self.completed = 0
        self.last_result = None
        self.next_request = 0
        self.session.api.SteamAPI_ISteamUGC_SuspendDownloads(self.session.ugc, False)

    def tick(self, results):
        current = time.monotonic()
        for item, result in results:
            if item == self.active:
                self.last_result = {'item_id': str(item), 'result': result}
                if result == 1:
                    self.completed += 1
                    self.retry_at.pop(item, None)
                else:
                    self.retry_at[item] = current + 300
                self.active = None
                self.refresh_at = 0
                self.next_request = current if result == 1 else current + 1
        if self.active is not None:
            return
        if current < self.next_request or not self.session.api.SteamAPI_ISteamUser_BLoggedOn(self.session.user):
            return
        if current >= self.refresh_at:
            self.items = self.session.subscribed_items()
            self.refresh_at = current + 30
        for item in self.items:
            state = self.session.item_state(item)
            if not state & 1 or state & 2 or self.retry_at.get(item, 0) > current:
                continue
            if state & 8 or not state & 4:
                if self.session.api.SteamAPI_ISteamUGC_DownloadItem(self.session.ugc, item, True):
                    self.active = item
                else:
                    self.retry_at[item] = current + 300
                    self.last_result = {'item_id': str(item), 'result': 'request_rejected'}
                self.next_request = current + 1
                break
        else:
            # Keep an idle session lightweight while waiting for new subscriptions or retries.
            self.next_request = current + 5

    def status(self):
        downloaded, total = self.session.progress(self.active) if self.active else (0, 0)
        return dict(active_item=str(self.active) if self.active else None,
                    downloaded_bytes=downloaded, total_bytes=total,
                    completed=self.completed,
                    deferred=sum(until > time.monotonic() for until in self.retry_at.values()),
                    last_result=self.last_result)


def write_status(args, state, **fields):
    data = dict(state=state, pid=os.getpid(), instance=args.instance, protocol=3,
                app_id=APP_ID, time=datetime.now().isoformat(timespec='seconds'), **fields)
    temporary = args.state_dir / f'workshop_guard.{os.getpid()}.tmp'
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(5):
        try:
            os.replace(temporary, args.state_dir / 'workshop_guard.json')
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02)


def run_session(args):
    """Own the Steam API in an expendable process so pause releases Steam's running state."""
    directory = args.state_dir
    previous = read_status(directory)
    session = None
    queue = None
    final_state = 'releasing'
    message = ''

    def write(state, **fields):
        write_status(args, state, downloads=queue.status() if queue else previous.get('downloads'),
                     retry_at=queue.retry_at if queue else previous.get('retry_at', {}), **fields)

    try:
        session = SteamSession(args.install_dir)
        deadline = time.monotonic() + 300
        launched_steam = False
        next_status = 0
        while not any((directory / marker).exists() for marker in (
                'workshop_guard.stop', 'workshop_guard.pause', 'workshop_guard_session.stop')):
            current = time.monotonic()
            if not session.initialized:
                error = session.connect()
                if error is not None:
                    write('waiting_for_steam', message=error, steam_session=False)
                    if not launched_steam and not session.api.SteamAPI_IsSteamRunning():
                        steam = steam_directory()
                        subprocess.Popen([str(steam / 'steam.exe'), '-silent'], cwd=steam,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
                        launched_steam = True
                    if current >= deadline:
                        raise RuntimeError('Timed out waiting for Steam. Sign in to Steam, then start again.')
                    time.sleep(1)
                    continue
                if args.sequential:
                    queue = DownloadQueue(session)
                    queue.completed = (previous.get('downloads') or {}).get('completed', 0)
                    queue.last_result = (previous.get('downloads') or {}).get('last_result')
                    queue.retry_at = {int(item): until for item, until in previous.get('retry_at', {}).items()}
            if not session.api.SteamAPI_IsSteamRunning():
                final_state = 'steam_closed'
                break
            results = session.callbacks()
            if queue:
                queue.tick(results)
            if time.monotonic() >= next_status:
                write('active', logged_on=session.api.SteamAPI_ISteamUser_BLoggedOn(session.user),
                      steam_session=True, we_ui_active=False,
                      mode='sequential' if queue else 'session_only')
                next_status = time.monotonic() + 1
            time.sleep(0.1)
    except Exception as error:
        final_state = 'error'
        message = str(error)
        raise
    finally:
        if queue:
            queue.active = None
        if session:
            session.close()
        write(final_state, message=message, steam_session=False)


def run(args):
    """Keep controls alive without loading Steam into the supervising process."""
    directory = args.state_dir
    stop = directory / 'workshop_guard.stop'
    paused = directory / 'workshop_guard.pause'
    session_stop = directory / 'workshop_guard_session.stop'
    with session_lock(directory) as acquired:
        if not acquired:
            return
        child = None
        final_state = 'stopped'
        message = ''
        write_status(args, 'starting', steam_session=False,
                     mode='sequential' if args.sequential else 'session_only')
        next_ui_check = 0
        next_status = 0
        ui_active = False
        release_at = None
        try:
            while not stop.exists():
                current = time.monotonic()
                if current >= next_ui_check:
                    ui_active = we_ui_running(args.install_dir)
                    next_ui_check = current + 1
                state = 'paused' if paused.exists() else 'yielding_to_we' if ui_active else 'active'
                if child and child.poll() is not None:
                    data = read_status(directory)
                    exit_code = child.returncode
                    child = None
                    release_at = None
                    next_status = 0
                    if data.get('state') in ('error', 'steam_closed'):
                        final_state = data['state']
                        message = data.get('message', '')
                        break
                    if exit_code:
                        raise RuntimeError(f'Steam worker exited with code {exit_code}. See workshop_guard.log.')
                if child:
                    if state != 'active' and release_at is None:
                        session_stop.touch()
                        release_at = current
                    if release_at is not None and current - release_at >= 10:
                        child.terminate()
                        child.wait(timeout=5)
                        child = None
                        release_at = None
                        next_status = 0
                elif state == 'active':
                    session_stop.unlink(missing_ok=True)
                    child = subprocess.Popen(
                        background_command(args.install_dir, directory, args.instance, args.sequential, action='session'),
                        cwd=ROOT, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                elif current >= next_status:
                    data = read_status(directory)
                    write_status(args, state, steam_session=False, we_ui_active=ui_active,
                                 mode='sequential' if args.sequential else 'session_only',
                                 downloads=data.get('downloads'), retry_at=data.get('retry_at', {}))
                    next_status = current + 1
                time.sleep(0.1)
        except Exception as error:
            final_state = 'error'
            message = str(error)
            raise
        finally:
            if child and child.poll() is None:
                session_stop.touch()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    child.wait(timeout=5)
            data = read_status(directory)
            write_status(args, final_state, message=message, steam_session=False,
                         downloads=data.get('downloads'), retry_at=data.get('retry_at', {}))


def background_command(install: Path, state_dir: Path, instance: str, sequential: bool, action: str = 'run') -> list[str]:
    if getattr(sys, 'frozen', False):
        executable = Path(sys.executable)
        if executable.stem.lower() == 'weworkshopguard':
            executable = executable.with_name('WEWorker.exe')
        command = [str(executable), '--worker', 'we_workshop_guard.py']
    else:
        command = [sys.executable, '-X', 'utf8', str(Path(__file__).resolve())]
    command.extend([action, '--install-dir', str(install), '--state-dir', str(state_dir), '--instance', instance])
    if sequential:
        command.append('--sequential')
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'status', 'pause', 'resume', 'stop', 'run', 'session'])
    parser.add_argument('--install-dir', type=Path)
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'output')
    parser.add_argument('--instance', default='')
    parser.add_argument('--sequential', action='store_true', help='Request one subscribed Workshop item at a time.')
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('This utility requires Windows and the Steam client.')
    args.state_dir = args.state_dir.resolve()
    if args.action in ['start', 'run', 'session']:
        if args.install_dir is None:
            config = tomllib.loads((ROOT / 'config.toml').read_text(encoding='utf-8-sig'))
            args.install_dir = Path(config['we_install_dir'])
        args.install_dir = args.install_dir.resolve()
        if not (args.install_dir / 'bin' / 'steam_api64.dll').is_file():
            parser.error('Wallpaper Engine steam_api64.dll was not found.')
    if args.action == 'run':
        run(args)
        return
    if args.action == 'session':
        run_session(args)
        return
    with session_lock(args.state_dir) as acquired:
        if args.action == 'status' or not acquired:
            data = read_status(args.state_dir)
            data['process_running'] = not acquired
            print(json.dumps(data, ensure_ascii=False, indent=2))
            if args.action == 'stop' and not acquired:
                (args.state_dir / 'workshop_guard.stop').touch()
                print('Stop requested. Steam may resume background Workshop scheduling.')
            elif args.action == 'pause' and not acquired:
                (args.state_dir / 'workshop_guard.pause').touch()
                print('Pause requested. The Steam session will be released.')
            elif args.action in ['start', 'resume'] and not acquired:
                (args.state_dir / 'workshop_guard.pause').unlink(missing_ok=True)
                print('Resume requested.')
            return
        if args.action in ['pause', 'resume']:
            print('The Workshop session guard is stopped. Use start to launch it.')
            return
        if args.action == 'stop':
            print('The Workshop session guard is already stopped.')
            return
        (args.state_dir / 'workshop_guard.stop').unlink(missing_ok=True)
        (args.state_dir / 'workshop_guard.pause').unlink(missing_ok=True)
        args.instance = uuid.uuid4().hex
    with (args.state_dir / 'workshop_guard.log').open('a', encoding='utf-8') as log:
        command = background_command(args.install_dir, args.state_dir, args.instance, args.sequential)
        child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(50):
        data = read_status(args.state_dir)
        if data.get('instance') == args.instance:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            if data.get('state') == 'error':
                raise SystemExit(1)
            return
        if child.poll() is not None:
            raise SystemExit('Guard startup failed. See output/workshop_guard.log.')
        time.sleep(0.1)
    print('Guard is starting. Use the status command to check its Steam session.')


if __name__ == '__main__':
    main()
