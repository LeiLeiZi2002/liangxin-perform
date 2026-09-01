$helperPath = Join-Path $PSScriptRoot '..\provider-key-sync.ps1'
$startDemoPath = Join-Path $PSScriptRoot '..\start-demo.ps1'

if (Test-Path -LiteralPath $helperPath) {
    . $helperPath
}

function Set-TestProcessApiKey {
    param([AllowNull()][string]$Value)

    if ($null -eq $Value) {
        Remove-Item Env:DASHSCOPE_API_KEY -ErrorAction SilentlyContinue
        return
    }
    $env:DASHSCOPE_API_KEY = $Value
}

Describe 'provider-key-sync' {
    It '存在可加载的辅助脚本' {
        Test-Path -LiteralPath $helperPath | Should Be $true
    }

    Context 'Get-StartupApiKey' {
        It '优先使用并清理当前进程环境中的 Key' {
            $hadOriginalValue = Test-Path Env:DASHSCOPE_API_KEY
            $originalValue = $env:DASHSCOPE_API_KEY
            try {
                Set-TestProcessApiKey '  fake-process-key  '
                Mock Get-UserEnvironmentVariable { 'fake-user-key' }

                Get-StartupApiKey | Should Be 'fake-process-key'
                Assert-MockCalled Get-UserEnvironmentVariable -Times 0 -Scope It
            }
            finally {
                if ($hadOriginalValue) {
                    Set-TestProcessApiKey $originalValue
                }
                else {
                    Set-TestProcessApiKey $null
                }
            }
        }

        It '进程环境为空白时使用并清理 User 环境中的 Key' {
            $hadOriginalValue = Test-Path Env:DASHSCOPE_API_KEY
            $originalValue = $env:DASHSCOPE_API_KEY
            try {
                Set-TestProcessApiKey '   '
                Mock Get-UserEnvironmentVariable { '  fake-user-key  ' } `
                    -ParameterFilter { $Name -eq 'DASHSCOPE_API_KEY' }

                Get-StartupApiKey | Should Be 'fake-user-key'
                Assert-MockCalled Get-UserEnvironmentVariable -Times 1 -Scope It `
                    -ParameterFilter { $Name -eq 'DASHSCOPE_API_KEY' }
            }
            finally {
                if ($hadOriginalValue) {
                    Set-TestProcessApiKey $originalValue
                }
                else {
                    Set-TestProcessApiKey $null
                }
            }
        }

        It 'User 环境读取异常只返回固定中文错误' {
            $hadOriginalValue = Test-Path Env:DASHSCOPE_API_KEY
            $originalValue = $env:DASHSCOPE_API_KEY
            $sensitiveError = 'fake-user-read-sensitive-error'
            try {
                Set-TestProcessApiKey '   '
                Mock Get-UserEnvironmentVariable {
                    throw 'fake-user-read-sensitive-error'
                } -ParameterFilter { $Name -eq 'DASHSCOPE_API_KEY' }
                $message = $null
                $errorText = $null

                try {
                    Get-StartupApiKey
                }
                catch {
                    $message = $_.Exception.Message
                    $errorText = $_ | Out-String
                }

                $message | Should Be '载入本机环境中的模型服务密钥失败。'
                $message | Should Not Match ([regex]::Escape($sensitiveError))
                $errorText | Should Not Match ([regex]::Escape($sensitiveError))
                Assert-MockCalled Get-UserEnvironmentVariable -Times 1 -Scope It
            }
            finally {
                if ($hadOriginalValue) {
                    Set-TestProcessApiKey $originalValue
                }
                else {
                    Set-TestProcessApiKey $null
                }
            }
        }
    }

    Context 'Sync-ProviderApiKey' {
        BeforeEach {
            $script:putBody = $null
            $script:providerConfigResponse = [pscustomobject]@{
                configured = $true
                masked_key = '••••0000'
                workspace_id = 'workspace-existing'
                director_model = 'director-existing'
                actor_model = 'actor-existing'
                asr_model = 'asr-existing'
                tts_model = 'tts-existing'
                tts_voice = 'voice-existing'
                director_temperature = 0.2
                actor_temperature = 0.7
            }
            Mock Invoke-RestMethod {
                if ($Method -eq 'Get') {
                    return $script:providerConfigResponse
                }
                if ($Method -eq 'Put') {
                    $script:putBody = $Body
                    return $script:providerConfigResponse
                }
                throw '测试遇到非预期 HTTP 方法。'
            }
        }

        It '只向固定本机端点提交 Key 和当前 workspace_id' {
            Sync-ProviderApiKey -ApiKey 'fake-sync-key'

            Assert-MockCalled Invoke-RestMethod -Times 1 -Scope It -ParameterFilter {
                $Uri -eq 'http://127.0.0.1:8000/api/provider-config' -and
                $Method -eq 'Get'
            }
            Assert-MockCalled Invoke-RestMethod -Times 1 -Scope It -ParameterFilter {
                $Uri -eq 'http://127.0.0.1:8000/api/provider-config' -and
                $Method -eq 'Put'
            }
            $payload = $script:putBody | ConvertFrom-Json
            $payload.api_key | Should Be 'fake-sync-key'
            $payload.workspace_id | Should Be 'workspace-existing'
            (($payload.PSObject.Properties.Name | Sort-Object) -join ',') |
                Should Be 'api_key,workspace_id'
        }

        It '成功输出固定文案且不泄露 Key' {
            $fakeApiKey = 'fake-output-secret'

            $output = (& { Sync-ProviderApiKey -ApiKey $fakeApiKey } 6>&1 | Out-String)

            $output | Should Match '^已载入本机环境中的模型服务密钥。\s*$'
            $output | Should Not Match ([regex]::Escape($fakeApiKey))
        }

        It '没有 Key 时不访问 HTTP' {
            $output = (& { Sync-ProviderApiKey -ApiKey '   ' } 6>&1 | Out-String)

            $output | Should BeNullOrEmpty
            Assert-MockCalled Invoke-RestMethod -Times 0 -Scope It
        }

        It 'HTTP 异常只返回固定中文错误' {
            $fakeApiKey = 'fake-error-secret'
            Mock Invoke-RestMethod { throw "响应包含 $fakeApiKey" }
            $message = $null

            try {
                Sync-ProviderApiKey -ApiKey $fakeApiKey
            }
            catch {
                $message = $_.Exception.Message
            }

            $message | Should Be '载入本机环境中的模型服务密钥失败。'
            $message | Should Not Match ([regex]::Escape($fakeApiKey))
        }
    }
}

Describe 'start-demo 密钥同步接入' {
    $source = Get-Content -LiteralPath $startDemoPath -Raw
    $startTokens = $null
    $startParseErrors = $null
    $startAst = [System.Management.Automation.Language.Parser]::ParseFile(
        (Resolve-Path -LiteralPath $startDemoPath).Path,
        [ref]$startTokens,
        [ref]$startParseErrors
    )
    $ifNodes = @($startAst.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.IfStatementAst]
    }, $true))
    $syncNodes = @($startAst.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.CommandAst] -and
        $node.GetCommandName() -eq 'Sync-ProviderApiKey'
    }, $true))
    $outerTryNodes = @($startAst.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.TryStatementAst]
    }, $true))

    It '载入辅助脚本且脚本语法可解析' {
        @($startParseErrors).Count | Should Be 0
        $source | Should Match '(?m)^\.\s+.*provider-key-sync\.ps1'
    }

    It 'CheckOnly 路径不会读取或同步 Key' {
        $startupReadGuards = @($ifNodes | Where-Object {
            $_.Clauses.Count -eq 1 -and
            $_.Clauses[0].Item1.Extent.Text.Trim() -eq '-not $CheckOnly'
        })
        $checkOnlyBranches = @($ifNodes | Where-Object {
            $_.Clauses.Count -eq 1 -and
            $_.Clauses[0].Item1.Extent.Text.Trim() -eq '$CheckOnly'
        })
        $startupReadCommands = @($startAst.FindAll({
            param($node)
            $node -is [System.Management.Automation.Language.CommandAst] -and
            $node.GetCommandName() -eq 'Get-StartupApiKey'
        }, $true))
        $directUserReadCommands = @($startAst.FindAll({
            param($node)
            $node -is [System.Management.Automation.Language.CommandAst] -and
            $node.GetCommandName() -eq 'Get-UserEnvironmentVariable'
        }, $true))

        $startupReadGuards.Count | Should Be 1
        $checkOnlyBranches.Count | Should Be 1
        $startupReadCommands.Count | Should Be 1
        $startupReadCommands[0].Parent.Parent.Parent |
            Should Be $startupReadGuards[0].Clauses[0].Item2
        $directUserReadCommands.Count | Should Be 0

        $checkOnlyBody = $checkOnlyBranches[0].Clauses[0].Item2
        $checkOnlyCommands = @($checkOnlyBody.FindAll({
            param($node)
            $node -is [System.Management.Automation.Language.CommandAst]
        }, $true) | ForEach-Object { $_.GetCommandName() })
        $checkOnlyExits = @($checkOnlyBody.FindAll({
            param($node)
            $node -is [System.Management.Automation.Language.ExitStatementAst]
        }, $true))
        ($checkOnlyCommands -contains 'Get-StartupApiKey') | Should Be $false
        ($checkOnlyCommands -contains 'Get-UserEnvironmentVariable') | Should Be $false
        ($checkOnlyCommands -contains 'Sync-ProviderApiKey') | Should Be $false
        $checkOnlyExits.Count | Should Be 1
        $checkOnlyExits[0].Extent.Text.Trim() | Should Be 'exit 0'
        $checkOnlyExits[0].Parent | Should Be $checkOnlyBody

        $guardText = $startupReadGuards[0].Clauses[0].Item2.Extent.Text
        $guardText | Should Match '\$env:DASHSCOPE_API_KEY'
        $guardText | Should Match 'Remove-Item Env:DASHSCOPE_API_KEY'
        $syncNodes.Count | Should Be 1
        $syncNodes[0].Extent.StartOffset |
            Should BeGreaterThan $checkOnlyBranches[0].Extent.EndOffset
    }

    It '服务已运行也不早退复用旧进程' {
        $runningBranches = @($ifNodes | Where-Object {
            $condition = $_.Clauses[0].Item1.Extent.Text -replace '\s+', ' '
            $condition -match '\$healthUrl.*\$demoUrl'
        })

        $runningBranches.Count | Should Be 0
        $source | Should Not Match 'DEMO 已在运行'
    }

    It '新启动健康出口只同步一次且不属于早退分支' {
        $mainTryNodes = @($outerTryNodes | Where-Object {
            @($_.Body.FindAll({
                param($node)
                $node -is [System.Management.Automation.Language.CommandAst] -and
                $node.GetCommandName() -eq 'Sync-ProviderApiKey'
            }, $true)).Count -eq 1
        })
        $mainTryNodes.Count | Should Be 1
        $outerTry = $mainTryNodes[0]
        $directHealthySyncs = @($syncNodes | Where-Object {
            $_.Parent.Parent -eq $outerTry.Body
        })
        $unhealthyGuards = @($ifNodes | Where-Object {
            $_.Clauses.Count -eq 1 -and
            $_.Clauses[0].Item1.Extent.Text.Trim() -eq '-not $healthy'
        })

        $syncNodes.Count | Should Be 1
        $directHealthySyncs.Count | Should Be 1
        $unhealthyGuards.Count | Should Be 1
        $directUnhealthyThrows = @($unhealthyGuards[0].Clauses[0].Item2.Statements |
            Where-Object {
                $_ -is [System.Management.Automation.Language.ThrowStatementAst]
            })
        $directUnhealthyThrows.Count | Should Be 1
        $directHealthySyncs[0].Extent.StartOffset |
            Should BeGreaterThan $unhealthyGuards[0].Extent.EndOffset
    }

    It '在首个 wsl.exe 调用前移除进程环境 Key 并在 finally 恢复' {
        $mainTry = @($outerTryNodes | Where-Object {
            @($_.Body.FindAll({
                param($node)
                $node -is [System.Management.Automation.Language.CommandAst] -and
                $node.GetCommandName() -eq 'Get-StartupApiKey'
            }, $true)).Count -eq 1
        }) | Select-Object -First 1
        $captureCommand = @($mainTry.Body.FindAll({
            param($node)
            $node -is [System.Management.Automation.Language.CommandAst] -and
            $node.GetCommandName() -eq 'Get-StartupApiKey'
        }, $true)) | Select-Object -First 1
        $removeCommand = @($mainTry.Body.FindAll({
            param($node)
            $node -is [System.Management.Automation.Language.CommandAst] -and
            $node.GetCommandName() -eq 'Remove-Item' -and
            $node.Extent.Text -match 'Env:DASHSCOPE_API_KEY'
        }, $true)) | Select-Object -First 1
        $firstWslCommand = @($mainTry.Body.FindAll({
            param($node)
            $node -is [System.Management.Automation.Language.CommandAst] -and
            $node.GetCommandName() -eq 'wsl.exe'
        }, $true) | Sort-Object { $_.Extent.StartOffset }) | Select-Object -First 1
        $restoreAssignment = @($mainTry.Finally.FindAll({
            param($node)
            $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $node.Left.Extent.Text -eq '$env:DASHSCOPE_API_KEY'
        }, $true)) | Select-Object -First 1

        $captureCommand | Should Not BeNullOrEmpty
        $removeCommand | Should Not BeNullOrEmpty
        $firstWslCommand | Should Not BeNullOrEmpty
        $restoreAssignment | Should Not BeNullOrEmpty
        $removeCommand.Extent.StartOffset |
            Should BeGreaterThan $captureCommand.Extent.EndOffset
        $firstWslCommand.Extent.StartOffset |
            Should BeGreaterThan $removeCommand.Extent.EndOffset
        $restoreAssignment.Extent.StartOffset |
            Should BeGreaterThan $mainTry.Body.Extent.EndOffset
    }
}
