#requires -Version 7.0

$ErrorActionPreference = 'Stop'

$scriptsRoot = Split-Path -Parent $PSScriptRoot
$preflightScript = Join-Path $scriptsRoot 'check-github-preflight.ps1'
$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    'psych-assessment-github-preflight-' + [guid]::NewGuid().ToString('N')
)

function Assert-Equal {
    param(
        [Parameter(Mandatory)][object]$Expected,
        [Parameter(Mandatory)][object]$Actual,
        [Parameter(Mandatory)][string]$Message
    )

    if ($Expected -ne $Actual) {
        throw "$Message`n预期：$Expected`n实际：$Actual"
    }
}

function Assert-Matches {
    param(
        [Parameter(Mandatory)][string]$Pattern,
        [Parameter(Mandatory)][string]$Actual,
        [Parameter(Mandatory)][string]$Message
    )

    if ($Actual -notmatch $Pattern) {
        throw "$Message`n没有找到：$Pattern`n实际输出：$Actual"
    }
}

function New-TestRepository {
    param([Parameter(Mandatory)][string]$Name)

    $repositoryPath = Join-Path $temporaryRoot $Name
    New-Item -ItemType Directory -Path $repositoryPath -Force | Out-Null
    & git -C $repositoryPath init --quiet
    if ($LASTEXITCODE -ne 0) {
        throw "无法创建测试仓库：$Name"
    }
    return $repositoryPath
}

function Invoke-Preflight {
    param([Parameter(Mandatory)][string]$RepositoryPath)

    $output = (& pwsh -NoProfile -File $preflightScript -RepositoryRoot $RepositoryPath 2>&1 | Out-String).Trim()
    return [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output = $output
    }
}

New-Item -ItemType Directory -Path $temporaryRoot -Force | Out-Null

try {
    $safeRepository = New-TestRepository -Name 'safe'
    Set-Content -LiteralPath (Join-Path $safeRepository '.env.example') -Value 'EXAMPLE_ONLY=true' -Encoding utf8
    & git -C $safeRepository add .env.example
    $safeResult = Invoke-Preflight -RepositoryPath $safeRepository
    Assert-Equal -Expected 0 -Actual $safeResult.ExitCode -Message "安全仓库应通过检查。`n$($safeResult.Output)"
    Assert-Matches -Pattern '未发现会误上传的运行数据或本地文件' -Actual $safeResult.Output -Message '通过结果应说明关键文件检查已完成。'

    $databaseRepository = New-TestRepository -Name 'database'
    New-Item -ItemType Directory -Path (Join-Path $databaseRepository 'data') | Out-Null
    Set-Content -LiteralPath (Join-Path $databaseRepository 'data/demo.db') -Value 'test-only' -Encoding utf8
    & git -C $databaseRepository add data/demo.db
    $databaseResult = Invoke-Preflight -RepositoryPath $databaseRepository
    Assert-Equal -Expected 1 -Actual $databaseResult.ExitCode -Message '数据库进入 Git 索引时必须阻止通过。'
    Assert-Matches -Pattern 'data/demo\.db' -Actual $databaseResult.Output -Message '失败结果应指出危险文件。'

    $toolStateRepository = New-TestRepository -Name 'tool-state'
    New-Item -ItemType Directory -Path (Join-Path $toolStateRepository '.superpowers') | Out-Null
    Set-Content -LiteralPath (Join-Path $toolStateRepository '.superpowers/state.json') -Value '{}' -Encoding utf8
    $toolStateResult = Invoke-Preflight -RepositoryPath $toolStateRepository
    Assert-Equal -Expected 1 -Actual $toolStateResult.ExitCode -Message '未忽略的本地工具状态必须阻止通过。'
    Assert-Matches -Pattern '\.superpowers/state\.json' -Actual $toolStateResult.Output -Message '失败结果应指出未忽略的工具文件。'

    Write-Host '上传前检查脚本测试通过。'
}
finally {
    $resolvedTemporaryRoot = [System.IO.Path]::GetFullPath($temporaryRoot)
    $systemTemporaryRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    if ($resolvedTemporaryRoot.StartsWith($systemTemporaryRoot, [System.StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolvedTemporaryRoot)) {
        Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force
    }
}
