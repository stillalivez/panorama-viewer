$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ViewerDir = Join-Path $ProjectRoot "outputs\panorama_viewer"
$PythonCandidates = @(
    (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
    (Join-Path $ProjectRoot ".venv-desktop\Scripts\python.exe"),
    "python",
    "python3"
)

$Python = $null
foreach ($Candidate in $PythonCandidates) {
    $Command = Get-Command $Candidate -ErrorAction SilentlyContinue
    if ($Command -and $Command.Source -notlike '*\Microsoft\WindowsApps\*') {
        $Python = $Command.Source
        break
    }
}

if (-not $Python) {
    throw "未找到可用 Python。请先安装 Python，或在脚本中补充 Python 路径。"
}

& $Python (Join-Path $ProjectRoot "start_panorama_viewer.py") --dir $ViewerDir
exit $LASTEXITCODE
