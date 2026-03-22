# Wallpaper Engine 视频去重工具

Wallpaper Engine 创意工坊视频筛重 + 批量取消订阅。

- **筛重**：抽关键帧 + 感知哈希 (pHash)，可选 chromaprint 音频指纹，自动检测同内容不同分辨率/编码的重复视频
- **取消订阅**：根据筛重结果批量取消订阅重复项，保留文件最大的版本
- **图形界面**：Tkinter UI，所有参数可视化编辑，实时日志输出

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

UI 包含两个标签页：

- **筛重 / 查重**：编辑/加载/保存 `config.toml` 的全部控制参数，一键运行筛重，实时查看日志
- **取消订阅**：选择筛重生成的 xlsx 文件，配置批量取消订阅参数，支持"仅保留最大文件"模式

### 方式二：命令行

**筛重：**

```bash
python we_duplicate_finder_readonly.py -c config.toml
```

结果输出到 `output/` 目录下的 CSV 和 XLSX 文件。

**取消订阅：**

1. 在默认浏览器中登录 Steam 账号
2. 安装 Tampermonkey 等脚本插件，导入 `wallpaper-engine-video-deduplication.js`
3. 执行：

```bash
cd output
python bulk_unsub_controller.py --xlsx duplicates_xxx.xlsx --batch-size 1 --single-page
```

> 如果网络不稳定导致取消订阅卡住，刷新浏览器标签页即可。

## 配置说明

编辑 `config.toml`（或通过 UI 编辑）：

```toml
# 创意工坊目录
workshop_root = "E:\\SteamLibrary\\steamapps\\workshop\\content\\431960"

# 采样参数
sample_frames = 36          # 抽帧数量，越多越精确
phash_size = 12             # pHash 哈希大小，越大越精确
video_window_seconds = 15   # 视频采样窗口（秒）

# 匹配阈值
phash_distance_threshold = 0.6   # 组合距离分（推荐 0.5~0.7，越小越严格）

# 音频指纹（可选）
require_both_signatures = false  # true=同时要求视频+音频匹配
```

### 匹配算法

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
|---|---|
| 同视频不同质量 | ~0.3 |
| 同视频不同分辨率（4K vs 1080p）| ~0.5 |
| 不同角色/服装同动作 | >0.7 或 ∞ |

### 断点续跑

签名数据缓存在 `output/we_dedup_cache.sqlite3`，中断后重跑自动跳过已计算的文件。

## 文件结构

```
├── we_ui.py                          # 图形界面
├── we_duplicate_finder_readonly.py   # 筛重核心脚本
├── config.toml                       # 配置文件
├── requirements.txt                  # Python 依赖
├── output/
│   ├── bulk_unsub_controller.py      # 取消订阅脚本
│   ├── we_dedup_cache.sqlite3        # 签名缓存数据库
│   └── duplicates_*.xlsx             # 筛重结果
└── wallpaper-engine-video-deduplication.js  # Tampermonkey 脚本
```
