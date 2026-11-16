from pathlib import Path
import ensurepip
import sys
import sysconfig
import zipfile
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH)
datas = [(str(root / "config.example.toml"), ".")]
for package in ("tqdm", "requests", "numpy", "packaging", "filelock"):
    datas += copy_metadata(package)

pip_wheel = next((Path(ensurepip.__file__).parent / "_bundled").glob("pip-*.whl"))
datas.append((str(pip_wheel), "bootstrap"))
# Optional wheels import standard-library modules outside the static application
# graph. Keep the Python standard library available to those installed packages.
stdlib = Path(sysconfig.get_paths()["stdlib"])
bootstrap = root / "build" / "bootstrap"
bootstrap.mkdir(parents=True, exist_ok=True)
stdlib_zip = bootstrap / "stdlib.zip"
with zipfile.ZipFile(stdlib_zip, "w", zipfile.ZIP_DEFLATED) as output:
    for name in sorted(sys.stdlib_module_names):
        source = stdlib / name
        if name in {"tkinter", "idlelib", "turtledemo", "ensurepip", "test"} or name.startswith("_test"):
            continue
        if source.is_dir():
            for file in source.rglob("*.py"):
                output.write(file, file.relative_to(stdlib).as_posix())
        elif source.with_suffix(".py").is_file():
            file = source.with_suffix(".py")
            output.write(file, file.name)
datas.append((str(stdlib_zip), "bootstrap"))
standard_binaries = [(str(file), ".") for file in (Path(sys.base_prefix) / "DLLs").glob("*.pyd")
                     if not file.name.startswith(("_test", "_tkinter"))]

a = Analysis(
    [str(root / "launcher.py")],
    pathex=[str(root), str(root / "output")],
    binaries=standard_binaries,
    datas=datas,
    hiddenimports=["semantic_features", "semantic_runtime", "scipy.fftpack"],
    hookspath=[],
    excludes=["torch", "torchvision", "transformers", "modelscope", "safetensors", "huggingface_hub", "tokenizers", "pip", "fsspec", "paramiko", "bcrypt", "nacl", "cryptography", "OpenSSL", "Crypto", "Cryptodome", "PyQt5", "PyQt6", "PySide2", "tkinter", "matplotlib", "IPython", "pytest", "tensorflow", "tensorboard", "onnxruntime", "cv2", "bitsandbytes", "triton", "sklearn", "torchaudio", "librosa", "numba"],
    noarchive=False,
    optimize=0,
)
# Qt uses the unversioned ICU API supplied by Windows. A versioned ICU from
# another program on PATH can be collected under the same DLL name.
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() not in {"icuuc.dll", "icudt78.dll"}]
pyz = PYZ(a.pure)
runtime_options = [("X utf8", None, "OPTION")]
gui = EXE(pyz, a.scripts, runtime_options, exclude_binaries=True, name="WEVideoTools", console=False, upx=False)
worker = EXE(pyz, a.scripts, runtime_options, exclude_binaries=True, name="WEWorker", console=True, upx=False)
guard = EXE(pyz, a.scripts, runtime_options, exclude_binaries=True, name="WEWorkshopGuard", console=False, upx=False)
coll = COLLECT(gui, worker, guard, a.binaries, a.datas, strip=False, upx=False, name="WEVideoTools")
