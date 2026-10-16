param([string]$ReleasePermit)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'release-control.ps1')
if (-not $ReleasePermit) { throw 'Manual release requires an explicitly approved private release permit.' }
$permitPath = [IO.Path]::GetFullPath($ReleasePermit)
if (-not $permitPath.StartsWith('E:\private\primex-release-approvals\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Release permit must be outside the application and checkout.' }
$parent = Get-Item -LiteralPath $permitPath
while ($parent -and $parent.FullName -ne 'E:\') {
    if ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Release permit path cannot contain reparse points.' }
    $parent = if ($parent.PSIsContainer) { $parent.Parent } else { $parent.Directory }
}
$permit = Get-Content -LiteralPath $permitPath -Raw | ConvertFrom-Json
Assert-NewsReleasePermit $permit $env:GITHUB_SHA
if (-not $env:GITHUB_WORKSPACE) { throw 'Explicit reviewed checkout is required.' }
$checkoutSha = & git -C $env:GITHUB_WORKSPACE rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $checkoutSha -cne $env:GITHUB_SHA) { throw 'Checkout does not match release approval.' }
$dirty = & git -C $env:GITHUB_WORKSPACE status --porcelain --untracked-files=all
if ($LASTEXITCODE -ne 0 -or $dirty) { throw 'Release checkout must be clean, including untracked files.' }

$root = 'E:\websites\news-intelligence'
$workspace = $env:GITHUB_WORKSPACE
$python = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe'
$npm = 'C:\Program Files\nodejs\npm.cmd'
$node = 'C:\Program Files\nodejs\node.exe'
$started = Get-Date
$tasks = @('NewsIntelligenceAPI', 'NewsIntelligenceWeb')
Assert-NewsPausedConfiguration (Join-Path $root '.env') (Get-NewsInheritedCollectionSettings)
if ((Get-ScheduledTask -TaskName 'NewsIntelligenceSync').State -ne 'Disabled') { throw 'Legacy sync task must be held by the approved administrator transition.' }
foreach ($task in $tasks) {
    if ((Get-ScheduledTask -TaskName $task).State -eq 'Disabled') { throw 'API/Web tasks must be startable with reviewed paused launchers before release.' }
}
# Durable one-use permit consumption precedes all release mutations.
$permitUse = [IO.File]::Open(($permitPath + '.consumed'), [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
$permitUse.Dispose()

if (-not $workspace -or -not (Test-Path -LiteralPath (Join-Path $workspace 'frontend\package-lock.json'))) {
    throw 'GITHUB_WORKSPACE does not contain the News application.'
}
foreach ($path in @($root, (Join-Path $root '.env'), $python, $npm)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required deployment path is missing: $path" }
}

$deployRoot = Join-Path $root '_deploy'
$stageRoot = Join-Path $deployRoot 'stage'
$backupRoot = Join-Path $deployRoot 'backups'
$cacheRoot = Join-Path $deployRoot 'node-cache'
$revision = if ($env:GITHUB_SHA) { $env:GITHUB_SHA.Substring(0, [Math]::Min(12, $env:GITHUB_SHA.Length)) } else { 'manual' }
$id = "$(Get-Date -Format yyyyMMddHHmmss)-$revision"
$stage = Join-Path $stageRoot $id
$backup = Join-Path $backupRoot $id

function Assert-NewsPath([string]$path) {
    $full = [IO.Path]::GetFullPath($path)
    $prefix = [IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside the News deployment: $full"
    }
}

function Move-NewsDirectory([string]$source, [string]$destination) {
    Assert-NewsPath $source
    Assert-NewsPath $destination
    [IO.Directory]::Move($source, $destination)
}

function Invoke-Checked([string]$executable, [string[]]$arguments, [string]$directory) {
    Push-Location -LiteralPath $directory
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $executable @arguments
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
        Pop-Location
    }
    if ($code -ne 0) { throw "$executable exited with code $code" }
}

function Stop-News {
    foreach ($task in $tasks) { Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    # Windows task stop may leave child processes. Never kill by a port/name
    # heuristic: the administrator must retire the freshly mapped News tree.
    foreach ($port in @(8001, 3101)) {
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
            throw "News port $port remains occupied. Revalidate process ownership; no process was force-stopped."
        }
    }
}

function Start-News {
    # Also applies to rollback: old code must never regain a provider key.
    Assert-NewsPausedConfiguration (Join-Path $root '.env') (Get-NewsInheritedCollectionSettings)
    foreach ($port in @(8001, 3101)) {
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
            throw "Refusing duplicate startup while News port $port is occupied. Administrator ownership review is required."
        }
    }
    foreach ($task in $tasks) { Start-ScheduledTask -TaskName $task }
}

function Wait-News {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $api = Invoke-WebRequest 'http://127.0.0.1:8001/api/health' -UseBasicParsing -TimeoutSec 3
            $web = Invoke-WebRequest 'http://127.0.0.1:3101/login' -UseBasicParsing -TimeoutSec 3
            if ($api.StatusCode -eq 200 -and $web.StatusCode -eq 200) { return }
        } catch { }
        Start-Sleep -Seconds 3
    }
    throw 'News did not pass the API and web health checks after deployment.'
}


function Get-DependencyKey([string]$directory, [string]$runtime) {
    $parts = @($runtime)
    foreach ($name in @('package.json', 'package-lock.json', '.npmrc')) {
        $path = Join-Path $directory $name
        $parts += if (Test-Path -LiteralPath $path) { (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash } else { 'missing' }
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(($parts -join '|'))))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Restore-NewsDirectories([string[]]$names, [string]$failed) {
    # A switch can fail between two directory moves. Restore only directories
    # actually backed up; never move an untouched live directory out of service.
    foreach ($name in $names) {
        $live = Join-Path $root $name
        $old = Join-Path $backup $name
        if (Test-Path -LiteralPath $old) {
            if (Test-Path -LiteralPath $live) { Move-NewsDirectory $live (Join-Path $failed $name) }
            Move-NewsDirectory $old $live
        }
    }
}

function Queue-NewsCleanup([string[]]$paths) {
    if (-not $paths.Count) { return }
    $queue = Join-Path $deployRoot 'cleanup-queue'
    New-Item -ItemType Directory -Path $queue -Force | Out-Null
    foreach ($path in $paths) { Assert-NewsPath $path }
    $request = Join-Path $queue (([guid]::NewGuid().ToString('N')) + '.json')
    $temporary = $request + '.tmp'
    ConvertTo-Json -InputObject @($paths) | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $request
    $worker = Join-Path $deployRoot 'cleanup.ps1'
    Copy-Item -LiteralPath (Join-Path $workspace 'deploy\cleanup.ps1') -Destination $worker -Force
    Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -WindowStyle Hidden -ArgumentList @('-NoProfile', '-File', ('"' + $worker + '"'), '-Root', ('"' + $root + '"')) | Out-Null
}

New-Item -ItemType Directory -Path $stageRoot, $backupRoot, $cacheRoot -Force | Out-Null
Assert-NewsPath $stage
Assert-NewsPath $backup
if (Test-Path -LiteralPath $stage) { throw "Deployment stage already exists: $stage" }
New-Item -ItemType Directory -Path $stage | Out-Null

$switched = $false
$stopping = $false
$healthy = $false
$replace = @('backend', 'frontend')
try {
    Copy-Item -LiteralPath (Join-Path $workspace 'backend') -Destination $stage -Recurse
    Copy-Item -LiteralPath (Join-Path $workspace 'frontend') -Destination $stage -Recurse

    $requirements = Join-Path $stage 'backend\requirements.txt'
    $pythonRuntime = & $python -c "import platform,sys; print(platform.python_version()+'|'+sys.base_prefix)"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot identify Python runtime.' }
    $pythonKey = "$pythonRuntime|$((Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash)"
    $livePython = Join-Path $root '.venv\Scripts\python.exe'
    $pythonStamp = Join-Path $root '.venv\.news-requirements-key'
    $reusePython = $false
    if (Test-Path -LiteralPath $livePython) {
        if (Test-Path -LiteralPath $pythonStamp) {
            $reusePython = (Get-Content -LiteralPath $pythonStamp -Raw).Trim() -eq $pythonKey
        } else {
            # Import the working environment from deployments made before caching.
            $liveRequirements = Join-Path $root 'backend\requirements.txt'
            $liveRuntime = & $livePython -c "import platform,sys; print(platform.python_version()+'|'+sys.base_prefix)"
            $reusePython = $LASTEXITCODE -eq 0 -and $liveRuntime -eq $pythonRuntime -and
                (Test-Path -LiteralPath $liveRequirements) -and
                (Get-FileHash -LiteralPath $liveRequirements).Hash -eq (Get-FileHash -LiteralPath $requirements).Hash
        }
    }
    if ($reusePython) {
        Write-Output 'Reusing Python environment and installed Chromium (requirements/runtime unchanged).'
        Invoke-Checked $livePython @('-m', 'pip', 'check') $stage
    } else {
        Write-Output 'Python dependencies changed; preparing a new isolated environment.'
        Invoke-Checked $python @('-m', 'venv', (Join-Path $stage '.venv')) $stage
        Invoke-Checked (Join-Path $stage '.venv\Scripts\python.exe') @(
            '-m', 'pip', 'install', '--disable-pip-version-check', '-r', $requirements
        ) $stage
        $env:PLAYWRIGHT_BROWSERS_PATH = '0'
        Invoke-Checked (Join-Path $stage '.venv\Scripts\python.exe') @('-m', 'playwright', 'install', 'chromium') $stage
        Set-Content -LiteralPath (Join-Path $stage '.venv\.news-requirements-key') -Value $pythonKey -NoNewline
        $replace += '.venv'
    }

    $nodeVersion = & $node --version
    if ($LASTEXITCODE -ne 0) { throw 'Cannot identify Node runtime.' }
    $npmVersion = & $npm --version
    if ($LASTEXITCODE -ne 0) { throw 'Cannot identify npm runtime.' }
    $nodeRuntime = "$nodeVersion|$npmVersion|$env:PROCESSOR_ARCHITECTURE"
    $frontend = Join-Path $stage 'frontend'
    $nodeKey = Get-DependencyKey $frontend $nodeRuntime
    $nodeCache = Join-Path $cacheRoot $nodeKey
    if (-not (Test-Path -LiteralPath (Join-Path $nodeCache '.ready'))) {
        $pendingCache = Join-Path $stage 'node-dependencies'
        New-Item -ItemType Directory -Path $pendingCache | Out-Null
        foreach ($name in @('package.json', 'package-lock.json', '.npmrc')) {
            $path = Join-Path $frontend $name
            if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination $pendingCache }
        }
        $liveFrontend = Join-Path $root 'frontend'
        $liveNodeStamp = Join-Path $liveFrontend '.news-node-key'
        $canImport = (Test-Path -LiteralPath (Join-Path $liveFrontend 'node_modules\next\package.json')) -and
            (Get-DependencyKey $liveFrontend $nodeRuntime) -eq $nodeKey -and
            (-not (Test-Path -LiteralPath $liveNodeStamp) -or (Get-Content -LiteralPath $liveNodeStamp -Raw).Trim() -eq $nodeKey)
        if ($canImport) {
            Write-Output 'Seeding dependency cache from the working frontend (one-time copy).'
            & robocopy.exe (Join-Path $liveFrontend 'node_modules') (Join-Path $pendingCache 'node_modules') /E /MT:16 /R:1 /W:1 /XJ /NFL /NDL /NJH /NJS /NP
            if ($LASTEXITCODE -ge 8) { throw "Dependency cache copy failed: $LASTEXITCODE" }
        } else {
            Write-Output 'Frontend dependencies/runtime changed; running npm ci for a new cache entry.'
            Invoke-Checked $npm @('ci', '--no-audit', '--no-fund') $pendingCache
        }
        if (-not (Test-Path -LiteralPath (Join-Path $pendingCache 'node_modules\next\dist\bin\next'))) {
            throw 'Dependency cache is missing the Next.js executable.'
        }
        Set-Content -LiteralPath (Join-Path $pendingCache '.ready') -Value $nodeKey -NoNewline
        Move-NewsDirectory $pendingCache $nodeCache
    } else { Write-Output 'Reusing frontend dependency cache; npm ci skipped.' }
    New-Item -ItemType Junction -Path (Join-Path $frontend 'node_modules') -Target (Join-Path $nodeCache 'node_modules') | Out-Null
    Set-Content -LiteralPath (Join-Path $frontend '.news-node-key') -Value $nodeKey -NoNewline
    $env:NEWS_API_INTERNAL_URL = 'http://127.0.0.1:8001'
    # The dependency junction must fall inside Turbopack's filesystem root.
    $env:NEWS_BUILD_ROOT = $root
    Invoke-Checked $npm @('run', 'build') $frontend
    if (-not (Test-Path -LiteralPath (Join-Path $frontend '.next\BUILD_ID'))) { throw 'Frontend build is incomplete.' }

    $stopping = $true
    Stop-News
    $switched = $true
    New-Item -ItemType Directory -Path $backup | Out-Null
    foreach ($name in $replace) {
        $live = Join-Path $root $name
        $old = Join-Path $backup $name
        $new = Join-Path $stage $name
        Move-NewsDirectory $live $old
        Move-NewsDirectory $new $live
    }

    Start-News
    Wait-News
    if ($reusePython) { Set-Content -LiteralPath $pythonStamp -Value $pythonKey -NoNewline }
    Set-Content -LiteralPath (Join-Path $deployRoot 'deployed-sha.txt') -Value $env:GITHUB_SHA -NoNewline
    $healthy = $true
    Write-Output "Deployed $($env:GITHUB_SHA) to $root in $([math]::Round(((Get-Date) - $started).TotalSeconds)) seconds (health checks passed)."
} catch {
    $failure = $_
    if ($switched) {
        Stop-News
        $failed = Join-Path $stage 'failed'
        Assert-NewsPath $failed
        New-Item -ItemType Directory -Path $failed -Force | Out-Null
        Restore-NewsDirectories $replace $failed
        Start-News
        Wait-News
    } elseif ($stopping) {
        Start-News
        Wait-News
    }
    throw $failure
} finally {
    # Housekeeping must not delay the next deploy or roll back a healthy release.
    try {
        $cleanup = @($stage)
        if ($healthy) {
            $cleanup += @(Get-ChildItem -LiteralPath $backupRoot -Directory | Sort-Object CreationTimeUtc -Descending | Select-Object -Skip 2 | ForEach-Object { $_.FullName })
        }
        Queue-NewsCleanup $cleanup
    } catch { Write-Warning "Cleanup was deferred: $($_.Exception.Message)" }
}
