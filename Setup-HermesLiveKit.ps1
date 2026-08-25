[CmdletBinding()]
param(
    [string]$HermesHome = (Join-Path $env:LOCALAPPDATA "hermes"),
    [string]$LiveKitUrl,
    [string]$LiveKitApiKey,
    [string]$LiveKitApiSecret,
    [string]$LiveKitEnvFile,
    [string]$Room = "hermes",
    [string]$AgentName = "Hermes",
    [switch]$AllowAllUsers,
    [switch]$ReplaceSoul,
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
    param([string]$HomePath, [string]$LegacyPort)

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

    $controlPath = Join-Path $HomePath "control"
    if (Test-Path -LiteralPath $controlPath -PathType Container) {
        Get-ChildItem -LiteralPath $controlPath -File -Force |
            Where-Object {
                $_.Name -eq "control_bridge.py" -or
                $_.Name -like "control_bridge.py.retired-*"
            } |
            ForEach-Object {
                Remove-Item -LiteralPath $_.FullName -Force
                Write-Host "Removed obsolete Hermes control bridge artifact '$($_.Name)'."
            }
    }
}

function Remove-StaleLiveKitBackups {
    param([string]$HomePath, [string]$PluginsPath)

    # Hermes scans every immediate plugin directory and de-duplicates by the
    # manifest's name. A directory named hermes-livekit.backup-* still contains
    # `name: hermes-livekit`, so it can override the active plugin at startup.
    # Rollback copies therefore live in the system temp directory during setup,
    # and any backups created by older versions of this script are removed only
    # after the new plugin and configuration have passed validation.
    if (Test-Path -LiteralPath $PluginsPath -PathType Container) {
        Get-ChildItem -LiteralPath $PluginsPath -Directory -Force |
            Where-Object { $_.Name -like "hermes-livekit.backup-*" } |
            ForEach-Object {
                Remove-Item -LiteralPath $_.FullName -Recurse -Force
                Write-Host "Removed stale discoverable plugin backup '$($_.Name)'."
            }
    }

    foreach ($pattern in @(".env.livekit-*.bak", "config.yaml.livekit-*.bak")) {
        Get-ChildItem -LiteralPath $HomePath -File -Filter $pattern -Force |
            ForEach-Object {
                Remove-Item -LiteralPath $_.FullName -Force
                Write-Host "Removed superseded LiveKit config backup '$($_.Name)'."
            }
    }
}

function Install-MiraSoul {
    param(
        [string]$SourcePath,
        [string]$TargetPath,
        [switch]$Force
    )

    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
        throw "MiRA SOUL template is missing: $SourcePath"
    }
    if (-not (Test-Path -LiteralPath $TargetPath -PathType Leaf)) {
        Copy-Item -LiteralPath $SourcePath -Destination $TargetPath -Force
        Write-Host "Installed the MiRA conversational identity in SOUL.md."
        return $true
    }

    $existing = (Get-Content -LiteralPath $TargetPath -Raw).Trim()
    $isStarterSoul = (
        $existing.StartsWith(
            "You are Hermes Agent, an intelligent AI assistant created by Nous Research.",
            [System.StringComparison]::Ordinal
        ) -and $existing.Length -lt 2000
    )
    if (-not $Force -and -not $isStarterSoul) {
        Write-Warning (
            "Preserving the customized Hermes SOUL.md. Compare it with " +
            "'$SourcePath', or rerun with -ReplaceSoul to install MiRA's identity."
        )
        return $false
    }

    Copy-Item -LiteralPath $TargetPath -Destination "$TargetPath.mira.bak" -Force
    Copy-Item -LiteralPath $SourcePath -Destination $TargetPath -Force
    Write-Host "Installed the MiRA conversational identity in SOUL.md."
    return $true
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
$soulPath = Join-Path $HermesHome "SOUL.md"
$soulSource = Join-Path $sourcePlugin "assets\SOUL.md"
$pythonExe = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe"
$hermesExe = Join-Path $HermesHome "hermes-agent\venv\Scripts\hermes.exe"
if (-not (Test-Path -LiteralPath $pythonExe) -or -not (Test-Path -LiteralPath $hermesExe)) {
    throw "Hermes Python/CLI was not found under '$HermesHome\hermes-agent\venv\Scripts'."
}

$existingEnv = Get-DotEnvValues $envPath
$allowAllUsersValue = "false"
if ($PSBoundParameters.ContainsKey("AllowAllUsers")) {
    $allowAllUsersValue = "true"
} elseif ($existingEnv["LIVEKIT_ALLOW_ALL_USERS"] -in @("true", "false")) {
    # Preserve an operator's explicit existing policy on upgrades. New installs
    # fail closed unless -AllowAllUsers is deliberately supplied.
    $allowAllUsersValue = $existingEnv["LIVEKIT_ALLOW_ALL_USERS"]
}
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
$hadConfig = Test-Path -LiteralPath $configPath -PathType Leaf
$hadEnv = Test-Path -LiteralPath $envPath -PathType Leaf
$hadSoul = Test-Path -LiteralPath $soulPath -PathType Leaf
$soulChanged = $false
if (Test-Path -LiteralPath $configPath) {
    Copy-Item -LiteralPath $configPath -Destination "$configPath.livekit.bak" -Force
}
if (Test-Path -LiteralPath $envPath) {
    Copy-Item -LiteralPath $envPath -Destination "$envPath.livekit.bak" -Force
}

$pluginRollback = $null
try {
    New-Item -ItemType Directory -Path $pluginsDir -Force | Out-Null
    if ($sourcePlugin -ne $targetPlugin) {
        foreach ($runtimeFile in @("adapter.py", "__init__.py", "tools.py", "configure_yaml.py", "plugin.yaml", "LICENSE")) {
            $runtimeSource = Join-Path $sourcePlugin $runtimeFile
            if (-not (Test-Path -LiteralPath $runtimeSource -PathType Leaf)) {
                throw "Required plugin runtime file is missing: $runtimeSource"
            }
        }
        $skillSource = Join-Path $sourcePlugin "skills\mira-new-zealand-tourism\SKILL.md"
        if (-not (Test-Path -LiteralPath $skillSource -PathType Leaf)) {
            throw "Required MiRA tourism skill is missing: $skillSource"
        }
        if (Test-Path -LiteralPath $targetPlugin) {
            $pluginRollback = Join-Path ([System.IO.Path]::GetTempPath()) "hermes-livekit-rollback-$timestamp"
            if (Test-Path -LiteralPath $pluginRollback) {
                Remove-Item -LiteralPath $pluginRollback -Recurse -Force
            }
            Copy-Item -LiteralPath $targetPlugin -Destination $pluginRollback -Recurse -Force
            Write-Host "Temporary rollback copy created outside the Hermes plugin directory."
            Remove-Item -LiteralPath $targetPlugin -Recurse -Force
        }
        New-Item -ItemType Directory -Path $targetPlugin -Force | Out-Null
        foreach ($runtimeFile in @("adapter.py", "__init__.py", "tools.py", "configure_yaml.py", "plugin.yaml", "LICENSE")) {
            $runtimeSource = Join-Path $sourcePlugin $runtimeFile
            Copy-Item -LiteralPath $runtimeSource -Destination (Join-Path $targetPlugin $runtimeFile) -Force
        }
        Copy-Item -LiteralPath (Join-Path $sourcePlugin "skills") -Destination $targetPlugin -Recurse -Force
    }

    $soulChanged = Install-MiraSoul -SourcePath $soulSource -TargetPath $soulPath -Force:$ReplaceSoul

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
    Set-DotEnvValue $envPath "LIVEKIT_ALLOW_ALL_USERS" $allowAllUsersValue
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
    Remove-LegacyControlBridge $HermesHome $existingEnv["HERMES_CONTROL_PORT"]

    $previousHermesHome = $env:HERMES_HOME
    $env:HERMES_HOME = $HermesHome
    try {
        & $hermesExe config migrate
        if ($LASTEXITCODE -ne 0) { throw "Hermes configuration migration failed." }
        & $hermesExe plugins enable hermes-livekit --no-allow-tool-override
        if ($LASTEXITCODE -ne 0) { throw "Hermes LiveKit plugin enablement failed." }
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

        Remove-StaleLiveKitBackups $HermesHome $pluginsDir
        if ($InstallAutoStart) {
            & $hermesExe gateway install
            if ($LASTEXITCODE -ne 0) { throw "Hermes gateway auto-start installation failed." }
        }
        if ($RestartGateway) {
            & $hermesExe gateway stop
            Start-Process -FilePath $hermesExe -ArgumentList @("gateway", "run") -WindowStyle Hidden
            Start-Sleep -Seconds 2
            & $hermesExe gateway status
            if ($LASTEXITCODE -ne 0) { throw "Hermes gateway did not restart successfully." }
        }
    } finally {
        $env:HERMES_HOME = $previousHermesHome
    }

    if ($pluginRollback -and (Test-Path -LiteralPath $pluginRollback)) {
        Remove-Item -LiteralPath $pluginRollback -Recurse -Force
        $pluginRollback = $null
    }
} catch {
    Write-Warning "Setup failed; restoring the previous Hermes LiveKit state."
    if ($hadConfig -and (Test-Path -LiteralPath "$configPath.livekit.bak")) {
        Copy-Item -LiteralPath "$configPath.livekit.bak" -Destination $configPath -Force
    }
    if ($hadEnv -and (Test-Path -LiteralPath "$envPath.livekit.bak")) {
        Copy-Item -LiteralPath "$envPath.livekit.bak" -Destination $envPath -Force
    }
    if ($soulChanged) {
        if ($hadSoul -and (Test-Path -LiteralPath "$soulPath.mira.bak")) {
            Copy-Item -LiteralPath "$soulPath.mira.bak" -Destination $soulPath -Force
        } elseif (-not $hadSoul -and (Test-Path -LiteralPath $soulPath)) {
            Remove-Item -LiteralPath $soulPath -Force
        }
    }
    if ($pluginRollback -and (Test-Path -LiteralPath $pluginRollback)) {
        if (Test-Path -LiteralPath $targetPlugin) {
            Remove-Item -LiteralPath $targetPlugin -Recurse -Force
        }
        Copy-Item -LiteralPath $pluginRollback -Destination $targetPlugin -Recurse -Force
        Remove-Item -LiteralPath $pluginRollback -Recurse -Force
        $pluginRollback = $null
    }
    throw
}

Write-Host ""
Write-Host "Hermes LiveKit setup is complete."
Write-Host "Plugin: $targetPlugin"
Write-Host "Config: $configPath"
Write-Host "Room:   $Room"
if (-not $RestartGateway) {
    Write-Host "Restart the Hermes gateway to load the new plugin and YAML configuration."
}
