function Get-UserEnvironmentVariable {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Name
    )

    return [Environment]::GetEnvironmentVariable($Name, 'User')
}

function Get-StartupApiKey {
    [CmdletBinding()]
    param()

    $processApiKey = $env:DASHSCOPE_API_KEY
    if (-not [string]::IsNullOrWhiteSpace($processApiKey)) {
        return $processApiKey.Trim()
    }

    try {
        $userApiKey = Get-UserEnvironmentVariable -Name 'DASHSCOPE_API_KEY'
    }
    catch {
        throw '载入本机环境中的模型服务密钥失败。'
    }
    if ([string]::IsNullOrWhiteSpace($userApiKey)) {
        return [string]::Empty
    }
    return $userApiKey.Trim()
}

function Sync-ProviderApiKey {
    [CmdletBinding()]
    param(
        [AllowNull()]
        [AllowEmptyString()]
        [string]$ApiKey
    )

    if ([string]::IsNullOrWhiteSpace($ApiKey)) {
        return
    }

    $providerConfigUri = 'http://127.0.0.1:8000/api/provider-config'
    try {
        $currentConfig = Invoke-RestMethod -Uri $providerConfigUri -Method Get -TimeoutSec 5
        $workspaceProperty = $currentConfig.PSObject.Properties['workspace_id']
        if ($null -eq $workspaceProperty) {
            throw '本机配置响应缺少 workspace_id。'
        }

        $payload = [ordered]@{
            api_key = $ApiKey.Trim()
            workspace_id = $workspaceProperty.Value
        } | ConvertTo-Json -Compress

        $null = Invoke-RestMethod -Uri $providerConfigUri -Method Put `
            -ContentType 'application/json; charset=utf-8' -Body $payload -TimeoutSec 5
        Write-Host '已载入本机环境中的模型服务密钥。'
    }
    catch {
        throw '载入本机环境中的模型服务密钥失败。'
    }
}
