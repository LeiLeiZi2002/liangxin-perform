[CmdletBinding()]
param(
    [string]$Distribution = 'Ubuntu-26.04'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$runtimeRoot = Join-Path $projectRoot 'data\.runtime'
$startedAt = [DateTime]::UtcNow

if (Get-NetTCPConnection -LocalPort 8000, 5173 -State Listen -ErrorAction SilentlyContinue) {
    throw '启动冒烟测试要求 8000 和 5173 端口空闲。'
}

try {
    & pwsh -NoProfile -File (Join-Path $projectRoot 'scripts\start-demo.ps1') `
        -Distribution $Distribution -NoBrowser
    if ($LASTEXITCODE -ne 0) {
        throw "一键启动脚本退出码为 $LASTEXITCODE。"
    }

    $backend = Invoke-WebRequest 'http://127.0.0.1:8000/api/health' `
        -UseBasicParsing -TimeoutSec 3
    $frontend = Invoke-WebRequest 'http://127.0.0.1:5173' `
        -UseBasicParsing -TimeoutSec 3
    if ($backend.StatusCode -ne 200 -or $frontend.StatusCode -ne 200) {
        throw '一键启动后健康检查未通过。'
    }
    Write-Host '一键启动冒烟测试通过。'
}
finally {
    $pids = @(@('backend.pid', 'frontend.pid') |
        ForEach-Object {
            $path = Join-Path $runtimeRoot $_
            if ((Test-Path -LiteralPath $path) -and
                (Get-Item -LiteralPath $path).LastWriteTimeUtc -ge $startedAt) {
                (Get-Content -LiteralPath $path -Raw).Trim()
            }
        } |
        Where-Object { $_ -match '^\d+$' })
    if ($pids.Count -gt 0) {
        & wsl.exe -d $Distribution -- kill @pids 2>$null
    }
}
