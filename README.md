# wallpaper-engine-video-deduplication
wallpaper engine视频筛重，取消订阅重复视频  
先筛查出重复视频，生成创意工坊链接表格，再靠程序批量访问重复文件的成创意工坊链接网页，取消订阅  
视频筛重靠抽关键帧 + 感知哈希 (pHash)加上chromaprint / fpcalc 音频指纹（可选）  
步骤1：下载ffmpeg/ffprobe/fpcalc，放入可用的环境变量用户变量路径，比如`C:\Windows\System32`  
步骤2：`config.toml`中`workshop_root` 填入wallpaper engine的创意工坊目录  
步骤3：安装环境：`pip install imagehash pillow pandas openpyxl tqdm numpy`  
步骤4：执行：`python we_duplicate_finder_readonly.py -c config.toml`，执行完成生成的文件在脚本子目录`output`文件夹内  
步骤5：在默认浏览器中登录steam账号  
步骤6：在浏览器中安装tampermonkey等脚本插件，导入`wallpaper-engine-video-deduplication.js`文件  
步骤7：进入output目录，在路径下执行：`python bulk_unsub_controller.py --xlsx xxx.xlsx --batch-size 1`  
其中`xxx.xlsx`为执行：`python we_duplicate_finder_readonly.py -c config.toml`最后在`output`文件夹生成的xlsx文件名称  
PS:如果网络不稳定导致取消订阅的时候卡住了，刷新下标签页就行
