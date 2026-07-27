[CmdletBinding()]
param(
    [string]$HermesHome = (Join-Path $env:LOCALAPPDATA "hermes"),
    [string]$LiveKitUrl,
    [string]$LiveKitApiKey,
    [string]$LiveKitApiSecret,
    [string]$LiveKitEnvFile,
    [string]$Room = "hermes",
    [string]$AgentName = "Hermes",
    [string]$ControlHost = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$ControlPort = 8790,
    [string]$ControlToken,
    [string]$AdvertisedControlUrl,
    [switch]$ShowPortalConfig,
    [switch]$InstallHermes,
    [switch]$InstallFfmpeg,
    [switch]$RestartGateway,
    [switch]$StartControlBridge,
    [switch]$InstallAutoStart
)

$ErrorActionPreference = "Stop"

if ($ShowPortalConfig -and
    ($InstallHermes -or $InstallFfmpeg -or $RestartGateway -or $StartControlBridge -or $InstallAutoStart)) {
    throw "ShowPortalConfig is read-only and cannot be combined with install, start, or restart switches."
}

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

function Set-DotEnvValue {
    param([string]$Path, [string]$Name, [string]$Value)
    $lines = [System.Collections.Generic.List[string]]::new()
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) {
            $lines.Add($line)
        }
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
    if (-not $found) {
        $lines.Add($replacement)
    }
    Set-Content -LiteralPath $Path -Value $lines -Encoding utf8
}

function Get-DotEnvValues {
    param([string]$Path)
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $values
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $parts = $line.Split([char[]]"=", 2)
        $name = $parts[0].Trim()
        if ($name.StartsWith("export ", [System.StringComparison]::OrdinalIgnoreCase)) {
            $name = $name.Substring(7).Trim()
        }
        $value = $parts[1].Trim()
        if ($value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or
             ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$name] = $value
    }
    return $values
}

function Format-ControlUrl {
    param([string]$HostName, [int]$Port)
    $urlHost = if ($HostName.Contains(":") -and
        -not ($HostName.StartsWith("[") -and $HostName.EndsWith("]"))) {
        "[$HostName]"
    } else {
        $HostName
    }
    return "http://$urlHost`:$Port"
}

function Resolve-AdvertisedControlUrl {
    param([string]$ConfiguredUrl, [string]$HostName, [int]$Port)
    if ([string]::IsNullOrWhiteSpace($ConfiguredUrl)) {
        if ($HostName -in @("0.0.0.0", "::", "[::]")) {
            throw "The control bridge uses wildcard bind address '$HostName'. Supply -AdvertisedControlUrl with the URL the portal can reach, for example 'http://130.216.208.118:$Port'."
        }
        return Format-ControlUrl $HostName $Port
    }

    $uri = $null
    if (-not [uri]::TryCreate($ConfiguredUrl, [System.UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -notin @("http", "https") -or
        $uri.Host -in @("0.0.0.0", "::", "[::]") -or
        -not [string]::IsNullOrEmpty($uri.UserInfo) -or
        -not [string]::IsNullOrEmpty($uri.Query) -or
        -not [string]::IsNullOrEmpty($uri.Fragment)) {
        throw "AdvertisedControlUrl must be an absolute HTTP(S) URL without credentials, query, or fragment."
    }
    return $ConfiguredUrl.TrimEnd("/")
}

function Write-PortalConfig {
    param([string]$Url, [string]$Token)
    if ([string]::IsNullOrWhiteSpace($Token) -or $Token.Length -lt 32) {
        throw "No valid existing HERMES_CONTROL_TOKEN was found. Run the full setup once or pass -ControlToken with at least 32 characters."
    }
    Write-Host ""
    Write-Host "Copy these server-only values to apps/web-portal/.env.local:"
    Write-Output "HERMES_CONTROL_URL=$Url"
    Write-Output "HERMES_CONTROL_TOKEN=$Token"
    Write-Output "HERMES_CONTROL_TIMEOUT_MS=10000"
    $uri = [uri]$Url
    if ($uri.Scheme -eq "http" -and $uri.Host -notin @("localhost", "127.0.0.1", "::1", "[::1]")) {
        Write-Output "HERMES_CONTROL_ALLOW_INSECURE=true"
        Write-Warning "Remote plain HTTP exposes the bearer token in transit. Use it only on a trusted development network; prefer an HTTPS reverse proxy."
    }
}

if (-not (Test-Path -LiteralPath $HermesHome) -and $InstallHermes) {
    Write-Host "Installing Hermes from the official Nous Research Windows installer..."
    $installerSource = Invoke-RestMethod -Uri "https://hermes-agent.nousresearch.com/install.ps1"
    & ([scriptblock]::Create($installerSource))
}

$sourcePlugin = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$sourceControlBridge = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "control_bridge.py")).Path
$pluginsDir = Join-Path $HermesHome "plugins"
$targetPlugin = Join-Path $pluginsDir "hermes-livekit"
$controlDir = Join-Path $HermesHome "control"
$targetControlBridge = Join-Path $controlDir "control_bridge.py"
$envPath = Join-Path $HermesHome ".env"
$configPath = Join-Path $HermesHome "config.yaml"
$pythonExe = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe"
$pythonwExe = Join-Path $HermesHome "hermes-agent\venv\Scripts\pythonw.exe"
$hermesExe = Join-Path $HermesHome "hermes-agent\venv\Scripts\hermes.exe"

if (-not (Test-Path -LiteralPath $HermesHome)) {
    throw "Hermes is not installed at '$HermesHome'. Install Hermes first, then rerun this script."
}
$existingEnv = Get-DotEnvValues $envPath
if (-not $PSBoundParameters.ContainsKey("ControlHost") -and
    $existingEnv.ContainsKey("HERMES_CONTROL_HOST") -and
    -not [string]::IsNullOrWhiteSpace($existingEnv["HERMES_CONTROL_HOST"])) {
    $ControlHost = $existingEnv["HERMES_CONTROL_HOST"]
}
if (-not $PSBoundParameters.ContainsKey("ControlPort") -and $existingEnv.ContainsKey("HERMES_CONTROL_PORT")) {
    $existingControlPort = 0
    if ([int]::TryParse($existingEnv["HERMES_CONTROL_PORT"], [ref]$existingControlPort) -and
        $existingControlPort -ge 1 -and $existingControlPort -le 65535) {
        $ControlPort = $existingControlPort
    }
}
if ([string]::IsNullOrWhiteSpace($ControlToken) -and $existingEnv.ContainsKey("HERMES_CONTROL_TOKEN")) {
    $ControlToken = $existingEnv["HERMES_CONTROL_TOKEN"]
}

if ($ShowPortalConfig) {
    $portalControlUrl = Resolve-AdvertisedControlUrl $AdvertisedControlUrl $ControlHost $ControlPort
    Write-PortalConfig $portalControlUrl $ControlToken
    return
}
$portalControlUrl = Resolve-AdvertisedControlUrl $AdvertisedControlUrl $ControlHost $ControlPort

if (-not (Test-Path -LiteralPath $pythonExe) -or -not (Test-Path -LiteralPath $hermesExe)) {
    throw "Hermes Python/CLI was not found under '$HermesHome\hermes-agent\venv\Scripts'."
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
        $existingEnv.ContainsKey($environmentName) -and
        -not [string]::IsNullOrWhiteSpace($existingEnv[$environmentName])) {
        Set-Variable -Name $parameterName -Value $existingEnv[$environmentName]
    }
}

$liveKitSourcePath = $null
$liveKitSourceEnv = @{}
if ($PSBoundParameters.ContainsKey("LiveKitEnvFile")) {
    $resolvedLiveKitEnvFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($LiveKitEnvFile)
    if (-not (Test-Path -LiteralPath $resolvedLiveKitEnvFile -PathType Leaf)) {
        throw "LiveKitEnvFile was not found: $resolvedLiveKitEnvFile"
    }
    $liveKitEnvCandidates = @($resolvedLiveKitEnvFile)
} else {
    $repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
    $liveKitEnvCandidates = @(
        (Join-Path (Get-Location).Path ".env"),
        (Join-Path $repositoryRoot "apps\web-portal\.env"),
        (Join-Path $repositoryRoot "infrastructure\livekitserver-docker\.env")
    ) | Select-Object -Unique
}
foreach ($candidate in $liveKitEnvCandidates) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        continue
    }
    $candidateValues = Get-DotEnvValues $candidate
    $hasCompleteTransport = @("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET") |
        ForEach-Object { -not [string]::IsNullOrWhiteSpace($candidateValues[$_]) } |
        Where-Object { -not $_ } |
        Measure-Object
    if ($hasCompleteTransport.Count -eq 0) {
        $liveKitSourcePath = $candidate
        $liveKitSourceEnv = $candidateValues
        break
    }
}
if ($PSBoundParameters.ContainsKey("LiveKitEnvFile") -and -not $liveKitSourcePath) {
    throw "LiveKitEnvFile must contain non-empty LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET values."
}

$capturedLiveKitValues = $false
foreach ($parameterName in @("LiveKitUrl", "LiveKitApiKey", "LiveKitApiSecret")) {
    $environmentName = $existingValueMap[$parameterName]
    $currentValue = Get-Variable -Name $parameterName -ValueOnly
    if (-not $PSBoundParameters.ContainsKey($parameterName) -and
        [string]::IsNullOrWhiteSpace($currentValue) -and
        -not [string]::IsNullOrWhiteSpace($liveKitSourceEnv[$environmentName])) {
        Set-Variable -Name $parameterName -Value $liveKitSourceEnv[$environmentName]
        $capturedLiveKitValues = $true
    }
}
if ($capturedLiveKitValues) {
    Write-Host "Reusing LiveKit transport from '$liveKitSourcePath'."
}

$LiveKitUrl = Read-RequiredValue $LiveKitUrl "LiveKit URL (ws:// or wss://)"
$LiveKitApiKey = Read-RequiredValue $LiveKitApiKey "LiveKit API key"
$LiveKitApiSecret = Read-RequiredValue $LiveKitApiSecret "LiveKit API secret" -Secret
if ([string]::IsNullOrWhiteSpace($LiveKitUrl) -or
    [string]::IsNullOrWhiteSpace($LiveKitApiKey) -or
    [string]::IsNullOrWhiteSpace($LiveKitApiSecret)) {
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
    foreach ($runtimeFile in @("adapter.py", "__init__.py", "plugin.yaml", "LICENSE")) {
        $runtimeSource = Join-Path $sourcePlugin $runtimeFile
        if (-not (Test-Path -LiteralPath $runtimeSource)) {
            throw "Required plugin runtime file is missing: $runtimeSource"
        }
        Copy-Item -LiteralPath $runtimeSource -Destination (Join-Path $targetPlugin $runtimeFile) -Force
    }
}
New-Item -ItemType Directory -Path $controlDir -Force | Out-Null
Copy-Item -LiteralPath $sourceControlBridge -Destination $targetControlBridge -Force

Write-Host "Installing LiveKit, vision, and YAML dependencies in the Hermes environment..."
& $pythonExe -m pip install "livekit==1.1.10" "livekit-api==1.1.0" "pillow>=10" "pyyaml>=6"
if ($LASTEXITCODE -ne 0) {
    throw "Python dependency installation failed."
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    if ($InstallFfmpeg) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if (-not $winget) {
            throw "ffmpeg is missing and winget is unavailable. Install ffmpeg and add it to PATH."
        }
        & $winget.Source install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) {
            throw "ffmpeg installation failed."
        }
    } else {
        Write-Warning "ffmpeg is not on PATH. Install it, or rerun with -InstallFfmpeg, before using TTS."
    }
}

New-Item -ItemType Directory -Path $HermesHome -Force | Out-Null
Set-DotEnvValue $envPath "LIVEKIT_URL" $LiveKitUrl
Set-DotEnvValue $envPath "LIVEKIT_API_KEY" $LiveKitApiKey
Set-DotEnvValue $envPath "LIVEKIT_API_SECRET" $LiveKitApiSecret
Set-DotEnvValue $envPath "LIVEKIT_ROOM" $Room
Set-DotEnvValue $envPath "LIVEKIT_AGENT_NAME" $AgentName
Set-DotEnvValue $envPath "LIVEKIT_ALLOW_ALL_USERS" "true"
Set-DotEnvValue $envPath "HERMES_LIVEKIT_AUTO_VISION" "true"
Set-DotEnvValue $envPath "HERMES_LIVEKIT_VIDEO_SAMPLE_SECONDS" "1.0"
Set-DotEnvValue $envPath "HERMES_LIVEKIT_VIDEO_MAX_AGE_SECONDS" "10"
Set-DotEnvValue $envPath "HERMES_LIVEKIT_SILENCE_SECONDS" "0.8"
Set-DotEnvValue $envPath "HERMES_LIVEKIT_WORK_ACK_SECONDS" "6"
Set-DotEnvValue $envPath "HERMES_LIVEKIT_WORK_ACK_MODE" "status"
Set-DotEnvValue $envPath "HERMES_LIVEKIT_WORK_ACK_TEXT" "Let me check that."
Set-DotEnvValue $envPath "HERMES_AGENT_NOTIFY_INTERVAL" "20"
Set-DotEnvValue $envPath "HERMES_CONTROL_HOST" $ControlHost
Set-DotEnvValue $envPath "HERMES_CONTROL_PORT" "$ControlPort"
if ([string]::IsNullOrWhiteSpace($ControlToken)) {
    $tokenBytes = [byte[]]::new(32)
    $randomNumberGenerator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $randomNumberGenerator.GetBytes($tokenBytes)
    } finally {
        $randomNumberGenerator.Dispose()
    }
    $ControlToken = [Convert]::ToBase64String($tokenBytes)
}
if ($ControlToken.Length -lt 32) {
    throw "ControlToken must contain at least 32 characters."
}
Set-DotEnvValue $envPath "HERMES_CONTROL_TOKEN" $ControlToken

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
    & $hermesExe config set voice.auto_tts true
    & $hermesExe config set display.busy_input_mode interrupt
    & $hermesExe config set display.platforms.livekit.streaming false
    & $hermesExe config set display.platforms.livekit.long_running_notifications true
    & $hermesExe config set display.platforms.livekit.busy_ack_detail false

    & $pythonExe -c "import livekit.rtc, livekit.api, PIL, yaml; print('Dependency check: OK')"
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency import check failed."
    }
    & $hermesExe config check

    if ($RestartGateway) {
        & $hermesExe gateway stop
        Start-Process -FilePath $hermesExe -ArgumentList @("gateway", "run") -WindowStyle Hidden
        Start-Sleep -Seconds 2
        & $hermesExe gateway status
    }

    if ($InstallAutoStart) {
        & $hermesExe gateway install
        if ($LASTEXITCODE -ne 0) {
            throw "Hermes gateway auto-start installation failed."
        }
        $taskName = "MiRA Hermes Control Bridge"
        $taskArguments = "`"$targetControlBridge`" --hermes-home `"$HermesHome`" --host $ControlHost --port $ControlPort"
        $bridgePython = if (Test-Path -LiteralPath $pythonwExe) { $pythonwExe } else { $pythonExe }
        $taskAction = New-ScheduledTaskAction -Execute $bridgePython -Argument $taskArguments
        $taskTrigger = New-ScheduledTaskTrigger -AtLogOn
        Register-ScheduledTask `
            -TaskName $taskName `
            -Action $taskAction `
            -Trigger $taskTrigger `
            -Description "Authenticated MiRA portal control bridge for Hermes" `
            -Force | Out-Null
        Write-Host "Installed the '$taskName' logon task."
    }

    if ($StartControlBridge -or $InstallAutoStart) {
        $healthHost = if ($ControlHost -in @("0.0.0.0", "::", "[::]")) {
            "127.0.0.1"
        } else {
            $ControlHost
        }
        $healthUri = "http://$healthHost`:$ControlPort/health"
        $alreadyRunning = $false
        try {
            $health = Invoke-RestMethod -Uri $healthUri -TimeoutSec 2
            $alreadyRunning = $health.ok -eq $true
        } catch {
            $alreadyRunning = $false
        }
        if (-not $alreadyRunning) {
            $bridgePython = if (Test-Path -LiteralPath $pythonwExe) { $pythonwExe } else { $pythonExe }
            Start-Process `
                -FilePath $bridgePython `
                -ArgumentList @(
                    $targetControlBridge,
                    "--hermes-home", $HermesHome,
                    "--host", $ControlHost,
                    "--port", "$ControlPort"
                ) `
                -WorkingDirectory $controlDir `
                -WindowStyle Hidden
            Start-Sleep -Seconds 1
            $health = Invoke-RestMethod -Uri $healthUri -TimeoutSec 5
            if ($health.ok -ne $true) {
                throw "Hermes control bridge did not become healthy."
            }
        }
    }
} finally {
    $env:HERMES_HOME = $previousHermesHome
}

Write-Host ""
Write-Host "Hermes LiveKit setup is complete."
Write-Host "Plugin: $targetPlugin"
Write-Host "Room:   $Room"
Write-Host "Control bridge bind address: $(Format-ControlUrl $ControlHost $ControlPort)"
Write-PortalConfig $portalControlUrl $ControlToken
Write-Warning "Store the control token only in the portal's server-side .env.local file. Do not expose it as NEXT_PUBLIC_*."
if (-not $RestartGateway) {
    Write-Host "Restart the Hermes gateway to load the new plugin and configuration."
}
