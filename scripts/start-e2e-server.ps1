param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('backend', 'frontend')]
    [string]$Service,
    [string]$Distribution = 'Ubuntu-26.04'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$portableRoot = $projectRoot.Replace('\', '/')
$wslRoot = (& wsl.exe -d $Distribution -- wslpath -a $portableRoot).Trim()
if (-not $wslRoot) {
    throw '无法把项目路径转换为 WSL 路径。'
}
if ($wslRoot.Contains("'")) {
    throw '当前 E2E 启动脚本不支持路径中包含单引号。'
}
if ($Service -eq 'backend') {
    $command = "cd '$wslRoot/backend' && env DATABASE_URL=sqlite:///../data/e2e.db FRONTEND_ORIGIN=http://127.0.0.1:5173 ../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
} else {
    $command = "cd '$wslRoot/frontend' && /usr/bin/npm run dev -- --host 127.0.0.1 --port 5173 --strictPort"
}

& wsl.exe -d $Distribution -- bash -lc $command
exit $LASTEXITCODE
