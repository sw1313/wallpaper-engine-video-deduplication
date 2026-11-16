"""Create a public Windows release from an explicit runtime file allowlist."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib


ROOT_FILES = (
    "WEVideoTools.exe", "WEWorker.exe", "WEWorkshopGuard.exe", "config.example.toml",
    "wallpaper-engine-video-deduplication.js", "we_archive_bridge.js",
    "we_archive_bridge.ps1", "we_workshop_guard.ps1", "LICENSE",
)
WEIGHT_SUFFIXES = {".pt", ".pth", ".ckpt", ".safetensors", ".gguf", ".onnx"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Release version, for example 1.1.0")
    parser.add_argument("--seven-zip", default="7z", help="7-Zip executable")
    parser.add_argument("--package-dir", type=Path, help="Directory containing the freshly built program")
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", args.version):
        parser.error("Expected a semantic version")

    root = Path(__file__).resolve().parent
    package = args.package_dir or root / "dist" / "WEVideoTools"
    # A new directory prevents files from a previous release entering this one.
    stage_parent = Path(tempfile.mkdtemp(prefix=f"release-{args.version}-", dir=root / "build"))
    stage = stage_parent / "WEVideoTools"
    stage.mkdir()

    sources = [(package / name, Path(name)) for name in ROOT_FILES]
    for source in (package / "_internal").rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(package)
        if any(part.lower() in {"torch", "torchvision", "transformers", "modelscope", "safetensors",
                                "huggingface_hub", "tokenizers", "tools"} for part in relative.parts):
            raise RuntimeError(f"Optional component found in core runtime: {relative}")
        if source.name.lower() in {"ffmpeg.exe", "ffprobe.exe", "fpcalc.exe"}:
            raise RuntimeError(f"External tool found in core runtime: {relative}")
        if "__pycache__" in relative.parts or source.suffix.lower() in WEIGHT_SUFFIXES:
            continue
        if source.name in {"pytorch_model.bin", "model.bin"} or source.suffix.lower() in {".log", ".sqlite3"}:
            continue
        sources.append((source, relative))

    for source, relative in sources:
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)

    # Public configuration always comes from the repository template.
    template = (root / "config.example.toml").read_text(encoding="utf-8-sig")
    config = tomllib.loads(template)
    assert not config.get("steam_api_key"), "Public template contains a Steam API key"
    assert config["output_dir"] == "output" and config["model_cache_dir"] == "models_cache"
    assert config["semantic_feature_device"] == "auto"
    (stage / "config.toml").write_text(template, encoding="utf-8")
    (stage / "使用说明.txt").write_text(
        "Wallpaper Engine 视频去重工具 " + args.version + "\n\n"
        "运行 WEVideoTools.exe，并保留完整目录。\n"
        "首次启动请填写创意工坊目录和 Wallpaper Engine 安装目录。\n"
        "启动时检查 ffmpeg、ffprobe 和可选的 fpcalc。请自行安装并配置 PATH 或程序路径。\n"
        "启用语义特征前，点击安装语义组件，选择 CPU 或 CUDA 版本。\n"
        "语义运行库安装在 runtime/semantic，模型首次使用时下载到 models_cache。\n"
        "output 保存筛重数据库、结果表和取消订阅重试进度。\n"
        "批量取消订阅需要浏览器登录 Steam，并安装随包的 Tampermonkey 脚本。\n"
        "创意工坊归档识别补丁的安装、重装和还原方法见 README.md。\n\n"
        "后台逐项下载可双击 WEWorkshopGuard.exe 启动，保留同目录的 WEWorker.exe 和 _internal。\n"
        "右键托盘图标可查看状态、暂停、恢复和退出。暂停会释放 Steam 占用，方便从 Steam 启动 WE。\n"
        "自动模式在 WE 主界面打开时让出，界面进程结束后自动接管。手动暂停会保持到恢复。\n"
        "命令行支持 .\\we_workshop_guard.ps1 -Action Start / Status / Pause / Resume / Stop。\n\n"
        "升级时将新版解压到独立目录，再复制自己的 config.toml、output 和 models_cache。\n",
        encoding="utf-8",
    )
    shutil.copy2(root / "README.md", stage / "README.md")
    (stage / "output").mkdir()
    assert not (stage / "models_cache").exists()

    release = root / "dist" / "release"
    release.mkdir(exist_ok=True)
    assets = stage_parent / "assets"
    assets.mkdir()
    archive = assets / f"WEVideoTools-{args.version}-windows-x64.7z"
    subprocess.run([
        args.seven_zip, "a", "-t7z", "-mx=5", "-mmt=4", "-v1900m", "-bsp0",
        str(archive), "WEVideoTools",
    ], cwd=stage_parent, check=True)
    volumes = sorted(assets.glob(archive.name + ".*"))
    if not volumes:
        raise RuntimeError("7-Zip did not create release volumes")
    subprocess.run([args.seven_zip, "t", "-bsp0", str(volumes[0])], check=True)
    checksum = assets / f"WEVideoTools-{args.version}-SHA256SUMS.txt"
    with checksum.open("w", encoding="utf-8", newline="\n") as output:
        for volume in volumes:
            assert volume.stat().st_size < 2 * 1024**3
            with volume.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            output.write(f"{digest}  {volume.name}\n")
    new_names = {volume.name for volume in volumes}
    for previous in release.glob(archive.name + ".*"):
        if re.fullmatch(re.escape(archive.name) + r"\.\d{3,}", previous.name) and previous.name not in new_names:
            previous.unlink()
    for source in [*volumes, checksum]:
        os.replace(source, release / source.name)
    print(f"Public runtime staged at {stage}", flush=True)
    print(f"Release {args.version} created with {len(volumes)} volume(s).", flush=True)


if __name__ == "__main__":
    main()
