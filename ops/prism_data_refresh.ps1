# 盘后数据刷新入口(给 Windows 计划任务用的极薄包装): 只转发参数 + 原样透传退出码
# 0=全段成功 / 1=存在失败段 / 2=存在空数据段。用法: .\ops\prism_data_refresh.ps1 [--dry-run]
python "$PSScriptRoot\..\prism\data_refresh.py" @args
exit $LASTEXITCODE
