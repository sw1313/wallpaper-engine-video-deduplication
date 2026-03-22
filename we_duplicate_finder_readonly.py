#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Wallpaper Engine (431960) 重复视频检测
- 严格只读（恢复 atime/mtime）
- 两阶段逻辑保留，但通过管线并行把“时长分桶”和“签名”重叠执行
- 中段取样（避免片头片尾黑屏）
- 临时缓存“匹完就删”（TemporaryDirectory 作用域内清理）
- 导出 CSV/XLSX：每组一行，组内链接按该条目命中的最大文件大小降序
- 进度条：整体仍有 S1/S2 两个 tqdm

现在的“重复判定”逻辑（与原版不同）：
  1. 先按“时长分桶 +（可选）音频指纹”做粗分桶；
  2. 在同一个粗桶内，使用 phash_parts 做模糊匹配：
     - 逐帧汉明距离归一化到 64 位基准
     - 组合分数 = 截尾均值 / (1 + 标准差)
     - 同内容不同分辨率：高标准差→分数低→容易匹配
     - 不同内容同模板：低标准差→分数不被压低→不易误匹配
     - 若分数 <= phash_distance_threshold（默认 1.0），就 union 成同一组
"""

import argparse
import contextlib
import csv
import hashlib
import io
import logging
import math
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from functools import lru_cache  # NEW: 缓存 hex->ImageHash

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

    # —— pHash 模糊匹配参数 ——
    phash_distance_threshold: float = 0.6  # 组合距离分阈值（截尾均值/(1+标准差)），越小越严格

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

def _utf8_stdout():
    """返回以 UTF-8 输出的流，避免 Windows 下文件名含 emoji 等字符时 gbk 编码报错。"""
    if hasattr(sys.stdout, "buffer"):
        return io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
        )
    return sys.stdout


def setup_logging(level=logging.INFO, log_file: Optional[str] = None, file_mode: str = "a"):
    # 重要：先 close 旧 handler，避免 Windows 上文件句柄一直占用
    for h in list(LOGGER.handlers):
        try:
            h.close()
        except Exception:
            pass
    LOGGER.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    h = logging.StreamHandler(_utf8_stdout())
    h.setFormatter(fmt)
    LOGGER.addHandler(h)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8", mode=file_mode)
        fh.setFormatter(fmt)
        LOGGER.addHandler(fh)
    LOGGER.setLevel(level)

# ----------------------------- 断点续跑缓存（sqlite） -----------------------------

def _cfg_visual_hash(cfg: "Config") -> str:
    data = {
        "sample_frames": int(cfg.sample_frames),
        "phash_size": int(cfg.phash_size),
        "video_window_seconds": float(cfg.video_window_seconds),
        "seek_ratio": float(cfg.seek_ratio),
    }
    return hashlib.sha1(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def _cfg_audio_hash(cfg: "Config") -> str:
    data = {
        "audio_window_seconds": int(cfg.audio_window_seconds),
        "seek_ratio": float(cfg.seek_ratio),
        "fpcalc_path": str(cfg.fpcalc_path),
        "ffmpeg_path": str(cfg.ffmpeg_path),
    }
    return hashlib.sha1(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


class SigCache:
    """
    sqlite 缓存：
    - duration：按文件 path+size+mtime_ns 缓存（跨运行复用）
    - signatures：视觉签名按 visual_cfg_hash；音频签名按 audio_cfg_hash
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.lock = Lock()
        self._write_count = 0
        self._checkpoint_interval = 50
        self.conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=FULL;")
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS file_cache (
                  path TEXT PRIMARY KEY,
                  size INTEGER NOT NULL,
                  mtime_ns INTEGER NOT NULL,
                  item_id TEXT,
                  url TEXT,
                  duration REAL,
                  phash_digest TEXT,
                  phash_parts_json TEXT,
                  visual_cfg_hash TEXT,
                  audio_fp_digest TEXT,
                  audio_cfg_hash TEXT,
                  color_histogram_json TEXT,
                  updated_ts INTEGER
                );
                """
            )

    def _maybe_checkpoint(self):
        self._write_count += 1
        if self._write_count % self._checkpoint_interval == 0:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            except Exception:
                pass

    def close(self):
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass

    @staticmethod
    def _key(p: Path) -> str:
        try:
            return str(p.resolve())
        except Exception:
            return str(p)

    def delete_path(self, p: Path) -> None:
        """删除已不存在文件在缓存中的记录，避免库持续膨胀。"""
        key = self._key(p)
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM file_cache WHERE path=?", (key,))

    def prune_missing(self) -> int:
        """删除缓存中路径已不存在的记录，返回删除条数。启动时调用一次，避免已删文件参与筛重。"""
        with self.lock:
            cur = self.conn.execute("SELECT path FROM file_cache")
            rows = cur.fetchall()
        deleted = 0
        for (key,) in rows:
            try:
                if not Path(key).exists():
                    with self.lock, self.conn:
                        self.conn.execute("DELETE FROM file_cache WHERE path=?", (key,))
                    deleted += 1
            except Exception:
                pass
        return deleted

    def get_row(self, p: Path, size: int, mtime_ns: int) -> Optional[Dict[str, object]]:
        key = self._key(p)
        with self.lock:
            cur = self.conn.execute(
                "SELECT size,mtime_ns,item_id,url,duration,phash_digest,phash_parts_json,visual_cfg_hash,audio_fp_digest,audio_cfg_hash,color_histogram_json "
                "FROM file_cache WHERE path=?",
                (key,),
            )
            row = cur.fetchone()
        if not row:
            return None
        if int(row[0]) != int(size) or int(row[1]) != int(mtime_ns):
            return None
        return {
            "item_id": row[2],
            "url": row[3],
            "duration": row[4],
            "phash_digest": row[5],
            "phash_parts_json": row[6],
            "visual_cfg_hash": row[7],
            "audio_fp_digest": row[8],
            "audio_cfg_hash": row[9],
            "color_histogram_json": row[10],
        }

    def upsert_duration(self, p: Path, size: int, mtime_ns: int, item_id: str, url: str, duration: Optional[float]):
        key = self._key(p)
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO file_cache(path,size,mtime_ns,item_id,url,duration,updated_ts)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                  size=excluded.size,
                  mtime_ns=excluded.mtime_ns,
                  item_id=excluded.item_id,
                  url=excluded.url,
                  duration=excluded.duration,
                  updated_ts=excluded.updated_ts
                """,
                (key, int(size), int(mtime_ns), item_id, url, duration, int(time.time())),
            )
        self._maybe_checkpoint()

    def upsert_visual(self, p: Path, size: int, mtime_ns: int, item_id: str, url: str,
                      phash_digest: Optional[str], phash_parts: List[str], visual_cfg_hash: str):
        key = self._key(p)
        parts_json = json.dumps(phash_parts, ensure_ascii=False)
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO file_cache(path,size,mtime_ns,item_id,url,phash_digest,phash_parts_json,visual_cfg_hash,updated_ts)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                  size=excluded.size,
                  mtime_ns=excluded.mtime_ns,
                  item_id=excluded.item_id,
                  url=excluded.url,
                  phash_digest=excluded.phash_digest,
                  phash_parts_json=excluded.phash_parts_json,
                  visual_cfg_hash=excluded.visual_cfg_hash,
                  updated_ts=excluded.updated_ts
                """,
                (key, int(size), int(mtime_ns), item_id, url, phash_digest, parts_json, visual_cfg_hash, int(time.time())),
            )
        self._maybe_checkpoint()

    def upsert_audio(self, p: Path, size: int, mtime_ns: int, item_id: str, url: str,
                     audio_fp_digest: Optional[str], audio_cfg_hash: str):
        key = self._key(p)
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO file_cache(path,size,mtime_ns,item_id,url,audio_fp_digest,audio_cfg_hash,updated_ts)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                  size=excluded.size,
                  mtime_ns=excluded.mtime_ns,
                  item_id=excluded.item_id,
                  url=excluded.url,
                  audio_fp_digest=excluded.audio_fp_digest,
                  audio_cfg_hash=excluded.audio_cfg_hash,
                  updated_ts=excluded.updated_ts
                """,
                (key, int(size), int(mtime_ns), item_id, url, audio_fp_digest, audio_cfg_hash, int(time.time())),
            )
        self._maybe_checkpoint()


_CACHE: Optional[SigCache] = None

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
    t0 = time.time()
    if trace:
        LOGGER.info("[exec] %s (timeout=%ss)", " ".join(shlex.quote(c) for c in cmd), timeout)
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
            LOGGER.error("[exec] TIMEOUT after %.2fs: %s", el, " ".join(shlex.quote(c) for c in cmd))
        return 124, b"", str(e).encode("utf-8", "ignore"), el
    except Exception as e:
        el = time.time() - t0
        if trace:
            LOGGER.error("[exec] FAIL after %.2fs: %s", el, e)
        return 1, b"", str(e).encode("utf-8", "ignore"), el

def nearest_bucket(d: Optional[float], mode: str) -> Optional[str]:
    if d is None:
        return None
    m = (mode or "").strip().lower()
    if m in {"nearest_0.5", "0.5", "half"}:
        b = round(d * 2) / 2.0
    else:
        # 兼容历史写法：
        # - "int"：整数
        # - "nearest_1.0"：最接近 1 秒（等价于整数）
        # - 其他/空：回退到整数
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
    """
    在“中间窗口”内提取最多 N 张 64x64 灰度帧。

    重要：为了跨编码/跨分辨率的稳定性，这里**优先使用基于时间轴的均匀采样（fps=...）**，
    而不是优先 I 帧。原因是 I 帧位置强依赖编码器/GOP 设置，不同转码版本会抽到不同时间点，
    对“运动多/细节多”的 3D 视频（如 Blender 输出）会导致 pHash 大幅漂移，即使内容完全一致。
    """
    def run_and_collect(vf: str, limit: int, start_s: float, win_s: float) -> List[np.ndarray]:
        cmd = [
            ffmpeg_path, "-hide_banner", "-v", "error", "-nostdin",
            "-hwaccel", "auto",  # GPU 可用时用硬解码，不可用时自动退回 CPU
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

    fps = max(1.0, min(15.0, frames / max(1.0, win)))
    frames1 = run_and_collect(f"fps={fps},scale=64:64,format=gray", frames, start, win)
    if frames1:
        return frames1

    # 兜底：fps 抽帧失败时再尝试 I 帧（某些坏文件 fps 可能拿不到任何帧）
    frames2 = run_and_collect("select='eq(pict_type\\,I)',scale=64:64,format=gray", frames, start, win)
    if frames2:
        return frames2

    # 放宽窗口（×2，上限 60s）
    if duration and win < min(duration, 60.0):
        win2 = min(duration, min(60.0, win * 2.0))
        start2 = middle_window_start(duration, win2, seek_ratio)
        fps2 = max(1.0, min(15.0, frames / max(1.0, win2)))
        frames3 = run_and_collect(f"fps={fps2},scale=64:64,format=gray", frames, start2, win2)
        if frames3:
            return frames3

        frames4 = run_and_collect("select='eq(pict_type\\,I)',scale=64:64,format=gray", frames, start2, win2)
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
            "-hwaccel", "auto",  # 同样尝试硬解码
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

# ----------------------------- 阶段1：测时长（保留旧实现，当前主流程不直接使用） -----------------------------

def measure_duration_one(item_id: str, vp: Path, cfg: Config) -> DurationRec:
    url = make_we_url(item_id)
    if not vp.exists():
        LOGGER.debug("[skip] 文件已删除，不参与筛重：%s", vp)
        if _CACHE:
            try:
                _CACHE.delete_path(vp)
            except Exception:
                pass
        return DurationRec(item_id=item_id, path=vp, size=0, duration=None, bucket=None, url=url)
    try:
        st = vp.stat()
        size = st.st_size
        mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
    except Exception:
        size = 0
        mtime_ns = 0

    # 断点续跑：优先用缓存的 duration（仅当文件仍存在且未变），bucket 每次按当前配置重算
    if _CACHE and size and mtime_ns:
        row = _CACHE.get_row(vp, size, mtime_ns)
        if row and row.get("duration") is not None:
            if vp.exists():
                try:
                    st2 = vp.stat()
                    if st2.st_size == size and getattr(st2, "st_mtime_ns", int(st2.st_mtime * 1e9)) == mtime_ns:
                        dur = float(row["duration"]) if row["duration"] is not None else None
                        bucket = nearest_bucket(dur, cfg.duration_rounding)
                        return DurationRec(item_id=item_id, path=vp, size=size, duration=dur, bucket=bucket, url=url)
                except Exception:
                    pass

    dur = ffprobe_duration(cfg.ffprobe_path, vp, cfg.ffprobe_timeout, cfg.trace)
    bucket = nearest_bucket(dur, cfg.duration_rounding)
    if dur is None:
        LOGGER.warning("[dur] %s (%s) 时长获取失败", item_id, vp.name)

    if _CACHE and size and mtime_ns:
        try:
            _CACHE.upsert_duration(vp, size, mtime_ns, item_id, url, dur)
        except Exception as e:
            LOGGER.debug("[cache] upsert_duration failed: %s", e)
    return DurationRec(item_id=item_id, path=vp, size=size, duration=dur, bucket=bucket, url=url)

def stage1_measure_and_bucket(items_map: Dict[str, List[Path]], cfg: Config) -> Dict[str, List[DurationRec]]:
    """旧版：先把所有时长算完再返回候选桶（主流程现改为管线式，仅保留以备需要）"""
    total_files = sum(len(v) for v in items_map.values())
    LOGGER.info("[S1] 开始：并行测时长（max_workers=%d，files=%d）", cfg.max_workers_stage1, total_files)
    bucket_map: Dict[str, List[DurationRec]] = defaultdict(list)
    futures = []
    with ThreadPoolExecutor(max_workers=cfg.max_workers_stage1) as ex:
        for item_id, paths in items_map.items():
            for vp in paths:
                futures.append(ex.submit(measure_duration_one, item_id, vp, cfg))

        pb = tqdm(total=len(futures), desc="[S1] durations", unit="file",
                  dynamic_ncols=True, disable=not cfg.progress)
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

    candidate_buckets: Dict[str, List[DurationRec]] = {}
    for b, lst in bucket_map.items():
        item_ids = {r.item_id for r in lst}
        if len(item_ids) >= 2 and len(lst) >= 2:
            candidate_buckets[b] = lst

    LOGGER.info("[S1] 完成：总桶=%d，候选桶=%d（进入阶段2）", len(bucket_map), len(candidate_buckets))
    return candidate_buckets

# ----------------------------- 阶段2：签名（函数保留，主流程会在管线模式下调用） -----------------------------

def sign_one(rec: DurationRec, cfg: Config) -> FileSig:
    """对单文件计算 pHash +（可选）音频指纹（均在中段窗口）。"""
    if not rec.path.exists():
        LOGGER.debug("[skip] 文件已删除，不参与筛重：%s", rec.path)
        if _CACHE:
            try:
                _CACHE.delete_path(rec.path)
            except Exception:
                pass
        return FileSig(
            item_id=rec.item_id, path=rec.path, size=rec.size,
            duration=rec.duration, duration_bucket=rec.bucket,
            phash_digest=None, phash_parts=[], audio_fp_digest=None, url=rec.url,
            errors=["file_deleted"],
        )
    prefix = f"[{rec.item_id}]({rec.path.name})"
    LOGGER.info("%s 签名开始：%s (%.2f MiB)", prefix, str(rec.path), rec.size/1024/1024)

    # 尝试走缓存（仅当文件仍存在；视觉/音频按各自 cfg hash 命中）
    st = None
    try:
        st = rec.path.stat()
        mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
    except Exception:
        mtime_ns = 0

    visual_hash = _cfg_visual_hash(cfg)
    audio_hash = _cfg_audio_hash(cfg)
    cached_phash_digest = None
    cached_phash_parts: List[str] = []
    cached_audio = None

    if _CACHE and rec.size and mtime_ns and rec.path.exists():
        row = _CACHE.get_row(rec.path, rec.size, mtime_ns)
        if row:
            if row.get("visual_cfg_hash") == visual_hash and row.get("phash_digest") and row.get("phash_parts_json"):
                cached_phash_digest = str(row.get("phash_digest") or "")
                try:
                    cached_phash_parts = json.loads(str(row.get("phash_parts_json") or "[]")) or []
                except Exception:
                    cached_phash_parts = []
            if row.get("audio_cfg_hash") == audio_hash and row.get("audio_fp_digest"):
                cached_audio = str(row.get("audio_fp_digest") or "")

    if cached_phash_digest and cached_phash_parts and (not cfg.require_both_signatures or cached_audio):
        if rec.path.exists():
            fs = FileSig(
                item_id=rec.item_id, path=rec.path, size=rec.size,
                duration=rec.duration, duration_bucket=rec.bucket,
                phash_digest=cached_phash_digest, phash_parts=cached_phash_parts,
                audio_fp_digest=cached_audio, url=rec.url
            )
            LOGGER.info("%s 命中缓存，跳过签名计算", prefix)
            LOGGER.info("%s 签名完成", prefix)
            return fs

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

    if _CACHE and rec.size and mtime_ns and phash_parts:
        try:
            _CACHE.upsert_visual(rec.path, rec.size, mtime_ns, rec.item_id, rec.url, phash_digest, phash_parts, visual_hash)
        except Exception as e:
            LOGGER.debug("[cache] upsert_visual failed: %s", e)

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
        if _CACHE and rec.size and mtime_ns:
            try:
                _CACHE.upsert_audio(rec.path, rec.size, mtime_ns, rec.item_id, rec.url, audio_digest, audio_hash)
            except Exception as e:
                LOGGER.debug("[cache] upsert_audio failed: %s", e)

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
    """旧版：拿到完整候选桶后再统一并行签名（主流程现改为管线式，仅保留以备需要）"""
    total_candidates = sum(len(v) for v in candidate_buckets.values())
    LOGGER.info("[S2] 开始：候选桶签名（max_workers=%d，files=%d）", cfg.max_workers_stage2, total_candidates)
    filesigs: List[FileSig] = []
    futures = []
    with ThreadPoolExecutor(max_workers=cfg.max_workers_stage2) as ex:
        for bucket, recs in candidate_buckets.items():
            for rec in recs:
                futures.append(ex.submit(sign_one, rec, cfg))

        pb = tqdm(total=len(futures), desc="[S2] signatures", unit="file",
                  dynamic_ncols=True, disable=not cfg.progress)
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

# ----------------------------- 新：阶段1+阶段2 管线并行 -----------------------------

def stage1_and_stage2_pipelined(items_map: Dict[str, List[Path]], cfg: Config) -> List[FileSig]:
    """
    管线式执行：
      - 线程池 A：测时长 + 分桶
      - 线程池 B：一旦某个桶变成候选桶，就立刻对该桶内文件提交签名任务
      - 这样 ffprobe / ffmpeg / fpcalc 可以同时跑，多核 / GPU 利用率更高
    """
    total_files = sum(len(v) for v in items_map.values())
    LOGGER.info("[PIPE] 开始：阶段1+阶段2 管线并行（dur_workers=%d, sig_workers=%d, files=%d）",
                cfg.max_workers_stage1, cfg.max_workers_stage2, total_files)

    bucket_map: Dict[str, List[DurationRec]] = defaultdict(list)
    bucket_items: Dict[str, set] = defaultdict(set)
    candidate_buckets: set = set()
    sig_futures = []
    filesigs: List[FileSig] = []
    lock = Lock()

    with ThreadPoolExecutor(max_workers=cfg.max_workers_stage1) as ex_dur, \
         ThreadPoolExecutor(max_workers=cfg.max_workers_stage2) as ex_sig:

        # 提交所有“测时长”任务
        dur_futs = []
        for item_id, paths in items_map.items():
            for vp in paths:
                dur_futs.append(ex_dur.submit(measure_duration_one, item_id, vp, cfg))

        pb1 = tqdm(total=len(dur_futs), desc="[S1] durations", unit="file",
                   dynamic_ncols=True, disable=not cfg.progress)
        try:
            for fut in as_completed(dur_futs):
                rec = None
                try:
                    rec = fut.result()
                except Exception as e:
                    LOGGER.exception("[PIPE-S1] 任务异常：%s", e)

                if rec and rec.duration is not None and rec.bucket is not None:
                    to_sign: List[DurationRec] = []
                    # 更新桶信息 & 判断是否成为候选桶
                    with lock:
                        bucket_map[rec.bucket].append(rec)
                        items = bucket_items[rec.bucket]
                        if rec.item_id not in items:
                            items.add(rec.item_id)
                        if rec.bucket not in candidate_buckets:
                            # 第一次成为候选桶：这个桶里所有已有文件都要签名
                            if len(bucket_map[rec.bucket]) >= 2 and len(items) >= 2:
                                candidate_buckets.add(rec.bucket)
                                to_sign = list(bucket_map[rec.bucket])
                        else:
                            # 已经是候选桶了，新文件直接签名
                            to_sign = [rec]

                    # 在锁外提交签名任务，避免阻塞其他时长任务
                    for r2 in to_sign:
                        sig_futures.append(ex_sig.submit(sign_one, r2, cfg))

                pb1.update(1)
        finally:
            pb1.close()

        LOGGER.info("[PIPE] 阶段1结束，候选桶=%d，已触发签名任务=%d", len(candidate_buckets), len(sig_futures))

        # 等待所有签名任务完成（此时阶段2可能已经完成了一部分）
        pb2 = tqdm(total=len(sig_futures), desc="[S2] signatures", unit="file",
                   dynamic_ncols=True, disable=not cfg.progress)
        try:
            for sf in as_completed(sig_futures):
                try:
                    fs = sf.result()
                    filesigs.append(fs)
                except Exception as e:
                    LOGGER.exception("[PIPE-S2] 签名任务异常：%s", e)
                pb2.update(1)
        finally:
            pb2.close()

    LOGGER.info("[PIPE] 完成：签名文件数=%d", len(filesigs))
    return filesigs

# ----------------------------- pHash 模糊匹配工具 -----------------------------

@lru_cache(maxsize=8192)
def _hex_to_hash_cached(h: str) -> imagehash.ImageHash:
    """缓存 hex->ImageHash，避免重复解析带来的开销。"""
    return imagehash.hex_to_hash(h)

def phash_distance(fs1: FileSig, fs2: FileSig) -> float:
    """
    组合分数 = 截尾均值 / (1 + 标准差)，附带截尾均值上限保护。

    - 逐帧距离归一化到 8x8（64 位）基准
    - 截尾均值：去掉最高 10%，降低编码差异产生的异常帧影响
    - 截尾均值上限（_TM_CAP=4.0）：如果截尾均值本身就很高，
      说明"大部分帧都有明显差异"，此时即使标准差高也不应匹配
      （防止"一半帧相同、一半帧不同"的换装/换角色视频被误判）
    - 除以 (1 + std)：对截尾均值较低的情况，
      同内容不同编码→高 std→分数更低→更容易匹配
    """
    _BASELINE_BITS = 64.0
    _TM_CAP = 4.0

    p1 = fs1.phash_parts
    p2 = fs2.phash_parts
    if not p1 or not p2:
        return float("inf")
    n = min(len(p1), len(p2))
    if n == 0:
        return float("inf")

    dists: List[float] = []
    for i in range(n):
        try:
            h1 = _hex_to_hash_cached(p1[i])
            h2 = _hex_to_hash_cached(p2[i])
            raw_d = h1 - h2
            bits = h1.hash.size
            norm_d = raw_d * _BASELINE_BITS / max(bits, 1)
            dists.append(norm_d)
        except Exception as e:
            LOGGER.debug("[phash_distance] 计算失败：%s", e)
            return float("inf")

    dists.sort()
    trim = max(1, n // 10)
    trimmed = dists[: n - trim]
    if not trimmed:
        return float("inf")
    tm = sum(trimmed) / len(trimmed)

    if tm > _TM_CAP:
        return float("inf")

    if n < 6:
        return tm

    mean_all = sum(dists) / n
    variance = sum((d - mean_all) ** 2 for d in dists) / (n - 1)
    std = math.sqrt(variance)

    return tm / (1.0 + std)


def cluster_bucket_by_phash(fs_list: List[FileSig], threshold: float) -> List[List[FileSig]]:
    """
    在同一个“粗桶”（时长 + 可选音频）里，对文件做 pHash 模糊聚类：
      - 任意两文件的 pHash 平均距离 <= threshold 就 union；
      - 使用并查集得到最终若干子组；
      - 只返回大小 >= 2 的子组。
    """
    n = len(fs_list)
    if n < 2:
        return []

    parent = list(range(n))
    rank = [0] * n

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            parent[ra] = rb
        elif rank[ra] > rank[rb]:
            parent[rb] = ra
        else:
            parent[rb] = ra
            rank[ra] += 1

    # 两两比较并 union
    for i in range(n):
        for j in range(i + 1, n):
            d = phash_distance(fs_list[i], fs_list[j])
            if d <= threshold:
                union(i, j)

    clusters_dict: Dict[int, List[FileSig]] = defaultdict(list)
    for idx in range(n):
        root = find(idx)
        clusters_dict[root].append(fs_list[idx])

    # 只保留至少 2 个文件的子组
    return [lst for lst in clusters_dict.values() if len(lst) >= 2]

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

        phash_distance_threshold = float(get("phash_distance_threshold", 0.6)),

        max_workers_stage1 = int(get("max_workers_stage1", 8)),
        max_workers_stage2 = int(get("max_workers_stage2", 6)),
        ffprobe_timeout = int(get("ffprobe_timeout", 25)),
        ffmpeg_timeout  = int(get("ffmpeg_timeout", 45)),
        fpcalc_timeout  = int(get("fpcalc_timeout", 35)),
        log_file        = get("log_file", None),
        progress        = bool(get("progress", True)),
    )

def main():
    ap = argparse.ArgumentParser(
        description="Wallpaper Engine 重复视频检测（管线并行 + pHash 模糊匹配）"
    )
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

    # 空字符串视为不写日志文件
    if cfg.log_file is not None and str(cfg.log_file).strip() == "":
        cfg.log_file = None

    # 输出目录先创建，用来放 state/cache
    root = Path(cfg.workshop_root).resolve()
    out_dir = Path(cfg.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 运行状态：决定 log 文件是重建还是追加
    state_path = out_dir / "we_dedup_state.json"
    prev_status = None
    if state_path.exists():
        try:
            prev = json.loads(state_path.read_text(encoding="utf-8"))
            prev_status = prev.get("status")
        except Exception:
            prev_status = None
    file_mode = "w" if prev_status == "completed" else "a"

    setup_logging(logging.DEBUG if cfg.verbose else logging.INFO, cfg.log_file, file_mode=file_mode)

    # 初始化缓存：记住每个文件的时长/视觉/音频特征，再次跑只计算新文件，用缓存参与比对
    global _CACHE
    try:
        _CACHE = SigCache(out_dir / "we_dedup_cache.sqlite3")
        pruned = _CACHE.prune_missing()
        if pruned:
            LOGGER.info("[cache] 已清理 %d 条已删除文件的缓存记录，这些文件将不参与筛重。", pruned)
        LOGGER.info("[cache] 特征值缓存已加载，已计算过的文件将直接使用缓存，仅对新文件计算特征值并参与比对。")
    except Exception as e:
        _CACHE = None
        LOGGER.warning("[cache] 初始化失败（将不启用断点续跑）：%s", e)

    # 写入 running 状态（如果异常退出，下次会自动当作未完成并续跑）
    try:
        state_path.write_text(json.dumps({
            "status": "running",
            "start_ts": int(time.time()),
            "pid": os.getpid(),
            "log_file": cfg.log_file,
            "log_mode": file_mode,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    LOGGER.info("[CFG] phash_distance_threshold=%.2f（数值越小越严格）", cfg.phash_distance_threshold)

    exit_code = 0
    try:
        LOGGER.info("[INFO] Scanning workshop: %s", root)
        items_map = find_items(root)
        LOGGER.info("[INFO] Found %d items with candidate video files", len(items_map))

        # 新主流程：阶段1 + 阶段2 管线并行（带缓存自动跳过）
        filesigs = stage1_and_stage2_pipelined(items_map, cfg)

        # 过滤可参与最终分组的文件
        eligible: List[FileSig] = []
        for fs in filesigs:
            if cfg.require_both_signatures:
                if fs.duration_bucket and fs.phash_parts and fs.audio_fp_digest:
                    eligible.append(fs)
            else:
                if fs.duration_bucket and fs.phash_parts:
                    eligible.append(fs)
        LOGGER.info("[INFO] 可参与最终分组的文件：%d / %d（候选）", len(eligible), len(filesigs))

        if not eligible:
            LOGGER.info("[INFO] 无可用签名文件，结束。")
            return

        # 粗分桶：按“时长分桶 +（可选）音频指纹”
        def bucket_key(fs: FileSig):
            if cfg.require_both_signatures:
                return (fs.duration_bucket, fs.audio_fp_digest)
            else:
                return (fs.duration_bucket,)

        bucket_groups: Dict[Tuple, List[FileSig]] = defaultdict(list)
        for fs in eligible:
            bucket_groups[bucket_key(fs)].append(fs)

        LOGGER.info(
            "[INFO] 粗分桶数量：%d（按时长%s）",
            len(bucket_groups),
            "+音频" if cfg.require_both_signatures else ""
        )

        # 在每个粗桶内用 phash_parts 做模糊聚类
        all_duplicate_groups: List[List[FileSig]] = []
        for bkey, flist in bucket_groups.items():
            if len(flist) < 2:
                continue
            clusters = cluster_bucket_by_phash(flist, cfg.phash_distance_threshold)
            if not clusters:
                continue
            all_duplicate_groups.extend(clusters)
            LOGGER.info("[dup-bucket] 粗桶 %s 内 fuzzy 子组数=%d", bkey, len(clusters))

        # 组织导出：对每个 fuzzy 组，合并到“不同 item”，并按该 item 命中的最大文件大小降序
        duplicate_groups_urls: List[List[str]] = []
        kept_groups = 0
        for group in all_duplicate_groups:
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
            LOGGER.info(
                "[dup] fuzzy 组 #%d → items=%s",
                kept_groups,
                [u.rsplit('=', 1)[-1] for u in urls]
            )

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
            LOGGER.info("[INFO] 未发现重复组（在当前 fuzzy 条件下）")

        LOGGER.info("[DONE] 任务完成，重复组数：%d", kept_groups)
    except KeyboardInterrupt:
        exit_code = 130
        LOGGER.warning("[EXIT] 用户中断（下次将从未完成部分继续）")
    except Exception as e:
        exit_code = 1
        LOGGER.exception("[EXIT] 异常退出（下次将从未完成部分继续）：%s", e)
    finally:
        # 写入最终状态
        try:
            status = "completed" if exit_code == 0 else "incomplete"
            state_path.write_text(json.dumps({
                "status": status,
                "end_ts": int(time.time()),
                "pid": os.getpid(),
                "exit_code": exit_code,
                "log_file": cfg.log_file,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        if _CACHE:
            try:
                _CACHE.close()
            except Exception:
                pass
            _CACHE = None
        # 关闭日志 handler，释放文件占用
        for h in list(LOGGER.handlers):
            try:
                h.close()
            except Exception:
                pass

    if exit_code != 0:
        raise SystemExit(exit_code)

if __name__ == "__main__":
    main()
