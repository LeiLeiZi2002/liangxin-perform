#requires -Version 7.0

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$failures = [System.Collections.Generic.List[string]]::new()

function Assert-Contract {
    param(
        [Parameter(Mandatory)][bool]$Condition,
        [Parameter(Mandatory)][string]$Message
    )

    if (-not $Condition) {
        $failures.Add($Message)
    }
}

$devcontainerPath = Join-Path $repositoryRoot '.devcontainer\devcontainer.json'
$startScriptPath = Join-Path $repositoryRoot 'scripts\start-codespace.sh'
$readmePath = Join-Path $repositoryRoot 'README.md'

Assert-Contract (Test-Path -LiteralPath $devcontainerPath -PathType Leaf) '.devcontainer/devcontainer.json 缺失'
Assert-Contract (Test-Path -LiteralPath $startScriptPath -PathType Leaf) 'scripts/start-codespace.sh 缺失'
Assert-Contract (Test-Path -LiteralPath $readmePath -PathType Leaf) 'README.md 缺失'

if (Test-Path -LiteralPath $devcontainerPath -PathType Leaf) {
    try {
        $devcontainer = Get-Content -LiteralPath $devcontainerPath -Raw -Encoding utf8 | ConvertFrom-Json
        Assert-Contract ($devcontainer.image -eq 'mcr.microsoft.com/devcontainers/python:3.14-bookworm') 'Codespaces 必须使用 Python 3.14 镜像'
        Assert-Contract ($devcontainer.features.'ghcr.io/devcontainers/features/node:2'.version -eq '22') 'Codespaces 必须安装 Node 22'
        Assert-Contract (@($devcontainer.forwardPorts).Count -eq 1 -and $devcontainer.forwardPorts[0] -eq 5173) 'Codespaces 只应转发 5173 端口'
        Assert-Contract ([string]$devcontainer.postCreateCommand -match 'backend/requirements\.lock') 'Codespaces 必须使用后端锁定依赖'
        Assert-Contract ([string]$devcontainer.postCreateCommand -match '--no-deps -e ./backend') 'Codespaces 必须以无额外解析方式安装本地后端包'
        Assert-Contract ([string]$devcontainer.postCreateCommand -match 'npm ci') 'Codespaces 必须通过 npm ci 安装前端依赖'
        Assert-Contract ([string]$devcontainer.postStartCommand -match 'start-codespace\.sh') 'Codespaces 启动钩子必须调用 start-codespace.sh'
        Assert-Contract ([string]$devcontainer.postStartCommand -match '\bnohup\b') 'Codespaces 启动钩子必须让服务在生命周期命令结束后继续运行'
        Assert-Contract ([string]$devcontainer.postStartCommand -notmatch '\bsetsid\b') 'Codespaces 启动钩子不得使用实测后无法保活服务的 setsid'
    }
    catch {
        Assert-Contract $false "devcontainer 配置无法解析：$($_.Exception.Message)"
    }
}

if (Test-Path -LiteralPath $startScriptPath -PathType Leaf) {
    $startScript = Get-Content -LiteralPath $startScriptPath -Raw -Encoding utf8
    Assert-Contract ($startScript -match 'uvicorn app\.main:app.*--host 0\.0\.0\.0.*--port 8000') 'Codespaces 后端必须监听 0.0.0.0:8000'
    Assert-Contract ($startScript -match 'npm run dev.*--host 0\.0\.0\.0.*--port 5173.*--strictPort') 'Codespaces 前端必须固定监听 0.0.0.0:5173'
    Assert-Contract ($startScript -match 'VITE_API_BASE_URL=""') 'Codespaces 必须让前端通过同源代理访问后端'
    Assert-Contract ($startScript -match 'FRONTEND_ORIGIN="https://\$\{CODESPACES_FRONTEND_HOST\}"') 'Codespaces 必须把真实前端来源交给后端'
    Assert-Contract ($startScript -match '/workspaces/\.codespaces/shared/\.env') 'Codespaces 启动脚本必须能读取平台共享环境文件'
    Assert-Contract ($startScript -match 'read_codespaces_value.*CODESPACE_NAME' -and $startScript -match 'read_codespaces_value.*GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN') 'Codespaces 启动脚本必须只补取构造转发地址所需的两个变量'
}

if (Test-Path -LiteralPath $readmePath -PathType Leaf) {
    $readme = Get-Content -LiteralPath $readmePath -Raw -Encoding utf8
    Assert-Contract ($readme -match 'https://codespaces\.new/LeiLeiZi2002/liangxin-perform\?quickstart=1') 'README 必须提供当前仓库的 Codespaces 入口'
    Assert-Contract ($readme -match '轻量角色链') 'README 必须简要介绍轻量角色链'
    Assert-Contract ($readme -match '点击 `我说完了`') 'README 必须说明语音话轮由受测者手动提交'
    Assert-Contract ($readme -match 'https://help\.aliyun\.com/zh/model-studio/get-api-key') 'README 必须链接百炼 API Key 官方文档'
    Assert-Contract ($readme -match 'backend/requirements\.lock') 'README 必须给出锁定的后端安装命令'
    Assert-Contract ($readme -match 'npm ci') 'README 必须给出可复现的前端安装命令'
    Assert-Contract ($readme -match 'PowerShell 7' -and $readme -match 'WSL2') 'README 必须说明 Windows 本地环境要求'
}

if ($failures.Count -gt 0) {
    Write-Host "Codespaces 与 README 契约检查未通过（$($failures.Count) 项）：" -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "- $_" -ForegroundColor Red }
    exit 1
}

Write-Host 'Codespaces 与 README 契约检查通过。' -ForegroundColor Green
exit 0
