"""Install optional semantic dependencies outside the portable application runtime."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

from filelock import FileLock


MODULES = ("torch", "torchvision", "transformers", "modelscope", "safetensors")


def app_directory() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def runtime_directory() -> Path:
    return app_directory() / "runtime" / "semantic"


def runtime_ready(directory: Path | None = None) -> bool:
    directory = directory or runtime_directory()
    try:
        state = json.loads((directory / "ready.json").read_text(encoding="utf-8"))
        return state["python"] == list(sys.version_info[:2]) and all(
            (directory / "site-packages" / name / "__init__.py").is_file() for name in MODULES
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def activate_runtime(site_packages: Path | None = None) -> None:
    if site_packages is None:
        if not runtime_ready():
            return
        site_packages = runtime_directory() / "site-packages"
    value = str(site_packages.resolve())
    if value not in sys.path:
        sys.path.insert(0, value)
    importlib.invalidate_caches()


def pip_main(arguments: list[str]) -> int:
    if getattr(sys, "frozen", False):
        wheels = sorted((Path(sys._MEIPASS) / "bootstrap").glob("pip-*.whl"))
        if not wheels:
            raise RuntimeError("缺少语义组件安装器，请重新解压主程序。")
        sys.path.insert(0, str(wheels[-1]))
    command = importlib.import_module("pip._internal.cli.main").main
    return command(arguments)


def check_runtime(site_packages: Path) -> None:
    activate_runtime(site_packages)
    for name in MODULES:
        importlib.import_module(name)
    torch = importlib.import_module("torch")
    print(f"语义组件检查通过，PyTorch {torch.__version__}，CUDA 可用 {torch.cuda.is_available()}", flush=True)


def check_installer() -> int:
    """Check wheel installation locally without downloading semantic packages."""
    with tempfile.TemporaryDirectory(prefix="we-installer-check-") as directory:
        root = Path(directory)
        wheel = root / "we_install_probe-1.0-py3-none-any.whl"
        metadata = "we_install_probe-1.0.dist-info/"
        with zipfile.ZipFile(wheel, "w") as output:
            output.writestr("we_install_probe.py", "value = 42\n")
            output.writestr(metadata + "METADATA", "Metadata-Version: 2.1\nName: we-install-probe\nVersion: 1.0\n")
            output.writestr(metadata + "WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            output.writestr(metadata + "RECORD", "")
        target = root / "installed"
        result = pip_main(["install", "--disable-pip-version-check", "--no-index", "--no-deps",
                           "--no-compile", "--target", str(target), str(wheel)])
        if result or not (target / "we_install_probe.py").is_file():
            raise RuntimeError("本地组件安装器检查失败。")
    print("组件安装器本地安装检查通过。", flush=True)
    return 0


def install_runtime(device: str) -> None:
    destination = runtime_directory()
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(parent / "semantic-install.lock"), timeout=1):
        if runtime_ready() and json.loads((destination / "ready.json").read_text(encoding="utf-8")).get("device") == device:
            print("语义组件已安装。", flush=True)
            return
        staging = Path(tempfile.mkdtemp(prefix="semantic-install-", dir=parent)).resolve()
        try:
            packages = staging / "site-packages"
            base = ["install", "--disable-pip-version-check", "--only-binary=:all:",
                    "--no-warn-script-location", "--no-compile", "--target", str(packages),
                    "--cache-dir", str(parent / "download-cache")]
            suffix = "cpu" if device == "cpu" else "cu130"
            indexes = ["--index-url", "https://pypi.org/simple",
                       "--extra-index-url", f"https://download.pytorch.org/whl/{suffix}"]
            requirements = [f"torch==2.11.0+{suffix}", f"torchvision==0.26.0+{suffix}",
                            "transformers>=4.56,<6", "modelscope>=1.28,<2",
                            "safetensors>=0.6,<1", "setuptools", "numpy>=2.1,<3",
                            "pillow>=11,<13", "requests>=2.34,<3"]
            print(f"正在下载并安装 {device.upper()} 语义组件，安装位置 {destination}", flush=True)
            result = pip_main([*base, *indexes, *requirements])
            if result:
                raise RuntimeError(f"语义组件安装失败，pip 返回 {result}。可重新安装以继续使用下载缓存。")
            worker = [sys.executable, "--worker", "semantic_runtime.py"] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve())]
            subprocess.run([*worker, "--check-dir", str(packages)], check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            (staging / "ready.json").write_text(json.dumps({
                "python": list(sys.version_info[:2]), "device": device,
            }), encoding="utf-8")
            if destination.exists():
                # An incomplete previous installation stays available until the replacement is verified.
                backup = parent / ("semantic-incomplete-" + staging.name)
                os.replace(destination, backup)
                try:
                    os.replace(staging, destination)
                except OSError:
                    os.replace(backup, destination)
                    raise
                if backup.resolve().is_relative_to(parent.resolve()):
                    try:
                        shutil.rmtree(backup)
                    except OSError:
                        print("旧语义组件正在使用中，可在关闭其他任务后清理 " + str(backup), flush=True)
            else:
                os.replace(staging, destination)
            print("语义组件安装完成。模型将在首次分析时下载。", flush=True)
        finally:
            if staging.exists() and staging.is_relative_to(parent.resolve()):
                shutil.rmtree(staging)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--install", choices=("cpu", "cuda"))
    group.add_argument("--check-dir", type=Path)
    group.add_argument("--pip-check", action="store_true")
    args = parser.parse_args()
    if args.install:
        install_runtime(args.install)
    elif args.check_dir:
        check_runtime(args.check_dir)
    else:
        raise SystemExit(check_installer())


if __name__ == "__main__":
    main()
