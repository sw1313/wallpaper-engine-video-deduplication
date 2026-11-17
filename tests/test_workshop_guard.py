from types import SimpleNamespace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from we_workshop_guard import DownloadQueue, background_command, read_status, run, run_session, write_status


class StatusFileTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'Windows file sharing behavior')
    def test_native_exclusive_lock_is_tolerated_until_access_returns(self):
        import _winapi

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            args = SimpleNamespace(state_dir=directory, instance='test')
            write_status(args, 'active', downloads={'completed': 59})
            fallback = read_status(directory)
            handle = _winapi.CreateFile(str(directory / 'workshop_guard.json'), 0x80000000, 0, 0, 3, 0, 0)
            try:
                self.assertIs(read_status(directory, fallback), fallback)
                self.assertFalse(write_status(args, 'active', downloads={'completed': 60}))
            finally:
                _winapi.CloseHandle(handle)
            self.assertTrue(write_status(args, 'active', downloads={'completed': 60}))
            self.assertEqual(read_status(directory)['downloads']['completed'], 60)

    def test_busy_status_write_keeps_previous_data_and_next_write_recovers(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            args = SimpleNamespace(state_dir=directory, instance='test')
            write_status(args, 'active', downloads={'completed': 1})
            with patch('we_workshop_guard.os.replace', side_effect=PermissionError('Busy')), \
                    patch('we_workshop_guard.time.sleep'):
                self.assertFalse(write_status(args, 'active', downloads={'completed': 2}))
            self.assertEqual(read_status(directory)['downloads']['completed'], 1)
            self.assertTrue(write_status(args, 'active', downloads={'completed': 2}))
            self.assertEqual(read_status(directory)['downloads']['completed'], 2)

    def test_permission_conflict_is_retried_before_returning_fresh_status(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / 'workshop_guard.json').write_text('{"state":"active"}', encoding='utf-8')
            with patch('we_workshop_guard.Path.read_text', side_effect=[PermissionError('Busy'), PermissionError('Busy'), '{"state":"active"}']) as read, \
                    patch('we_workshop_guard.time.sleep'):
                self.assertEqual(read_status(directory), {'state': 'active'})
            self.assertEqual(read.call_count, 3)

    def test_unavailable_status_preserves_the_callers_last_valid_value(self):
        fallback = {'state': 'active', 'downloads': {'completed': 59}}
        with patch('we_workshop_guard.Path.read_text', side_effect=PermissionError('Busy')), \
                patch('we_workshop_guard.time.sleep'):
            self.assertIs(read_status(Path('output'), fallback), fallback)

    def test_invalid_json_shape_preserves_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            fallback = {'state': 'paused'}
            (directory / 'workshop_guard.json').write_text('[]', encoding='utf-8')
            self.assertIs(read_status(directory, fallback), fallback)


class FakeSession:
    def __init__(self):
        self.ugc = object()
        self.user = object()
        self.states = {11: 5, 22: 9, 33: 9}
        self.requests = []
        self.accept = True
        self.api = SimpleNamespace(
            SteamAPI_ISteamUGC_SuspendDownloads=lambda ugc, paused: None,
            SteamAPI_ISteamUser_BLoggedOn=lambda user: True,
            SteamAPI_ISteamUGC_DownloadItem=self.request,
        )

    def subscribed_items(self):
        return list(self.states)

    def item_state(self, item):
        return self.states[item]

    def progress(self, item):
        return 5, 100

    def request(self, ugc, item, priority):
        self.requests.append((item, priority))
        return self.accept


class DownloadQueueTests(unittest.TestCase):
    def test_one_request_waits_for_success_callback_before_next_item(self):
        session = FakeSession()
        queue = DownloadQueue(session)
        with patch('we_workshop_guard.time.monotonic', return_value=100):
            queue.tick([])
            queue.tick([])
            self.assertEqual(session.requests, [(22, True)])
            self.assertEqual(queue.completed, 0)
            session.states[22] = 5
            queue.tick([(22, 1)])
            self.assertEqual(session.requests, [(22, True), (33, True)])
            self.assertEqual(queue.completed, 1)

    def test_failure_defers_only_that_item_and_preserves_next_request(self):
        session = FakeSession()
        queue = DownloadQueue(session)
        with patch('we_workshop_guard.time.monotonic', return_value=100):
            queue.tick([])
            queue.tick([(22, 3)])
            self.assertIsNone(queue.active)
            self.assertEqual(queue.completed, 0)
            self.assertEqual(queue.status()['deferred'], 1)
        with patch('we_workshop_guard.time.monotonic', return_value=102):
            queue.tick([])
            self.assertEqual(session.requests, [(22, True), (33, True)])

    def test_unrelated_callback_cannot_advance_current_download(self):
        session = FakeSession()
        queue = DownloadQueue(session)
        with patch('we_workshop_guard.time.monotonic', return_value=100):
            queue.tick([])
            queue.tick([(33, 1)])
            self.assertEqual(queue.active, 22)
            self.assertEqual(queue.completed, 0)
            self.assertEqual(session.requests, [(22, True)])

    def test_rejected_requests_are_deferred_without_marking_success(self):
        session = FakeSession()
        session.accept = False
        queue = DownloadQueue(session)
        with patch('we_workshop_guard.time.monotonic', return_value=100):
            queue.tick([])
            self.assertIsNone(queue.active)
            self.assertEqual(queue.completed, 0)
            self.assertEqual(queue.status()['deferred'], 1)
        with patch('we_workshop_guard.time.monotonic', return_value=102):
            queue.tick([])
            self.assertEqual(session.requests, [(22, True), (33, True)])

    def test_installed_unsubscribed_and_legacy_items_are_skipped(self):
        session = FakeSession()
        session.states = {11: 5, 22: 8, 33: 3}
        queue = DownloadQueue(session)
        queue.tick([])
        self.assertEqual(session.requests, [])

    def test_empty_queue_waits_before_rescanning_subscriptions(self):
        session = FakeSession()
        session.states = {11: 5}
        queue = DownloadQueue(session)
        with patch('we_workshop_guard.time.monotonic', return_value=100), \
                patch.object(session, 'item_state', wraps=session.item_state) as states:
            queue.tick([])
            queue.tick([])
            self.assertEqual(states.call_count, 1)


class BackgroundCommandTests(unittest.TestCase):
    def test_frozen_worker_dispatches_guard_instead_of_reopening_gui(self):
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', 'WEWorker.exe'):
            command = background_command(Path('E:/WE'), Path('output'), 'test-instance', True)
        self.assertEqual(command[:4], ['WEWorker.exe', '--worker', 'we_workshop_guard.py', 'run'])
        self.assertEqual(command[-1], '--sequential')
        self.assertEqual(command[command.index('--state-dir') + 1], str(Path('output')))

    def test_source_worker_uses_python_with_utf8(self):
        with patch.object(sys, 'frozen', False, create=True):
            command = background_command(Path('E:/WE'), Path('output'), 'test-instance', False)
        self.assertEqual(command[:3], [sys.executable, '-X', 'utf8'])
        self.assertTrue(command[3].endswith('we_workshop_guard.py'))
        self.assertEqual(command[4], 'run')
        self.assertNotIn('--sequential', command)

    def test_tray_uses_console_worker_for_hidden_background_commands(self):
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', 'WEWorkshopGuard.exe'):
            command = background_command(Path('E:/WE'), Path('output'), '', True)
        self.assertEqual(command[0], 'WEWorker.exe')


class HandoffTests(unittest.TestCase):
    def test_restarting_same_instance_preserves_paused_counts_and_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            args = SimpleNamespace(state_dir=directory, install_dir=directory, instance='test', sequential=True)
            write_status(args, 'paused', downloads={'completed': 59, 'deferred': 6}, retry_at={'22': 9999})
            (directory / 'workshop_guard.pause').touch()
            with patch('we_workshop_guard.we_ui_running', return_value=True), \
                    patch('we_workshop_guard.subprocess.Popen') as child, \
                    patch('we_workshop_guard.time.sleep', side_effect=lambda delay: (directory / 'workshop_guard.stop').touch()):
                run(args)
            child.assert_not_called()
            self.assertEqual(read_status(directory)['downloads']['completed'], 59)
            self.assertEqual(read_status(directory)['retry_at'], {'22': 9999})

    def test_pause_releases_session_and_ui_handoff_resumes_after_closing(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            args = SimpleNamespace(state_dir=directory, install_dir=directory, instance='test', sequential=True)
            children, states = [], []
            clock = [100]
            ui = [False]

            class Child:
                def __init__(self, *arguments, **keywords):
                    self.returncode = None
                    children.append(self)
                    write_status(args, 'active', steam_session=True, downloads={'completed': 7})

                def poll(self):
                    if (directory / 'workshop_guard_session.stop').exists():
                        self.wait()
                    return self.returncode

                def wait(self, timeout=None):
                    self.returncode = 0
                    write_status(args, 'releasing', steam_session=False, downloads={'completed': 7})
                    return 0

            def step(delay):
                states.append(read_status(directory))
                clock[0] += 2
                if len(states) == 1:
                    (directory / 'workshop_guard.pause').touch()
                elif len(states) == 2:
                    self.assertEqual(states[-1]['state'], 'active')
                elif len(states) == 3:
                    self.assertEqual(children[0].returncode, 0)
                    (directory / 'workshop_guard.pause').unlink()
                    ui[0] = True
                elif len(states) == 4:
                    self.assertEqual(len(children), 1)
                    ui[0] = False
                else:
                    (directory / 'workshop_guard.stop').touch()

            with patch('we_workshop_guard.subprocess.Popen', Child), \
                    patch('we_workshop_guard.we_ui_running', side_effect=lambda install: ui[0]), \
                    patch('we_workshop_guard.time.monotonic', side_effect=lambda: clock[0]), \
                    patch('we_workshop_guard.time.sleep', side_effect=step):
                run(args)
            self.assertEqual([state['state'] for state in states], ['active', 'active', 'paused', 'yielding_to_we', 'active'])
            self.assertFalse(states[2]['steam_session'])
            self.assertFalse(states[3]['steam_session'])
            self.assertEqual(states[2]['downloads']['completed'], 7)
            self.assertEqual(len(children), 2)
            self.assertTrue(all(child.returncode == 0 for child in children))
            self.assertEqual(read_status(directory)['state'], 'stopped')

    def test_starting_with_we_ui_open_does_not_open_another_steam_session(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            args = SimpleNamespace(state_dir=directory, install_dir=directory, instance='test', sequential=True)
            with patch('we_workshop_guard.subprocess.Popen') as child, \
                    patch('we_workshop_guard.we_ui_running', return_value=True), \
                    patch('we_workshop_guard.time.sleep', side_effect=lambda delay: (directory / 'workshop_guard.stop').touch()):
                run(args)
            child.assert_not_called()

    def test_session_preserves_completion_and_retry_state_across_pause(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            args = SimpleNamespace(state_dir=directory, install_dir=directory, instance='test', sequential=True)
            write_status(args, 'paused', downloads={'completed': 7}, retry_at={'22': 9999})

            class Session(FakeSession):
                def __init__(self, install):
                    super().__init__()
                    self.initialized = False
                    self.api.SteamAPI_IsSteamRunning = lambda: True

                def connect(self):
                    self.initialized = True

                def callbacks(self):
                    return []

                def close(self):
                    self.initialized = False

            session = Session(directory)
            with patch('we_workshop_guard.SteamSession', return_value=session), \
                    patch('we_workshop_guard.time.monotonic', return_value=100), \
                    patch('we_workshop_guard.time.sleep', side_effect=lambda delay: (directory / 'workshop_guard.pause').touch()):
                run_session(args)
            data = read_status(directory)
            self.assertFalse(session.initialized)
            self.assertEqual(data['downloads']['completed'], 7)
            self.assertEqual(data['downloads']['deferred'], 1)
            self.assertEqual(session.requests, [(33, True)])


if __name__ == '__main__':
    unittest.main()
