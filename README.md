# Wallpaper Engine 视频去重工具

Wallpaper Engine 创意工坊视频筛重 + 批量取消订阅 + **已下架物品检测与本地归档**。

- **筛重**：抽关键帧 + 感知哈希 (pHash)，可选 chromaprint 音频指纹，自动检测同内容不同分辨率/编码的重复视频
- **取消订阅**：根据筛重结果批量取消订阅重复项，保留文件最大的版本
- **下架归档**：检测创意工坊已下架/不可见物品，复制到 `projects/myprojects`，按原文件夹位置更新 `config.json`，并可批量取消订阅
- **图形界面**：Tkinter UI，参数可视化编辑，实时日志输出

## 环境准备

### 1. 安装外部工具

下载以下工具并确保在系统 PATH 中可用（或在 `config.toml` 中指定完整路径）：

- [ffmpeg / ffprobe](https://ffmpeg.org/download.html)
- [fpcalc (chromaprint)](https://acoustid.org/chromaprint)（仅启用音频指纹时需要）

### 2. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

## 使用方法

### 方式一：图形界面（推荐）

```bash
python we_ui.py
```

UI 包含三个标签页：

| 标签页 | 说明 |
|--------|------|
| **筛重 / 查重** | 编辑/加载/保存 `config.toml` 的全部筛重参数，一键运行筛重，实时日志 |
| **取消订阅** | 选择筛重生成的 `duplicates_*.xlsx`，配合油猴脚本批量取消订阅，支持「仅保留最大文件」 |
| **下架归档** | 检测已下架物品、归档到本地 `myprojects`、生成链接并启动取消订阅流程 |

**下架归档** 标签页需填写：

- **WE 安装目录**：含 `config.json` 的 Wallpaper Engine 根目录
- **Steam API Key（推荐）**：在 [Steam 开发者](https://steamcommunity.com/dev/apikey) 免费申请；有 Key 时使用 `IPublishedFileService` 检测，比无 Key 的旧接口更准确

「取消订阅已下架」会打开 `https://steamcommunity.com/my/myworkshopfiles?...`（`/my/` 在已登录浏览器中自动对应当前账号，无需填写个人资料 URL）。

建议流程：**检测下架物品** → **归档到本地**（复制 + 改 `config.json`）→ **取消订阅已下架**（浏览器 + 油猴，需已登录 Steam）。

### 方式二：命令行

**筛重：**

```bash
python we_duplicate_finder_readonly.py -c config.toml
```

结果输出到 `output/` 目录下的 CSV 和 XLSX 文件。

**下架检测 / 归档：**

```bash
# 仅检测（结果写入 output/delisted_items.json）
python we_delisted_archiver.py -c config.toml --detect

# 仅归档（需先有 delisted_items.json）
python we_delisted_archiver.py -c config.toml --archive

# 检测 + 归档
python we_delisted_archiver.py -c config.toml --both

# 指定 Steam API Key（覆盖 config）
python we_delisted_archiver.py -c config.toml --detect --steam-api-key YOUR_KEY
```

**取消订阅：**

1. 在默认浏览器中登录 Steam 账号
2. 安装 Tampermonkey，导入 `wallpaper-engine-video-deduplication.js`
3. 执行：

```bash
cd output
python bulk_unsub_controller.py --xlsx duplicates_xxx.xlsx --batch-size 1 --single-page
```

下架物品取消订阅时，UI 会生成 `delisted_unsub_*.xlsx` 并打开 `steamcommunity.com/my/myworkshopfiles?...#bulk_unsub=2`（与 `#bulk_unsub=1` 行为一致，便于区分用途）。

> 如果网络不稳定导致取消订阅卡住，刷新浏览器标签页即可。

## 配置说明

编辑 `config.toml`（或通过 UI 编辑）：

```toml
# 创意工坊目录
workshop_root = "E:\\SteamLibrary\\steamapps\\workshop\\content\\431960"

# 输出目录
output_dir = "output"

# 采样参数
sample_frames = 36          # 抽帧数量，越多越精确
phash_size = 12             # pHash 哈希大小，越大越精确
video_window_seconds = 15   # 视频采样窗口（秒）

# 匹配阈值
phash_distance_threshold = 0.6   # 组合距离分（推荐 0.5~0.7，越小越严格）

# 音频指纹（可选）
require_both_signatures = false  # true=同时要求视频+音频匹配

# —— 下架归档 ——
we_install_dir = "E:\\SteamLibrary\\steamapps\\common\\wallpaper_engine"
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

筛重使用**组合距离分**判定重复：

```
分数 = 截尾均值 / (1 + 标准差)
```

- 逐帧 pHash 汉明距离归一化到 64 位基准
- 截尾均值去掉最高 10% 异常帧，降低编码差异影响
- 截尾均值 > 4.0 直接判定为不同内容（防止换装/换角色误判）
- 标准差越高说明距离分布越分散（编码差异特征），分数越低越容易匹配

典型分数参考：

| 场景 | 分数 |
|------|------|
| 同视频不同质量 | ~0.3 |
| 同视频不同分辨率（4K vs 1080p）| ~0.5 |
| 不同角色/服装同动作 | >0.7 或 ∞ |

### 断点续跑

签名数据缓存在 `output/we_dedup_cache.sqlite3`，中断后重跑自动跳过已计算的文件。

## 文件结构

```
├── we_ui.py                          # 图形界面（筛重 / 取消订阅 / 下架归档）
├── we_duplicate_finder_readonly.py   # 筛重核心脚本
├── we_delisted_archiver.py           # 下架检测 + 归档 + 生成取消订阅 xlsx
├── config.toml                       # 配置文件
├── requirements.txt                  # Python 依赖
├── 取消收藏已下架的创意工坊物品-0.1.user.js   # 参考用：浏览器内检测下架并取消订阅
├── output/
│   ├── bulk_unsub_controller.py      # 取消订阅协调（本地 HTTP + 打开浏览器）
│   ├── we_dedup_cache.sqlite3        # 签名缓存数据库
│   ├── duplicates_*.xlsx             # 筛重结果
│   ├── delisted_items.json           # 下架检测结果（由 we_delisted_archiver 生成）
│   └── delisted_unsub_*.xlsx         # 下架物品取消订阅列表（UI 生成）
└── wallpaper-engine-video-deduplication.js  # Tampermonkey：#bulk_unsub=1 / =2
```

## 相关脚本说明

- **`wallpaper-engine-video-deduplication.js`**：URL 含 `#bulk_unsub=1` 或 `#bulk_unsub=2` 时与 `bulk_unsub_controller.py` 配合，在浏览器内调用 Steam 取消订阅接口（使用当前登录 Cookie）。
- **`取消收藏已下架的创意工坊物品-0.1.user.js`**：在订阅列表分页爬取并访问详情页判断下架；本仓库的 Python 检测以 API 为主，可与油猴流程互补。
