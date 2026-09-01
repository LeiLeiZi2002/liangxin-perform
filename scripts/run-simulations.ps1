<#
.SYNOPSIS
运行固定脚本黑箱测评，或只检查脚本目录。

.PARAMETER Suite
要运行的固定脚本编号；使用 all 运行当前案例目录中的全部脚本。

.PARAMETER CaseId
案例编号。不传时沿用旧案例 crisis_student_main。

.PARAMETER Scene
测评场域，可选 hotline、online 或 institution。不传时沿用 hotline。

.PARAMETER Catalog
可选的固定脚本目录 JSON 路径。不传时使用项目内置目录。

.PARAMETER Distribution
承载项目 Python 环境的 WSL 发行版名称。

.PARAMETER CheckOnly
只校验案例、场域和固定脚本结构，不启动服务，也不调用模型。
#>
[CmdletBinding()]
param(
    [string]$Suite = 'normal',
    [string]$CaseId = 'crisis_student_main',
    [ValidateSet('institution', 'hotline', 'online')]
    [string]$Scene = 'hotline',
    [string]$Catalog,
    [string]$Distribution = 'Ubuntu-26.04',
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$healthUrl = 'http://127.0.0.1:8000/api/health'

function Test-BackendHealth {
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        return $response.status -eq 'ready' -and
            $response.service -eq 'psych-assessment-demo'
    } catch {
        return $false
    }
}

function Wait-BackendHealth {
    param(
        [ValidateRange(1, 120)][int]$MaxAttempts = 8,
        [ValidateRange(0, 5000)][int]$DelayMilliseconds = 250
    )

    for ($attempt = 0; $attempt -lt $MaxAttempts; $attempt++) {
        if (Test-BackendHealth) {
            return $true
        }
        if ($attempt + 1 -lt $MaxAttempts) {
            Start-Sleep -Milliseconds $DelayMilliseconds
        }
    }
    return $false
}

function Get-SimulationRunnerExitMessage {
    param([int]$ExitCode)

    switch ($ExitCode) {
        1 { return '测评已完成，但结果未通过；请查看黑箱检查结果' }
        2 { return '测评未开始或运行中断；具体原因请查看上方运行器输出' }
        default { return '黑箱测评脚本或运行器发生错误；请查看上方运行器输出' }
    }
}

try {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        throw '没有找到 WSL，请先确认 Ubuntu 已安装。'
    }

    $escapedWindowsPath = $projectRoot.Replace('\', '\\')
    $wslRoot = ((& wsl.exe -d $Distribution -- wslpath -a $escapedWindowsPath) -join '').Trim()
    if ($LASTEXITCODE -ne 0 -or -not $wslRoot) {
        throw "Ubuntu 无法访问项目目录，请确认发行版名称为 $Distribution。"
    }

    $pythonPath = "$wslRoot/.venv/bin/python"
    & wsl.exe -d $Distribution -- test -x $pythonPath
    if ($LASTEXITCODE -ne 0) {
        throw '没有找到项目虚拟环境，请先按照 README 完成环境准备。'
    }

    $runnerArguments = @(
        '-m', 'app.simulations.runner',
        '--suite', $Suite,
        '--case-id', $CaseId,
        '--scene', $Scene
    )
    if ($Catalog) {
        $resolvedCatalog = (Resolve-Path -LiteralPath $Catalog).Path
        $escapedCatalogPath = $resolvedCatalog.Replace('\', '\\')
        $wslCatalog = ((& wsl.exe -d $Distribution -- wslpath -a $escapedCatalogPath) -join '').Trim()
        if ($LASTEXITCODE -ne 0 -or -not $wslCatalog) {
            throw "Ubuntu 无法访问固定脚本目录：$resolvedCatalog"
        }
        $runnerArguments += @('--catalog', $wslCatalog)
    }
    if ($CheckOnly) {
        $runnerArguments += '--check-only'
        Write-Host "正在检查黑箱场景：$Suite（不会调用模型）"
        & wsl.exe -d $Distribution --cd "$wslRoot/backend" -- `
            $pythonPath @runnerArguments
        if ($LASTEXITCODE -ne 0) {
            throw '黑箱测评环境或场景检查未通过。'
        }
        exit 0
    }

    if (-not (Wait-BackendHealth)) {
        Write-Host 'DEMO 尚未启动，正在启动后台服务……'
        & (Join-Path $PSScriptRoot 'start-demo.ps1') `
            -Distribution $Distribution `
            -NoBrowser
        if ($LASTEXITCODE -ne 0 -or -not (Wait-BackendHealth)) {
            throw '后台服务启动失败，请先运行启动DEMO.cmd 查看具体提示。'
        }
    }

    Write-Host "开始运行黑箱场景：$Suite"
    & wsl.exe -d $Distribution --cd "$wslRoot/backend" -- `
        $pythonPath @runnerArguments
    $runnerExitCode = $LASTEXITCODE
    if ($runnerExitCode -ne 0) {
        Write-Host (Get-SimulationRunnerExitMessage -ExitCode $runnerExitCode) `
            -ForegroundColor Red
        exit $runnerExitCode
    }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
