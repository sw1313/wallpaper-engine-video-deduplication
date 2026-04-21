# Wallpaper Engine 视频去重工具

Wallpaper Engine 创意工坊视频筛重 + 批量取消订阅 + **已下架物品检测与本地归档**。

- **筛重**：抽关键帧 + 感知哈希 (pHash) + DINOv2/v3 patch 级空间裁决，可选 chromaprint 音频指纹；默认**同时扫描创意工坊目录与** `projects/myprojects` **本地项目**，可发现跨订阅与本地的重复视频；同大小时 myprojects 默认作为保留项
- **取消订阅**：根据筛重结果批量取消订阅重复项，保留文件最大的版本
- **下架归档 / 按文件夹归档**：检测已下架/不可见物品 + 任意指定的 WE 文件夹，整批 `workshop → myprojects` 归档并同步 `config.json`，随后可一键批量取消订阅
- **图形界面**：PySide6 (Qt 6) UI，参数可视化编辑，实时日志输出

## 环境准备

### 1. 安装外部工具

下载以下工具并确保在系统 PATH 中可用（或在 `config.toml` 中指定完整路径）：

- [ffmpeg / ffprobe](https://ffmpeg.org/download.html)
- [fpcalc (chromaprint)](https://acoustid.org/chromaprint)（仅启用音频指纹时需要）

### 2. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 3. 准备 `config.toml`

仓库只提交模板 `config.example.toml`，真正的 `config.toml` 由你本地自行维护（已被 `.gitignore` 忽略，避免不小心把本机路径 / API Key 推上来）：

```bash
# Windows PowerShell
Copy-Item config.example.toml config.toml
# Linux / macOS
cp config.example.toml config.toml
```

按你的环境修改 `workshop_root` / `we_install_dir` / 外部工具路径即可，阈值保持默认。

## 使用方法

### 方式一：图形界面（推荐）

```bash
python we_ui.py
```

UI 包含三个标签页：

| 标签页 | 说明 |
|--------|------|
| **筛重 / 查重** | 编辑筛重参数；填写 `we_install_dir` 并勾选 `include_myprojects` 后，会一并扫描 `…/projects/myprojects` 下各子文件夹内的视频 |
| **取消订阅** | 选择筛重 `duplicates_*.xlsx`：每行首列保留，其余列 Steam 由油猴退订，myprojects 本地链会先删文件夹 |
| **下架归档** | 两类归档 + 两类退订：针对"已下架物品"的自动流程，和针对"任意 WE 文件夹"的手动流程 |

**下架归档** 与 **筛重** 共用 **WE 安装目录**（`we_install_dir`，含 `config.json` 的 WE 根目录）。下架检测建议填写 **Steam API Key**（[申请地址](https://steamcommunity.com/dev/apikey)）：有 Key 时用 `IPublishedFileService`，比无 Key 更准确。

「取消订阅已下架」和「取消订阅手动归档」都会打开 `https://steamcommunity.com/my/myworkshopfiles?...`（`/my/` 在已登录浏览器中自动对应当前账号，无需填写个人资料 URL）。

下架归档页共 5 个操作按钮：

1. **检测下架物品**：调 Steam API 判断哪些订阅项已被删除 / 隐藏，结果写到 `output/delisted_items.json`
2. **下架物品归档到本地**：把 `delisted_items.json` 里的物品整夹复制到 `projects/myprojects/`，并把路径写回 `config.json` 原文件夹
3. **指定文件夹归档到本地**：弹出 WE 文件夹选择器（列出 `config.json` 所有分类 + 每个分类的 `workshop` / 已归档 数量），选定后把该文件夹内**所有** workshop 项批量归档到 `myprojects`（已归档的自动跳过）
4. **取消订阅已下架**：生成 `delisted_unsub_*.xlsx` 并驱动浏览器 + 油猴退订 `delisted_items.json` 里的物品
5. **取消订阅手动归档**：弹出文件夹选择器，只退订"workshop 订阅中 且 myprojects 已存在"的那部分 wid（未归档的保持订阅），带二次确认弹窗

两套推荐流程：

- **下架物品自动清理**：检测下架物品 → 下架物品归档到本地 → 取消订阅已下架
- **按分类手动归档**：指定文件夹归档到本地 → 取消订阅手动归档

### 方式二：命令行

**筛重：**

```bash
python we_duplicate_finder_readonly.py -c config.toml
```

结果输出到 `output/` 目录下两份 XLSX：

- **`duplicates_{ts}.xlsx`**：创意工坊链接版。每行**第一格 = 保留项**：按文件体积降序排；**大小相等时 `projects/myprojects` 那份优先排前**，保证退订的永远是还没归档的 workshop 版本。取消订阅流程（`bulk_unsub_controller.py`）只认这个文件。myprojects 本地项列为 `file:///...` 本地链接。
- **`duplicate_paths_{ts}.xlsx`**：所在文件夹路径版（`…\431960\<id>\` / `…\myprojects\<子文件夹>\`）。方便在资源管理器直接打开；Excel 里点击单元格也能开，不会被取消订阅流程误选（文件名前缀故意与 `duplicates_` 区分）。

**下架检测 / 归档：**

```bash
# 仅检测（结果写入 output/delisted_items.json）
python we_delisted_archiver.py -c config.toml --detect

# 仅归档下架物品（需先有 delisted_items.json）
python we_delisted_archiver.py -c config.toml --archive

# 检测 + 归档
python we_delisted_archiver.py -c config.toml --both

# 指定 Steam API Key（覆盖 config）
python we_delisted_archiver.py -c config.toml --detect --steam-api-key YOUR_KEY
```

**按 WE 文件夹批量归档 / 查询（JSON 到 stdout，日志到 stderr）：**

```bash
# 列出 config.json 所有文件夹 + 每个文件夹的 workshop / 已归档计数
python we_delisted_archiver.py -c config.toml --list-folders

# 把第 N 个文件夹里的全部 workshop 项归档到 myprojects（已归档跳过）
python we_delisted_archiver.py -c config.toml --archive-folder-index 2

# 列出第 N 个文件夹里已归档的 wid（可喂给 bulk_unsub_controller 退订）
python we_delisted_archiver.py -c config.toml --list-archived-in-folder-index 2
```

> `--list-folders` / `--list-archived-in-folder-index` 只读，输出纯 JSON 到 stdout，便于脚本化接入；日志统一走 stderr。

**取消订阅：**

1. 在默认浏览器中登录 Steam 账号
2. 安装 Tampermonkey，导入 `wallpaper-engine-video-deduplication.js`
3. 执行：

```bash
cd output
python bulk_unsub_controller.py --xlsx duplicates_xxx.xlsx --batch-size 1 --single-page
```

`bulk_unsub_controller.py` 固定按筛重表语义：**每行第一个链接保留**；从第二列起，若为 **`file:///.../myprojects/...`** 则**直接删除**对应 `myprojects/<子项目文件夹>` 整夹，若为 **`http` Steam 链接**则交给油猴取消订阅。

下架物品取消订阅时，UI 会生成 `delisted_unsub_*.xlsx`；按文件夹批量退订时生成 `archived_unsub_*.xlsx`。两者都会打开 `steamcommunity.com/my/myworkshopfiles?...#bulk_unsub=2`（与 `#bulk_unsub=1` 行为一致，便于区分用途）。

> 如果网络不稳定导致取消订阅卡住，刷新浏览器标签页即可。

## 配置说明

编辑 `config.toml`（或通过 UI 编辑）：

```toml
# 创意工坊目录
workshop_root = "E:\\SteamLibrary\\steamapps\\workshop\\content\\431960"
# 筛重时是否扫描 WE 下的 projects/myprojects（需配置 we_install_dir）
include_myprojects = true
# WE 安装根目录（与下架归档共用）
we_install_dir = "E:\\SteamLibrary\\steamapps\\common\\wallpaper_engine"

# 输出目录
output_dir = "output"

# 采样参数
sample_frames = 36          # 抽帧数量，越多越精确
phash_size = 12             # pHash 哈希大小，越大越精确
video_window_seconds = 15   # 视频采样窗口（秒）

# 匹配阈值（针对以下需求校准，一般不用改）
#   合并：同视频不同码率 / 不同分辨率 / 带不带字幕
#   不合并：同视频不同角色 / 服饰 / 差分
phash_distance_threshold = 1.5   # pHash 组合距离分阈值，越小越严格
phash_trimmed_mean_cap = 10.0    # 截尾均值上限（64 位基准），> 此值直接判为不同内容
phash_trim_ratio = 0.2           # 丢弃最高距离帧的比例，抑制对齐漂移帧
phash_bimodal_gap_cap = 30.0     # 双峰差上限；水印+不同码率也会造成双峰，放宽由后面语义闸把关
color_hist_bins_h = 16           # HSV 色相 bin 数
color_hist_bins_s = 4            # HSV 饱和度 bin 数
color_distance_threshold = 0.10  # 颜色直方图距离阈值（Bhattacharyya），挡"同场景不同角色/服饰"

# —— 视觉语义特征（可选第三道闸）——
# 专治 pHash + 颜色双失效的"同源局部差分"类：表情差分/道具差分/场景元素变体——
# 少数帧有局部形变，全局统计看不见，但自监督视觉模型能区分。
# 首次启用会从 github 下载模型权重（~/.cache/torch/hub）：dinov2_s ~84MB / _b ~330MB / _l ~1.1GB
semantic_feature_enabled = false
semantic_feature_model = "dinov2_s"           # dinov2_s / dinov2_b / dinov2_l
semantic_feature_device = "auto"              # auto / cuda / cpu
semantic_sample_frames = 60                   # 语义专用：全片均匀抽帧数，建议 >= 48
semantic_distance_threshold = 0.015           # mean 上限：整体性差异
semantic_max_threshold = 0.040                # max 上限：绝对值兜底
semantic_peak_ratio_threshold = 3.8           # max/mean 上限（尖峰闸核心，区分水印vs差分）
semantic_peak_min_max = 0.015                 # 尖峰闸前置：max 超过此值才按 ratio 判
semantic_drift_p90_exempt = 0.005             # 平坦漂移例外：p90 <= 此值时豁免 max/ratio 尖峰闸
semantic_drift_sparse_mid_count = 2           # 稀疏超级尖峰例外：中间带(0.5×max闸, max闸]帧数 <= 此值时豁免

# —— 人工覆写（兜底，默认空）——
# 算法仍误判的极少数边缘对，在这里直接声明强制覆盖。格式 "wid_A|wid_B"。
# 先做 force_split（拆冲突对）再做 force_merge（union）。
# force_merge_pairs = ["3038539716|3625519587"]
# force_split_pairs = ["3707313669|3707336138"]
force_merge_pairs = []
force_split_pairs = []

# 音频指纹（可选）
require_both_signatures = false  # true=同时要求视频+音频匹配

# —— 下架归档 ——
steam_api_key = ""   # 推荐填写：https://steamcommunity.com/dev/apikey
```

### 下架检测逻辑（与 Steam 状态对照）

脚本将「不可用」的物品视为下架，包括但不限于：

- `result != 1`（如彻底删除 `9` / `17`）
- `banned != 0`（违规下架）
- `visibility != 0`（作者隐藏 / 私密 / 仅好友等，**需新 API 或完整字段**）
- 元数据异常（如无标题、API 未返回该 ID）

**有 `steam_api_key`**：使用 `IPublishedFileService/GetDetails/v1/`，字段更全，与常见对照表一致（正常：`result=1, banned=0, visibility=0`）。

**无 Key**：回退 `ISteamRemoteStorage/GetPublishedFileDetails/v1/`，无需认证，但部分物品可能漏检，建议配置 Key。

### 归档与 config.json

- 将 `workshop/content/431960/<id>/` **整夹复制**到 `wallpaper_engine/projects/myprojects/<id>/`
- 在 `config.json` 的 **原文件夹** 中增加一条指向本地视频的 UNC 路径（与你在 WE 里放的文件夹一致）
- 若该物品**只在主页、未在任何文件夹**：不写进文件夹，由 WE 从 `myprojects` 自动发现
- 写回前会备份 `config.json.bak`，再原子替换

### 匹配算法（筛重）

**业务定义**：

- ✓ 合并：同视频不同码率 / 不同分辨率 / 带不带字幕
- ✗ 不合并：同视频不同角色 / 服饰 / 差分

**抽帧阶段**：在视频中段窗口等距抽取 N 张 RGB 帧：

- 默认抽 64×64 给 pHash + 颜色直方图用（中段 `video_window_seconds` 窗口）
- 启用语义特征时，**额外**抽一份 224×224 给 DINOv2 推理，并且**改为全片均匀抽** `semantic_sample_frames` 帧（默认 60）。全片抽是关键——差分帧可能集中在视频某一段，中段窗口会漏掉。缓存独立，关掉即不抽。

三个特征：

1. **pHash**：转灰度后做 12×12 DCT 感知哈希（默认），对构图/明暗/轮廓敏感
2. **颜色直方图**：HSV 空间 (色相 × 饱和度) 2D 直方图（默认 16×4 bin），多帧求平均后归一化，抓服饰/肤色/主色调
3. **语义 embedding**（可选）：DINOv2 自监督视觉模型，**每帧独立编码 + L2 归一化**，保留 per-frame embedding 矩阵。抓"像素很像但语义不同"的场景（差分类）

**匹配阶段**（距离归一化到 64 位 / [0,1] 基准后，三道 pHash 闸 + 颜色闸 + 语义闸）：

1. **pHash 双峰差闸**：`高半段均值 − 低半段均值` > `phash_bimodal_gap_cap`（默认 40.0）→ 不同
2. **pHash 截尾均值闸**：丢掉最高 `phash_trim_ratio`（默认 20%）后均值 > `phash_trimmed_mean_cap`（默认 12.0）→ 不同
3. **pHash 组合分**：`截尾均值 / (1 + 标准差)` > `phash_distance_threshold`（默认 1.5）→ 不同
4. **颜色闸**：Bhattacharyya 距离 > `color_distance_threshold`（默认 0.15）→ 不同
5. **语义闸**（两侧都有 embedding 才生效，**三元 OR 逻辑**，任一触发都挡下）：
   - **mean 闸**：`mean(per-frame cosine)` > `semantic_distance_threshold`（默认 0.015）→ 整体性差异
   - **max 闸**：`max(per-frame cosine)` > `semantic_max_threshold`（默认 0.040）→ 存在极端偏离帧
   - **尖峰闸**（核心）：`max/mean` > `semantic_peak_ratio_threshold`（默认 3.8）**且** `max` > `semantic_peak_min_max`（默认 0.015）→ 分布尖峰
   - **编码漂移例外**（两种模式，OR；命中即豁免 max / 尖峰闸，mean 闸仍生效）：
     - **A. 平坦漂移**：`p90(per-frame) <= semantic_drift_p90_exempt`（默认 0.005）——绝大多数帧几乎完全一致（同源不同码率的 decoder 细漂移）
     - **B. 稀疏超级尖峰**：`max > semantic_max_threshold` **且** 中间带 `(0.5×semantic_max_threshold, semantic_max_threshold]` 区间帧数 `<= semantic_drift_sparse_mid_count`（默认 2）**且** `mean <= semantic_distance_threshold`——极少数孤立脏帧（水印关键帧突变 / decoder 对齐漂移），但整体 mean 未超且"中间没有过渡帧"。真差分的差异连续，中间带帧数普遍 ≥ 3，所以不会触发此豁免。

> **为什么 per-frame 而不是 mean-pooled embedding？** 实测 mean-pool 会把少数差分帧的语义差异稀释到整体平均里——"局部差分 ≈ 同视频 ≈ 0.0001"，彻底失效；而 per-frame 策略让每一对帧独立贡献。
>
> **为什么关键是"尖峰闸"（max/mean）而不是 P90 或 max 单阈值？** 水印/不同码率的同一视频在 60 帧每帧都有小漂移（mean≈0.005~0.011，max≈0.01~0.04），而同源局部差分大部分帧是 0、少数帧 0.03~0.10——**两者在 mean、P90、max 的绝对值上都有重叠，无法单阈值分开**。但分布形状完全不同：水印是"平坦分布"（max/mean≈2~3.5），局部差分是"尖峰分布"（max/mean≈4~6+）。`max/mean > 3.8` 是实测下可干净分开两类的最窄安全区；`semantic_peak_min_max` 是前置保护，防止 mean 接近 0 时 ratio 虚高。

**什么时候开启语义闸**：pHash 和颜色都是**全局统计**，对"同源视频+少数帧局部形变"（表情/道具/场景元素变体/长视频局部段差分）会判相似（pHash 甚至可能 0.000）——**只有语义模型能看穿这类差分**。代价是需要装 torch、首次跑下载模型、每个视频多一次 224×224 抽帧。

| 类型 | pHash | 颜色 | 语义 mean | 语义 max | max/mean | 结果 |
|---|---|---|---|---|---|---|
| 同视频不同码率/分辨率/带字幕 | 低 | ~0.01 | 0.0006 ~ 0.008 | 0.002 ~ 0.012 | ≈ 2 | 合并 ✓ |
| **同视频不同码率（decoder 对齐漂移尖峰）** | 低 | ~0.09 | ~0.008 | **0.117** | **14.4** | 合并 ✓（**p90≈0.004 触发平坦漂移例外**） |
| **同视频带孤立水印/关键帧突变尖峰** | 低 | ~0.03 | 0.005 ~ 0.011 | **0.047 ~ 0.154** | **5.8 ~ 14.3** | 合并 ✓（**中间带 ≤ 2 触发稀疏尖峰例外**） |
| 同视频带水印 / 重编码漂移 | 0.7~1.1 (或 inf) | 0.02 ~ 0.07 | 0.004 ~ 0.011 | 0.011 ~ 0.038 | **2.2 ~ 3.65** | 合并 ✓ |
| 同场景不同角色（人形差异小）| 低 | 0.115 ~ 0.18 | - | - | - | 不合并（颜色挡）✗ |
| 同场景不同角色（服饰/主色差异明显）| 高 | > 0.5 | - | - | - | 不合并（pHash/颜色挡）✗ |
| **同源局部差分（短视频）** | ≈0 | ≈0.004 | **0.017 ~ 0.020** | 0.05 ~ 0.09 | 3~5 | **mean 闸挡** ✗ |
| **同源局部差分（长视频局部段）** | 0.2 ~ 1.0 | 0.01 ~ 0.04 | 0.005 ~ 0.011 *(稀释)* | **0.019 ~ 0.069** | **3.94 ~ 6.46** | **max 闸 / 尖峰闸挡** ✗ |

**调参建议**：

- **误合并（不同角色被合）** → 先**调小** `color_distance_threshold`（比如 0.10 → 0.08）
- **误合并（短视频差分类被合）** → **调小** `semantic_distance_threshold`（0.015 → 0.012）
- **误合并（长视频局部差分被合）** → 先看 log 里挡不住的那对的 `max` 和 `max/mean`
  - 如果 `max` 在 0.030~0.040 之间 → **调小** `semantic_max_threshold`（0.040 → 0.030）
  - 如果 `max/mean` 在 3.5~3.8 之间 → **调小** `semantic_peak_ratio_threshold`（3.8 → 3.6）
- **漏合并（水印/重编码被误拆）** → 看 log 里挡下的指标
  - `[semantic-gate/max]` 且 log 中 `p90` 很低（< 0.008）→ 优先**调大** `semantic_drift_p90_exempt`（0.005 → 0.008）救回"平坦漂移"
  - `[semantic-gate/max]` 且 log 中 mean 很小、中间带帧数(`mid=...`)很少但 max 飙得很高 → **调大** `semantic_drift_sparse_mid_count`（2 → 3）救回"稀疏超级尖峰"；但注意 ≥3 会开始碰到真差分
  - `[semantic-gate/peak]` → **调大** `semantic_peak_ratio_threshold`（3.8 → 4.0），或 `semantic_peak_min_max`（0.015 → 0.020）让低 max 情况下不启用尖峰闸
  - 看到 `[semantic-gate/drift-exempt:flat(p90)]` 或 `[semantic-gate/drift-exempt:sparse-peak]` 日志即说明该对被哪种编码漂移例外放行了
- **差分帧很少很小**（只改了 1-2 帧的道具）→ **调大** `semantic_sample_frames`（60 → 96/120），提高采样到差分帧的概率
- `phash_bimodal_gap_cap` 和 `phash_distance_threshold` 现在由后面语义闸兜底，可以保持默认放宽值（40/1.5），调小可能反而误杀水印/重编码对

**语义特征安装**：

```bash
# GPU（CUDA 12.x，根据显卡驱动选 index-url）
pip install torch --index-url https://download.pytorch.org/whl/cu121
# CPU 版本
pip install torch --index-url https://download.pytorch.org/whl/cpu
```

首次启用后，程序会自动下载 DINOv2 权重。默认缓存目录为项目内 `models_cache/`（可通过 `model_cache_dir` 改）。

### Patch 级空间裁决 + 中心 mask 复验

DINO 的 per-frame 全局距离只能告诉你"两段视频差多少"，但分不开"角落水印/字幕"和"中央内容差分"。本项目额外跑一层 patch 级裁决：

1. **Patch grid**：把每帧 DINO token 按 8×8 空间网格折回，单独比 cosine，得到每个 patch 的距离热图。
2. **形态分类**：按热点占比 / 角落 vs 中心占比 / 持续帧数判断是 `watermark`（贴边噪声）、`watermark_anim`（动态水印）、`content_diff`（中央内容差分）还是 `uncertain`。
3. **Ratio / Max rescue**：`uncertain` 但明显"角落主导" → 走 ratio-rescue 或 max-rescue 放行。
4. **中心 patch mask 复验**（rescue 的最后一道闸）：屏蔽最外圈两层 patch，只看内圈 4×4 中心区的 mean / max / p90 / hot-ratio，全部低于阈值才真正合并。对应 config 里 `semantic_patch_center_mask_*` 系列。
    - 严格层：`mean ≤ 0.006 & max ≤ 0.025 & hot_ratio ≤ 0.06`
    - 宽松层（仅 max-corner-dominant 候选）：`mean ≤ 0.0065 & max ≤ 0.060 & p90 ≤ 0.010 & corner ≥ 0.85 & center ≤ 0.05 & dom_q ≤ 0.30`

两层联立既能把"角落水印 / 字幕 / 边缘重压"放行，又能把"角落都有不同内容"的差分拦下。

### 人工覆写（兜底）

算法在极端边缘案例仍可能误判。`force_merge_pairs` 和 `force_split_pairs` 允许显式声明"这两个 wid 就是该合 / 该拆"，**一锤定音**。格式为 `"wid_A|wid_B"` 字符串数组，流水线执行顺序：**clustering → force_split → force_merge → 导出**。

### 断点续跑

签名数据缓存在 `output/we_dedup_cache.sqlite3`，中断后重跑自动跳过已计算的文件。

## 文件结构

```
├── we_ui.py                          # 图形界面（筛重 / 取消订阅 / 下架归档）
├── we_duplicate_finder_readonly.py   # 筛重核心脚本
├── we_delisted_archiver.py           # 下架检测 + 归档 + 按文件夹归档 + 生成退订 xlsx
├── semantic_features.py              # DINOv2/v3 加载与 patch 级空间裁决
├── config.example.toml               # 配置模板（真实 config.toml 本地自建，已 .gitignore）
├── requirements.txt                  # Python 依赖
├── 取消收藏已下架的创意工坊物品-0.1.user.js   # 参考用：浏览器内检测下架并取消订阅
├── output/
│   ├── bulk_unsub_controller.py      # 取消订阅协调（本地 HTTP + 打开浏览器）
│   ├── we_dedup_cache.sqlite3        # 签名缓存数据库
│   ├── duplicates_*.xlsx             # 筛重结果（创意工坊链接版，取消订阅流程使用）
│   ├── duplicate_paths_*.xlsx        # 筛重结果（所在文件夹路径版，人工处理用）
│   ├── delisted_items.json           # 下架检测结果（由 we_delisted_archiver 生成）
│   ├── delisted_unsub_*.xlsx         # 下架物品取消订阅列表（UI 生成）
│   └── archived_unsub_*.xlsx         # 按文件夹取消订阅列表（UI 生成）
└── wallpaper-engine-video-deduplication.js  # Tampermonkey：#bulk_unsub=1 / =2
```

## 相关脚本说明

- **`wallpaper-engine-video-deduplication.js`**：URL 含 `#bulk_unsub=1` 或 `#bulk_unsub=2` 时与 `bulk_unsub_controller.py` 配合，在浏览器内调用 Steam 取消订阅接口（使用当前登录 Cookie）。
- **`取消收藏已下架的创意工坊物品-0.1.user.js`**：在订阅列表分页爬取并访问详情页判断下架；本仓库的 Python 检测以 API 为主，可与油猴流程互补。
