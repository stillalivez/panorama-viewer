# 720°全景照片查看器

轻量的 Windows 桌面全景查看器，支持 JPEG、PNG、WebP 格式的 2:1 等距柱状全景。照片在本机读取和显示，支持方向校准、视角记忆与系统全屏。另提供可独立运行的浏览器版。

## Windows 桌面版

在仓库的 Releases 页面下载 Windows x64 便携包，完整解压后双击 `PanoramaViewer.exe`，不需要 Python。请保留同目录的 `_internal` 和 `resources` 文件夹。支持 Windows 10/11 x64，需要 [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。

公开版不附带个人照片。首次启动点击“打开全景图”或按 Ctrl+O 选择自己的图片。

桌面版支持原生文件选择、图片拖入、最近打开记录，以及场景、视角、手动方向校准和窗口大小的自动恢复。照片保留在原位置；设置默认位于 `%LOCALAPPDATA%\PanoramaViewer`。关闭窗口自动停止本次本地服务。使用细节见 `desktop_README.md`。

开发与重新打包（在 Windows 上运行）：

```powershell
python -m venv .venv-desktop
.\.venv-desktop\Scripts\python.exe -m pip install -r requirements-desktop-lock.txt
.\.venv-desktop\Scripts\python.exe desktop_viewer.py
.\.venv-desktop\Scripts\python.exe build_desktop.py --public
```

建议使用 Python 3.11 x64。`requirements-desktop-lock.txt` 记录验证过的完整依赖版本；`requirements-desktop.txt` 列出直接依赖。使用 uv 时也可按锁定文件安装。桌面窗口使用 pywebview，打包使用 PyInstaller，与浏览器版共用 `panorama_viewer_template.html`；Node.js 仅用于前端测试。

`--public` 输出到 `outputs/public-release/`，始终使用空场景配置，忽略本机照片。省略该参数可制作包含已生成内置场景的个人包，输出到 `outputs/desktop-release/`；个人包可能含照片，不应直接作为开源附件。源码在尚未生成场景时也能直接启动。测试可用 `--data-dir` 指定独立设置目录。

## 目录

- `build_panorama_viewer.py`：根据全景图片生成本地查看器。
- `panorama_viewer_template.html`：WebGL/Canvas 全景查看器前端模板。
- `start_panorama_viewer.py`：启动本地 HTTP 服务并打开浏览器。
- `desktop_viewer.py`：桌面入口、本机图片服务及状态保存。
- `build_desktop.py`：Windows 桌面打包。
- `outputs/panorama_viewer/`：本机生成的页面与照片，不进入版本库。
- `assets/app-icon.ico`：生成新查看器时使用的应用图标。
- `tests/`：构建失败回滚、元数据及前端交互状态的回归测试。

## 启动

```powershell
python build_panorama_viewer.py
.\启动查看器.ps1
```

也可以手动启动：

```powershell
python .\start_panorama_viewer.py --dir ".\outputs\panorama_viewer"
```

需要 Python 3.10 或更高版本。启动已有页面不需要额外库；重新生成需要 `requirements.txt` 中的 Pillow。启动器默认使用空闲端口，请以实际输出地址为准；可添加 `--port 8765` 指定首选端口。服务仅绑定本机，不需要联网。

## 浏览与导入

- 支持 JPEG、PNG、WebP 格式的完整 2:1 等距柱状全景（宽高比容差 0.02）。普通照片和损坏图片会明确提示，不加入列表。
- 添加多张图片时逐张检查、显示进度，生成小缩略图；相同文件名、大小和修改时间的临时图片不会重复添加。可移除临时图片并释放其资源。
- 浏览器版的临时图片及手动方向校准只保留在当前页面，刷新后需重新选择。桌面版会自动保存打开记录、视角和校准。
- 鼠标拖拽旋转、滚轮/滑块缩放、双击复位；手机支持单指旋转、双指捏合。焦点在画面上时方向键旋转；按钮和输入框保持原生键盘行为。数字 1–9 切换前九个场景，空格切换旋转，R 复位，F 切换全屏。
- 全屏中保留工具、缩放和场景选择，可点击“退出全屏”或按 Esc 退出。
- 拍摄时间优先来自 EXIF/XMP，其次是文件名；文件修改时间单独标注。没有可靠航向时显示“未校准”和相对角度；已知实际方位时可校准当前画面中心。元数据方向仍需根据实际场景确认。
- 超出显卡纹理上限时自动缩小渲染；上传失败则使用兼容模式。切换正常图片时重新尝试图形加速，场景信息显示实际渲染尺寸。图形上下文中断时可自动降级并在恢复后重试。

## 重新生成

默认复用本机已生成配置中的图片；首次使用且没有配置时生成空查看器：

```powershell
python .\build_panorama_viewer.py
```

指定其他全景图：

```powershell
python .\build_panorama_viewer.py --images "D:\path\to\pano.jpg" --labels "场景名称" --max-width 0
```

`--max-width 0` 表示输出图片保留原始尺寸（需要时会修正 EXIF 旋转方向）；浏览器还会按显卡能力调整实际渲染尺寸。也可使用 `--max-width 8192` 在生成时缩小。

构建先在输出目录旁的临时目录准备所有资源，成功后再切换版本。准备或替换失败会保留/恢复原版本；其他原有文件也会保留。构建期间请确保有足够磁盘空间容纳暂存副本。模板缺失会直接报错，不会回退到旧界面。构建时服务若仍占用输出目录，请关闭旧版启动器后用当前启动器重开。

## 验证

```powershell
python -m unittest discover -s tests -p "test_*.py"
node --test tests/test_frontend.cjs
```

Node.js 仅用于运行前端回归测试，不是查看器运行依赖。测试包含模拟的图形接口失败和触摸事件；真实设备表现还应结合页面及真机检查。

真实跨窗口拖放、全新 Windows 安装、多显示器缩放及长时间压力测试仍需补充；自动测试通过不代表所有设备均已验收。

## 许可与贡献

源码使用 [MIT 许可证](LICENSE)。发行包随附第三方组件许可声明；用户自行打开的照片不属于本项目的许可范围。

欢迎提交 Issue 和 Pull Request。反馈问题时请说明版本、Windows 版本、图片格式与尺寸、复现步骤；发布日志或截图前请移除私人路径及照片信息。
