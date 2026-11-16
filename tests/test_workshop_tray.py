import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from we_workshop_tray import GuardTray, control_command


class TrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        (self.directory / 'bin').mkdir()
        (self.directory / 'bin/steam_api64.dll').touch()
        self.tray = GuardTray(self.app, self.directory, self.directory)
        self.tray.timer.stop()

    def tearDown(self):
        self.app.aboutToQuit.disconnect(self.tray.request_stop)
        self.tray.log_timer.stop()
        self.tray.tray.hide()
        self.tray.window.hide()
        self.tray.window.deleteLater()
        self.tray.menu.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_paused_state_explains_steam_release_and_enables_resume(self):
        state = {'state': 'paused', 'protocol': 3, 'steam_session': False, 'downloads': {'completed': 7}}
        with patch.object(self.tray, 'backend_status', return_value=(state, True)):
            self.tray.refresh()
        self.assertIn('已释放 Steam', self.tray.window.state.text())
        self.assertTrue(self.tray.start_action.isEnabled())
        self.assertFalse(self.tray.pause_action.isEnabled())
        self.assertEqual(self.tray.window.completed.text(), '7')

    def test_exit_waits_for_backend_release_before_quitting(self):
        application = SimpleNamespace(quit=Mock())
        self.tray.app = application
        with patch.object(self.tray, 'backend_status', return_value=({'state': 'active'}, True)):
            self.tray.exit()
        self.assertTrue((self.directory / 'workshop_guard.stop').exists())
        application.quit.assert_not_called()
        with patch.object(self.tray, 'backend_status', return_value=({'state': 'stopped'}, False)):
            self.tray.refresh()
        application.quit.assert_called_once()

    def test_pause_uses_pause_command_and_start_requests_sequential_mode(self):
        paused = control_command('pause', self.directory, self.directory)
        started = control_command('start', self.directory, self.directory)
        self.assertIn('pause', paused)
        self.assertNotIn('--sequential', paused)
        self.assertIn('start', started)
        self.assertIn('--sequential', started)

    def test_existing_paused_backend_is_resumed_without_starting_another_instance(self):
        with patch.object(self.tray, 'backend_status', return_value=({'state': 'paused', 'protocol': 3}, True)), \
                patch.object(self.tray, 'launch') as launch:
            self.tray.start()
        launch.assert_called_once_with('resume')

    def test_command_output_is_drained_without_a_blocking_pipe(self):
        with patch('we_workshop_tray.subprocess.Popen') as launch:
            launch.return_value.poll.return_value = None
            self.tray.launch('pause')
            output = launch.call_args.kwargs['stdout']
            output.write(b'x' * 100000)
            launch.return_value.poll.return_value = 0
            launch.return_value.returncode = 0
            self.tray.refresh()
        self.assertTrue(output.closed)
        self.assertIsNone(self.tray.command)

    def test_failed_command_launch_keeps_status_available(self):
        with patch('we_workshop_tray.subprocess.Popen', side_effect=OSError('Launch failed')):
            self.tray.launch('pause')
        self.assertIn('Launch failed', self.tray.window.detail.text())
        self.assertIsNone(self.tray.command_output)

    def test_status_menu_shows_and_restores_the_window(self):
        self.tray.menu.actions()[0].trigger()
        self.app.processEvents()
        self.assertTrue(self.tray.window.isVisible())
        self.tray.window.showMinimized()
        self.tray.menu.actions()[0].trigger()
        self.app.processEvents()
        self.assertFalse(self.tray.window.isMinimized())
        self.tray.window.close()
        self.assertFalse(self.tray.window.isVisible())
        self.tray.menu.actions()[0].trigger()
        self.app.processEvents()
        self.assertTrue(self.tray.window.isVisible())

    def test_log_opens_inside_window_and_refreshes_appended_text(self):
        path = self.directory / 'workshop_guard.log'
        path.write_text('首条日志\n', encoding='utf-8')
        with patch('we_workshop_tray.subprocess.Popen') as launch:
            self.tray.open_log()
            launch.assert_not_called()
        self.assertTrue(self.tray.window.isVisible())
        self.assertIs(self.tray.window.tabs.currentWidget(), self.tray.window.log_tab)
        self.assertIn('首条日志', self.tray.window.log.toPlainText())
        with path.open('a', encoding='utf-8') as output:
            output.write('新增日志\n')
        self.tray.refresh_log()
        self.assertIn('新增日志', self.tray.window.log.toPlainText())
        self.tray.show_status()
        self.assertIs(self.tray.window.tabs.currentWidget(), self.tray.window.status_tab)

    def test_log_handles_missing_files_and_bounds_large_reads(self):
        self.tray.open_log()
        self.assertIn('暂无运行日志', self.tray.window.log.toPlainText())
        path = self.directory / 'workshop_guard.log'
        path.write_text('old line\n' * 20000 + 'latest line\n', encoding='utf-8')
        self.tray.refresh_log()
        self.assertIn('latest line', self.tray.window.log.toPlainText())
        self.assertLessEqual(self.tray.window.log.document().blockCount(), 2000)
        self.assertLessEqual(len(self.tray.window.log.toPlainText()), 128 * 1024)

    def test_existing_instance_request_opens_status_window(self):
        request = self.directory / 'workshop_guard.show_status'
        request.touch()
        self.tray.refresh()
        self.app.processEvents()
        self.assertFalse(request.exists())
        self.assertTrue(self.tray.window.isVisible())


if __name__ == '__main__':
    unittest.main()
