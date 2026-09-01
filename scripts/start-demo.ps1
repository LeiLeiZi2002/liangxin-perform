[CmdletBinding()]
param(
    [string]$Distribution = 'Ubuntu-26.04',
    [switch]$NoBrowser,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$demoUrl = 'http://127.0.0.1:5173'
$healthUrl = 'http://127.0.0.1:8000/api/health'
. (Join-Path $PSScriptRoot 'provider-key-sync.ps1')

function Test-BackendHealth {
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        return $response.status -eq 'ready' -and
            $response.service -eq 'psych-assessment-demo'
    }
    catch {
        return $false
    }
}

function Test-FrontendHealth {
    try {
        $response = Invoke-WebRequest -Uri $demoUrl -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200 -and
            $response.Content -match '<title>\s*心智评鉴工作台\s*</title>' -and
            $response.Content -match '<div\s+id=["'']root["'']></div>'
    }
    catch {
        return $false
    }
}

function Stop-OwnedDemoProcess {
    param(
        [Parameter(Mandatory)][string]$PidFileName,
        [Parameter(Mandatory)][string]$RuntimeRoot,
        [Parameter(Mandatory)][string]$WslProjectRoot,
        [Parameter(Mandatory)][string]$DistributionName
    )

    $pidFile = Join-Path $RuntimeRoot $PidFileName
    if (-not (Test-Path -LiteralPath $pidFile)) {
        return
    }

    $pidText = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    if ($pidText -notmatch '^\d+$') {
        Remove-Item -LiteralPath $pidFile -Force
        return
    }

    $servicePid = [int]$pidText
    $processCwd = ((& wsl.exe -d $DistributionName -- readlink -f "/proc/$servicePid/cwd" 2>$null) -join '').Trim()
    if ($LASTEXITCODE -ne 0 -or -not $processCwd) {
        Remove-Item -LiteralPath $pidFile -Force
        return
    }

    $projectPrefix = $WslProjectRoot.TrimEnd('/') + '/'
    $owned = $processCwd -eq $WslProjectRoot -or
        $processCwd.StartsWith($projectPrefix, [StringComparison]::Ordinal)
    if (-not $owned) {
        Write-Host "忽略过期的 $PidFileName：对应进程不属于当前项目。"
        Remove-Item -LiteralPath $pidFile -Force
        return
    }

    $killCommand = 'kill'
    & wsl.exe -d $DistributionName -- $killCommand -- $servicePid 2>$null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $remainingCwd = ((& wsl.exe -d $DistributionName -- readlink -f "/proc/$servicePid/cwd" 2>$null) -join '').Trim()
        if ($LASTEXITCODE -ne 0 -or -not $remainingCwd) {
            break
        }
        Start-Sleep -Milliseconds 100
    }
    Remove-Item -LiteralPath $pidFile -Force
}

function Assert-DemoPortsAvailable {
    $listeners = @()
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $listeners = @(Get-NetTCPConnection -LocalPort 8000, 5173 -State Listen -ErrorAction SilentlyContinue)
        if ($listeners.Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 100
    }

    $ports = ($listeners.LocalPort | Sort-Object -Unique) -join '、'
    throw "端口 $ports 仍被其他程序占用。为避免误停未知进程，启动器不会自动结束它们，请先关闭占用程序后重试。"
}

function Open-DemoPage {
    if (-not $NoBrowser) {
        Start-Process $demoUrl
    }
}

$hadProcessApiKey = $false
$originalProcessApiKey = $null
$startupApiKey = [string]::Empty
$restoreProcessApiKey = $false

try {
    if (-not $CheckOnly) {
        $hadProcessApiKey = Test-Path Env:DASHSCOPE_API_KEY
        $originalProcessApiKey = $env:DASHSCOPE_API_KEY
        $restoreProcessApiKey = $true
        $startupApiKey = Get-StartupApiKey
        Remove-Item Env:DASHSCOPE_API_KEY -ErrorAction SilentlyContinue
    }

    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        throw '没有找到 WSL，请先确认 Ubuntu 已安装。'
    }

    $escapedWindowsPath = $projectRoot.Replace('\', '\\')
    $wslRoot = ((& wsl.exe -d $Distribution -- wslpath -a $escapedWindowsPath) -join '').Trim()
    if ($LASTEXITCODE -ne 0 -or -not $wslRoot) {
        throw "Ubuntu 无法访问项目目录，请确认发行版名称为 $Distribution。"
    }

    $startScript = "$wslRoot/scripts/start-demo.sh"
    if ($CheckOnly) {
        & wsl.exe -d $Distribution -- bash -n $startScript
        if ($LASTEXITCODE -ne 0) {
            throw 'WSL 启动脚本检查失败。'
        }
        Write-Host "检查通过：$Distribution 可以访问项目。"
        exit 0
    }

    $runtimeRoot = Join-Path $projectRoot 'data\.runtime'
    $launcherLog = Join-Path $runtimeRoot 'launcher.log'
    [void](New-Item -ItemType Directory -Path $runtimeRoot -Force)
    Stop-OwnedDemoProcess -PidFileName 'backend.pid' -RuntimeRoot $runtimeRoot `
        -WslProjectRoot $wslRoot -DistributionName $Distribution
    Stop-OwnedDemoProcess -PidFileName 'frontend.pid' -RuntimeRoot $runtimeRoot `
        -WslProjectRoot $wslRoot -DistributionName $Distribution
    Assert-DemoPortsAvailable
    [System.IO.File]::WriteAllText($launcherLog, [string]::Empty)

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = 'wsl.exe'
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    @(
        '-d', $Distribution, '--cd', $wslRoot, '--', 'bash', '-c',
        'exec bash scripts/start-demo.sh >data/.runtime/launcher.log 2>&1'
    ) | ForEach-Object {
        [void]$startInfo.ArgumentList.Add($_)
    }

    $process = [System.Diagnostics.Process]::Start($startInfo)
    if ($null -eq $process) {
        throw '无法启动 WSL。'
    }

    $healthy = $false
    $consecutiveReadySamples = 0
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        if ($process.HasExited) {
            break
        }
        if ((Test-BackendHealth) -and (Test-FrontendHealth)) {
            $consecutiveReadySamples++
            if ($consecutiveReadySamples -ge 2) {
                $healthy = $true
                break
            }
        }
        else {
            $consecutiveReadySamples = 0
        }
        Start-Sleep -Milliseconds 500
    }

    if (-not $healthy) {
        $details = if (Test-Path -LiteralPath $launcherLog) {
            (Get-Content -LiteralPath $launcherLog -Raw).Trim()
        }
        if ($details) {
            throw "DEMO 启动失败：$details"
        }
        throw "DEMO 启动失败，请查看 $runtimeRoot 下的日志。"
    }

    Sync-ProviderApiKey -ApiKey $startupApiKey
    Write-Host "DEMO 已启动：$demoUrl"
    Open-DemoPage
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally {
    if ($restoreProcessApiKey) {
        if ($hadProcessApiKey) {
            $env:DASHSCOPE_API_KEY = $originalProcessApiKey
        }
        else {
            Remove-Item Env:DASHSCOPE_API_KEY -ErrorAction SilentlyContinue
        }
    }
}
