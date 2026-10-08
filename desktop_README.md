# 720°全景照片查看器 · Windows 桌面版 1.0.0

解压整个压缩包，然后双击 `PanoramaViewer.exe`。程序在独立窗口中打开，不需要安装 Python，也不需要运行命令。

请保持 exe、`_internal` 和 `resources` 在同一个文件夹中。照片位于独立的资源目录，程序不会把用户打开的照片复制进 exe。

GitHub 公开发行包不附带个人照片；首次打开时请选择自己的全景图。

## 使用

- 点击“打开全景图”、按 Ctrl+O，或从资源管理器把照片拖入窗口。也可把照片拖到 exe 图标上启动。
- 支持 JPEG、PNG、WebP 的完整 2:1 全景，支持批量选择。普通照片和损坏图片会显示原因。
- 拖拽画面旋转，滚轮或滑块缩放；双击复位。画面聚焦时可用方向键调整视角。
- 全屏中保留缩放与场景切换，按 Esc 或点击“退出全屏”返回。
- 自动保存当前场景、每张照片的视角、手动方向校准，以及窗口大小和最大化状态。下次打开时恢复；自动旋转不会自动开启。
- 最近打开记录最多保留 20 张；可同时打开最多 100 张。移除场景或清空记录不会删除原照片。
- 照片始终保留在原位置；如果照片移动、删除或外接盘未连接，重新选择照片即可。程序会提示无法恢复的文件，其他场景仍可使用。
- 关闭窗口会停止本次程序的本机图片服务。

## 环境与设置

适用于 Windows 10/11 x64，需 Microsoft Edge WebView2 Runtime。已安装该运行环境的电脑可以离线使用。缺失时从微软官方页面获取：

[Microsoft Edge WebView2 下载](https://developer.microsoft.com/microsoft-edge/webview2/)

设置、缩略图缓存和运行日志默认保存到 `%LOCALAPPDATA%\PanoramaViewer`，不需要管理员权限。设置中的最近文件记录只保存在本机。

高级用法：`PanoramaViewer.exe --data-dir "D:\我的查看器设置"` 可指定独立设置目录。

本程序不修改照片内容；方向元数据可能需要实景校准。桌面版仍按显卡能力决定实际渲染尺寸，可在场景信息中查看。

## 故障定位

- 启动失败：确认已完整解压，且 `_internal`、`resources` 和 WebView2 Runtime 可用。
- 设置无法保存：检查设置目录的访问权限和磁盘空间。
- 运行日志：`%LOCALAPPDATA%\PanoramaViewer\desktop.log`。
- 第三方组件许可证见 `THIRD_PARTY_NOTICES.txt`。
- 源码使用 MIT 许可证，见 `LICENSE`。
