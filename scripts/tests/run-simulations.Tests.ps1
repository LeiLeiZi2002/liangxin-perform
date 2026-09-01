$scriptPath = Join-Path $PSScriptRoot '..\run-simulations.ps1'
$scriptText = Get-Content -LiteralPath $scriptPath -Raw -Encoding UTF8
$tokens = $null
$parseErrors = $null
$scriptAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $scriptPath,
    [ref]$tokens,
    [ref]$parseErrors
)
$messageFunction = @($scriptAst.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Get-SimulationRunnerExitMessage'
}, $true)) | Select-Object -First 1
$healthFunction = @($scriptAst.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Test-BackendHealth'
}, $true)) | Select-Object -First 1
$waitFunction = @($scriptAst.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Wait-BackendHealth'
}, $true)) | Select-Object -First 1

if ($null -ne $messageFunction) {
    Invoke-Expression $messageFunction.Extent.Text
}
else {
    function Get-SimulationRunnerExitMessage {
        param([int]$ExitCode)

        return $null
    }
}

if ($null -ne $waitFunction) {
    Invoke-Expression $waitFunction.Extent.Text
}

Describe 'run-simulations 退出码投影' {
    It '提供独立的退出码提示函数' {
        $messageFunction | Should Not BeNullOrEmpty
        $parseErrors.Count | Should Be 0
    }

    It '退出 1 表示测评完成但结果未通过，且不猜测 API Key' {
        $message = Get-SimulationRunnerExitMessage -ExitCode 1

        $message | Should Be '测评已完成，但结果未通过；请查看黑箱检查结果'
        $message | Should Not Match 'API Key|密钥|配置'
    }

    It '退出 2 只说明运行未开始或中断，并提示保留上方原因' {
        $message = Get-SimulationRunnerExitMessage -ExitCode 2

        $message | Should Be '测评未开始或运行中断；具体原因请查看上方运行器输出'
        $message | Should Not Match 'API Key|密钥|配置'
    }

    foreach ($exitCode in @(3, 9, 127)) {
        It "退出 $exitCode 表示脚本或运行器错误" {
            Get-SimulationRunnerExitMessage -ExitCode $exitCode |
                Should Be '黑箱测评脚本或运行器发生错误；请查看上方运行器输出'
        }
    }

    It '普通运行保存并原样传播 Python 退出码' {
        $scriptText | Should Match '\$runnerExitCode\s*=\s*\$LASTEXITCODE'
        $scriptText | Should Match 'exit\s+\$runnerExitCode'
        $scriptText | Should Not Match '若 API Key 尚未配置'
    }

    It 'CheckOnly 保持失败抛错和成功退出 0 的现有语义' {
        $scriptText | Should Match '黑箱测评环境或场景检查未通过。'
        $scriptText | Should Match 'exit\s+0'
    }

    It '允许单独运行直接跳问场景' {
        $scriptText | Should Match '\[string\]\$Suite'
    }

    It '向 Python 运行器透传案例、场域和可选目录' {
        $scriptText | Should Match '''--case-id'',\s*\$CaseId'
        $scriptText | Should Match '''--scene'',\s*\$Scene'
        $scriptText | Should Match "'--catalog'"
        $scriptText | Should Match '\$Catalog'
    }

    It '保留旧案例与热线作为无参数默认值' {
        $scriptText | Should Match '\[string\]\$CaseId\s*=\s*''crisis_student_main'''
        $scriptText | Should Match '\[string\]\$Scene\s*=\s*''hotline'''
    }

    It '健康检查只接受当前项目已就绪的后端' {
        $healthFunction | Should Not BeNullOrEmpty
        $healthText = $healthFunction.Extent.Text

        $healthText | Should Match 'Invoke-RestMethod'
        $healthText | Should Match "status.*'ready'"
        $healthText | Should Match "service.*'psych-assessment-demo'"
        $healthText | Should Not Match 'StatusCode\s+-eq\s+200'
    }

    It '短条件轮询会吸收瞬时未就绪' {
        $waitFunction | Should Not BeNullOrEmpty
        if ($null -eq $waitFunction) {
            return
        }

        $script:healthProbeCount = 0
        function Test-BackendHealth {
            $script:healthProbeCount++
            return $script:healthProbeCount -ge 3
        }

        Wait-BackendHealth -MaxAttempts 3 -DelayMilliseconds 0 | Should Be $true
        $script:healthProbeCount | Should Be 3
    }

    It '启动服务前后都通过有界轮询确认后端就绪' {
        $waitCalls = @($scriptAst.FindAll({
            param($node)
            $node -is [System.Management.Automation.Language.CommandAst] -and
                $node.GetCommandName() -eq 'Wait-BackendHealth'
        }, $true))

        $waitCalls.Count | Should Be 2
        $waitFunction.Extent.Text | Should Match 'Start-Sleep\s+-Milliseconds'
    }
}
