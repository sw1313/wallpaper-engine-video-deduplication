"""
可选的视觉语义特征：DINOv2 / DINOv3 作为"第三道闸"。

专治 pHash + 颜色直方图双失效的边界案例：
  同一源视频只有少数帧局部形变（如场景里某个元素被替换/变体、表情差分、道具差分）。
  这种差异对所有全局空间统计几乎不可见，但自监督视觉模型能把它当成不同语义看出来。

使用方式：
    from semantic_features import load_semantic_model, semantic_distance
    m = load_semantic_model("dinov3_s", device="auto")  # auto: cuda if available else cpu
    # 关键：要全片抽帧（window_seconds = duration, seek_ratio = 0）而不是中段 15s
    embs = m.embed_frames_per_frame(rgb_frames_uint8_list)  # np.ndarray(N, D), L2 归一化
    d = semantic_distance(embs_a, embs_b)  # [0, 1]：按 index 对齐的 per-frame cosine 距离的均值

为什么 per-frame mean 而不是 mean pooled embedding：
  - pooled 会把少数差分帧的语义差异稀释到整体平均里，差分 ≈ 同视频 ≈ 0.0001（实测）
  - per-frame 让每一对帧的差异独立贡献；实测区分度 ~30×。

单 mean 还不够，要搭配"尖峰检测"（max + max/mean）。

DINOv2 vs DINOv3：
  - DINOv2（默认 `dinov2_s`）：torch.hub 自动下载，patch_size=14，输入 224，无 register token。
  - DINOv3（默认 `dinov3_s`）：走 HF transformers，patch_size=16，输入 256，含 4 个 register token。
    Gram anchoring 让 patch-level 特征更干净，对 patch_spatial_verdict 更友好。
    首次下载需接受 gated license：
      1) pip install -U 'transformers>=4.56' accelerate safetensors
      2) huggingface-cli login（到 https://huggingface.co/facebook/dinov3-vits16-pretrain-lvd1689m 点 agree）
    离线可通过 HF_HOME 指向已下载目录。切模型会让缓存 hash 变化，旧缓存自动失效。

运行要求：
  - 需要用户安装 torch（GPU 优先，CPU 也能跑但较慢）
  - 本模块顶层 **不 import torch/transformers**，按需懒加载；未启用语义特征时完全不受影响。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

LOGGER = logging.getLogger("we_dedup.semantic")


# 每个模型的完整描述（含 backend / 关键形状参数）。
#   backend="torch_hub"        → DINOv2 经 torch.hub 加载
#   backend="hf_transformers"  → DINOv3 经 transformers.AutoModel 加载
# input_size / patch_size：保证 input_size % patch_size == 0 且商数一致（=16）以便 patch grid 复用。
_SUPPORTED_MODELS: Dict[str, Dict[str, Any]] = {
    # DINOv2（老）—— 无 register token，patch_size=14，输入 224 → 16×16=256 patches/帧
    "dinov2_s":  {"backend": "torch_hub", "repo": "facebookresearch/dinov2",
                  "entry": "dinov2_vits14", "input_size": 224, "patch_size": 14,
                  "num_register_tokens": 0},
    "dinov2_b":  {"backend": "torch_hub", "repo": "facebookresearch/dinov2",
                  "entry": "dinov2_vitb14", "input_size": 224, "patch_size": 14,
                  "num_register_tokens": 0},
    "dinov2_l":  {"backend": "torch_hub", "repo": "facebookresearch/dinov2",
                  "entry": "dinov2_vitl14", "input_size": 224, "patch_size": 14,
                  "num_register_tokens": 0},
    # DINOv3（新）—— 4 个 register token，patch_size=16，输入 256 → 16×16=256 patches/帧
    # 规模上与 DINOv2 对应档位近似，dim 也相同（384/768/1024）。
    # hf_id：HuggingFace 模型 ID（gated，需审批）；ms_id：ModelScope 镜像 ID（同名，免审批，推荐）。
    # 加载优先级：1) 已有本地目录  2) modelscope.snapshot_download(ms_id)  3) HF from_pretrained(hf_id)
    "dinov3_s":  {"backend": "hf_transformers",
                  "hf_id": "facebook/dinov3-vits16-pretrain-lvd1689m",
                  "ms_id": "facebook/dinov3-vits16-pretrain-lvd1689m",
                  "input_size": 256, "patch_size": 16, "num_register_tokens": 4},
    "dinov3_b":  {"backend": "hf_transformers",
                  "hf_id": "facebook/dinov3-vitb16-pretrain-lvd1689m",
                  "ms_id": "facebook/dinov3-vitb16-pretrain-lvd1689m",
                  "input_size": 256, "patch_size": 16, "num_register_tokens": 4},
    "dinov3_l":  {"backend": "hf_transformers",
                  "hf_id": "facebook/dinov3-vitl16-pretrain-lvd1689m",
                  "ms_id": "facebook/dinov3-vitl16-pretrain-lvd1689m",
                  "input_size": 256, "patch_size": 16, "num_register_tokens": 4},
}


def _resolve_dinov3_checkpoint(spec: Dict[str, Any],
                               ms_cache_dir: Optional[Path] = None) -> str:
    """返回可传给 `AutoModel.from_pretrained` 的路径或 repo id。
    优先级：modelscope 镜像（免授权）→ HF 原仓库（需 gated 审批）。
    安装了 modelscope 就自动走镜像；本地已缓存的会直接命中，不重复下载。
    """
    ms_id = spec.get("ms_id")
    hf_id = spec["hf_id"]
    if ms_id:
        try:
            from modelscope import snapshot_download as _ms_snapshot
            if ms_cache_dir is not None:
                try:
                    local = _ms_snapshot(ms_id, cache_dir=str(ms_cache_dir))
                except TypeError:
                    local = _ms_snapshot(ms_id)
            else:
                local = _ms_snapshot(ms_id)
            LOGGER.info("[semantic] DINOv3 权重走 ModelScope 镜像：%s", local)
            return local
        except ImportError:
            LOGGER.info("[semantic] 未安装 modelscope，回退到 HuggingFace；"
                        "如 HF gated 失败请先 `pip install modelscope`。")
        except Exception as e:
            LOGGER.warning("[semantic] ModelScope 下载失败（%s），回退到 HuggingFace。", e)
    if os.path.isdir(hf_id):
        return hf_id
    return hf_id


def supported_models() -> List[str]:
    return list(_SUPPORTED_MODELS.keys())


def _resolve_model_cache_root(cache_dir: Optional[str]) -> Path:
    raw = (cache_dir or "").strip()
    if raw:
        root = Path(raw).expanduser()
    else:
        root = Path(__file__).resolve().parent / "models_cache"
    if not root.is_absolute():
        root = (Path(__file__).resolve().parent / root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


class SemanticModel:
    """DINOv2 / DINOv3 视觉 embedding 包装。每实例独占一份模型；线程不安全。"""

    def __init__(self, name: str, device: str = "auto", cache_dir: Optional[str] = None):
        if name not in _SUPPORTED_MODELS:
            raise ValueError(
                f"unsupported semantic model: {name!r}; "
                f"choose from {supported_models()}"
            )

        try:
            import torch  # noqa: F401
            import torch.nn.functional as F  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "启用语义特征需要安装 torch。参考：\n"
                "  GPU (CUDA 12.x): pip install torch --index-url https://download.pytorch.org/whl/cu121\n"
                "  CPU:            pip install torch --index-url https://download.pytorch.org/whl/cpu"
            ) from e

        import torch

        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device
        self.name = name
        self.cache_root = _resolve_model_cache_root(cache_dir)
        self.hf_home = self.cache_root / "huggingface"
        self.hf_hub_cache = self.hf_home / "hub"
        self.hf_tf_cache = self.hf_home / "transformers"
        self.torch_hub_cache = self.cache_root / "torch_hub"
        self.ms_cache = self.cache_root / "modelscope"
        self.hf_hub_cache.mkdir(parents=True, exist_ok=True)
        self.hf_tf_cache.mkdir(parents=True, exist_ok=True)
        self.torch_hub_cache.mkdir(parents=True, exist_ok=True)
        self.ms_cache.mkdir(parents=True, exist_ok=True)
        # 强制统一到项目目录，便于整目录清理。
        os.environ["HF_HOME"] = str(self.hf_home)
        os.environ["HUGGINGFACE_HUB_CACHE"] = str(self.hf_hub_cache)
        os.environ["TRANSFORMERS_CACHE"] = str(self.hf_tf_cache)
        os.environ["MODELSCOPE_CACHE"] = str(self.ms_cache)

        spec = _SUPPORTED_MODELS[name]
        self.backend: str = spec["backend"]
        self.input_size: int = int(spec["input_size"])
        self.patch_size: int = int(spec["patch_size"])
        self.num_register_tokens: int = int(spec["num_register_tokens"])

        if self.backend == "torch_hub":
            LOGGER.info("[semantic] 加载 DINOv2：%s  device=%s", name, device)
            torch.hub.set_dir(str(self.torch_hub_cache))
            self.model = torch.hub.load(
                spec["repo"], spec["entry"], verbose=False, trust_repo=True
            )
        elif self.backend == "hf_transformers":
            try:
                from transformers import AutoModel
            except ImportError as e:
                raise RuntimeError(
                    "加载 DINOv3 需要 transformers>=4.56：\n"
                    "  pip install -U 'transformers>=4.56' accelerate safetensors"
                ) from e
            checkpoint = _resolve_dinov3_checkpoint(spec, ms_cache_dir=self.ms_cache)
            LOGGER.info("[semantic] 加载 DINOv3：%s (%s)  device=%s", name, checkpoint, device)
            self.model = AutoModel.from_pretrained(checkpoint, cache_dir=str(self.hf_hub_cache))
            # 与 spec 对一下实际 config（DINOv3 S 的 patch_size/num_register_tokens 都能从 config 拿）
            cfg = getattr(self.model, "config", None)
            if cfg is not None:
                self.patch_size = int(getattr(cfg, "patch_size", self.patch_size))
                self.num_register_tokens = int(
                    getattr(cfg, "num_register_tokens", self.num_register_tokens)
                )
        else:
            raise ValueError(f"unknown backend: {self.backend!r}")

        self.model.eval()
        self.model.to(device)

        self._mean = torch.tensor([0.485, 0.456, 0.406], device=device).view(1, 3, 1, 1)
        self._std  = torch.tensor([0.229, 0.224, 0.225], device=device).view(1, 3, 1, 1)

        with torch.no_grad():
            dummy = torch.zeros(1, 3, self.input_size, self.input_size, device=device)
            cls, _ = self._forward(dummy)
        self.dim = int(cls.shape[-1])
        LOGGER.info(
            "[semantic] 就绪：backend=%s 输入=%dx%d patch=%d grid=%d num_register=%d dim=%d",
            self.backend, self.input_size, self.input_size, self.patch_size,
            self.input_size // self.patch_size, self.num_register_tokens, self.dim,
        )

    def _forward(self, batch):
        """统一前向：返回 (cls_tokens, patch_tokens)
          cls_tokens   : (B, D)
          patch_tokens : (B, side*side, D)   side = input_size // patch_size
        每个 backend 内部处理 CLS / register / patch 的切分。
        """
        import torch
        side = self.input_size // self.patch_size
        expected_patches = side * side

        if self.backend == "torch_hub":
            cls = self.model(batch)  # (B, D)
            try:
                feats = self.model.get_intermediate_layers(
                    batch, n=1, return_class_token=False, norm=True
                )
            except TypeError:
                feats = self.model.get_intermediate_layers(batch, n=1)
            tokens = feats[-1] if isinstance(feats, (list, tuple)) else feats
            if tokens.shape[1] == expected_patches + 1:
                tokens = tokens[:, 1:]  # 去掉 CLS
            return cls, tokens

        # hf_transformers: last_hidden_state 形如 (B, 1 + num_register + P, D)
        out = self.model(pixel_values=batch)
        last = out.last_hidden_state  # (B, L, D)
        cls = last[:, 0, :]
        start = 1 + self.num_register_tokens
        tokens = last[:, start:start + expected_patches, :]
        if tokens.shape[1] != expected_patches:
            LOGGER.warning(
                "[semantic] DINOv3 patch token 数异常：got=%d expected=%d（last_hidden_state=%s）",
                tokens.shape[1], expected_patches, tuple(last.shape),
            )
        return cls, tokens

    def _preprocess(self, rgb_frames: List[np.ndarray]):
        """把 RGB uint8 帧列表转成 (N, 3, H, W) 归一化 tensor（放到 device）。"""
        import torch
        import torch.nn.functional as F
        arr = np.stack(rgb_frames, axis=0).astype(np.float32) / 255.0
        arr = np.transpose(arr, (0, 3, 1, 2))
        t = torch.from_numpy(arr).to(self.device)
        if t.shape[-1] != self.input_size or t.shape[-2] != self.input_size:
            t = F.interpolate(t, size=(self.input_size, self.input_size),
                              mode="bilinear", align_corners=False)
        t = (t - self._mean) / self._std
        return t

    def embed_frames_per_frame(self, rgb_frames: List[np.ndarray], batch_size: int = 16) -> Optional[np.ndarray]:
        """对一组 RGB uint8 帧独立编码，返回 **per-frame** L2 归一化 CLS embedding (N, D)。"""
        import torch
        import torch.nn.functional as F

        if not rgb_frames:
            return None

        with torch.no_grad():
            t = self._preprocess(rgb_frames)
            emb_chunks = []
            for i in range(0, t.shape[0], batch_size):
                batch = t[i:i + batch_size]
                cls, _ = self._forward(batch)
                cls = F.normalize(cls, dim=-1)
                emb_chunks.append(cls.cpu())
            all_emb = torch.cat(emb_chunks, dim=0)
            return all_emb.numpy().astype(np.float32)

    def embed_frames_patch_grid(self,
                                rgb_frames: List[np.ndarray],
                                grid_side: int = 8,
                                batch_size: int = 16) -> Optional[np.ndarray]:
        """提取每帧的 patch-level embedding 网格，用于空间分布分析。

        ViT 把 input_size × input_size 的图切成 (input_size/patch_size)² 个 token。
          - DINOv2 s/b/l：patch_size=14，input=224 → 16×16=256 patches/帧
          - DINOv3 s/b/l：patch_size=16，input=256 → 16×16=256 patches/帧
        这里把 16×16 平均池化到 grid_side×grid_side（默认 8×8=64）以控制缓存体积，
        并做 L2 归一化以便后续直接算 cosine 距离。

        返回 shape=(N, grid_side*grid_side, D) 的 float32 矩阵；任何异常返回 None。
        """
        import torch
        import torch.nn.functional as F

        if not rgb_frames:
            return None

        side = int(self.input_size // self.patch_size)
        expected = side * side
        if grid_side < 1 or grid_side > side:
            grid_side = side

        with torch.no_grad():
            t = self._preprocess(rgb_frames)
            out_chunks = []
            for i in range(0, t.shape[0], batch_size):
                batch = t[i:i + batch_size]
                _, tokens = self._forward(batch)  # (B, P, D)
                if tokens.shape[1] != expected:
                    LOGGER.warning("[semantic.patch] patch token 数异常：got=%d expected=%d",
                                   tokens.shape[1], expected)
                    return None
                D = tokens.shape[-1]
                grid = tokens.reshape(tokens.shape[0], side, side, D).permute(0, 3, 1, 2).contiguous()
                if grid_side != side:
                    grid = F.adaptive_avg_pool2d(grid, (grid_side, grid_side))
                grid = grid.permute(0, 2, 3, 1).reshape(tokens.shape[0], grid_side * grid_side, D)
                grid = F.normalize(grid, dim=-1)
                out_chunks.append(grid.cpu())

            all_out = torch.cat(out_chunks, dim=0)
            return all_out.numpy().astype(np.float32)


def load_semantic_model(name: str, device: str = "auto",
                        cache_dir: Optional[str] = None) -> SemanticModel:
    return SemanticModel(name, device, cache_dir=cache_dir)


def semantic_frame_distances(embs_a: Optional[np.ndarray],
                              embs_b: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """
    按 index 对齐的 per-frame cosine 距离数组（映射到 [0, 1]）：
      0 = 完全相同，0.5 = 正交，1 = 反向。
    两段视频的 per-frame embedding 矩阵（shape=(N, D)，L2 归一化）。
    按较短的帧数对齐（前提：两段视频语义抽帧时用相同帧数均匀覆盖全片、且时长同桶）。
    任一侧缺失或维度不一致返回 None。

    两侧可以接受 list-of-list（从 JSON 缓存读回），内部会转 numpy。
    """
    if embs_a is None or embs_b is None:
        return None
    a = np.asarray(embs_a, dtype=np.float32)
    b = np.asarray(embs_b, dtype=np.float32)
    if a.ndim != 2 or b.ndim != 2 or a.shape[1] != b.shape[1] or a.shape[0] == 0 or b.shape[0] == 0:
        return None
    n = min(a.shape[0], b.shape[0])
    a = a[:n]; b = b[:n]
    # 假设已 L2 归一化；保险起见不再归一化以免掩盖问题
    cos = (a * b).sum(axis=-1)
    cos = np.clip(cos, -1.0, 1.0)
    return (0.5 * (1.0 - cos)).astype(np.float32, copy=False)


def semantic_distance(embs_a: Optional[np.ndarray], embs_b: Optional[np.ndarray]) -> float:
    """per-frame cosine 距离的均值。

    - 短视频大部分帧都不同时灵敏（117/457 类）；
    - 长视频只有少数差分帧时会被稀释——这种情况交给 max + peak_ratio 兜底。
    """
    d = semantic_frame_distances(embs_a, embs_b)
    if d is None or d.size == 0:
        return float("nan")
    return float(d.mean())


def semantic_distance_max(embs_a: Optional[np.ndarray],
                          embs_b: Optional[np.ndarray]) -> float:
    """per-frame cosine 距离的最大值——即"最不像的那一帧"有多不像。

    设计目的：即便差分只发生在少数几帧、mean 被稀释成"几乎同视频"的水平，
    max 仍能直接暴露出"存在一帧语义上显著不同"。
    """
    d = semantic_frame_distances(embs_a, embs_b)
    if d is None or d.size == 0:
        return float("nan")
    return float(d.max())


def semantic_distance_p90(embs_a: Optional[np.ndarray],
                           embs_b: Optional[np.ndarray]) -> float:
    """per-frame cosine 距离的 90 分位。

    作用：识别"编码漂移尖峰"——同源不同码率视频会有极少数帧 max 很高，
    但 90% 的帧几乎完全一致，p90 会极低（< 0.005）；
    真差分因差异连续，p90 一般 > 0.008。用作 max/ratio 闸的豁免条件。
    """
    d = semantic_frame_distances(embs_a, embs_b)
    if d is None or d.size == 0:
        return float("nan")
    return float(np.percentile(d, 90))


def patch_center_masked_distances(patch_a: Optional[np.ndarray],
                                  patch_b: Optional[np.ndarray],
                                  grid_side: int = 8,
                                  keep_inner: int = 4) -> Optional[Dict[str, float]]:
    """只在"中心 keep_inner × keep_inner"格内计算 per-frame cosine 距离分布。

    用途：patch 空间判决给出"uncertain + 角落主导"但中心相当干净时，用这个函数
    做最后一道像素级复验——若中心区的 mean/max 都足够小，说明两视频的主要内容
    区域几乎一致，可安全判定为同源。

    Args:
        patch_a/patch_b: shape=(N, P, D) L2-normalized patch embedding 矩阵，
            P = grid_side * grid_side。
        grid_side: 原网格边长。
        keep_inner: 保留的内圈边长（偶数；默认 4，对 8×8 即屏蔽外围两层）。

    Returns:
        dict {"mean": ..., "max": ..., "p90": ..., "n_frames": int, "n_patches": int}
        或 None（shape 不匹配 / 空输入 / keep_inner 非法）。
    """
    if patch_a is None or patch_b is None:
        return None
    a = np.asarray(patch_a, dtype=np.float32)
    b = np.asarray(patch_b, dtype=np.float32)
    if a.ndim != 3 or b.ndim != 3:
        return None
    gs = int(grid_side)
    if a.shape[1] != gs * gs or b.shape[1] != gs * gs:
        return None
    if a.shape[2] != b.shape[2]:
        return None
    n = min(a.shape[0], b.shape[0])
    if n == 0:
        return None

    keep = int(keep_inner)
    if keep < 1 or keep > gs:
        return None
    # 计算保留的中心 patch 索引
    lo = (gs - keep) // 2
    hi = lo + keep
    rr = np.arange(gs).reshape(gs, 1).repeat(gs, axis=1)
    cc = np.arange(gs).reshape(1, gs).repeat(gs, axis=0)
    mask = (rr >= lo) & (rr < hi) & (cc >= lo) & (cc < hi)
    idx = np.where(mask.reshape(-1))[0]
    if idx.size == 0:
        return None

    a = a[:n, idx, :]
    b = b[:n, idx, :]
    # 逐帧、逐 patch cosine 距离 → 每帧取这批 patch 的均值
    cos = (a * b).sum(axis=-1)            # (n, |idx|)
    cos = np.clip(cos, -1.0, 1.0)
    d = 0.5 * (1.0 - cos)                 # (n, |idx|)
    per_frame = d.mean(axis=-1)           # (n,)
    return {
        "mean": float(per_frame.mean()),
        "max": float(per_frame.max()),
        "p90": float(np.percentile(per_frame, 90)),
        "n_frames": int(n),
        "n_patches": int(idx.size),
    }


# ============================ Patch-level 空间分布判决 ============================
#
# 背景：
#   mean/max/ratio 三元闸把整段视频压成一个标量分布统计，判不出"差异到底在画面哪里"。
#   带水印/字幕框的同源视频 vs "几帧内主体局部微调"的差分视频，mean/max/ratio 分布
#   几乎重叠（回归集 G340~G346 vs 用户水印对同时落在 ratio 3~6 区间），算法层难以区分。
#   但两者的**空间位置 + 时序持久性**差异极大：
#     - 真水印：固定几个角落 patch **帧帧都高距** → persistent_hot 多、且在角落
#     - 真差分：高距 patch 在帧间到处飘、持续在同一位置的极少 → persistent_hot 接近 0
#   所以保留 patch 空间分辨率 + 时序统计才能做可靠判决。
#
# 函数契约：
#   输入两侧 patch embedding 矩阵 shape=(N, P, D)，P=grid_side²，已 L2 归一化
#   输出 dict，字段：
#     kind:        "watermark_strong" | "watermark_weak" | "content_diff" | "uncertain"
#     hot, hot_ratio, corner_frac, center_frac, persistent, pers_corner_frac, ...
#
#   kind 判据分层：
#     - **watermark_strong**（能推翻 max 闸 + ratio 闸）：
#         persistent >= persistent_min
#         AND pers_corner_frac >= persistent_corner_min
#         AND corner_frac >= corner_merge_frac
#       语义：少数角落 patch 帧帧都高距 → 肯定是固定位置的水印/字幕框。
#     - **watermark_weak**（仅推翻 ratio 闸）：
#         corner_frac >= corner_merge_frac
#         AND center_frac < weak_center_max
#         AND hot_ratio < weak_hot_ratio_max
#       语义：没持久热点但整体热点集中在边角且稀疏 → 像是重编码边缘抖动，不改 max 判决。
#     - **content_diff**（提示性；不改任何判决，仅 log 用）：
#         center_frac >= center_split_frac
#     - **uncertain**：其它（维持原判决）
#
#   若两侧帧数/patch 数/维度不一致或热点数 < min_hot → uncertain


def patch_spatial_verdict(patch_a: Optional[np.ndarray],
                          patch_b: Optional[np.ndarray],
                          grid_side: int = 8,
                          hot_threshold: float = 0.015,
                          min_hot_patches: int = 12,
                          center_margin: float = 0.4,
                          edge_margin: float = 0.6,
                          corner_merge_frac: float = 0.55,
                          center_split_frac: float = 0.45,
                          # 新判据：时序持久性
                          persistent_frame_frac: float = 0.5,
                          persistent_min: int = 2,
                          persistent_max: int = 8,
                          persistent_corner_min: float = 0.8,
                          weak_center_max: float = 0.12,
                          weak_hot_ratio_max: float = 0.10,
                          # content_diff_heavy：大面积稳定内容差异（S44 型：PERS 高 + 热点铺满 + 不纯角落）
                          heavy_persistent_min: int = 10,
                          heavy_hot_ratio_min: float = 0.20,
                          heavy_pers_corner_max: float = 0.85,
                          # content_diff_center_persistent：中心持久差异（S43 型：持久热点都在中心）
                          center_persistent_corner_max: float = 0.20,
                          center_persistent_total_corner_max: float = 0.25,
                          # watermark_anim：动画型水印（U6 型：位置轻微抖动导致 persistent=0，
                          # 但空间分布仍是明显的角落主导 + 中心近乎无差异）
                          anim_hot_ratio_min: float = 0.15,
                          anim_corner_min: float = 0.65,
                          anim_center_max: float = 0.10) -> dict:
    """按 patch 空间分布 + 时序持久性联合判定两段视频的差异类型。
    详细判据见上方注释。"""
    default = {
        "kind": "uncertain",
        "hot": 0,
        "hot_ratio": 0.0,
        "corner_frac": 0.0,
        "center_frac": 0.0,
        "persistent": 0,
        "pers_corner_frac": 0.0,
        "dom_q_frac": 0.0,
        "n_frames": 0,
        "grid": int(grid_side),
    }
    if patch_a is None or patch_b is None:
        return default
    a = np.asarray(patch_a, dtype=np.float32)
    b = np.asarray(patch_b, dtype=np.float32)
    if a.ndim != 3 or b.ndim != 3:
        return default
    n = min(a.shape[0], b.shape[0])
    if n == 0 or a.shape[1] != b.shape[1] or a.shape[2] != b.shape[2]:
        return default
    P = a.shape[1]
    if P != grid_side * grid_side:
        side = int(round(np.sqrt(P)))
        if side * side != P:
            return default
        grid_side = side
        default["grid"] = side

    a = a[:n]; b = b[:n]
    cos = (a * b).sum(axis=-1)
    cos = np.clip(cos, -1.0, 1.0)
    dist = 0.5 * (1.0 - cos)    # (N, P) in [0, 1]

    hot_mask = dist > float(hot_threshold)
    hot_count = int(hot_mask.sum())
    hot_ratio = hot_count / float(n * P) if n > 0 and P > 0 else 0.0
    default.update({"hot": hot_count, "hot_ratio": hot_ratio, "n_frames": int(n)})
    if hot_count < int(min_hot_patches):
        return default

    idx = np.arange(P, dtype=np.float32)
    rr = idx // grid_side
    cc = idx %  grid_side
    center = (grid_side - 1) / 2.0
    norm = center if center > 0 else 1.0
    r_dist = np.maximum(np.abs(rr - center), np.abs(cc - center)) / norm

    r_dist_tiled = np.broadcast_to(r_dist[None, :], dist.shape)
    hot_r = r_dist_tiled[hot_mask]
    corner = float((hot_r >= float(edge_margin)).mean())
    center_f = float((hot_r <= float(center_margin)).mean())

    # 时序持久性：某 patch 位置在 >= persistent_frame_frac * N 帧中都是热点？
    per_pos_freq = hot_mask.sum(axis=0) / float(n)  # (P,)
    persistent_mask = per_pos_freq >= float(persistent_frame_frac)
    persistent_count = int(persistent_mask.sum())
    if persistent_count > 0:
        pers_corner_frac = float((r_dist[persistent_mask] >= float(edge_margin)).mean())
    else:
        pers_corner_frac = 0.0

    # 主导象限占比（dom_q_frac）：把 grid 划成四象限（TL/TR/BL/BR），
    # 统计累积热点总量在最大象限的比例。真水印几乎都在一个角（DOMQ ≥ 0.55），
    # 而真内容差异的"角落噪声"往往在 4 个角均匀分布（DOMQ ≈ 0.25~0.45）。
    mid = grid_side / 2.0
    row_hi = rr >= mid
    col_hi = cc >= mid
    quadrant = (row_hi.astype(np.int64) * 2 + col_hi.astype(np.int64))
    hot_per_pos = hot_mask.sum(axis=0)  # (P,)
    q_counts = np.zeros(4, dtype=np.int64)
    for q in range(4):
        q_counts[q] = int(hot_per_pos[quadrant == q].sum())
    dom_q_frac = float(q_counts.max()) / hot_count if hot_count > 0 else 0.0

    # 决策树（优先级从水印 → 内容差异 → 不确定）
    # strong_wm：真·固定水印 — 少量持久热点 (2~persistent_max) 全部扎堆在角落。
    strong_wm = (
        int(persistent_min) <= persistent_count <= int(persistent_max)
        and pers_corner_frac >= float(persistent_corner_min)
        and corner >= float(corner_merge_frac)
    )
    # weak_wm：角落主导 + 中心稀疏 + 整体稀疏（低码率重编码边缘抖动）
    weak_wm = (
        corner >= float(corner_merge_frac)
        and center_f < float(weak_center_max)
        and hot_ratio < float(weak_hot_ratio_max)
    )
    # heavy：大量持久热点 + 全图高密度热点 + 不纯角落（S44 型大面积稳定内容差异，语义 max/ratio 可能未触发）
    heavy = (
        persistent_count >= int(heavy_persistent_min)
        and hot_ratio >= float(heavy_hot_ratio_min)
        and pers_corner_frac < float(heavy_pers_corner_max)
    )
    # center_persistent：少量持久热点全部在中心、且总热点也不在角落（S43 型微弱但稳定的中心差异）
    center_persistent = (
        persistent_count >= int(persistent_min)
        and pers_corner_frac <= float(center_persistent_corner_max)
        and corner <= float(center_persistent_total_corner_max)
    )
    # anim_wm：动画型水印（U6 型）——位置在帧间轻微抖动使得没有单个 patch 能稳定地
    # 踩满 persistent_frame_frac，但**整体空间分布**仍然呈现典型水印形态：
    # 中心几乎没有差异 + 角落热点占主导 + 热点密度不低。和 weak_wm 的区别是
    # hot_ratio 允许更高（0.15~），代价是要求 persistent_count==0 以避免误吞真差分。
    anim_wm = (
        persistent_count == 0
        and hot_ratio >= float(anim_hot_ratio_min)
        and corner >= float(anim_corner_min)
        and center_f < float(anim_center_max)
    )
    if strong_wm:
        kind = "watermark_strong"
    elif weak_wm:
        kind = "watermark_weak"
    elif anim_wm:
        kind = "watermark_anim"
    elif heavy:
        kind = "content_diff_heavy"
    elif center_persistent:
        kind = "content_diff_center_persistent"
    elif center_f >= float(center_split_frac):
        kind = "content_diff"
    else:
        kind = "uncertain"

    return {
        "kind": kind,
        "hot": hot_count,
        "hot_ratio": hot_ratio,
        "corner_frac": corner,
        "center_frac": center_f,
        "persistent": persistent_count,
        "pers_corner_frac": pers_corner_frac,
        "dom_q_frac": dom_q_frac,
        "n_frames": int(n),
        "grid": int(grid_side),
    }


def semantic_peak_ratio(embs_a: Optional[np.ndarray],
                        embs_b: Optional[np.ndarray]) -> float:
    """max / mean，衡量"帧间差异分布的尖峰程度"。

    关键判别依据——区分"全局漂移（水印/重编码）"和"局部差分（几帧被改）"：
      - 水印让每帧都有同一小块差异 → 分布平坦 → ratio 通常 2.0 ~ 3.6
      - 差分只发生在少数帧      → 分布尖峰 → ratio 通常 > 3.9

    mean 过小（数值噪声区）时比率会虚高，调用方需同时检查 max 的绝对值。
    """
    d = semantic_frame_distances(embs_a, embs_b)
    if d is None or d.size == 0:
        return float("nan")
    m = float(d.mean())
    mx = float(d.max())
    if m <= 0.0:
        return float("inf") if mx > 0 else 0.0
    return mx / m
