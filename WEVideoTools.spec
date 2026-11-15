from pathlib import Path
import shutil
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH)
datas = [(str(root / "config.example.toml"), ".")]
for package in ("torch", "torchvision", "transformers", "modelscope", "safetensors", "huggingface_hub", "tqdm", "regex", "requests", "numpy", "packaging", "filelock", "tokenizers", "PyYAML"):
    datas += copy_metadata(package)

binaries = []
for tool in ("ffmpeg", "ffprobe", "fpcalc"):
    source = shutil.which(tool)
    if not source:
        raise RuntimeError(f"Missing external tool: {tool}")
    binaries.append((source, "tools"))

a = Analysis(
    [str(root / "launcher.py")],
    pathex=[str(root), str(root / "output")],
    binaries=binaries,
    datas=datas,
    hiddenimports=["semantic_features", "torchvision", "scipy.fftpack", "transformers.models.dinov3_vit.modeling_dinov3_vit"],
    hookspath=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "tkinter", "matplotlib", "IPython", "pytest", "tensorflow", "tensorboard", "onnxruntime", "cv2", "bitsandbytes", "triton", "sklearn", "torchaudio", "librosa", "numba"],
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
coll = COLLECT(gui, worker, a.binaries, a.datas, strip=False, upx=False, name="WEVideoTools")
