import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from openpyxl import Workbook
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import we_delisted_archiver as archive
import we_ui

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("controller", ROOT / "output" / "bulk_unsub_controller.py")
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


class RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        if controller.QUEUE is not None:
            controller.QUEUE.close()
            controller.QUEUE = None
        self.temporary.cleanup()

    def workbook(self, header, rows):
        path = self.root / "input.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(header)
        for row in rows:
            sheet.append(row)
        workbook.save(path)
        return path

    def project(self, directory, content=b"complete video"):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "project.json").write_text(json.dumps({"file": "video.mp4"}), encoding="utf-8")
        (directory / "video.mp4").write_bytes(content)

    def we_config(self, directory, wids):
        directory.mkdir(parents=True, exist_ok=True)
        data = {"?installdirectory": str(directory), "profile": {"general": {"browser": {
            "folders": [{"title": "测试分类", "items": {wid: 1 for wid in wids}}]
        }}}}
        (directory / "config.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def pump_until(self, done, timeout=5):
        deadline = time.monotonic() + timeout
        while not done() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertTrue(done())

    def test_duplicate_single_link_preserves_keeper(self):
        keeper = "https://steamcommunity.com/sharedfiles/filedetails/?id=111"
        path = self.workbook(["序号", "链接1", "链接2"], [[1, keeper, None]])
        self.assertEqual(controller.read_urls_from_xlsx(path), ([], 0))

    def test_explicit_unsubscribe_list_keeps_single_target(self):
        target = "https://steamcommunity.com/sharedfiles/filedetails/?id=222"
        path = self.workbook(["url"], [[target]])
        self.assertEqual(controller.read_urls_from_xlsx(path), ([target], 0))

    def test_duplicate_only_nonfirst_link_is_target(self):
        keeper = "https://steamcommunity.com/sharedfiles/filedetails/?id=111"
        target = "https://steamcommunity.com/sharedfiles/filedetails/?id=222"
        path = self.workbook(["序号", "链接1", "链接2"], [[1, keeper, target]])
        self.assertEqual(controller.read_urls_from_xlsx(path), ([target], 0))

    def test_file_uri_percent_encoded_name_roundtrips_once(self):
        path = self.root / "myprojects" / "100%20真实" / "video.mp4"
        self.assertEqual(controller._file_uri_to_path(path.as_uri()), path)

    def test_repeated_report_is_counted_once(self):
        queue = controller.UrlQueue(["https://steamcommunity.com/sharedfiles/filedetails/?id=123"])
        queue.pop()
        queue.mark_done(True, "123")
        queue.mark_done(True, "123")
        self.assertEqual(queue.stats()["done"], 1)
        self.assertEqual(queue.stats()["ok"], 1)

    def test_unknown_report_does_not_complete_task(self):
        queue = controller.UrlQueue([])
        queue.mark_done(True, "999")
        self.assertEqual(queue.stats()["done"], 0)

    def test_retry_queue_retries_to_limit_and_keeps_failure(self):
        queue = controller.UrlQueue(["https://steamcommunity.com/sharedfiles/filedetails/?id=123"],
                                    max_attempts=3, retry_delay=0)
        try:
            for attempt in range(3):
                task = queue.lease_batch(1)[0]
                self.assertTrue(queue.mark_done(False, "123", "http_503", task["token"]))
                self.assertEqual(queue.stats()["attempts"], attempt + 1)
            self.assertEqual(queue.stats()["fail"], 1)
            self.assertEqual(queue.lease_batch(1), [])
        finally:
            queue.close()

    def test_persistent_queue_resumes_only_unconfirmed_items(self):
        urls = [f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}" for wid in (111, 222)]
        database = self.root / "unsub_progress.sqlite3"
        queue = controller.UrlQueue(urls, state_path=database, max_attempts=3, retry_delay=0)
        tasks = queue.lease_batch(2)
        queue.mark_done(True, "111", token=tasks[0]["token"])
        queue.mark_done(False, "222", "http_429", tasks[1]["token"])
        queue.close()
        resumed = controller.UrlQueue(urls, state_path=database, max_attempts=3, retry_delay=0)
        try:
            self.assertEqual(resumed.stats()["ok"], 1)
            self.assertEqual([task["id"] for task in resumed.lease_batch(2)], ["222"])
        finally:
            resumed.close()

    def test_old_attempt_report_cannot_override_new_attempt(self):
        queue = controller.UrlQueue(["https://steamcommunity.com/sharedfiles/filedetails/?id=123"],
                                    max_attempts=3, retry_delay=0)
        try:
            first = queue.lease_batch(1)[0]
            queue.mark_done(False, "123", "timeout", first["token"])
            second = queue.lease_batch(1)[0]
            self.assertFalse(queue.mark_done(True, "123", token=first["token"]))
            self.assertEqual(queue.stats()["pending"], 1)
            self.assertTrue(queue.mark_done(True, "123", token=second["token"]))
            self.assertFalse(queue.mark_done(False, "123", "late_failure", second["token"]))
            self.assertEqual(queue.stats()["ok"], 1)
        finally:
            queue.close()

    def test_unreported_lease_is_recovered(self):
        with patch.object(controller.time, "time", return_value=100.0):
            queue = controller.UrlQueue(["https://steamcommunity.com/sharedfiles/filedetails/?id=123"],
                                        max_attempts=3, retry_delay=0, lease_seconds=1)
            first = queue.lease_batch(1)[0]
        try:
            with patch.object(controller.time, "time", return_value=102.0):
                second = queue.lease_batch(1)[0]
            self.assertNotEqual(first["token"], second["token"])
            self.assertEqual(queue.stats()["attempts"], 2)
        finally:
            queue.close()

    def test_rate_limit_pauses_new_leases(self):
        urls = [f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}" for wid in (111, 222)]
        with patch.object(controller.time, "time", return_value=100.0):
            queue = controller.UrlQueue(urls, max_attempts=3, retry_delay=0)
            task = queue.lease_batch(1)[0]
            queue.mark_done(False, "111", "http_429", task["token"])
            self.assertEqual(queue.lease_batch(2), [])
        try:
            with patch.object(controller.time, "time", return_value=111.0):
                self.assertEqual(len(queue.lease_batch(2)), 2)
        finally:
            queue.close()

    def test_same_job_cannot_be_started_twice(self):
        urls = ["https://steamcommunity.com/sharedfiles/filedetails/?id=123"]
        database = self.root / "progress.sqlite3"
        queue = controller.UrlQueue(urls, state_path=database)
        try:
            with self.assertRaises(RuntimeError):
                controller.UrlQueue(urls, state_path=database)
        finally:
            queue.close()

    def test_progress_survives_abrupt_worker_exit(self):
        database = self.root / "progress.sqlite3"
        url = "https://steamcommunity.com/sharedfiles/filedetails/?id=123"
        script = (
            f"import sys,os; sys.path.insert(0,{str(ROOT / 'output')!r}); "
            "from pathlib import Path; from bulk_unsub_controller import UrlQueue; "
            f"queue=UrlQueue([{url!r}],state_path=Path({str(database)!r})); "
            "task=queue.lease_batch(1)[0]; queue.mark_done(True,'123',token=task['token']); os._exit(0)"
        )
        subprocess.run([sys.executable, "-c", script], capture_output=True, check=True, timeout=15)
        queue = controller.UrlQueue([url], state_path=database)
        try:
            self.assertEqual(queue.stats()["ok"], 1)
            self.assertEqual(queue.lease_batch(1), [])
        finally:
            queue.close()

    def test_controller_automatically_retries_failed_reports(self):
        urls = [f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}" for wid in (111, 222)]
        workbook = self.workbook(["url"], [[url] for url in urls])
        start_server = controller.start_server
        servers, browsers = [], []
        attempts = {}
        errors = []

        def create_server(port):
            server = start_server(0)
            servers.append(server)
            return server

        def process_browser():
            try:
                base = f"http://127.0.0.1:{servers[0].server_port}"
                for _ in range(100):
                    with urlopen(base + "/batch?size=2", timeout=3) as response:
                        batch = json.load(response)
                    for task in batch["tasks"]:
                        wid = task["id"]
                        attempts[wid] = attempts.get(wid, 0) + 1
                        ok = wid == "222" or attempts[wid] > 1
                        report = {"id": wid, "token": task["token"], "job": batch["job"], "ok": ok, "reason": "http_503" if not ok else ""}
                        with urlopen(Request(base + "/report", data=json.dumps(report).encode()), timeout=3) as response:
                            self.assertTrue(json.load(response)["ok"])
                    if batch["complete"]:
                        return
                    time.sleep(0.01)
                raise AssertionError("Browser did not finish retrying")
            except (OSError, ValueError, AssertionError, KeyError) as exc:
                errors.append(exc)

        def open_browser(url):
            browser = threading.Thread(target=process_browser)
            browsers.append(browser)
            browser.start()

        arguments = ["controller", "--xlsx", str(workbook), "--single-page", "--batch-size", "1",
                     "--retry-delay", "0", "--state-db", str(self.root / "progress.sqlite3")]
        with patch("sys.argv", arguments), patch.object(controller, "start_server", create_server), patch.object(controller, "open_url", open_browser):
            controller.main()
        for browser in browsers:
            browser.join(timeout=3)
            self.assertFalse(browser.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(attempts, {"111": 2, "222": 1})
        self.assertEqual(controller.QUEUE.stats()["ok"], 2)

    def test_explicit_restart_resets_success_for_same_list(self):
        urls = ["https://steamcommunity.com/sharedfiles/filedetails/?id=123"]
        database = self.root / "progress.sqlite3"
        queue = controller.UrlQueue(urls, state_path=database)
        task = queue.lease_batch(1)[0]
        queue.mark_done(True, "123", token=task["token"])
        queue.close()
        restarted = controller.UrlQueue(urls, state_path=database, restart=True)
        try:
            self.assertEqual(restarted.stats()["ok"], 0)
            self.assertEqual(restarted.stats()["left"], 1)
        finally:
            restarted.close()

    def test_custom_notify_port_reaches_browser(self):
        result = controller.set_hash_flag("https://steamcommunity.com/my/myworkshopfiles", notify_port=9988)
        self.assertEqual(parse_qs(urlparse(result).fragment)["cb"], ["http://127.0.0.1:9988/next"])

    def test_handshake_does_not_consume_queue(self):
        controller.QUEUE = controller.UrlQueue(["https://steamcommunity.com/sharedfiles/filedetails/?id=123"], True)
        server = controller.start_server(0)
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/batch?size=0", timeout=3) as response:
                self.assertEqual(json.load(response)["ids"], [])
            self.assertEqual(controller.QUEUE.stats()["left"], 1)
        finally:
            server.shutdown()
            server.server_close()

    def run_controller_with_report(self, success):
        target = "https://steamcommunity.com/sharedfiles/filedetails/?id=123"
        workbook = self.workbook(["url"], [[target]])
        start_server = controller.start_server
        servers = []

        def create_server(port):
            server = start_server(0)
            servers.append(server)
            return server

        def browser_callback(url):
            base = f"http://127.0.0.1:{servers[0].server_port}"
            with urlopen(base + "/batch?size=1", timeout=3) as response:
                self.assertEqual(json.load(response)["ids"], ["123"])
            report = json.dumps({"id": "123", "ok": success}).encode()
            for _ in range(2):
                with urlopen(Request(base + "/report", data=report), timeout=3) as response:
                    self.assertTrue(json.load(response)["ok"])

        arguments = ["controller", "--xlsx", str(workbook), "--single-page", "--batch-size", "1",
                     "--max-attempts", "1", "--state-db", str(self.root / "progress.sqlite3")]
        with patch("sys.argv", arguments), patch.object(controller, "start_server", create_server), patch.object(controller, "open_url", browser_callback):
            controller.main()

    def test_controller_success_requires_result_reports(self):
        self.run_controller_with_report(True)
        self.assertEqual(controller.QUEUE.stats()["done"], 1)

    def test_controller_failure_returns_nonzero_for_ui_cleanup(self):
        with self.assertRaises(SystemExit) as result:
            self.run_controller_with_report(False)
        self.assertEqual(result.exception.code, 1)

    def test_failed_archive_never_publishes_partial_target(self):
        source, target = self.root / "source", self.root / "myprojects" / "123"
        self.project(source)
        target.parent.mkdir()

        def fail_copy(src, dst, backend):
            dst.mkdir()
            (dst / "partial.bin").write_bytes(b"partial")
            raise OSError("simulated interrupted copy")

        with patch.object(archive, "_copytree_archive_payload", side_effect=fail_copy), self.assertRaises(OSError):
            archive._copytree_for_archive(source, target, "python")
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.iterdir()), [])
        archive._copytree_for_archive(source, target, "python")
        self.assertTrue(archive._archive_complete(source, target))

    def test_legacy_incomplete_archive_can_be_repaired(self):
        source, target = self.root / "source", self.root / "myprojects" / "123"
        self.project(source)
        self.project(target, b"short")
        (target / "local-extra.txt").write_text("local", encoding="utf-8")
        self.assertFalse(archive._archive_complete(source, target))
        archive._copytree_for_archive(source, target, "python")
        self.assertTrue(archive._archive_complete(source, target))
        self.assertTrue((target / "local-extra.txt").exists())

    def test_missing_archive_is_not_accepted_from_config_entry(self):
        self.assertFalse(archive._archive_complete(self.root / "missing", self.root / "123"))

    def test_archive_folder_skips_old_missing_subscriptions(self):
        workshop, we = self.root / "workshop", self.root / "we"
        self.project(workshop / "111")
        self.we_config(we, ["111", "999"])
        archive.run_archive_folder(workshop, we, 0, "python")
        self.assertTrue(archive._archive_complete(workshop / "111", we / "projects/myprojects/111"))
        data = json.loads((we / "config.json").read_text(encoding="utf-8"))
        container, key = archive._locate_folders_slot(data)
        self.assertTrue(any("myprojects/111" in name for name in container[key][0]["items"]))

    def test_archived_query_excludes_incomplete_and_skips_orphan_validation(self):
        workshop, we = self.root / "workshop", self.root / "we"
        self.project(workshop / "111")
        self.project(workshop / "222")
        self.we_config(we, ["111", "222", "999"])
        self.project(we / "projects/myprojects/111")
        self.project(we / "projects/myprojects/222", b"partial")
        self.project(we / "projects/myprojects/999")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(archive, "_archive_complete", wraps=archive._archive_complete) as verify:
            archive.cmd_list_archived_in_folder(we, 0, workshop)
        data = json.loads(output.getvalue())
        self.assertEqual(data["archived_wids"], ["111"])
        self.assertEqual(data["incomplete_wids"], ["222"])
        self.assertEqual(data["orphan_wids"], ["999"])
        self.assertEqual(verify.call_count, 2)

    def test_background_archive_query_keeps_ui_responsive(self):
        window = we_ui.App()
        results, ticks = [], []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start(10)
        script = "import time; print('[VERIFY] progress',flush=True); time.sleep(0.6); print('{\"archived_wids\":[\"111\"]}',flush=True)"
        try:
            with patch.object(we_ui, "_worker_command", return_value=[sys.executable, "-c", script]):
                started = time.monotonic()
                window._query_archive_json(["worker"], "查询测试", results.append)
                self.assertLess(time.monotonic() - started, 0.4)
                self.pump_until(lambda: bool(results))
            self.assertEqual(results, [{"archived_wids": ["111"]}])
            self.assertGreater(len(ticks), 5)
        finally:
            timer.stop()
            window.close()

    def test_stopping_archive_query_discards_its_result(self):
        window = we_ui.App()
        results = []
        script = "import time; time.sleep(5); print('{\"archived_wids\":[\"111\"]}')"
        try:
            with patch.object(we_ui, "_worker_command", return_value=[sys.executable, "-c", script]):
                window._query_archive_json(["worker"], "查询测试", results.append)
                handle = window.proc
                window._stop_running()
                handle.reader_thread.join(timeout=3)
                self.app.processEvents()
            self.assertFalse(handle.reader_thread.is_alive())
            self.assertTrue(handle.cancelled)
            self.assertEqual(results, [])
            self.assertIsNone(window._archive_query_token)
        finally:
            window.close()

    def test_manual_unsubscribe_queries_selected_folders_in_background(self):
        window = we_ui.App()
        picked = [{"index": 0, "title": "分类一"}, {"index": 1, "title": "分类二"}]
        prepared = []

        def worker_command(command):
            index = command[command.index("--list-archived-in-folder-index") + 1]
            result = json.dumps({"archived_wids": [index + "11"], "orphan_wids": [], "incomplete_wids": []})
            return [sys.executable, "-c", f"import time; time.sleep(0.1); print({result!r})"]

        try:
            window._field_set("workshop_root", str(self.root))
            window._field_set("we_install_dir", str(self.root))
            with patch.object(window, "_arc_fetch_folders", return_value=picked), \
                 patch.object(window, "_arc_pick_folders_multi", return_value=picked), \
                 patch.object(we_ui, "_worker_command", side_effect=worker_command), \
                 patch.object(window, "_arc_prepare_unsub_folder", side_effect=lambda *args: prepared.append(args)):
                window._run_arc_unsub_folder()
                self.pump_until(lambda: bool(prepared))
            self.assertEqual(prepared[0][0], picked)
            self.assertEqual(prepared[0][1], ["011", "111"])
            self.assertEqual(prepared[0][2], [])
        finally:
            window.close()

    def test_partial_archive_batch_keeps_archive_stage(self):
        window = we_ui.App()
        try:
            with patch.object(window, "_arc_advance") as advance:
                window._arc_run_next_archive_folder(True, total_planned=11, total_ok=8)
            advance.assert_not_called()
            window._refresh_task_status()
            self.assertIn("部分归档失败", window.task_status.text())
        finally:
            window.close()

    def test_api_failure_is_not_a_successful_empty_detection(self):
        with patch.object(archive, "_fetch_batch_old_api", return_value=None), self.assertRaises(RuntimeError):
            archive.batch_check_delisted(["123"])

    def test_toml_string_escapes_roundtrip(self):
        value = '含中文\n换行\t"引号"\\路径'
        text = we_ui.dump_simple_toml({"value": value})
        self.assertEqual(we_ui.tomllib.loads(text)["value"], value)

    def test_worker_folder_json_is_utf8_with_legacy_environment(self):
        title = "中文分类 / 动画 / 音乐 🎵 / 日本語"
        config = {"profile": {"general": {"browser": {"folders": [{"title": title, "items": {}}]}}}}
        (self.root / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-X", "utf8=0", str(ROOT / "launcher.py"), "--worker",
             "we_delisted_archiver.py", "--list-folders", "--we-install-dir", str(self.root)],
            capture_output=True, check=False, timeout=15,
            env={**os.environ, "PYTHONIOENCODING": "cp936", "PYTHONUTF8": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(json.loads(result.stdout.decode("utf-8"))["folders"][0]["title"], title)

    def test_worker_error_logs_are_utf8_with_legacy_environment(self):
        result = subprocess.run(
            [sys.executable, "-X", "utf8=0", str(ROOT / "launcher.py"), "--worker",
             "we_delisted_archiver.py", "--list-folders", "--we-install-dir", str(self.root / "不存在 🎵")],
            capture_output=True, check=False, timeout=15,
            env={**os.environ, "PYTHONIOENCODING": "cp936", "PYTHONUTF8": "0"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("无效", result.stderr.decode("utf-8"))
        self.assertIn("不存在 🎵", result.stderr.decode("utf-8"))

    def test_ui_preserves_extra_config_and_synchronizes_install_directory(self):
        path = self.root / "config.toml"
        we_ui._write_toml_atomic(path, {"workshop_root": str(self.root), "force_merge_pairs": ["111|222"], "semantic_patch_anim_hot_ratio_min": 0.23})
        window = we_ui.App()
        try:
            window.dedup_config_path.setText(str(path))
            window._load_config_into_form(path)
            window._field_set("we_install_dir", "D:/new-location")
            self.assertEqual(window.arc_we_install_dir.text(), "D:/new-location")
            calls = []
            with patch.object(window, "_start_process", side_effect=lambda cmd, **kwargs: calls.append((cmd, kwargs))):
                window._run_dedup()
            command, options = calls[0]
            generated = Path(command[command.index("-c") + 1])
            data = we_ui.load_toml(generated)
            self.assertEqual(data["force_merge_pairs"], ["111|222"])
            self.assertEqual(data["semantic_patch_anim_hot_ratio_min"], 0.23)
            self.assertTrue(data["semantic_patch_enabled"])
            options["on_finish"](0)
            self.assertFalse(generated.exists())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
