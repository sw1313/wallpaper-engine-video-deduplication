import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import semantic_runtime as runtime


class OptionalRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.destination = self.root / "runtime" / "semantic"

    def tearDown(self):
        self.temporary.cleanup()

    def create_packages(self, directory):
        for name in runtime.MODULES:
            package = directory / name
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")

    def test_ready_requires_matching_python_and_complete_packages(self):
        self.create_packages(self.destination / "site-packages")
        marker = self.destination / "ready.json"
        marker.write_text(json.dumps({"python": list(sys.version_info[:2])}))
        self.assertTrue(runtime.runtime_ready(self.destination))
        marker.write_text(json.dumps({"python": [3, 8]}))
        self.assertFalse(runtime.runtime_ready(self.destination))
        marker.write_text(json.dumps({"python": list(sys.version_info[:2])}))
        (self.destination / "site-packages" / "torch" / "__init__.py").unlink()
        self.assertFalse(runtime.runtime_ready(self.destination))

    def test_optional_packages_can_be_loaded_from_external_directory(self):
        packages = self.root / "site-packages"
        packages.mkdir()
        (packages / "optional_runtime_probe.py").write_text("value = 42", encoding="utf-8")
        original_path = sys.path[:]
        try:
            runtime.activate_runtime(packages)
            self.assertEqual(importlib.import_module("optional_runtime_probe").value, 42)
            runtime.activate_runtime(packages)
            self.assertEqual(sys.path.count(str(packages.resolve())), 1)
        finally:
            sys.path[:] = original_path
            sys.modules.pop("optional_runtime_probe", None)

    def test_failed_download_preserves_previous_installation(self):
        self.destination.mkdir(parents=True)
        previous = self.destination / "previous.txt"
        previous.write_text("previous")
        with patch.object(runtime, "runtime_directory", return_value=self.destination), \
                patch.object(runtime, "pip_main", return_value=1):
            with self.assertRaisesRegex(RuntimeError, "安装失败"):
                runtime.install_runtime("cpu")
        self.assertEqual(previous.read_text(), "previous")
        self.assertEqual(list(self.destination.parent.glob("semantic-install-*")), [])

    def test_installation_is_verified_before_becoming_ready(self):
        def install(arguments):
            self.assertIn("torch==2.11.0+cpu", arguments)
            target = Path(arguments[arguments.index("--target") + 1])
            self.create_packages(target)
            self.assertFalse(self.destination.exists())
            return 0

        with patch.object(runtime, "runtime_directory", return_value=self.destination), \
                patch.object(runtime, "pip_main", side_effect=install), \
                patch.object(runtime.subprocess, "run") as verify:
            runtime.install_runtime("cpu")
        verify.assert_called_once()
        self.assertTrue(runtime.runtime_ready(self.destination))

    def test_failed_verification_does_not_activate_partial_runtime(self):
        def install(arguments):
            self.create_packages(Path(arguments[arguments.index("--target") + 1]))
            return 0

        with patch.object(runtime, "runtime_directory", return_value=self.destination), \
                patch.object(runtime, "pip_main", side_effect=install), \
                patch.object(runtime.subprocess, "run", side_effect=RuntimeError("verification failed")):
            with self.assertRaisesRegex(RuntimeError, "verification failed"):
                runtime.install_runtime("cuda")
        self.assertFalse(runtime.runtime_ready(self.destination))
        self.assertFalse(self.destination.exists())

    def test_switching_cpu_to_cuda_replaces_verified_runtime(self):
        self.create_packages(self.destination / "site-packages")
        (self.destination / "ready.json").write_text(json.dumps({
            "python": list(sys.version_info[:2]), "device": "cpu",
        }))

        def install(arguments):
            self.assertIn("torch==2.11.0+cu130", arguments)
            self.create_packages(Path(arguments[arguments.index("--target") + 1]))
            self.assertTrue(runtime.runtime_ready(self.destination))
            return 0

        with patch.object(runtime, "runtime_directory", return_value=self.destination), \
                patch.object(runtime, "pip_main", side_effect=install), \
                patch.object(runtime.subprocess, "run"):
            runtime.install_runtime("cuda")
        self.assertTrue(runtime.runtime_ready(self.destination))
        self.assertEqual(json.loads((self.destination / "ready.json").read_text())["device"], "cuda")
        self.assertEqual(list(self.destination.parent.glob("semantic-incomplete-*")), [])


if __name__ == "__main__":
    unittest.main()
