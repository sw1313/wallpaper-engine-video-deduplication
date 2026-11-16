from types import SimpleNamespace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from we_workshop_guard import DownloadQueue, background_command


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


if __name__ == '__main__':
    unittest.main()
