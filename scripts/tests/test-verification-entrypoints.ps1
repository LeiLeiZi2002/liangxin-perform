#requires -Version 7.0

$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$verifyPath = Join-Path $repositoryRoot 'scripts\verify-demo.sh'

if (-not (Test-Path -LiteralPath $verifyPath -PathType Leaf)) {
    throw 'scripts/verify-demo.sh 缺失'
}

$verifyScript = Get-Content -LiteralPath $verifyPath -Raw -Encoding utf8
$requirements = [ordered]@{
    '\[\[ "\$ROOT" == /mnt/\* \]\]' = 'Windows 挂载盘必须使用单独的前端测试参数'
    'npm run test -- --run --pool=threads --maxWorkers=1' = 'Windows 挂载盘必须避免 Vitest fork 启动超时'
    'npm run test -- --run' = '原生 Linux 文件系统必须保留正常测试入口'
    'npm run build' = '验证入口必须执行前端构建'
    'npm run lint' = '验证入口必须执行前端静态检查'
}

$failures = [System.Collections.Generic.List[string]]::new()
foreach ($entry in $requirements.GetEnumerator()) {
    if ($verifyScript -notmatch $entry.Key) {
        $failures.Add($entry.Value)
    }
}

if ($failures.Count -gt 0) {
    Write-Host "验证入口检查未通过（$($failures.Count) 项）：" -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "- $_" -ForegroundColor Red }
    exit 1
}

Write-Host '验证入口检查通过。' -ForegroundColor Green
exit 0
