$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$startDemoPath = Join-Path $projectRoot 'scripts\start-demo.ps1'
$startShellPath = Join-Path $projectRoot 'scripts\start-demo.sh'
$source = Get-Content -LiteralPath $startDemoPath -Raw -Encoding UTF8
$shellSource = Get-Content -LiteralPath $startShellPath -Raw -Encoding UTF8
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $startDemoPath,
    [ref]$tokens,
    [ref]$parseErrors
)
$functions = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $true))

function Get-LauncherFunctionText {
    param([Parameter(Mandatory)][string]$Name)

    return @($functions | Where-Object Name -eq $Name | Select-Object -First 1)[0].Extent.Text
}

Describe 'start-demo 一键重启与身份检查' {
    It 'PowerShell 启动脚本语法可解析' {
        @($parseErrors).Count | Should Be 0
    }

    It '重复启动不再因为两个端点返回 200 而直接复用旧进程' {
        $source | Should Not Match 'DEMO 已在运行'
        $source | Should Not Match '(?s)Test-BackendHealth.*Test-FrontendHealth.*exit 0.*Get-Command wsl\.exe'
    }

    It '只在 PID 文件进程的 cwd 属于当前项目时停止该 PID' {
        $stopFunction = Get-LauncherFunctionText -Name 'Stop-OwnedDemoProcess'

        $stopFunction | Should Match 'readlink'
        $stopFunction | Should Match '/proc/\$servicePid/cwd'
        $stopFunction | Should Match '\$processCwd -eq \$WslProjectRoot'
        $stopFunction | Should Match 'StartsWith\('
        $stopFunction | Should Match 'wsl\.exe.*kill'
        $stopFunction.IndexOf('readlink') | Should BeLessThan $stopFunction.IndexOf("'kill'")
        $source | Should Not Match '(?i)\bpkill\b|\bfuser\b|\btaskkill\b|Stop-Process'
    }

    It '不覆盖 PowerShell 只读的 PID 自动变量' {
        $stopFunction = Get-LauncherFunctionText -Name 'Stop-OwnedDemoProcess'

        $stopFunction | Should Not Match '(?im)^\s*\$pid\s*='
        $stopFunction | Should Match '\$servicePid\s*=\s*\[int\]\$pidText'
    }

    It '只从本项目两个服务的 PID 文件发起重启清理' {
        $source | Should Match "Stop-OwnedDemoProcess.*'backend\.pid'"
        $source | Should Match "Stop-OwnedDemoProcess.*'frontend\.pid'"
    }

    It '自管进程停止后检查固定端口，未知占用只报错不终止' {
        $portFunction = Get-LauncherFunctionText -Name 'Assert-DemoPortsAvailable'

        $portFunction | Should Match 'Get-NetTCPConnection'
        $portFunction | Should Match '8000.*5173|5173.*8000'
        $portFunction | Should Match '不会自动结束'
        $portFunction | Should Not Match 'kill|Stop-Process|taskkill'
    }

    It '后端健康检查验证固定 status 和 service' {
        $healthFunction = Get-LauncherFunctionText -Name 'Test-BackendHealth'

        $healthFunction | Should Match "status.*'ready'"
        $healthFunction | Should Match "service.*'psych-assessment-demo'"
    }

    It '前端健康检查验证本项目标题和根节点' {
        $frontendFunction = Get-LauncherFunctionText -Name 'Test-FrontendHealth'

        $frontendFunction | Should Match '心智评鉴工作台'
        $frontendFunction | Should Match 'id=[^\r\n]*root'
    }

    It '前后端必须连续两次通过检查才报告启动成功' {
        $source | Should Match '\$consecutiveReadySamples\s*=\s*0'
        $source | Should Match '\$consecutiveReadySamples\+\+'
        $source | Should Match '\$consecutiveReadySamples\s+-ge\s+2'
        $source | Should Match '(?s)else\s*\{\s*\$consecutiveReadySamples\s*=\s*0'
    }
}

Describe 'start-demo WSL 服务进程' {
    It '直接记录 Python 服务 PID' {
        $shellSource | Should Match '(?s)cd "\$ROOT/backend".*"\$PYTHON" -m uvicorn[^\r\n]*&\s*BACKEND_PID=\$!'
        $shellSource | Should Not Match '\(cd "\$ROOT/backend"'
    }

    It '直接记录 Node 服务 PID并严格占用 5173' {
        $shellSource | Should Match '(?s)cd "\$ROOT/frontend".*"\$VITE"[^\r\n]*--strictPort[^\r\n]*&\s*FRONTEND_PID=\$!'
        $shellSource | Should Not Match 'npm run dev'
    }
}
