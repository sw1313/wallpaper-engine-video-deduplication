#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Wallpaper Engine (431960) 重复视频检测
- 严格只读（恢复 atime/mtime）
- 两阶段并行：先“时长分桶”，再对候选桶并行做 pHash + 音频指纹
- 中段取样（避免片头片尾黑屏）
- 临时缓存“匹完就删”（TemporaryDirectory 作用域内清理）
- 导出 CSV/XLSX：每组一行，组内链接按该条目命中的最大文件大小降序
- 进度条：阶段1/阶段2均显示（tqdm）

用法：
  python we_duplicate_finder_readonly.py -c config.toml --verbose --trace
  # 如需关闭进度条：加 --no-progress
"""

import argparse
import contextlib
import csv
import hashlib
import logging
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import tomllib  # py311+
except Exception:
    import tomli as tomllib  # py310

from PIL import Image
import numpy as np
import imagehash
from openpyxl import Workbook
from openpyxl.styles import Font
from tqdm import tqdm  # 进度条

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".mpg", ".mpeg"}

# ----------------------------- 配置与数据 -----------------------------

@dataclass
class Config:
    workshop_root: str
    output_dir: str = "output"
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    fpcalc_path: str = "fpcalc"

    # —— 中段取样设置 ——
    sample_frames: int = 12             # pHash 抽帧数量（在“视频窗口”内）
    phash_size: int = 8                 # pHash 尺寸
    audio_window_seconds: int = 120     # 音频指纹“中间窗口”长度（秒）
    video_window_seconds: int = 20      # 视觉签名“中间窗口”长度（秒）
    seek_ratio: float = 0.5             # 窗口中心比例：0=开头，0.5=中点，1=结尾

    duration_rounding: str = "int"      # "int" 或 "nearest_0.5"
    require_both_signatures: bool = True

    # —— 并行与超时 ——
    max_workers_stage1: int = 8         # 阶段1（测时长）线程数
    max_workers_stage2: int = 6         # 阶段2（pHash/音频）线程数
    ffprobe_timeout: int = 25
    ffmpeg_timeout: int = 45
    fpcalc_timeout: int = 35

    # —— 日志 / UI ——
    log_file: Optional[str] = None
    verbose: bool = False
    trace: bool = False
    progress: bool = True               # 显示进度条

@dataclass
class DurationRec:
    item_id: str
    path: Path
    size: int
    duration: Optional[float]
    bucket: Optional[str]
    url: str

@dataclass
class FileSig:
    item_id: str
    path: Path
    size: int
    duration: Optional[float]
    duration_bucket: Optional[str]
    phash_digest: Optional[str]
    phash_parts: List[str] = field(default_factory=list)
    audio_fp_digest: Optional[str] = None
    url: str = ""
    errors: List[str] = field(default_factory=list)

LOGGER = logging.getLogger("we_dup")

# ----------------------------- 日志 -----------------------------

def setup_logging(level=logging.INFO, log_file: Optional[str]=None):
    LOGGER.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(fmt)
    LOGGER.addHandler(h)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        LOGGER.addHandler(fh)
    LOGGER.setLevel(level)

# ----------------------------- 通用工具 -----------------------------

@contextlib.contextmanager
def preserve_times(p: Path):
    """保存并在退出时恢复 atime/mtime，保证最终不变。"""
    try:
        st = p.stat()
        at_ns, mt_ns = st.st_atime_ns, st.st_mtime_ns
    except Exception as e:
        LOGGER.debug(f"[times] stat 失败 {p}: {e}")
        at_ns = mt_ns = None
    try:
        yield
    finally:
        if at_ns is not None and mt_ns is not None:
            try:
                os.utime(p, ns=(at_ns, mt_ns), follow_symlinks=True)
                LOGGER.debug(f"[times] 恢复 atime/mtime：{p}")
            except Exception as e:
                LOGGER.warning(f"[times] 恢复 atime/mtime 失败 {p}: {e}")

def run_cmd(cmd: List[str], timeout: int, trace=False) -> Tuple[int, bytes, bytes, float]:
    """运行外部命令，返回 (rc, stdout, stderr, 耗时秒)。"""
    import shlex as _shlex
    t0 = time.time()
    if trace:
        LOGGER.info("[exec] %s (timeout=%ss)", " ".join(_shlex.quote(c) for c in cmd), timeout)
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False, creationflags=flags
        )
        el = time.time() - t0
        if trace:
            first_err = res.stderr.decode("utf-8", "ignore").splitlines()[:1]
            LOGGER.info("[exec] rc=%s in %.2fs%s", res.returncode, el,
                        f" | stderr: {first_err[0]}" if first_err else "")
        return res.returncode, res.stdout, res.stderr, el
    except subprocess.TimeoutExpired as e:
        el = time.time() - t0
        if trace:
            LOGGER.error("[exec] TIMEOUT after %.2fs: %s", el, " ".join(_shlex.quote(c) for c in cmd))
        return 124, b"", str(e).encode("utf-8", "ignore"), el
    except Exception as e:
        el = time.time() - t0
        if trace:
            LOGGER.error("[exec] FAIL after %.2fs: %s", el, e)
        return 1, b"", str(e).encode("utf-8", "ignore"), el

def nearest_bucket(d: Optional[float], mode: str) -> Optional[str]:
    if d is None:
        return None
    if mode == "nearest_0.5":
        b = round(d * 2) / 2.0
    else:
        b = int(round(d))
    return str(b)

def make_we_url(item_id: str) -> str:
    return f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}"

def middle_window_start(duration: Optional[float], window: float, ratio: float) -> float:
    """计算以 ratio 为中心的窗口起点（ratio∈[0,1]；0=开头，0.5=中点，1=结尾）。"""
    if duration is None or duration <= 0:
        return 0.0
    window = min(window, duration)
    center = max(0.0, min(duration, duration * ratio))
    start = center - window / 2.0
    start = max(0.0, min(start, max(0.0, duration - window)))
    return float(start)

# ----------------------------- ffprobe / ffmpeg / fpcalc -----------------------------

def ffprobe_duration(ffprobe_path: str, video: Path, timeout: int, trace: bool) -> Optional[float]:
    with preserve_times(video):
        rc, out, err, _ = run_cmd(
            [ffprobe_path, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            timeout=timeout, trace=trace
        )
    if rc == 0:
        try:
            val = float(out.decode("utf-8", "ignore").strip())
            if math.isfinite(val) and val > 0:
                return val
        except Exception:
            pass
    with preserve_times(video):
        rc2, out2, err2, _ = run_cmd(
            [ffprobe_path, "-v", "error", "-show_entries", "stream=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            timeout=timeout, trace=trace
        )
    if rc2 == 0:
        vals = []
        for line in out2.decode("utf-8", "ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                f = float(line)
                if math.isfinite(f) and f > 0:
                    vals.append(f)
            except Exception:
                pass
        if vals:
            return max(vals)
    return None

def ffmpeg_extract_small_gray_frames_middle(ffmpeg_path: str, video: Path, frames: int,
                                            timeout: int, trace: bool,
                                            duration: Optional[float],
                                            window_seconds: float,
                                            seek_ratio: float) -> List[np.ndarray]:
    """在“中间窗口”内提取最多 N 张 64x64 灰度帧：先 I 帧，不足再均匀采样；必要时放宽窗口。"""
    def run_and_collect(vf: str, limit: int, start_s: float, win_s: float) -> List[np.ndarray]:
        cmd = [
            ffmpeg_path, "-hide_banner", "-v", "error", "-nostdin",
            "-ss", f"{start_s:.3f}",
            "-t",  f"{win_s:.3f}",
            "-i",  str(video),
            "-vf", vf,
            "-vsync", "vfr",
            "-frames:v", str(limit),
            "-f", "rawvideo", "-pix_fmt", "gray", "-"
        ]
        with preserve_times(video):
            rc, out, err, _ = run_cmd(cmd, timeout=timeout, trace=trace)
        if rc != 0 or not out:
            return []
        frame_size = 64 * 64
        n = len(out) // frame_size
        n = min(n, limit)
        buf = out[: n * frame_size]
        arr = np.frombuffer(buf, dtype=np.uint8).reshape((n, 64, 64))
        return [arr[i] for i in range(n)]

    win = window_seconds if duration is None else min(window_seconds, duration)
    start = middle_window_start(duration, win, seek_ratio)

    frames1 = run_and_collect("select='eq(pict_type\\,I)',scale=64:64,format=gray", frames, start, win)
    if frames1:
        return frames1

    fps = max(1.0, min(15.0, frames / max(1.0, win)))
    frames2 = run_and_collect(f"fps={fps},scale=64:64,format=gray", frames, start, win)
    if frames2:
        return frames2

    # 放宽窗口（×2，上限 60s）
    if duration and win < min(duration, 60.0):
        win2 = min(duration, min(60.0, win * 2.0))
        start2 = middle_window_start(duration, win2, seek_ratio)
        frames3 = run_and_collect("select='eq(pict_type\\,I)',scale=64:64,format=gray", frames, start2, win2)
        if frames3:
            return frames3
        fps2 = max(1.0, min(15.0, frames / max(1.0, win2)))
        frames4 = run_and_collect(f"fps={fps2},scale=64:64,format=gray", frames, start2, win2)
        if frames4:
            return frames4

    return []

def compute_phash_from_frames(frames: List[np.ndarray], hash_size: int, prefix: str) -> Tuple[Optional[str], List[str]]:
    parts: List[str] = []
    for i, frame in enumerate(frames):
        try:
            img = Image.fromarray(frame)  # 不传 mode，避免 Pillow 弃用警告
            h = imagehash.phash(img, hash_size=hash_size)
            parts.append(str(h))
        except Exception as e:
            LOGGER.debug("%s pHash 帧 %d 失败: %s", prefix, i, e)
    if not parts:
        return None, []
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return digest, parts

def parse_fpcalc_stdout(stdout: bytes) -> Optional[str]:
    text = stdout.decode("utf-8", "ignore")
    for line in text.splitlines():
        if line.startswith("FINGERPRINT="):
            fp = line.split("=", 1)[1].strip()
            if fp:
                return hashlib.sha1(fp.encode("utf-8")).hexdigest()
    return None

def fpcalc_on_wav(fpcalc_path: str, wav_path: Path, timeout: int, trace: bool) -> Tuple[Optional[str], str]:
    """对 WAV 先尝试 `-raw`，失败再不带 `-raw`。"""
    last_reason = "unknown"
    for cmd in ([fpcalc_path, "-raw", str(wav_path)], [fpcalc_path, str(wav_path)]):
        rc, out, err, _ = run_cmd(cmd, timeout=timeout, trace=trace)
        if rc == 0:
            dig = parse_fpcalc_stdout(out)
            if dig:
                return dig, "ok"
        last_reason = f"rc={rc} {err.decode('utf-8','ignore').splitlines()[:1]}"
    return None, last_reason

def fpcalc_fingerprint_middle(fpcalc_path: str, ffmpeg_path: str, video: Path,
                              duration: Optional[float],
                              audio_window_seconds: int,
                              seek_ratio: float,
                              timeouts: Tuple[int, int],
                              trace: bool) -> Tuple[Optional[str], Optional[str]]:
    """
    从视频“中间窗口”解 WAV（≤audio_window_seconds），再跑 fpcalc。
    使用 TemporaryDirectory + 随机文件名，并加 -y，确保用完即删且无“已存在”冲突。
    """
    win = audio_window_seconds if duration is None else min(audio_window_seconds, int(duration))
    start = middle_window_start(duration, win, seek_ratio)

    with tempfile.TemporaryDirectory(prefix="we_mid_") as tdir:
        wav_path = Path(tdir) / (uuid.uuid4().hex + ".wav")  # 文件尚不存在
        cmd_ff = [
            ffmpeg_path, "-hide_banner", "-v", "error", "-nostdin",
            "-ss", f"{start:.3f}",
            "-t",  f"{win:.3f}",
            "-i",  str(video),
            "-vn", "-ac", "1", "-ar", "11025",
            "-f",  "wav",
            "-y",  str(wav_path)  # 强制覆盖（以防极端残留）
        ]
        with preserve_times(video):
            rc1, out1, err1, _ = run_cmd(cmd_ff, timeout=timeouts[1], trace=trace)

        if rc1 != 0 or not wav_path.exists() or wav_path.stat().st_size == 0:
            return None, f"ffmpeg->wav rc={rc1} {err1.decode('utf-8','ignore').splitlines()[:1]}"

        dig, reason = fpcalc_on_wav(fpcalc_path, wav_path, timeout=timeouts[0], trace=trace)
        if dig:
            return dig, None
        return None, f"fpcalc on wav failed: {reason}"

# ----------------------------- 扫描 -----------------------------

def find_items(workshop_root: Path) -> Dict[str, List[Path]]:
    items: Dict[str, List[Path]] = defaultdict(list)
    if not workshop_root.is_dir():
        raise FileNotFoundError(f"Workshop 路径不存在：{workshop_root}")
    for child in sorted(workshop_root.iterdir()):
        if not child.is_dir():
            continue
        item_id = child.name
        if not re.fullmatch(r"\d+", item_id):
            continue
        for root, _, files in os.walk(child):
            for fn in files:
                if Path(fn).suffix.lower() in VIDEO_EXTS:
                    items[item_id].append(Path(root) / fn)
    return items

# ----------------------------- 阶段1：并行测时长 & 分桶（带进度条） -----------------------------

def measure_duration_one(item_id: str, vp: Path, cfg: Config) -> DurationRec:
    url = make_we_url(item_id)
    try:
        size = vp.stat().st_size
    except Exception:
        size = 0
    dur = ffprobe_duration(cfg.ffprobe_path, vp, cfg.ffprobe_timeout, cfg.trace)
    bucket = nearest_bucket(dur, cfg.duration_rounding)
    if dur is None:
        LOGGER.warning("[dur] %s (%s) 时长获取失败", item_id, vp.name)
    return DurationRec(item_id=item_id, path=vp, size=size, duration=dur, bucket=bucket, url=url)

def stage1_measure_and_bucket(items_map: Dict[str, List[Path]], cfg: Config) -> Dict[str, List[DurationRec]]:
    """并行测时长 → 按分桶聚合，只保留候选桶（≥2个不同 item）。"""
    total_files = sum(len(v) for v in items_map.values())
    LOGGER.info("[S1] 开始：并行测时长（max_workers=%d，files=%d）", cfg.max_workers_stage1, total_files)
    bucket_map: Dict[str, List[DurationRec]] = defaultdict(list)
    futures = []
    with ThreadPoolExecutor(max_workers=cfg.max_workers_stage1) as ex:
        for item_id, paths in items_map.items():
            for vp in paths:
                futures.append(ex.submit(measure_duration_one, item_id, vp, cfg))

        pb = tqdm(total=len(futures), desc="[S1] durations", unit="file", dynamic_ncols=True, disable=not cfg.progress)
        try:
            for fut in as_completed(futures):
                rec = None
                try:
                    rec = fut.result()
                except Exception as e:
                    LOGGER.exception("[S1] 任务异常：%s", e)
                if rec and rec.duration is not None and rec.bucket is not None:
                    bucket_map[rec.bucket].append(rec)
                pb.update(1)
        finally:
            pb.close()

    # 只保留候选桶：至少来自两个不同 item（能产生重复的可能）
    candidate_buckets: Dict[str, List[DurationRec]] = {}
    for b, lst in bucket_map.items():
        item_ids = {r.item_id for r in lst}
        if len(item_ids) >= 2 and len(lst) >= 2:
            candidate_buckets[b] = lst

    LOGGER.info("[S1] 完成：总桶=%d，候选桶=%d（进入阶段2）", len(bucket_map), len(candidate_buckets))
    return candidate_buckets

# ----------------------------- 阶段2：并行取样与签名（带进度条，仅候选桶） -----------------------------

def sign_one(rec: DurationRec, cfg: Config) -> FileSig:
    """对单文件计算 pHash +（可选）音频指纹（均在中段窗口）。"""
    prefix = f"[{rec.item_id}]({rec.path.name})"
    LOGGER.info("%s 签名开始：%s (%.2f MiB)", prefix, str(rec.path), rec.size/1024/1024)

    # pHash（中段窗口）
    phash_digest = None
    phash_parts: List[str] = []
    frames = ffmpeg_extract_small_gray_frames_middle(
        cfg.ffmpeg_path, rec.path, cfg.sample_frames, cfg.ffmpeg_timeout, cfg.trace,
        duration=rec.duration, window_seconds=float(cfg.video_window_seconds), seek_ratio=float(cfg.seek_ratio)
    )
    if not frames:
        LOGGER.warning("%s 帧提取失败（中间窗口）", prefix)
    else:
        phash_digest, phash_parts = compute_phash_from_frames(frames, cfg.phash_size, prefix)
        if not phash_digest:
            LOGGER.warning("%s pHash 计算失败", prefix)

    # 音频指纹（中段窗口；严格模式下才做）
    audio_digest = None
    reason = None
    if cfg.require_both_signatures:
        ad, rsn = fpcalc_fingerprint_middle(
            cfg.fpcalc_path, cfg.ffmpeg_path, rec.path, duration=rec.duration,
            audio_window_seconds=int(cfg.audio_window_seconds),
            seek_ratio=float(cfg.seek_ratio),
            timeouts=(cfg.fpcalc_timeout, cfg.ffmpeg_timeout),
            trace=cfg.trace
        )
        audio_digest, reason = ad, rsn
        if not audio_digest:
            LOGGER.warning("%s 音频指纹获取失败（%s）", prefix, reason)

    fs = FileSig(
        item_id=rec.item_id, path=rec.path, size=rec.size,
        duration=rec.duration, duration_bucket=rec.bucket,
        phash_digest=phash_digest, phash_parts=phash_parts,
        audio_fp_digest=audio_digest, url=rec.url
    )
    if cfg.require_both_signatures:
        if not (rec.bucket and phash_digest and audio_digest):
            fs.errors.append("incomplete_signature")
    else:
        if not (rec.bucket and phash_digest):
            fs.errors.append("no_visual_signature")

    LOGGER.info("%s 签名完成", prefix)
    return fs

def stage2_sign_candidates(candidate_buckets: Dict[str, List[DurationRec]], cfg: Config) -> List[FileSig]:
    """并行对候选桶内所有文件计算签名（pHash + 音频）。"""
    total_candidates = sum(len(v) for v in candidate_buckets.values())
    LOGGER.info("[S2] 开始：候选桶签名（max_workers=%d，files=%d）", cfg.max_workers_stage2, total_candidates)
    filesigs: List[FileSig] = []
    futures = []
    with ThreadPoolExecutor(max_workers=cfg.max_workers_stage2) as ex:
        for bucket, recs in candidate_buckets.items():
            for rec in recs:
                futures.append(ex.submit(sign_one, rec, cfg))

        pb = tqdm(total=len(futures), desc="[S2] signatures", unit="file", dynamic_ncols=True, disable=not cfg.progress)
        try:
            for fut in as_completed(futures):
                try:
                    fs = fut.result()
                    filesigs.append(fs)
                except Exception as e:
                    LOGGER.exception("[S2] 签名任务异常：%s", e)
                pb.update(1)
        finally:
            pb.close()

    LOGGER.info("[S2] 完成：已计算签名文件数=%d", len(filesigs))
    return filesigs

# ----------------------------- 导出 -----------------------------

def export_csv(groups: List[List[str]], out_csv: Path):
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        max_cols = max((len(g) for g in groups), default=0)
        header = ["序号"] + [f"链接{i}" for i in range(1, max_cols+1)]
        w.writerow(header)
        for idx, g in enumerate(groups, 1):
            row = [idx] + g + [""] * (max_cols - len(g))
            w.writerow(row)

def export_xlsx(groups: List[List[str]], out_xlsx: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = "duplicates"
    max_cols = max((len(g) for g in groups), default=0)
    ws.cell(row=1, column=1, value="序号")
    for c in range(2, 2 + max_cols):
        ws.cell(row=1, column=c, value=f"链接{c-1}")
    for r, g in enumerate(groups, start=2):
        ws.cell(row=r, column=1, value=(r-1))
        for j, url in enumerate(g, start=2):
            cell = ws.cell(row=r, column=j, value=url)
            try:
                cell.hyperlink = url
                cell.font = Font(color="0000FF", underline="single")
            except Exception:
                pass
    wb.save(out_xlsx)

# ----------------------------- 主流程 -----------------------------

def load_config(path: Optional[Path]) -> Config:
    data = {}
    if path and path.exists():
        with path.open("rb") as f:
            data = tomllib.load(f)
    def get(k, default): return data.get(k, default)
    return Config(
        workshop_root = get("workshop_root", ""),
        output_dir    = get("output_dir", "output"),
        ffmpeg_path   = get("ffmpeg_path", "ffmpeg"),
        ffprobe_path  = get("ffprobe_path", "ffprobe"),
        fpcalc_path   = get("fpcalc_path", "fpcalc"),

        sample_frames = int(get("sample_frames", 12)),
        phash_size    = int(get("phash_size", 8)),
        audio_window_seconds = int(get("audio_window_seconds", 120)),
        video_window_seconds = int(get("video_window_seconds", 20)),
        seek_ratio = float(get("seek_ratio", 0.5)),

        duration_rounding = get("duration_rounding", "int"),
        require_both_signatures = bool(get("require_both_signatures", True)),

        max_workers_stage1 = int(get("max_workers_stage1", 8)),
        max_workers_stage2 = int(get("max_workers_stage2", 6)),
        ffprobe_timeout = int(get("ffprobe_timeout", 25)),
        ffmpeg_timeout  = int(get("ffmpeg_timeout", 45)),
        fpcalc_timeout  = int(get("fpcalc_timeout", 35)),
        log_file        = get("log_file", None),
        progress        = bool(get("progress", True)),
    )

def main():
    ap = argparse.ArgumentParser(description="Wallpaper Engine 重复视频检测（两阶段并行 + 中段取样 + 进度条）")
    ap.add_argument("-c", "--config", type=Path, default=None, help="config.toml")
    ap.add_argument("--verbose", action="store_true", help="DEBUG 级别日志")
    ap.add_argument("--trace", action="store_true", help="打印外部命令与耗时/首行错误")
    ap.add_argument("--no-progress", action="store_true", help="关闭进度条显示")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cfg.verbose = bool(args.verbose)
    cfg.trace   = bool(args.trace)
    if args.no_progress:
        cfg.progress = False

    setup_logging(logging.DEBUG if cfg.verbose else logging.INFO, cfg.log_file)

    root = Path(cfg.workshop_root).resolve()
    out_dir = Path(cfg.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    LOGGER.info("[INFO] Scanning workshop: %s", root)
    items_map = find_items(root)
    LOGGER.info("[INFO] Found %d items with candidate video files", len(items_map))

    # 阶段1：并行测时长 & 分桶（进度条）
    candidate_buckets = stage1_measure_and_bucket(items_map, cfg)

    if not candidate_buckets:
        LOGGER.info("[INFO] 没有候选时长分桶（>=2个不同条目），直接结束")
        LOGGER.info("[DONE] 任务完成（无重复候选）")
        return

    # 阶段2：仅对候选桶并行计算签名（进度条）
    filesigs = stage2_sign_candidates(candidate_buckets, cfg)

    # 过滤可参与最终分组的文件
    eligible: List[FileSig] = []
    for fs in filesigs:
        if cfg.require_both_signatures:
            if fs.duration_bucket and fs.phash_digest and fs.audio_fp_digest:
                eligible.append(fs)
        else:
            if fs.duration_bucket and fs.phash_digest:
                eligible.append(fs)
    LOGGER.info("[INFO] 可参与最终分组的文件：%d / %d（候选）", len(eligible), len(filesigs))

    # 最终分组键：时长分桶 + 视觉 +（可选）音频
    def gkey(fs: FileSig):
        return (fs.duration_bucket, fs.phash_digest, fs.audio_fp_digest if cfg.require_both_signatures else None)

    groups_map: Dict[Tuple[str, str, Optional[str]], List[FileSig]] = defaultdict(list)
    for fs in eligible:
        groups_map[gkey(fs)].append(fs)

    # 组织导出：同组内去重到“不同 item”，并按该 item 命中的最大文件大小降序
    duplicate_groups_urls: List[List[str]] = []
    kept_groups = 0
    for key, group in groups_map.items():
        item_to_bestsize: Dict[str, int] = {}
        item_to_url: Dict[str, str] = {}
        for fs in group:
            if fs.item_id not in item_to_bestsize or fs.size > item_to_bestsize[fs.item_id]:
                item_to_bestsize[fs.item_id] = fs.size
                item_to_url[fs.item_id] = fs.url
        if len(item_to_bestsize) <= 1:
            continue
        ordered = sorted(item_to_bestsize.items(), key=lambda kv: kv[1], reverse=True)
        urls = [item_to_url[iid] for iid, _ in ordered]
        duplicate_groups_urls.append(urls)
        kept_groups += 1
        LOGGER.info("[dup] 组 %s 大小=%d → %s", key, len(urls), [u.rsplit('=',1)[-1] for u in urls])

    # 导出
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_csv = out_dir / f"duplicates_{ts}.csv"
    out_xlsx = out_dir / f"duplicates_{ts}.xlsx"
    if duplicate_groups_urls:
        export_csv(duplicate_groups_urls, out_csv)
        export_xlsx(duplicate_groups_urls, out_xlsx)
        LOGGER.info("[OUT] CSV : %s", out_csv)
        LOGGER.info("[OUT] XLSX: %s", out_xlsx)
    else:
        LOGGER.info("[INFO] 未发现重复组（满足当前判定条件）")

    LOGGER.info("[DONE] 任务完成，重复组数：%d", kept_groups)

if __name__ == "__main__":
    main()