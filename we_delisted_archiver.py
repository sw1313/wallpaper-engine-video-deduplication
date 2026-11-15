#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检测已下架的 Steam 创意工坊物品，归档到 Wallpaper Engine 本地 myprojects，
并更新 config.json 使其在 WE 中可见。

用法：
  python we_delisted_archiver.py --detect   -c config.toml
  python we_delisted_archiver.py --archive  -c config.toml
  python we_delisted_archiver.py --both     -c config.toml
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    import tomllib
except Exception:
    import tomli as tomllib

log = logging.getLogger("delisted")

STEAM_API_OLD = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
STEAM_API_NEW = "https://api.steampowered.com/IPublishedFileService/GetDetails/v1/"
BATCH_SIZE = 80
REQUEST_TIMEOUT = 30
ARCHIVE_COPY_BACKENDS = ("auto", "python", "robocopy")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: Path) -> Dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f) or {}


# ---------------------------------------------------------------------------
# Detection via Steam Web API
# ---------------------------------------------------------------------------

def scan_workshop_ids(workshop_root: Path) -> List[str]:
    """列出本地 workshop 目录下所有数字 ID 文件夹。"""
    ids: List[str] = []
    if not workshop_root.is_dir():
        log.error("workshop_root 不存在: %s", workshop_root)
        return ids
    for entry in workshop_root.iterdir():
        if entry.is_dir() and entry.name.isdigit():
            ids.append(entry.name)
    ids.sort(key=int)
    return ids


def _classify_item(item: dict) -> Optional[Dict[str, Any]]:
    """
    判断单个 API 返回的物品是否已下架/不可用。
    判定为下架的条件（与 Tampermonkey 脚本 sectionText 检测对齐）：
      - result != 1          → 物品已删除/不存在
      - banned != 0          → 被 Valve 封禁
      - visibility != 0      → 作者设为私密(2)/仅好友(1)
      - 缺少 title 或为空   → 元数据不完整，通常意味着不可访问
      - ban_reason 非空       → 有封禁原因
    只有 result==1 且 banned==0 且 visibility==0 且 title 非空 的物品才视为正常。
    """
    wid = str(item.get("publishedfileid", ""))
    res = item.get("result", 0)
    banned = item.get("banned", 0)
    visibility = item.get("visibility", -1)
    title = str(item.get("title", "")).strip()
    ban_reason = str(item.get("ban_reason", "")).strip()

    reason = ""
    if res != 1:
        reason = "not_found" if res == 9 else f"result_{res}"
    elif banned or ban_reason:
        reason = "banned"
    elif visibility not in (0,):
        if visibility == -1:
            reason = "no_metadata"
        elif visibility == 1:
            reason = "friends_only"
        elif visibility == 2:
            reason = "private"
        else:
            reason = f"visibility_{visibility}"
    elif not title:
        reason = "no_title"

    if reason:
        return {
            "id": wid,
            "result": res,
            "banned": banned,
            "visibility": visibility,
            "reason": reason,
        }
    return None


def _fetch_batch_new_api(chunk: List[str], api_key: str) -> Optional[List[dict]]:
    """IPublishedFileService/GetDetails/v1/ (需要 API Key，返回更丰富的状态)。"""
    params: Dict[str, Any] = {"key": api_key}
    for i, wid in enumerate(chunk):
        params[f"publishedfileids[{i}]"] = wid
    try:
        resp = requests.get(STEAM_API_NEW, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        details = data.get("response", {}).get("publishedfiledetails", [])
        if isinstance(details, list):
            return details
    except Exception as e:
        log.warning("[DETECT] IPublishedFileService 请求失败: %s", e)
    return None


def _fetch_batch_old_api(chunk: List[str]) -> Optional[List[dict]]:
    """ISteamRemoteStorage/GetPublishedFileDetails/v1/ (无需 Key)。"""
    data: Dict[str, Any] = {"itemcount": len(chunk)}
    for i, wid in enumerate(chunk):
        data[f"publishedfileids[{i}]"] = wid
    try:
        resp = requests.post(STEAM_API_OLD, data=data, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        result = resp.json()
        details = result.get("response", {}).get("publishedfiledetails", [])
        if isinstance(details, list):
            return details
    except Exception as e:
        log.warning("[DETECT] ISteamRemoteStorage 请求失败: %s", e)
    return None


def batch_check_delisted(
    all_ids: List[str], api_key: str = "",
) -> List[Dict[str, Any]]:
    """
    批量检测已下架物品。
    有 api_key 时优先用 IPublishedFileService（更可靠），否则回退到 ISteamRemoteStorage。
    判定标准：result!=1 / banned / visibility非公开 / 缺少标题 均视为下架。
    """
    use_new = bool(api_key)
    if use_new:
        log.info("[DETECT] 使用 IPublishedFileService API（推荐）")
    else:
        log.info("[DETECT] 未配置 steam_api_key，使用 ISteamRemoteStorage API（可在 config.toml 添加 steam_api_key 获得更准确的检测）")

    delisted: List[Dict[str, Any]] = []
    seen_ids: set = set()
    total = len(all_ids)
    batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_idx in range(batches):
        start = batch_idx * BATCH_SIZE
        end = min(start + BATCH_SIZE, total)
        chunk = all_ids[start:end]

        if use_new:
            details = _fetch_batch_new_api(chunk, api_key)
        else:
            details = _fetch_batch_old_api(chunk)

        if details is None:
            raise RuntimeError(f"Steam API 批次 {batch_idx + 1}/{batches} 请求失败，请重试检测")

        returned_ids = set()
        for item in details:
            wid = str(item.get("publishedfileid", ""))
            returned_ids.add(wid)
            hit = _classify_item(item)
            if hit and wid not in seen_ids:
                seen_ids.add(wid)
                delisted.append(hit)

        for wid in chunk:
            if wid not in returned_ids and wid not in seen_ids:
                seen_ids.add(wid)
                delisted.append({
                    "id": wid, "result": -1, "banned": 0,
                    "visibility": -1, "reason": "api_no_response",
                })

        checked = end
        log.info(
            "[DETECT] 批次 %d/%d: 已检测 %d/%d 项, 发现下架 %d 项",
            batch_idx + 1, batches, checked, total, len(delisted),
        )
        if batch_idx < batches - 1:
            time.sleep(0.3)

    return delisted


def run_detect(
    workshop_root: Path, output_path: Path, api_key: str = "",
) -> List[Dict[str, Any]]:
    log.info("[DETECT] 扫描 workshop 目录: %s", workshop_root)
    all_ids = scan_workshop_ids(workshop_root)
    log.info("[DETECT] 找到 %d 个订阅项目", len(all_ids))

    if not all_ids:
        log.warning("[DETECT] 没有找到任何订阅项目")

    log.info("[DETECT] 开始通过 Steam API 检测下架状态...")
    delisted = batch_check_delisted(all_ids, api_key=api_key)

    result = {
        "detected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_subscribed": len(all_ids),
        "delisted_count": len(delisted),
        "items": delisted,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    log.info(
        "[DETECT] 完成: 共 %d 个订阅, %d 个已下架, 结果已保存到 %s",
        len(all_ids), len(delisted), output_path,
    )
    return delisted


# ---------------------------------------------------------------------------
# Archive: copy folders + update config.json
# ---------------------------------------------------------------------------

def _win_path_to_unc_forward(local_path: str, install_dir: str) -> str:
    """
    将本地路径转换为 WE config.json 使用的 UNC 正斜杠格式。
    install_dir 来自 config.json 的 ?installdirectory（已是 //MACHINE/... 格式）。
    """
    return install_dir.rstrip("/") + "/" + local_path.lstrip("/")


def _find_item_in_folders(folders: list, item_id: str) -> List[dict]:
    """在 folders 树中查找包含指定 workshop ID 的 folder 节点。"""
    found: List[dict] = []
    for folder in folders:
        if not isinstance(folder, dict):
            continue
        items = folder.get("items", {})
        if isinstance(items, dict) and item_id in items:
            found.append(folder)
        subs = folder.get("subfolders", [])
        if isinstance(subs, list):
            found.extend(_find_item_in_folders(subs, item_id))
    return found


def _locate_folders_slot(we_cfg: dict) -> Tuple[Optional[dict], Optional[str]]:
    """定位 config.json 中的 folders 容器。"""
    for _, profile in we_cfg.items():
        if not isinstance(profile, dict):
            continue
        general = profile.get("general")
        if not isinstance(general, dict):
            continue
        browser = general.get("browser")
        if isinstance(browser, dict) and isinstance(browser.get("folders"), list):
            return browser, "folders"
        if isinstance(general.get("folders"), list):
            return general, "folders"
    return None, None



def _force_writable_and_remove(path: str) -> bool:
    """尽力清除只读属性并删除文件，供 Windows 下覆盖旧 .bak 使用。返回是否已成功删除。"""
    if not os.path.exists(path):
        return True
    try:
        os.chmod(path, stat.S_IWRITE)
    except Exception:
        pass
    try:
        os.remove(path)
        return True
    except Exception:
        return False


def _write_config_atomic(cfg_path: Path, cfg: dict) -> None:
    """原子写入 config.json。

    先尽力做个 .bak 备份（失败不阻断主流程，因为 os.replace 本身已经是原子替换）；
    再写 .tmp；最后 os.replace 替换 config.json。

    备份会遇到的坑（Windows）：
      1. config.json 自带只读属性时，shutil.copy2 会把同样的只读位带到 .bak，
         第二次再 copy2 会因目标只读而 PermissionError——先清 readonly 再删。
      2. 若 WE 正在运行可能锁住 .bak——fallback 到带时间戳的 .bak.YYYYmmdd_HHMMSS。
    """
    tmp_file = str(cfg_path) + ".tmp"
    bak_file = str(cfg_path) + ".bak"

    if cfg_path.is_file():
        _force_writable_and_remove(bak_file)
        try:
            shutil.copy2(str(cfg_path), bak_file)
            # copy2 会把只读属性带过来，清掉方便下次覆盖
            try:
                os.chmod(bak_file, stat.S_IWRITE)
            except Exception:
                pass
            log.info("[ARCHIVE] config.json 已备份到 %s", bak_file)
        except Exception as e:
            ts_bak = str(cfg_path) + f".bak.{time.strftime('%Y%m%d_%H%M%S')}"
            try:
                shutil.copy2(str(cfg_path), ts_bak)
                try:
                    os.chmod(ts_bak, stat.S_IWRITE)
                except Exception:
                    pass
                log.warning(
                    "[ARCHIVE] %s 不可写（%s），已改备份到 %s",
                    bak_file, e, ts_bak,
                )
            except Exception as e2:
                log.warning(
                    "[ARCHIVE] 备份 config.json 失败，继续原子写入（主文件有 os.replace 保护）：%s",
                    e2,
                )

    # 写临时文件前也清掉可能存在的残留 .tmp 的只读位
    _force_writable_and_remove(tmp_file)
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent="\t")
    os.replace(tmp_file, str(cfg_path))
    log.info("[ARCHIVE] config.json 已更新")


def _normalize_copy_backend(value: Any) -> str:
    backend = str(value or "auto").strip().lower()
    if backend not in ARCHIVE_COPY_BACKENDS:
        log.warning("[ARCHIVE] 未知 archive_copy_backend=%r，回退 auto", value)
        return "auto"
    return backend


def _copytree_python(src_dir: Path, dst_dir: Path) -> None:
    shutil.copytree(str(src_dir), str(dst_dir), dirs_exist_ok=True)


def _file_sizes(directory: Path) -> Dict[str, int]:
    sizes: Dict[str, int] = {}
    pending = [(directory, Path())]
    while pending:
        parent, relative = pending.pop()
        with os.scandir(parent) as entries:
            for entry in entries:
                name = relative / entry.name
                if entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), name))
                elif entry.is_file():
                    sizes[str(name)] = entry.stat().st_size
    return sizes


def _archive_complete(src_dir: Optional[Path], dst_dir: Path) -> bool:
    """Validate the archived project and source file sizes before offering unsubscribe."""
    try:
        with (dst_dir / "project.json").open("r", encoding="utf-8-sig") as stream:
            project = json.load(stream)
        media = str(project.get("file", ""))
        if not media:
            return False
        target = (dst_dir / media).resolve()
        if not target.is_relative_to(dst_dir.resolve()) or not target.is_file():
            return False
        if src_dir is not None and src_dir.is_dir():
            source_sizes = _file_sizes(src_dir)
            archived_sizes = _file_sizes(dst_dir)
            if any(archived_sizes.get(name) != size for name, size in source_sizes.items()):
                return False
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _copytree_robocopy(src_dir: Path, dst_dir: Path) -> Tuple[bool, str]:
    robocopy = shutil.which("robocopy")
    if not robocopy:
        return False, "robocopy 不可用"

    cmd = [
        robocopy,
        str(src_dir),
        str(dst_dir),
        "/E",
        "/COPY:DAT",
        "/DCOPY:DAT",
        "/R:2",
        "/W:1",
        "/NP",
        "/NFL",
        "/NDL",
    ]
    r = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    if 0 <= r.returncode <= 7:
        return True, f"robocopy rc={r.returncode}"
    out = (r.stdout or "").strip()
    detail = f"robocopy rc={r.returncode}"
    if out:
        detail += f": {out[-1000:]}"
    return False, detail


def _copytree_archive_payload(src_dir: Path, dst_dir: Path, copy_backend: str) -> None:
    """复制归档目录。

    `robocopy` 走 Windows 原生复制路径，SMB 服务端支持时可能触发服务端复制；
    不支持时仍会退化为普通客户端复制。`auto` 下 robocopy 失败会回退 Python。
    """
    backend = _normalize_copy_backend(copy_backend)
    if backend == "python" or os.name != "nt":
        if backend == "robocopy" and os.name != "nt":
            log.warning("[ARCHIVE] robocopy 仅支持 Windows，改用 Python 复制")
        _copytree_python(src_dir, dst_dir)
        return

    ok, detail = _copytree_robocopy(src_dir, dst_dir)
    if ok:
        log.info(
            "[ARCHIVE] 已使用 robocopy 复制（%s）；是否服务端内部复制取决于 SMB 服务器支持",
            detail,
        )
        return
    if backend == "robocopy":
        raise RuntimeError(detail)
    log.warning("[ARCHIVE] robocopy 复制失败，回退 Python 复制：%s", detail)
    _copytree_python(src_dir, dst_dir)


def _copytree_for_archive(src_dir: Path, dst_dir: Path, copy_backend: str) -> None:
    if dst_dir.exists():
        # Repair incomplete archives left by older releases. Extra local files are retained.
        _copytree_archive_payload(src_dir, dst_dir, copy_backend)
        if not _archive_complete(src_dir, dst_dir):
            raise RuntimeError(f"归档校验失败：{dst_dir}")
        return
    staging = dst_dir.with_name(f".{dst_dir.name}.{uuid.uuid4().hex}.partial")
    try:
        _copytree_archive_payload(src_dir, staging, copy_backend)
        if not _archive_complete(src_dir, staging):
            raise RuntimeError(f"归档校验失败：{src_dir}")
        staging.rename(dst_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def run_archive(
    workshop_root: Path,
    we_install_dir: Path,
    delisted_path: Path,
    copy_backend: str = "auto",
) -> None:
    if not delisted_path.exists():
        log.error("[ARCHIVE] 找不到下架记录文件: %s", delisted_path)
        log.error("[ARCHIVE] 请先运行 --detect")
        return

    with delisted_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    items = data.get("items", [])
    if not items:
        log.info("[ARCHIVE] 没有需要归档的下架物品")
        return

    cfg_path = we_install_dir / "config.json"
    if not cfg_path.exists():
        log.error("[ARCHIVE] 找不到 config.json: %s", cfg_path)
        return

    with cfg_path.open("r", encoding="utf-8") as f:
        we_cfg = json.load(f)

    install_dir = we_cfg.get("?installdirectory", "")
    if not install_dir:
        log.error("[ARCHIVE] config.json 中缺少 ?installdirectory")
        return

    container, key = _locate_folders_slot(we_cfg)
    if container is None or key is None:
        log.error("[ARCHIVE] config.json 中找不到 folders 结构")
        return
    folders = container[key]

    myprojects_dir = we_install_dir / "projects" / "myprojects"
    myprojects_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    config_updated = 0

    for item in items:
        wid = item["id"]
        src_dir = workshop_root / wid
        dst_dir = myprojects_dir / wid

        # --- 复制文件夹 ---
        if _archive_complete(src_dir, dst_dir):
            log.info("[ARCHIVE] %s: 已有完整归档，跳过复制", wid)
        elif not src_dir.is_dir():
            log.warning("[ARCHIVE] %s: workshop 源文件夹不存在，跳过复制", wid)
            continue
        else:
            try:
                _copytree_for_archive(src_dir, dst_dir, copy_backend)
                copied += 1
                log.info("[ARCHIVE] %s: 已复制到 %s", wid, dst_dir)
            except Exception as e:
                log.error("[ARCHIVE] %s: 复制失败: %s", wid, e)
                continue

        # --- 读取 project.json 确定视频文件名 ---
        proj_json = dst_dir / "project.json"
        if not proj_json.exists():
            proj_json = src_dir / "project.json"
        if not proj_json.exists():
            log.warning("[ARCHIVE] %s: 找不到 project.json，跳过 config 更新", wid)
            continue

        try:
            with proj_json.open("r", encoding="utf-8") as f:
                proj = json.load(f)
        except Exception as e:
            log.warning("[ARCHIVE] %s: 读取 project.json 失败: %s", wid, e)
            continue

        video_file = proj.get("file", "")
        if not video_file:
            log.warning("[ARCHIVE] %s: project.json 中无 file 字段", wid)
            continue

        # 构造 WE config.json 中的路径
        rel_path = f"projects/myprojects/{wid}/{video_file}"
        we_path_entry = _win_path_to_unc_forward(rel_path, install_dir)

        # --- 更新 config.json folders（放回原文件夹） ---
        matched_folders = _find_item_in_folders(folders, wid)
        if matched_folders:
            for fld in matched_folders:
                fld_items = fld.get("items", {})
                if we_path_entry not in fld_items:
                    fld_items[we_path_entry] = 1
                    config_updated += 1
                    log.info(
                        "[ARCHIVE] %s: 已添加到文件夹 '%s'",
                        wid, fld.get("title", "?"),
                    )
        else:
            log.info("[ARCHIVE] %s: 不在任何文件夹中（主页），WE 将自动从 myprojects 发现", wid)

    if config_updated > 0:
        _write_config_atomic(cfg_path, we_cfg)

    log.info(
        "[ARCHIVE] 完成: 复制 %d 个文件夹, 更新 %d 条 config 条目",
        copied, config_updated,
    )
    if any(not _archive_complete(workshop_root / item["id"], myprojects_dir / item["id"]) for item in items):
        raise RuntimeError("部分物品归档失败，可修复后重新执行归档")


# ---------------------------------------------------------------------------
# Archive a specific WE folder (category) - copy all workshop items → myprojects
# ---------------------------------------------------------------------------

# 匹配 config.json items 里的两种路径：
#   workshop:    .../workshop/content/431960/<wid>/...
#   myprojects:  .../projects/myprojects/<subdir>/...
import re as _re
_WORKSHOP_PATH_RE = _re.compile(r"/workshop/content/431960/(\d+)/", _re.IGNORECASE)
_MYPROJECTS_PATH_RE = _re.compile(r"/projects/myprojects/([^/]+)/", _re.IGNORECASE)


def _path_normalize(p: str) -> str:
    return str(p).replace("\\", "/")


def _collect_workshop_in_items(items: Dict[str, Any]) -> List[str]:
    """从 folder.items 里抓所有 workshop 订阅项的 wid。

    WE config.json 里 workshop 订阅项通常直接用 wid 字符串做 key（如
    "3527146660": 1），部分新版本/存量也可能存完整的 UNC 路径
    "//MACHINE/…/workshop/content/431960/<wid>/…"。两种都要接。
    """
    wids: List[str] = []
    for raw in (items or {}).keys():
        s = str(raw)
        # a) 纯数字 key = 直接就是 wid
        if s.isdigit():
            wids.append(s)
            continue
        # b) 完整路径 fallback
        m = _WORKSHOP_PATH_RE.search(_path_normalize(s))
        if m:
            wids.append(m.group(1))
    return wids


def _collect_myprojects_in_items(items: Dict[str, Any]) -> List[str]:
    """从 folder.items 里抓所有 myprojects 子目录名。"""
    names: List[str] = []
    for raw in (items or {}).keys():
        m = _MYPROJECTS_PATH_RE.search(_path_normalize(raw))
        if m:
            names.append(m.group(1))
    return names


def _flatten_folders(folders: List[dict], prefix: str = "") -> List[Tuple[str, dict]]:
    """把 folders 树扁平成 [(display_path, folder_dict), ...]，保留出现顺序。"""
    out: List[Tuple[str, dict]] = []
    for f in folders:
        if not isinstance(f, dict):
            continue
        title = str(f.get("title", "?"))
        display = title if not prefix else f"{prefix} / {title}"
        out.append((display, f))
        subs = f.get("subfolders", [])
        if isinstance(subs, list):
            out.extend(_flatten_folders(subs, display))
    return out


def _load_we_config(we_install_dir: Path) -> Tuple[Optional[dict], Optional[Path]]:
    cfg_path = we_install_dir / "config.json"
    if not cfg_path.exists():
        log.error("[FOLDER] 找不到 config.json: %s", cfg_path)
        return None, None
    try:
        with cfg_path.open("r", encoding="utf-8") as f:
            return json.load(f), cfg_path
    except Exception as e:
        log.error("[FOLDER] 读取 config.json 失败: %s", e)
        return None, None


def _snapshot_workshop_wids(workshop_root: Path) -> Optional[set]:
    """一次性扫 workshop_root 下的 wid 目录名到 set，供 O(1) 查询。

    对一万多条 workshop 逐个 `Path.is_dir()` 在机械盘 / 网络盘上会慢到
    15s timeout 都扛不住，换成 `os.scandir` 扫一次全量名字，后续 `wid in
    snap` 常数复杂度。
    """
    try:
        snap: set = set()
        with os.scandir(str(workshop_root)) as it:
            for entry in it:
                # wid 目录名都是纯数字；名字对得上就视为"目录存在"
                # （极端情况里有同名文件，会被误判为在盘，概率极小不处理）
                if entry.name.isdigit():
                    snap.add(entry.name)
        return snap
    except OSError as e:
        log.warning("[FOLDER] 扫 workshop_root 失败: %s", e)
        return None


def cmd_list_folders(we_install_dir: Path, workshop_root: Optional[Path] = None) -> None:
    """列出 config.json 中全部文件夹 + 各自 workshop / myprojects 条目数，输出 JSON 到 stdout。

    给了 workshop_root 时，额外统计 `workshop_alive_count`：
    即 config.json items 里记录的 workshop wid 中，`<workshop_root>/<wid>/`
    目录仍真实存在（= Steam 还在订阅中）的个数。
    """
    we_cfg, _cfg_path = _load_we_config(we_install_dir)
    if we_cfg is None:
        print(json.dumps({"folders": [], "error": "config_json_missing"}, ensure_ascii=False))
        return
    container, key = _locate_folders_slot(we_cfg)
    if container is None or key is None:
        print(json.dumps({"folders": [], "error": "no_folders_slot"}, ensure_ascii=False))
        return
    wr_valid = workshop_root is not None and workshop_root.is_dir()
    alive_snap: Optional[set] = None
    if wr_valid:
        assert workshop_root is not None
        alive_snap = _snapshot_workshop_wids(workshop_root)
    flat = _flatten_folders(container[key])
    folders_out: List[Dict[str, Any]] = []
    for idx, (display, fdict) in enumerate(flat):
        items = fdict.get("items", {}) or {}
        ws = _collect_workshop_in_items(items)
        mp = _collect_myprojects_in_items(items)
        entry: Dict[str, Any] = {
            "index": idx,
            "title": display,
            "workshop_count": len(ws),
            "myprojects_count": len(mp),
        }
        if alive_snap is not None:
            alive = sum(1 for wid in ws if wid in alive_snap)
            entry["workshop_alive_count"] = alive
            entry["workshop_orphan_count"] = len(ws) - alive
        folders_out.append(entry)
    print(json.dumps({"folders": folders_out}, ensure_ascii=False))


def _archive_one_wid(
    wid: str,
    workshop_root: Path,
    myprojects_dir: Path,
    install_dir_unc: str,
    folder_items: Dict[str, Any],
    copy_backend: str = "auto",
) -> Tuple[bool, bool]:
    """把单个 workshop wid 归档到 myprojects，并把路径加进目标 folder.items。

    返回 (copied, config_added)。copied=False 表示目标已存在或源缺失，没有真实复制。
    """
    src_dir = workshop_root / wid
    dst_dir = myprojects_dir / wid

    copied = False
    if _archive_complete(src_dir, dst_dir):
        log.info("[FOLDER-ARC] %s: 已有完整归档，跳过复制", wid)
    elif not src_dir.is_dir():
        log.warning("[FOLDER-ARC] %s: workshop 源目录不存在，跳过", wid)
        return (False, False)
    else:
        try:
            _copytree_for_archive(src_dir, dst_dir, copy_backend)
            copied = True
            log.info("[FOLDER-ARC] %s: 已复制到 %s", wid, dst_dir)
        except Exception as e:
            log.error("[FOLDER-ARC] %s: 复制失败: %s", wid, e)
            raise RuntimeError(f"{wid}: 复制失败：{e}") from e

    # 读 project.json 拿 file 字段（用 dst > src 顺序，兜底用 src）
    proj_json = dst_dir / "project.json"
    if not proj_json.exists():
        proj_json = src_dir / "project.json"
    if not proj_json.exists():
        log.warning("[FOLDER-ARC] %s: 找不到 project.json，跳过 config 更新", wid)
        raise RuntimeError(f"{wid}: 找不到 project.json")
    try:
        with proj_json.open("r", encoding="utf-8") as f:
            proj = json.load(f)
    except Exception as e:
        log.warning("[FOLDER-ARC] %s: 读取 project.json 失败: %s", wid, e)
        raise RuntimeError(f"{wid}: 读取 project.json 失败：{e}") from e

    video_file = proj.get("file", "")
    if not video_file:
        log.warning("[FOLDER-ARC] %s: project.json 缺 file 字段", wid)
        raise RuntimeError(f"{wid}: project.json 缺 file 字段")

    rel_path = f"projects/myprojects/{wid}/{video_file}"
    we_path_entry = _win_path_to_unc_forward(rel_path, install_dir_unc)

    if we_path_entry in folder_items:
        log.info("[FOLDER-ARC] %s: folder 已包含该 myprojects 条目，跳过", wid)
        return (copied, False)

    folder_items[we_path_entry] = 1
    log.info("[FOLDER-ARC] %s: 已添加到 folder.items", wid)
    return (copied, True)


def run_archive_folder(
    workshop_root: Path,
    we_install_dir: Path,
    folder_index: int,
    copy_backend: str = "auto",
) -> None:
    """把 config.json 中指定 folder 内所有 workshop 项归档到 myprojects（不动已归档的）。"""
    we_cfg, cfg_path = _load_we_config(we_install_dir)
    if we_cfg is None or cfg_path is None:
        return
    container, key = _locate_folders_slot(we_cfg)
    if container is None or key is None:
        log.error("[FOLDER-ARC] config.json 找不到 folders 结构")
        return
    flat = _flatten_folders(container[key])
    if not (0 <= folder_index < len(flat)):
        log.error("[FOLDER-ARC] folder_index 超界: %d（可选 0..%d）", folder_index, len(flat) - 1)
        return
    display, target = flat[folder_index]

    install_dir_unc = we_cfg.get("?installdirectory", "")
    if not install_dir_unc:
        log.error("[FOLDER-ARC] config.json 缺 ?installdirectory")
        return

    myprojects_dir = we_install_dir / "projects" / "myprojects"
    myprojects_dir.mkdir(parents=True, exist_ok=True)

    items = target.get("items", {})
    if not isinstance(items, dict):
        log.error("[FOLDER-ARC] folder '%s' items 不是 dict", display)
        return

    workshop_wids = _collect_workshop_in_items(items)
    existing_mp = set(_collect_myprojects_in_items(items))
    alive_wids = _snapshot_workshop_wids(workshop_root)
    if alive_wids is None:
        raise RuntimeError("无法读取创意工坊目录，请检查网络或磁盘连接")
    active_wids = [wid for wid in workshop_wids if wid in alive_wids]
    skipped_missing = len(workshop_wids) - len(active_wids)

    log.info(
        "[FOLDER-ARC] 目标 folder: '%s'（workshop=%d, 已归档=%d）",
        display, len(workshop_wids), len(existing_mp),
    )

    copied = 0
    config_added = 0
    skipped_already = 0
    failed_wids: List[str] = []
    for wid in active_wids:
        if wid in existing_mp and _archive_complete(workshop_root / wid, myprojects_dir / wid):
            skipped_already += 1
            continue
        try:
            c, a = _archive_one_wid(
                wid, workshop_root, myprojects_dir, install_dir_unc, items, copy_backend,
            )
        except Exception as exc:
            failed_wids.append(wid)
            log.error("[FOLDER-ARC] %s: %s", wid, exc)
            continue
        if c:
            copied += 1
        if a:
            config_added += 1

    if config_added > 0:
        _write_config_atomic(cfg_path, we_cfg)

    log.info(
        "[FOLDER-ARC] 完成 '%s': 复制 %d 个目录, 新增 %d 条 config 条目, 已归档跳过 %d, 源已不存在跳过 %d, 失败 %d",
        display, copied, config_added, skipped_already, skipped_missing, len(failed_wids),
    )
    if failed_wids:
        raise RuntimeError(f"{len(failed_wids)} 个物品归档失败，wid：{', '.join(failed_wids[:20])}")


def cmd_list_archived_in_folder(
    we_install_dir: Path,
    folder_index: int,
    workshop_root: Optional[Path] = None,
) -> None:
    """列出指定 folder 中 "workshop 存在 且 myprojects 已归档" 的 wid（用于取消订阅手动归档）。

    识别"已归档"的方式：
      1) folder.items 里同时有 workshop/content/431960/<wid>/... 和
         projects/myprojects/<wid>/... 两条路径（最常见）
      2) 或即便 folder.items 里没有 myprojects 条目，但 myprojects/<wid>/ 目录
         实际存在（用户自己搬过文件的情况）

    给了 workshop_root 时，会额外过滤：只保留 `<workshop_root>/<wid>/` 仍然存在
    的 wid——Steam 端已退订的 wid 本地 workshop 目录早就没了，再塞给
    bulk_unsub_controller 也是空跑（Steam 网页端直接 404），没必要。
    """
    we_cfg, _cfg_path = _load_we_config(we_install_dir)
    if we_cfg is None:
        print(json.dumps({"wids": [], "error": "config_json_missing"}, ensure_ascii=False))
        return
    container, key = _locate_folders_slot(we_cfg)
    if container is None or key is None:
        print(json.dumps({"wids": [], "error": "no_folders_slot"}, ensure_ascii=False))
        return
    flat = _flatten_folders(container[key])
    if not (0 <= folder_index < len(flat)):
        print(json.dumps({"wids": [], "error": "index_out_of_range"}, ensure_ascii=False))
        return
    display, target = flat[folder_index]
    items = target.get("items", {}) or {}

    workshop_wids = _collect_workshop_in_items(items)
    myprojects_dir = we_install_dir / "projects" / "myprojects"

    # 同理：一次性扫 myprojects / workshop 下的子目录名到 set，避免逐个 stat。
    mp_disk_snap: set = set()
    try:
        with os.scandir(str(myprojects_dir)) as it:
            for entry in it:
                mp_disk_snap.add(entry.name)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RuntimeError(f"无法读取本地归档目录：{exc}") from exc

    ws_alive_snap: Optional[set] = None
    if workshop_root is not None:
        ws_alive_snap = _snapshot_workshop_wids(workshop_root)
        if ws_alive_snap is None:
            raise RuntimeError("无法读取创意工坊目录，请检查网络或磁盘连接")

    archived: List[str] = []
    skipped_already_unsubbed = 0
    orphan_wids: List[str] = []
    incomplete_wids: List[str] = []
    candidates: List[str] = []
    for wid in workshop_wids:
        alive_on_disk = (ws_alive_snap is None) or (wid in ws_alive_snap)
        if not alive_on_disk:
            # 磁盘上 workshop 目录已经没了（= 老版本就已经退订过）。不管有没有
            # 归档，都是 config.json 里的幽灵条目，需要清理。
            orphan_wids.append(wid)
            if wid in mp_disk_snap:
                skipped_already_unsubbed += 1
            continue
        if wid in mp_disk_snap:
            candidates.append(wid)

    log.info("[VERIFY] 文件夹 '%s': 开始校验 %d 个本地归档项目", display, len(candidates))
    for index, wid in enumerate(candidates, start=1):
        if _archive_complete(workshop_root / wid if workshop_root is not None else None, myprojects_dir / wid):
            archived.append(wid)
        else:
            incomplete_wids.append(wid)
        if index % 25 == 0 or index == len(candidates):
            log.info("[VERIFY] 已校验 %d/%d，完整 %d，不完整 %d", index, len(candidates), len(archived), len(incomplete_wids))

    out = {
        "folder": display,
        "index": folder_index,
        "workshop_total": len(workshop_wids),
        "archived_wids": archived,
        "skipped_already_unsubbed": skipped_already_unsubbed,
        "orphan_wids": orphan_wids,
        "incomplete_wids": incomplete_wids,
    }
    print(json.dumps(out, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Cleanup orphan workshop entries in config.json
# ---------------------------------------------------------------------------

def run_remove_workshop_wids_from_config(
    we_install_dir: Path,
    wids: List[str],
) -> None:
    """把 config.json 所有 folders.items 里匹配给定 wid 集的 workshop 条目删掉。

    场景：刚用 bulk_unsub_controller 把这批 wid 在 Steam 端退订完，Steam
    客户端删本地 workshop 目录有延迟，不能只靠"磁盘目录是否存在"来判断
    幽灵；这里直接按明确的 wid 列表动手，时序安全。

    匹配两种 key：纯数字（"3527146660"）+ 含 workshop 路径的 UNC
    字符串。
    """
    if not wids:
        log.info("[REMOVE-WIDS] wid 列表为空，跳过。")
        return
    we_cfg, cfg_path = _load_we_config(we_install_dir)
    if we_cfg is None or cfg_path is None:
        return
    container, key = _locate_folders_slot(we_cfg)
    if container is None or key is None:
        log.error("[REMOVE-WIDS] config.json 找不到 folders 结构")
        return
    target_set = {str(w).strip() for w in wids if str(w).strip()}
    flat = _flatten_folders(container[key])

    total_removed = 0
    affected_folders = 0
    for display, fdict in flat:
        items = fdict.get("items", {})
        if not isinstance(items, dict):
            continue
        dead_keys: List[str] = []
        for raw_key in list(items.keys()):
            s = str(raw_key)
            wid: Optional[str] = None
            if s.isdigit():
                wid = s
            else:
                m = _WORKSHOP_PATH_RE.search(_path_normalize(s))
                if m:
                    wid = m.group(1)
            if wid is not None and wid in target_set:
                dead_keys.append(raw_key)
        if dead_keys:
            affected_folders += 1
            for k in dead_keys:
                items.pop(k, None)
            total_removed += len(dead_keys)
            log.info(
                "[REMOVE-WIDS] '%s': 移除 %d 条已退订 workshop 记录",
                display, len(dead_keys),
            )

    if total_removed > 0:
        _write_config_atomic(cfg_path, we_cfg)
        log.info(
            "[REMOVE-WIDS] 完成: 共清理 %d 条 workshop 记录，涉及 %d 个文件夹",
            total_removed, affected_folders,
        )
    else:
        log.info("[REMOVE-WIDS] config.json 里没有需要清理的 workshop 记录。")


def run_cleanup_orphan_workshop(
    workshop_root: Path,
    we_install_dir: Path,
) -> None:
    """扫描 config.json 所有 folders.items，把"workshop 条目但对应 wid 在
    `<workshop_root>/<wid>/` 不存在"的死键删掉。

    为什么需要：用户在 Steam 取消订阅后，workshop 目录会被 Steam 删掉，但
    WE 自己不会清 config.json 里的分类记录，导致该文件夹下
    `workshop_count` 永远不掉。"""
    if not workshop_root.is_dir():
        log.error("[CLEANUP] workshop_root 无效: %s", workshop_root)
        return
    we_cfg, cfg_path = _load_we_config(we_install_dir)
    if we_cfg is None or cfg_path is None:
        return
    container, key = _locate_folders_slot(we_cfg)
    if container is None or key is None:
        log.error("[CLEANUP] config.json 找不到 folders 结构")
        return
    flat = _flatten_folders(container[key])

    alive_snap = _snapshot_workshop_wids(workshop_root)
    if alive_snap is None:
        log.error("[CLEANUP] 无法枚举 workshop_root，放弃清理")
        return

    total_removed = 0
    affected_folders = 0
    for display, fdict in flat:
        items = fdict.get("items", {})
        if not isinstance(items, dict):
            continue
        dead_keys: List[str] = []
        for raw_key in list(items.keys()):
            s = str(raw_key)
            wid: Optional[str] = None
            if s.isdigit():
                wid = s
            else:
                m = _WORKSHOP_PATH_RE.search(_path_normalize(s))
                if m:
                    wid = m.group(1)
            if wid is None:
                continue
            if wid not in alive_snap:
                dead_keys.append(raw_key)
        if dead_keys:
            affected_folders += 1
            for k in dead_keys:
                items.pop(k, None)
            total_removed += len(dead_keys)
            log.info(
                "[CLEANUP] '%s': 移除 %d 个幽灵 workshop 条目",
                display, len(dead_keys),
            )

    if total_removed > 0:
        _write_config_atomic(cfg_path, we_cfg)
        log.info(
            "[CLEANUP] 完成: 共清理 %d 个幽灵 workshop 条目，涉及 %d 个文件夹",
            total_removed, affected_folders,
        )
    else:
        log.info("[CLEANUP] 没有发现幽灵 workshop 条目")


# ---------------------------------------------------------------------------
# Generate xlsx for bulk unsubscribe
# ---------------------------------------------------------------------------

def generate_unsub_xlsx(delisted_path: Path, output_xlsx: Path) -> Optional[Path]:
    """从 delisted_items.json 生成用于 bulk_unsub_controller.py 的 xlsx。"""
    if not delisted_path.exists():
        log.error("[UNSUB] 找不到下架记录文件: %s", delisted_path)
        return None

    with delisted_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    items = data.get("items", [])
    if not items:
        log.info("[UNSUB] 没有需要取消订阅的物品")
        return None

    try:
        from openpyxl import Workbook
    except ImportError:
        log.error("[UNSUB] 需要 openpyxl: pip install openpyxl")
        return None

    wb = Workbook()
    ws = wb.active
    ws.title = "delisted_unsub"
    ws.append(["url"])
    for item in items:
        wid = item["id"]
        url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}"
        ws.append([url])

    output_xlsx.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_xlsx))
    log.info("[UNSUB] 已生成 %d 条取消订阅链接: %s", len(items), output_xlsx)
    return output_xlsx


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging():
    # 所有日志走 stderr；stdout 留给机读 JSON（--list-folders 等子命令）。
    # 外层 UI 用 stderr=STDOUT 合流捕获，在日志面板里仍能看到全部输出。
    stream = sys.stderr
    if sys.platform == "win32":
        try:
            stream = io.TextIOWrapper(
                sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
            )
        except Exception:
            pass

    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    setup_logging()

    ap = argparse.ArgumentParser(description="检测并归档已下架创意工坊物品")
    ap.add_argument("-c", "--config", default="config.toml", help="config.toml 路径")
    ap.add_argument("--detect", action="store_true", help="检测下架物品")
    ap.add_argument("--archive", action="store_true", help="归档下架物品到本地 myprojects")
    ap.add_argument("--both", action="store_true", help="检测 + 归档（下架）")
    ap.add_argument("--gen-xlsx", action="store_true", help="从检测结果生成取消订阅 xlsx")
    ap.add_argument("--list-folders", action="store_true",
                    help="列出 config.json 中全部文件夹（JSON 到 stdout，供 UI 消费）")
    ap.add_argument("--archive-folder-index", type=int, default=None,
                    help="把指定索引的文件夹（来自 --list-folders）内全部 workshop 项归档到 myprojects")
    ap.add_argument("--list-archived-in-folder-index", type=int, default=None,
                    help="列出指定文件夹里已归档的 wid（JSON 到 stdout，供 UI 生成退订 xlsx）")
    ap.add_argument("--cleanup-orphan-workshop-items", action="store_true",
                    help="扫描 config.json，删除磁盘上 workshop 目录已不存在的幽灵分类条目")
    ap.add_argument("--remove-workshop-wids-from-config", action="store_true",
                    help="按显式 wid 列表从 config.json 里删除 workshop 分类条目（配 --wids-file 使用）")
    ap.add_argument("--wids-file", default=None,
                    help="JSON 文件路径：顶层是 wid 数组，或对象 {\"archived_wids\":[...]}")

    ap.add_argument("--workshop-root", help="覆盖 workshop_root")
    ap.add_argument("--we-install-dir", help="覆盖 we_install_dir")
    ap.add_argument("--steam-api-key", help="覆盖 steam_api_key")
    ap.add_argument("--output", help="覆盖 delisted_items.json 路径")
    ap.add_argument(
        "--copy-backend",
        choices=ARCHIVE_COPY_BACKENDS,
        help="归档复制后端：auto/python/robocopy（默认读 archive_copy_backend，缺省 auto）",
    )
    args = ap.parse_args()

    cfg = load_config(Path(args.config))

    workshop_root = Path(args.workshop_root or cfg.get("workshop_root", ""))
    we_install_dir = Path(args.we_install_dir or cfg.get("we_install_dir", ""))
    api_key = args.steam_api_key or str(cfg.get("steam_api_key", "")).strip()
    copy_backend = _normalize_copy_backend(args.copy_backend or cfg.get("archive_copy_backend", "auto"))
    output_dir = Path(cfg.get("output_dir", "output"))
    delisted_path = Path(args.output) if args.output else (output_dir / "delisted_items.json")

    if args.both:
        args.detect = True
        args.archive = True

    any_action = (
        args.detect or args.archive or args.gen_xlsx
        or args.list_folders
        or args.archive_folder_index is not None
        or args.list_archived_in_folder_index is not None
        or args.cleanup_orphan_workshop_items
        or args.remove_workshop_wids_from_config
    )
    if not any_action:
        ap.print_help()
        return

    # 先处理"只读 + JSON 到 stdout"的子命令，这类不需要 workshop_root。
    if args.list_folders:
        if not we_install_dir or not we_install_dir.is_dir():
            log.error("we_install_dir 无效: %s", we_install_dir)
            print(json.dumps({"folders": [], "error": "we_install_dir_invalid"}, ensure_ascii=False))
            sys.exit(1)
        wr = workshop_root if workshop_root and workshop_root.is_dir() else None
        cmd_list_folders(we_install_dir, workshop_root=wr)
        return

    if args.list_archived_in_folder_index is not None:
        if not we_install_dir or not we_install_dir.is_dir():
            log.error("we_install_dir 无效: %s", we_install_dir)
            print(json.dumps({"wids": [], "error": "we_install_dir_invalid"}, ensure_ascii=False))
            sys.exit(1)
        wr = workshop_root if workshop_root and workshop_root.is_dir() else None
        cmd_list_archived_in_folder(we_install_dir, args.list_archived_in_folder_index, workshop_root=wr)
        return

    if args.detect:
        if not workshop_root or not workshop_root.is_dir():
            log.error("workshop_root 无效: %s", workshop_root)
            sys.exit(1)
        run_detect(workshop_root, delisted_path, api_key=api_key)

    if args.archive:
        if not we_install_dir or not we_install_dir.is_dir():
            log.error("we_install_dir 无效: %s", we_install_dir)
            sys.exit(1)
        run_archive(workshop_root, we_install_dir, delisted_path, copy_backend=copy_backend)

    if args.archive_folder_index is not None:
        if not workshop_root or not workshop_root.is_dir():
            log.error("workshop_root 无效: %s", workshop_root)
            sys.exit(1)
        if not we_install_dir or not we_install_dir.is_dir():
            log.error("we_install_dir 无效: %s", we_install_dir)
            sys.exit(1)
        run_archive_folder(
            workshop_root,
            we_install_dir,
            args.archive_folder_index,
            copy_backend=copy_backend,
        )

    if args.cleanup_orphan_workshop_items:
        if not workshop_root or not workshop_root.is_dir():
            log.error("workshop_root 无效: %s", workshop_root)
            sys.exit(1)
        if not we_install_dir or not we_install_dir.is_dir():
            log.error("we_install_dir 无效: %s", we_install_dir)
            sys.exit(1)
        run_cleanup_orphan_workshop(workshop_root, we_install_dir)

    if args.remove_workshop_wids_from_config:
        if not we_install_dir or not we_install_dir.is_dir():
            log.error("we_install_dir 无效: %s", we_install_dir)
            sys.exit(1)
        if not args.wids_file:
            log.error("--remove-workshop-wids-from-config 需要 --wids-file")
            sys.exit(1)
        wids_path = Path(args.wids_file)
        if not wids_path.exists():
            log.error("--wids-file 不存在: %s", wids_path)
            sys.exit(1)
        try:
            with wids_path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            log.error("解析 --wids-file 失败: %s", e)
            sys.exit(1)
        if isinstance(raw, dict):
            wids_list = raw.get("archived_wids") or raw.get("wids") or []
        elif isinstance(raw, list):
            wids_list = raw
        else:
            log.error("--wids-file 顶层必须是数组或 {archived_wids/wids: [...]}")
            sys.exit(1)
        run_remove_workshop_wids_from_config(we_install_dir, [str(x) for x in wids_list])

    if args.gen_xlsx:
        xlsx_path = output_dir / f"delisted_unsub_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
        generate_unsub_xlsx(delisted_path, xlsx_path)


if __name__ == "__main__":
    main()
