[CmdletBinding()]
param(
    [string]$HermesHome = (Join-Path $env:LOCALAPPDATA "hermes"),
    [string]$LiveKitUrl,
    [string]$LiveKitApiKey,
    [string]$LiveKitApiSecret,
    [string]$LiveKitEnvFile,
    [string]$Room = "hermes",
    [string]$AgentName = "Hermes",
    [switch]$InstallHermes,
    [switch]$InstallFfmpeg,
    [switch]$RestartGateway,
    [switch]$InstallAutoStart
)

$ErrorActionPreference = "Stop"

function Read-RequiredValue {
    param([string]$CurrentValue, [string]$Prompt, [switch]$Secret)
    if (-not [string]::IsNullOrWhiteSpace($CurrentValue)) {
        return $CurrentValue
    }
    if ($Secret) {
        $secure = Read-Host $Prompt -AsSecureString
        return [System.Net.NetworkCredential]::new("", $secure).Password
    }
    return Read-Host $Prompt
}

function Get-DotEnvValues {
    param([string]$Path)
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $values
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $parts = $line.Split([char[]]"=", 2)
        $name = $parts[0].Trim()
        if ($name.StartsWith("export ", [System.StringComparison]::OrdinalIgnoreCase)) {
            $name = $name.Substring(7).Trim()
        }
        $value = $parts[1].Trim().Trim('"').Trim("'")
        $values[$name] = $value
    }
    return $values
}

function Set-DotEnvValue {
    param([string]$Path, [string]$Name, [string]$Value)
    $lines = [System.Collections.Generic.List[string]]::new()
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) { $lines.Add($line) }
    }
    $replacement = "$Name=$Value"
    $found = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match "^$([regex]::Escape($Name))=") {
            $lines[$index] = $replacement
            $found = $true
            break
        }
    }
    if (-not $found) { $lines.Add($replacement) }
    Set-Content -LiteralPath $Path -Value $lines -Encoding utf8
}

function Remove-DotEnvValues {
    param([string]$Path, [string[]]$Names)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $patterns = @($Names | ForEach-Object { "^$([regex]::Escape($_))=" })
    $lines = Get-Content -LiteralPath $Path | Where-Object {
        $line = $_
        -not ($patterns | Where-Object { $line -match $_ })
    }
    Set-Content -LiteralPath $Path -Value $lines -Encoding utf8
}

function Remove-LegacyControlBridge {
    param([string]$HomePath, [string]$Timestamp, [string]$LegacyPort)

    $taskName = "MiRA Hermes Control Bridge"
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Removed the obsolete '$taskName' scheduled task."
    }

    $parsedPort = 0
    if ([int]::TryParse($LegacyPort, [ref]$parsedPort) -and
        $parsedPort -ge 1 -and $parsedPort -le 65535) {
        $listeners = Get-NetTCPConnection -LocalPort $parsedPort -State Listen -ErrorAction SilentlyContinue
        foreach ($listener in $listeners) {
            Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped obsolete control listener on port $parsedPort (process $($listener.OwningProcess))."
        }
    }

    $escapedHome = [regex]::Escape($HomePath)
    $bridgeProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -match "control_bridge\.py" -and $_.CommandLine -match $escapedHome
    }
    foreach ($process in $bridgeProcesses) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped obsolete Hermes control bridge process $($process.ProcessId)."
    }

    $bridgePath = Join-Path $HomePath "control\control_bridge.py"
    if (Test-Path -LiteralPath $bridgePath -PathType Leaf) {
        $retiredPath = "$bridgePath.retired-$Timestamp"
        Move-Item -LiteralPath $bridgePath -Destination $retiredPath
        Write-Host "Retired legacy bridge code to $retiredPath"
    }
}

if (-not (Test-Path -LiteralPath $HermesHome) -and $InstallHermes) {
    Write-Host "Installing Hermes from the official Nous Research Windows installer..."
    $installerSource = Invoke-RestMethod -Uri "https://hermes-agent.nousresearch.com/install.ps1"
    & ([scriptblock]::Create($installerSource))
}
if (-not (Test-Path -LiteralPath $HermesHome)) {
    throw "Hermes is not installed at '$HermesHome'. Install Hermes first, then rerun this script."
}

$sourcePlugin = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$pluginsDir = Join-Path $HermesHome "plugins"
$targetPlugin = Join-Path $pluginsDir "hermes-livekit"
$envPath = Join-Path $HermesHome ".env"
$configPath = Join-Path $HermesHome "config.yaml"
$pythonExe = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe"
$hermesExe = Join-Path $HermesHome "hermes-agent\venv\Scripts\hermes.exe"
if (-not (Test-Path -LiteralPath $pythonExe) -or -not (Test-Path -LiteralPath $hermesExe)) {
    throw "Hermes Python/CLI was not found under '$HermesHome\hermes-agent\venv\Scripts'."
}

$existingEnv = Get-DotEnvValues $envPath
$existingValueMap = @{
    LiveKitUrl       = "LIVEKIT_URL"
    LiveKitApiKey    = "LIVEKIT_API_KEY"
    LiveKitApiSecret = "LIVEKIT_API_SECRET"
    Room             = "LIVEKIT_ROOM"
    AgentName        = "LIVEKIT_AGENT_NAME"
}
foreach ($parameterName in $existingValueMap.Keys) {
    $environmentName = $existingValueMap[$parameterName]
    if (-not $PSBoundParameters.ContainsKey($parameterName) -and
        -not [string]::IsNullOrWhiteSpace($existingEnv[$environmentName])) {
        Set-Variable -Name $parameterName -Value $existingEnv[$environmentName]
    }
}

$sourceEnv = @{}
if ($PSBoundParameters.ContainsKey("LiveKitEnvFile")) {
    $candidatePaths = @($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($LiveKitEnvFile))
} else {
    $repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
    $candidatePaths = @(
        (Join-Path $repositoryRoot ".env"),
        (Join-Path $repositoryRoot "infrastructure\livekitserver-docker\.env")
    )
}
foreach ($candidate in $candidatePaths) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    $candidateValues = Get-DotEnvValues $candidate
    if ($candidateValues["LIVEKIT_URL"] -and
        $candidateValues["LIVEKIT_API_KEY"] -and
        $candidateValues["LIVEKIT_API_SECRET"]) {
        $sourceEnv = $candidateValues
        Write-Host "Reusing LiveKit transport from '$candidate'."
        break
    }
}
foreach ($parameterName in @("LiveKitUrl", "LiveKitApiKey", "LiveKitApiSecret")) {
    $environmentName = $existingValueMap[$parameterName]
    if (-not $PSBoundParameters.ContainsKey($parameterName) -and
        [string]::IsNullOrWhiteSpace((Get-Variable -Name $parameterName -ValueOnly)) -and
        -not [string]::IsNullOrWhiteSpace($sourceEnv[$environmentName])) {
        Set-Variable -Name $parameterName -Value $sourceEnv[$environmentName]
    }
}

$LiveKitUrl = Read-RequiredValue $LiveKitUrl "LiveKit URL (ws:// or wss://)"
$LiveKitApiKey = Read-RequiredValue $LiveKitApiKey "LiveKit API key"
$LiveKitApiSecret = Read-RequiredValue $LiveKitApiSecret "LiveKit API secret" -Secret
if (-not $LiveKitUrl -or -not $LiveKitApiKey -or -not $LiveKitApiSecret) {
    throw "LiveKit URL, API key, and API secret are required."
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
if (Test-Path -LiteralPath $configPath) {
    Copy-Item -LiteralPath $configPath -Destination "$configPath.livekit-$timestamp.bak"
}
if (Test-Path -LiteralPath $envPath) {
    Copy-Item -LiteralPath $envPath -Destination "$envPath.livekit-$timestamp.bak"
}

New-Item -ItemType Directory -Path $pluginsDir -Force | Out-Null
if ($sourcePlugin -ne $targetPlugin) {
    if (Test-Path -LiteralPath $targetPlugin) {
        $pluginBackup = "$targetPlugin.backup-$timestamp"
        Move-Item -LiteralPath $targetPlugin -Destination $pluginBackup
        Write-Host "Existing plugin backed up to $pluginBackup"
    }
    New-Item -ItemType Directory -Path $targetPlugin -Force | Out-Null
    foreach ($runtimeFile in @("adapter.py", "__init__.py", "configure_yaml.py", "plugin.yaml", "LICENSE")) {
        $runtimeSource = Join-Path $sourcePlugin $runtimeFile
        if (-not (Test-Path -LiteralPath $runtimeSource)) {
            throw "Required plugin runtime file is missing: $runtimeSource"
        }
        Copy-Item -LiteralPath $runtimeSource -Destination (Join-Path $targetPlugin $runtimeFile) -Force
    }
}

Write-Host "Installing LiveKit and vision dependencies in the Hermes environment..."
& $pythonExe -m pip install "livekit==1.1.10" "livekit-api==1.1.0" "pillow>=10" "pyyaml>=6"
if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    if ($InstallFfmpeg) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if (-not $winget) { throw "ffmpeg is missing and winget is unavailable." }
        & $winget.Source install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw "ffmpeg installation failed." }
    } else {
        Write-Warning "ffmpeg is not on PATH. Install it before using TTS."
    }
}

Set-DotEnvValue $envPath "LIVEKIT_URL" $LiveKitUrl
Set-DotEnvValue $envPath "LIVEKIT_API_KEY" $LiveKitApiKey
Set-DotEnvValue $envPath "LIVEKIT_API_SECRET" $LiveKitApiSecret
Set-DotEnvValue $envPath "LIVEKIT_ROOM" $Room
Set-DotEnvValue $envPath "LIVEKIT_AGENT_NAME" $AgentName
Set-DotEnvValue $envPath "LIVEKIT_ALLOW_ALL_USERS" "true"
Remove-DotEnvValues $envPath @(
    "HERMES_LIVEKIT_AUTO_VISION",
    "HERMES_LIVEKIT_VIDEO_SAMPLE_SECONDS",
    "HERMES_LIVEKIT_VIDEO_MAX_AGE_SECONDS",
    "HERMES_LIVEKIT_SILENCE_SECONDS",
    "HERMES_LIVEKIT_WORK_ACK_SECONDS",
    "HERMES_LIVEKIT_WORK_ACK_MODE",
    "HERMES_LIVEKIT_WORK_ACK_TEXT",
    "HERMES_AGENT_NOTIFY_INTERVAL",
    "HERMES_CONTROL_HOST",
    "HERMES_CONTROL_PORT",
    "HERMES_CONTROL_TOKEN"
)
Remove-LegacyControlBridge $HermesHome $timestamp $existingEnv["HERMES_CONTROL_PORT"]

$previousHermesHome = $env:HERMES_HOME
$env:HERMES_HOME = $HermesHome
try {
    & $hermesExe plugins enable hermes-livekit
    & $hermesExe config set platforms.livekit.enabled true
    & $hermesExe config set platforms.livekit.group_sessions_per_user false
    & $hermesExe config set platforms.livekit.extra.url '${LIVEKIT_URL}'
    & $hermesExe config set platforms.livekit.extra.api_key '${LIVEKIT_API_KEY}'
    & $hermesExe config set platforms.livekit.extra.api_secret '${LIVEKIT_API_SECRET}'
    & $hermesExe config set platforms.livekit.extra.room '${LIVEKIT_ROOM}'
    & $hermesExe config set platforms.livekit.extra.agent_name '${LIVEKIT_AGENT_NAME}'
    & $pythonExe (Join-Path $targetPlugin "configure_yaml.py") --config $configPath
    if ($LASTEXITCODE -ne 0) { throw "Hermes LiveKit YAML update failed." }
    & $hermesExe config set voice.auto_tts true
    & $hermesExe config set display.busy_input_mode interrupt
    & $hermesExe config set display.platforms.livekit.streaming false
    & $hermesExe config set display.platforms.livekit.long_running_notifications false
    & $hermesExe config set display.platforms.livekit.busy_ack_detail false

    & $pythonExe -c "import livekit.rtc, livekit.api, PIL, yaml; print('Dependency check: OK')"
    if ($LASTEXITCODE -ne 0) { throw "Dependency import check failed." }
    & $hermesExe config check
    if ($LASTEXITCODE -ne 0) { throw "Hermes configuration validation failed." }

    if ($InstallAutoStart) {
        & $hermesExe gateway install
        if ($LASTEXITCODE -ne 0) { throw "Hermes gateway auto-start installation failed." }
    }
    if ($RestartGateway) {
        & $hermesExe gateway stop
        Start-Process -FilePath $hermesExe -ArgumentList @("gateway", "run") -WindowStyle Hidden
        Start-Sleep -Seconds 2
        & $hermesExe gateway status
    }
} finally {
    $env:HERMES_HOME = $previousHermesHome
}

Write-Host ""
Write-Host "Hermes LiveKit setup is complete."
Write-Host "Plugin: $targetPlugin"
Write-Host "Config: $configPath"
Write-Host "Room:   $Room"
if (-not $RestartGateway) {
    Write-Host "Restart the Hermes gateway to load the new plugin and YAML configuration."
}
