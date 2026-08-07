<#
.SYNOPSIS
    Brings the agent-tracking stack up and keeps it up, without a human in the
    loop - and registers itself as the Windows logon task that does so.

.DESCRIPTION
    The stack is infrastructure, not an accessory of a coding session: three
    different clients (Claude Code desktop app, Claude Code CLI, Codex CLI)
    route their LLM traffic through LiteLLM on `:4000`, and only one of them
    has a hook system. So liveness is owned here, at the OS layer, rather than
    lazily by whichever client happens to start first.

    Docker Desktop's own `AutoStart` plus each service's `restart: always`
    already cover a plain reboot. What they do *not* cover is `make down`,
    which removes the containers outright - a restart policy has nothing left
    to restart. That gap, plus the 30-60s window where Docker Desktop is still
    coming up and `:4000` is not listening yet, is what this script closes.

    The stack is started through `make start`, run under Git Bash - not
    through `docker compose` directly. That is not a stylistic choice:
    `docker-compose.yml` interpolates image tags that only the Makefile
    produces (`scripts/resolve_image_version.py` -> `.image-tags.mk`), and it
    fails loudly and deliberately when they are absent ("run via `make`, not
    `docker compose` directly"). Git Bash specifically, because `make` invoked
    from PowerShell has no POSIX tools on PATH and silently loses
    `$(shell cat .python-version)`.

    The entry point is PowerShell anyway, because the Scheduled Task needs to
    wait for the Docker daemon and for the proxy port before and after that
    `make`, and `pwsh` is the one interpreter guaranteed present at logon.

.PARAMETER Install
    Registers (or refreshes) the logon Scheduled Task, pointing it at this
    script's current location. Re-run after moving the repo - the task stores
    an absolute path and this is what updates it. Also reports (without
    touching) whether the Claude Code SessionStart hook in the user's global
    settings still points at this same copy, since it stores a path too.

.PARAMETER Uninstall
    Removes the Scheduled Task. Does not touch running containers.

.PARAMETER Probe
    Fast path for a client-side hook: checks whether LiteLLM answers its
    liveliness endpoint, and if it doesn't, kicks off a detached full run and
    says so. Never blocks, always exits 0 - it is a safety net reporting a
    fact, not a gate.

.EXAMPLE
    pwsh -File scripts\ensure-stack.ps1 -Install

.EXAMPLE
    pwsh -File scripts\ensure-stack.ps1
#>
[CmdletBinding(DefaultParameterSetName = 'Ensure')]
param(
    [Parameter(ParameterSetName = 'Install')][switch]$Install,
    [Parameter(ParameterSetName = 'Uninstall')][switch]$Uninstall,
    [Parameter(ParameterSetName = 'Probe')][switch]$Probe,

    # Docker Desktop is slow to come up at logon and how slow depends on the
    # machine's mood, so this polls the daemon instead of guessing a sleep.
    [int]$DaemonTimeoutSeconds = 300,
    [int]$ProxyTimeoutSeconds = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$TaskName = 'receipt-goblin-ensure-stack'
$LogDir = Join-Path $env:LOCALAPPDATA 'receipt-goblin'
$LogFile = Join-Path $LogDir 'ensure-stack.log'
$MaxLogBytes = 1MB

function Write-Log {
    param([string]$Message, [ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level = 'INFO')

    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    # A logon task nobody watches will happily fill a disk over a year.
    if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt $MaxLogBytes) {
        Get-Content $LogFile -Tail 200 | Set-Content $LogFile
    }
    Add-Content -Path $LogFile -Value $line
    switch ($Level) {
        'ERROR' { Write-Host $line -ForegroundColor Red }
        'WARN' { Write-Host $line -ForegroundColor Yellow }
        default { Write-Host $line }
    }
}

function Read-DotEnv {
    param([string]$Path)

    $values = @{}
    if (-not (Test-Path $Path)) { return $values }
    foreach ($line in Get-Content $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        $split = $trimmed.IndexOf('=')
        if ($split -lt 1) { continue }
        $key = $trimmed.Substring(0, $split).Trim()
        $value = $trimmed.Substring($split + 1).Trim().Trim('"', "'")
        $values[$key] = $value
    }
    return $values
}

function Get-StackConfig {
    $dotEnvPath = Join-Path $RepoRoot '.env'
    if (-not (Test-Path $dotEnvPath)) {
        throw "No .env at $dotEnvPath - the stack has never been configured here (see 'make init')."
    }
    $dotEnv = Read-DotEnv $dotEnvPath

    $environment = if ($env:ENVIRONMENT) { $env:ENVIRONMENT }
    elseif ($dotEnv.ContainsKey('ENVIRONMENT') -and $dotEnv['ENVIRONMENT']) { $dotEnv['ENVIRONMENT'] }
    else { 'development' }

    # Compose file selection is deliberately NOT mirrored here - `make` owns it,
    # and duplicating it is how the two drift apart.
    $port = if ($dotEnv.ContainsKey('LITELLM_PORT') -and $dotEnv['LITELLM_PORT']) { [int]$dotEnv['LITELLM_PORT'] } else { 4000 }

    return [pscustomobject]@{
        Environment = $environment
        ProxyPort   = $port
    }
}

function Get-GitBashPath {
    # Never resolve this with `Get-Command bash`: on a machine with WSL that
    # finds System32\bash.exe, which would run `make` inside a Linux distro
    # against a /mnt/c path - a different Docker context and a different repo.
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Git\bin\bash.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Git\bin\bash.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Git\bin\bash.exe')
    )
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        $candidates += (Join-Path (Split-Path -Parent (Split-Path -Parent $git.Source)) 'bin\bash.exe')
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    throw 'Git Bash not found - `make` cannot be run without it.'
}

function Test-ProxyHealthy {
    param([int]$Port, [int]$TimeoutMs = 2000)

    # Deliberately an HTTP request, not a TCP connect. The proxy port is
    # published by the `load-balancer` (nginx) container, not by `litellm`
    # itself - so a TCP connect succeeds whenever nginx is up, including while
    # litellm is stopped and every agent call is failing with a 502. Asking
    # litellm's own liveliness endpoint is what actually distinguishes the two.
    $client = [System.Net.Http.HttpClient]::new()
    try {
        $client.Timeout = [TimeSpan]::FromMilliseconds($TimeoutMs)
        $response = $client.GetAsync("http://127.0.0.1:$Port/health/liveliness").GetAwaiter().GetResult()
        return $response.IsSuccessStatusCode
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Wait-DockerDaemon {
    param([int]$TimeoutSeconds)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $announced = $false
    while ((Get-Date) -lt $deadline) {
        docker info 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { return $true }
        if (-not $announced) {
            Write-Log "Docker daemon not ready yet, waiting up to ${TimeoutSeconds}s."
            $announced = $true
        }
        Start-Sleep -Seconds 3
    }
    return $false
}

function Wait-Proxy {
    param([int]$Port, [int]$TimeoutSeconds)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-ProxyHealthy -Port $Port) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Get-PwshPath {
    $command = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $fallback = Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'
    if (Test-Path $fallback) { return $fallback }
    throw 'pwsh not found - cannot register a task that needs it.'
}

function Test-ClaudeHookPath {
    # The Claude Code safety-net hook lives in the user's *global* settings and
    # stores an absolute path to this script, exactly like the task does. This
    # deliberately only reports - silently rewriting someone's global settings
    # from a repo script is a worse surprise than a stale path. Re-running
    # -Install is when you'd want to hear about it, so that is when it warns.
    $settingsPath = Join-Path $env:USERPROFILE '.claude\settings.json'
    if (-not (Test-Path $settingsPath)) {
        Write-Log "No $settingsPath - Claude Code safety-net hook is not installed." 'WARN'
        return
    }

    $commands = @()
    try {
        $settings = Get-Content $settingsPath -Raw | ConvertFrom-Json
        $sessionStart = $settings.PSObject.Properties['hooks'].Value.PSObject.Properties['SessionStart'].Value
        foreach ($group in $sessionStart) {
            foreach ($hook in $group.hooks) {
                if ($hook.command -like '*ensure-stack.ps1*') { $commands += $hook.command }
            }
        }
    } catch {
        Write-Log "Could not read SessionStart hooks out of ${settingsPath}: $($_.Exception.Message)" 'WARN'
        return
    }

    if (-not $commands) {
        Write-Log "Claude Code safety-net hook not found in $settingsPath - only the logon task is active." 'WARN'
        return
    }
    $stale = @($commands | Where-Object { $_ -notlike "*$PSCommandPath*" })
    if ($stale) {
        Write-Log "Claude Code SessionStart hook in $settingsPath points at a different copy of this script - update it to '$PSCommandPath'." 'WARN'
        foreach ($command in $stale) { Write-Log "  stale: $command" 'WARN' }
        return
    }
    Write-Log 'Claude Code safety-net hook is present and points here.'
}

function Invoke-Install {
    $scriptPath = $PSCommandPath
    $pwshPath = Get-PwshPath

    $action = New-ScheduledTaskAction -Execute $pwshPath `
        -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $scriptPath) `
        -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User ('{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME)
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    $principal = New-ScheduledTaskPrincipal `
        -UserId ('{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME) `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force `
        -Description "Brings the receipt-goblin agent-tracking stack up at logon (repo: $RepoRoot)." | Out-Null

    Write-Log "Registered logon task '$TaskName' -> $scriptPath (repo: $RepoRoot)."
    Write-Log "Log file: $LogFile"
    Test-ClaudeHookPath
}

function Invoke-Uninstall {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Log "No task named '$TaskName' registered - nothing to remove."
        return
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Log "Removed logon task '$TaskName'. Running containers were left alone."
}

function Invoke-Probe {
    # This runs from the user's *global* Claude Code settings, so it fires in
    # every project on the machine. An unhandled throw here would put a
    # PowerShell stack trace at the top of every session everywhere - and the
    # most likely trigger is the mundane one: the repo got moved and the
    # hook's stored path now points at nothing. Report it in one line and get
    # out of the way instead.
    try {
        $config = Get-StackConfig
        if (Test-ProxyHealthy -Port $config.ProxyPort) { exit 0 }

        # Deliberately fire-and-forget: a full cold start outlives any sane hook
        # timeout, and blocking the session start buys nothing the caller can use.
        $pwshPath = Get-PwshPath
        Start-Process -FilePath $pwshPath `
            -ArgumentList @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', $PSCommandPath) `
            -WindowStyle Hidden | Out-Null

        Write-Output "receipt-goblin: LiteLLM proxy on 127.0.0.1:$($config.ProxyPort) is not answering its liveliness check - agent traffic is NOT being tracked, and calls routed through the proxy will fail until it is back. A background 'make start' was started; give it a minute. Log: $LogFile"
    } catch {
        Write-Output "receipt-goblin: stack probe could not run ($($_.Exception.Message)). Fix or remove the SessionStart hook in ~/.claude/settings.json that points at $PSCommandPath."
    }
    exit 0
}

function Invoke-Ensure {
    $config = Get-StackConfig
    Write-Log "Ensuring stack is up (repo: $RepoRoot, ENVIRONMENT=$($config.Environment))."

    if (Test-ProxyHealthy -Port $config.ProxyPort) {
        Write-Log "Proxy healthy on 127.0.0.1:$($config.ProxyPort) - nothing to do."
        return 0
    }

    if (-not (Wait-DockerDaemon -TimeoutSeconds $DaemonTimeoutSeconds)) {
        Write-Log "Docker daemon never became ready within ${DaemonTimeoutSeconds}s. Giving up." 'ERROR'
        return 1
    }

    $bash = Get-GitBashPath
    Push-Location $RepoRoot
    try {
        # cwd is inherited by bash, so the repo path never has to survive a
        # Windows->POSIX translation.
        Write-Log "Running: make start (via $bash)"
        $output = & $bash -c 'make start 2>&1'
        $makeExit = $LASTEXITCODE
        foreach ($line in $output) { Write-Log "  make: $line" }
        if ($makeExit -ne 0) {
            Write-Log "make start exited $makeExit." 'ERROR'
            return 1
        }
    } finally {
        Pop-Location
    }

    if (-not (Wait-Proxy -Port $config.ProxyPort -TimeoutSeconds $ProxyTimeoutSeconds)) {
        # Containers are started at this point; only the proxy is still not
        # answering, which is a health problem rather than a start-up problem.
        Write-Log "Containers started, but the proxy on 127.0.0.1:$($config.ProxyPort) still fails its liveliness check after ${ProxyTimeoutSeconds}s. Check 'make status'." 'ERROR'
        return 1
    }

    Write-Log "Stack up, proxy healthy on 127.0.0.1:$($config.ProxyPort)."
    return 0
}

switch ($PSCmdlet.ParameterSetName) {
    'Install' { Invoke-Install; exit 0 }
    'Uninstall' { Invoke-Uninstall; exit 0 }
    'Probe' { Invoke-Probe }
    default { exit (Invoke-Ensure) }
}
