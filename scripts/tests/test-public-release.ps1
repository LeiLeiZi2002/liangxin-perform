#requires -Version 7.0

param(
    [switch]$RequirePublicSnapshot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$failures = [System.Collections.Generic.List[string]]::new()

function Assert-ReleaseContract {
    param(
        [Parameter(Mandatory)][bool]$Condition,
        [Parameter(Mandatory)][string]$Message
    )

    if (-not $Condition) {
        $failures.Add($Message)
    }
}

$readmePath = Join-Path $repositoryRoot 'README.md'
Assert-ReleaseContract (Test-Path -LiteralPath $readmePath -PathType Leaf) 'README.md 缺失'

$readme = if (Test-Path -LiteralPath $readmePath -PathType Leaf) {
    Get-Content -LiteralPath $readmePath -Raw -Encoding utf8
}
else {
    ''
}

$requiredReadmePatterns = [ordered]@{
    '量心 Perform' = 'README 必须使用公开项目名“量心 Perform”'
    '欢迎体验' = 'README 必须包含欢迎体验文案'
    '自由对话' = 'README 必须说明可以自由对话'
    '轻量角色链' = 'README 必须介绍轻量角色链'
    '语音' = 'README 必须介绍语音能力'
    '工作记录' = 'README 必须介绍工作记录能力'
    'Codespaces' = 'README 必须提供 Codespaces 体验入口'
    'https://bailian\.console\.aliyun\.com/\?apiKey=1' = 'README 必须提供百炼 API Key 控制台地址'
    '不提供真实心理咨询' = 'README 必须说明产品使用边界'
}

foreach ($entry in $requiredReadmePatterns.GetEnumerator()) {
    Assert-ReleaseContract ($readme -match $entry.Key) $entry.Value
}

$forbiddenReadmePatterns = [ordered]@{
    '(?<![A-Za-z])Actor(?![A-Za-z])' = 'README 不得公开内部角色名 Actor'
    '(?<![A-Za-z])Director(?![A-Za-z])' = 'README 不得公开内部角色名 Director'
    '旧的复杂链|旧复杂链|复杂链只保留' = 'README 不得展开旧链路迁移过程'
}

foreach ($entry in $forbiddenReadmePatterns.GetEnumerator()) {
    Assert-ReleaseContract ($readme -notmatch $entry.Key) $entry.Value
}

$caseDataRoot = Join-Path $repositoryRoot 'backend\app\cases\data'
$privateCaseTerms = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)

function Add-CaseTerms {
    param([object]$Value)

    if ($null -eq $Value) {
        return
    }

    if ($Value -is [System.Array]) {
        foreach ($item in $Value) {
            Add-CaseTerms -Value $item
        }
        return
    }

    if ($Value -is [pscustomobject]) {
        foreach ($property in $Value.PSObject.Properties) {
            if (($property.Name -eq 'name' -or $property.Name -eq 'title') -and
                $property.Value -is [string] -and
                $property.Value.Length -ge 2) {
                [void]$privateCaseTerms.Add($property.Value)
            }
            Add-CaseTerms -Value $property.Value
        }
    }
}

if (Test-Path -LiteralPath $caseDataRoot -PathType Container) {
    Get-ChildItem -LiteralPath $caseDataRoot -Filter '*.json' -File -Recurse | ForEach-Object {
        try {
            Add-CaseTerms -Value (Get-Content -LiteralPath $_.FullName -Raw -Encoding utf8 | ConvertFrom-Json)
        }
        catch {
            $failures.Add("案例文件无法解析：$($_.FullName)")
        }
    }
}

foreach ($term in $privateCaseTerms) {
    Assert-ReleaseContract (-not $readme.Contains($term, [System.StringComparison]::OrdinalIgnoreCase)) (
        'README 不得出现案例标题或人物姓名；发现来自案例数据的受限词。'
    )
}

foreach ($fileName in @('LICENSE', 'CONTENT_LICENSE.md', 'CONTENT_PROVENANCE.md', 'THIRD_PARTY_NOTICES.md')) {
    Assert-ReleaseContract (Test-Path -LiteralPath (Join-Path $repositoryRoot $fileName) -PathType Leaf) (
        "$fileName 缺失"
    )
}

foreach ($fileName in @('backend/uv.lock', 'backend/requirements.lock', '.github/workflows/ci.yml')) {
    Assert-ReleaseContract (Test-Path -LiteralPath (Join-Path $repositoryRoot $fileName) -PathType Leaf) (
        "公开构建文件缺失：$fileName"
    )
}

$packageLockPath = Join-Path $repositoryRoot 'frontend\package-lock.json'
if (Test-Path -LiteralPath $packageLockPath -PathType Leaf) {
    $packageLock = Get-Content -LiteralPath $packageLockPath -Raw -Encoding utf8
    Assert-ReleaseContract ($packageLock -notmatch 'registry\.npmmirror\.com') (
        'frontend/package-lock.json 不得依赖区域镜像地址'
    )
}

foreach ($unusedAsset in @(
    'frontend/public/icons.svg',
    'frontend/src/assets/vite.svg',
    'frontend/src/assets/hero.png',
    'frontend/src/assets/react.svg'
)) {
    Assert-ReleaseContract (-not (Test-Path -LiteralPath (Join-Path $repositoryRoot $unusedAsset))) (
        "未使用的模板素材仍存在：$unusedAsset"
    )
}

$secretPattern = [regex]'(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}'
$textExtensions = @(
    '.css', '.html', '.js', '.json', '.jsx', '.md', '.mjs', '.ps1', '.py', '.sh',
    '.toml', '.ts', '.tsx', '.txt', '.yaml', '.yml'
)
$secretHits = [System.Collections.Generic.List[string]]::new()
$candidateFiles = @(& git -C $repositoryRoot ls-files --cached --others --exclude-standard)
foreach ($relativePath in $candidateFiles) {
    if ($relativePath -like 'docs/superpowers/*') {
        continue
    }
    $extension = [System.IO.Path]::GetExtension($relativePath)
    if ($textExtensions -notcontains $extension) {
        continue
    }
    $absolutePath = Join-Path $repositoryRoot $relativePath
    if (-not (Test-Path -LiteralPath $absolutePath -PathType Leaf)) {
        continue
    }
    $lineNumber = 0
    foreach ($line in Get-Content -LiteralPath $absolutePath -Encoding utf8) {
        $lineNumber++
        if ($secretPattern.IsMatch($line)) {
            $secretHits.Add("${relativePath}:$lineNumber")
        }
    }
}
Assert-ReleaseContract ($secretHits.Count -eq 0) (
    "公开文件仍包含容易被识别为真实密钥的测试字符串。`n$($secretHits -join "`n")"
)

if ($RequirePublicSnapshot) {
    Assert-ReleaseContract (-not (Test-Path -LiteralPath (Join-Path $repositoryRoot 'docs\superpowers'))) (
        '公开快照不得包含 docs/superpowers 内部施工材料'
    )
}

if ($failures.Count -gt 0) {
    Write-Host "公开发布内容检查未通过（$($failures.Count) 项）：" -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "- $_" -ForegroundColor Red }
    exit 1
}

Write-Host '公开发布内容检查通过。' -ForegroundColor Green
exit 0
