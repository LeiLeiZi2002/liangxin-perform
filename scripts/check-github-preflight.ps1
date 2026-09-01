#requires -Version 7.0

[CmdletBinding()]
param(
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$RequireClean
)

$ErrorActionPreference = 'Stop'

function Get-GitLines {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string[]]$Arguments
    )

    $output = @(& git -C $Root @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $detail = ($output | ForEach-Object { "$_" }) -join [Environment]::NewLine
        throw "Git 命令执行失败：git $($Arguments -join ' ')`n$detail"
    }

    return @(
        $output |
            ForEach-Object { "$_" } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
}

function Test-ProhibitedPath {
    param([Parameter(Mandatory)][string]$Path)

    $normalizedPath = $Path.Replace('\', '/')
    while ($normalizedPath.StartsWith('./')) {
        $normalizedPath = $normalizedPath.Substring(2)
    }
    $lowerPath = $normalizedPath.ToLowerInvariant()

    if ($lowerPath -eq '.env') {
        return $true
    }
    if ($lowerPath.StartsWith('.env.') -and $lowerPath -ne '.env.example') {
        return $true
    }

    $prohibitedDirectories = @(
        'data/',
        'backend/data/',
        '.superpowers/',
        '.worktrees/',
        '.runtime/',
        'docs/superpowers/',
        'backend/3.14/',
        '.playwright-cli/',
        'output/playwright/',
        'output/stability/'
    )
    if ($prohibitedDirectories | Where-Object { $lowerPath.StartsWith($_) }) {
        return $true
    }

    return $lowerPath -match '\.(db|db-shm|db-wal|db-journal|sqlite|sqlite3|wav|mp3|m4a|ogg|webm)$'
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error '没有找到 Git，无法执行上传前检查。'
    exit 1
}

try {
    $resolvedRoot = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $RepositoryRoot).Path)
    $gitRootLines = @(Get-GitLines -Root $resolvedRoot -Arguments @('rev-parse', '--show-toplevel'))
    $gitRootRaw = $gitRootLines[0]
    $gitRoot = [System.IO.Path]::GetFullPath($gitRootRaw)
}
catch {
    Write-Error "无法确认项目仓库：$($_.Exception.Message)"
    exit 1
}

if (-not $resolvedRoot.Equals($gitRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Error "指定目录不是独立仓库根目录。`n指定目录：$resolvedRoot`n实际仓库：$gitRoot"
    exit 1
}

$trackedPaths = @(Get-GitLines -Root $resolvedRoot -Arguments @('-c', 'core.quotepath=false', 'ls-files'))
$unignoredPaths = @(
    Get-GitLines -Root $resolvedRoot -Arguments @(
        '-c', 'core.quotepath=false', 'ls-files', '--others', '--exclude-standard'
    )
)
$statusLines = @(Get-GitLines -Root $resolvedRoot -Arguments @('status', '--porcelain=v1'))
$branchLines = @(Get-GitLines -Root $resolvedRoot -Arguments @('branch', '--show-current'))
$remoteLines = @(Get-GitLines -Root $resolvedRoot -Arguments @('remote'))

$prohibitedTracked = @($trackedPaths | Where-Object { Test-ProhibitedPath -Path $_ } | Sort-Object -Unique)
$prohibitedUnignored = @($unignoredPaths | Where-Object { Test-ProhibitedPath -Path $_ } | Sort-Object -Unique)
$largeWarnings = [System.Collections.Generic.List[string]]::new()
$largeFailures = [System.Collections.Generic.List[string]]::new()

foreach ($relativePath in $trackedPaths) {
    $absolutePath = Join-Path $resolvedRoot $relativePath
    if (-not (Test-Path -LiteralPath $absolutePath -PathType Leaf)) {
        continue
    }

    $sizeMb = (Get-Item -LiteralPath $absolutePath).Length / 1MB
    if ($sizeMb -ge 95) {
        $largeFailures.Add("$relativePath（$([math]::Round($sizeMb, 1)) MB）")
    }
    elseif ($sizeMb -ge 25) {
        $largeWarnings.Add("$relativePath（$([math]::Round($sizeMb, 1)) MB）")
    }
}

$branch = if ($branchLines.Count -gt 0) { $branchLines[0] } else { 'detached HEAD' }
Write-Host "仓库：$resolvedRoot"
Write-Host "分支：$branch"
Write-Host "已跟踪文件：$($trackedPaths.Count)；未忽略的未跟踪文件：$($unignoredPaths.Count)"

$failures = [System.Collections.Generic.List[string]]::new()
if ($prohibitedTracked.Count -gt 0) {
    $failures.Add("以下运行数据或本地文件已经进入 Git 索引：`n  - $($prohibitedTracked -join "`n  - ")")
}
if ($prohibitedUnignored.Count -gt 0) {
    $failures.Add("以下运行数据或本地文件尚未被忽略：`n  - $($prohibitedUnignored -join "`n  - ")")
}
if ($largeFailures.Count -gt 0) {
    $failures.Add("以下已跟踪文件接近或超过 GitHub 单文件限制：`n  - $($largeFailures -join "`n  - ")")
}
if ($RequireClean -and $statusLines.Count -gt 0) {
    $failures.Add("工作区仍有 $($statusLines.Count) 项变化；最终上传检查要求工作区干净。")
}

if ($failures.Count -gt 0) {
    foreach ($failure in $failures) {
        Write-Host "[失败] $failure" -ForegroundColor Red
    }
    Write-Host '[提醒] 本命令不替代正式的密钥扫描；首次上传前仍需使用 gitleaks 检查当前文件和 Git 历史。' -ForegroundColor Yellow
    exit 1
}

Write-Host '[通过] 未发现会误上传的运行数据或本地文件。' -ForegroundColor Green
if ($statusLines.Count -gt 0) {
    Write-Host "[提醒] 工作区仍有 $($statusLines.Count) 项变化。开发期间允许，最终上传前请使用 -RequireClean 再检查。" -ForegroundColor Yellow
}
if ($largeWarnings.Count -gt 0) {
    Write-Host "[提醒] 以下已跟踪文件较大：`n  - $($largeWarnings -join "`n  - ")" -ForegroundColor Yellow
}
if ($remoteLines.Count -eq 0) {
    Write-Host '[信息] 当前没有配置远程仓库，不会发生误推送。'
}
else {
    Write-Host "[信息] 已配置远程仓库：$($remoteLines -join ', ')"
}
Write-Host '[提醒] 本命令不替代正式的密钥扫描；首次上传前仍需使用 gitleaks 检查当前文件和 Git 历史。'
exit 0
