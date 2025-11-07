# wallpaper-engine-video-deduplication
wallpaper engine视频筛重，取消订阅重复视频  
先筛查出重复视频，生成创意工坊链接表格，再靠程序批量访问重复文件的成创意工坊链接网页，取消订阅  
1.视频筛重靠抽关键帧 + 感知哈希 (pHash)加上chromaprint / fpcalc 音频指纹（可选）  
步骤1：下载ffmpeg/ffprobe/fpcalc，放入可用的环境变量用户变量路径，比如C:\Windows\System32  
config.toml中workshop_root 填入wallpaper engine的创意工坊目录  
pip install imagehash pillow pandas openpyxl tqdm numpy  
执行：python we_duplicate_finder_readonly.py -c config.toml  
生成的表格在output路径中  
在默认浏览器中登录steam账号  
执行：python bulk_unsub_controller.py --xlsx xxx.xlsx --batch-size 1  
`xxx.xlsx`为执行：`python we_duplicate_finder_readonly.py -c config.toml`最后在`output`文件夹生成的xlsx文件名称  
