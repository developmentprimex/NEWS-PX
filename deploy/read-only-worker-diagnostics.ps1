param([string]$NewsRoot = 'E:\websites\news-intelligence')
$ErrorActionPreference = 'Stop'
# No commands below stop/start services, containers, workflows or scrapers.
# Do not return full command lines: those can contain unknown credentials.
$result = [ordered]@{ capturedAt = (Get-Date).ToUniversalTime().ToString('o'); productionModified = $false; providerRequests = 0 }
$result.processes = @(Get-CimInstance Win32_Process | ForEach-Object {
    $module = ''; $port = ''; $script = ''
    if ($_.CommandLine -match '-m\s+([\w.]+)') { $module = $Matches[1] }
    if ($_.CommandLine -match '--port\s+(\d+)') { $port = $Matches[1] }
    # Only a quoted absolute Windows script path, never arbitrary quoted arguments.
    if ($_.CommandLine -match '(?:^|\s)"([A-Za-z]:\\[^"\r\n]*\.(?:py|ps1|cmd|bat|php))"(?:\s|$)') {
        $scriptCandidate = $Matches[1]
        if ($scriptCandidate -notmatch '\s--?[^\\\s]|(?i)(password|token|secret|api[_-]?key)\s*[:=]|://') { $script = $scriptCandidate }
    }
    [ordered]@{ pid=$_.ProcessId; parentPid=$_.ParentProcessId; name=$_.Name; executable=$_.ExecutablePath;
        runtimeMarker=($_.Name -match 'python|uvicorn|node|php|docker|cloudflare|wsl|java|ruby|perl|powershell|cmd');
        module=$module; port=$port; script=$script; newsPath=($_.CommandLine -like '*news-intelligence*');
        providerMarker=($_.CommandLine -match 'brightdata|linkedin|check_due_sources');
        laravelMarker=($_.CommandLine -match 'artisan|schedule:run|schedule:work|queue:work');
        n8nMarker=($_.CommandLine -match 'n8n') }
})
$result.services = @(Get-CimInstance Win32_Service | ForEach-Object {
    [ordered]@{ name=$_.Name; state=$_.State; pid=$_.ProcessId;
        newsPath=($_.PathName -like '*news-intelligence*'); providerMarker=($_.PathName -match 'brightdata|linkedin');
        runtimeMarker=($_.PathName -match 'python|node|php|docker|n8n|cloudflare|runner') }
})
$result.tasks = @(Get-ScheduledTask | ForEach-Object {
    [ordered]@{ name=$_.TaskName; path=$_.TaskPath; state=[string]$_.State;
        multipleInstances=[string]$_.Settings.MultipleInstances; actions=@($_.Actions | ForEach-Object {
            [ordered]@{ executable=$_.Execute; workingDirectory=$_.WorkingDirectory;
                newsPath=($_.Arguments -like '*news-intelligence*'); providerMarker=($_.Arguments -match 'brightdata|linkedin|check_due_sources');
                laravelMarker=($_.Arguments -match 'artisan|schedule:run|queue:work'); n8nMarker=($_.Arguments -match 'n8n') }
        }) }
})
$result.listeners = @(Get-NetTCPConnection -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess)
if (Get-Command docker.exe -ErrorAction SilentlyContinue) {
    try {
        # Full-row JSON also includes labels: never export it or container secrets.
        $result.containers = @(docker.exe ps -a --format '{"id":{{json .ID}},"name":{{json .Names}},"image":{{json .Image}},"status":{{json .Status}}}' 2>$null)
        $result.dockerExitCode = $LASTEXITCODE
    } catch { $result.docker = 'Inventory unavailable; administrator must inspect Docker separately' }
} else { $result.docker = 'not installed on PATH' }
if (Get-Command wsl.exe -ErrorAction SilentlyContinue) {
    try {
        $result.wslDistributions = @(wsl.exe --list --quiet 2>$null)
        $result.wslExitCode = $LASTEXITCODE
    } catch { $result.wsl = 'Inventory unavailable; administrator must inspect WSL separately' }
}
$result.notes = @('No full command lines, process/container environment variables or container labels are exported.',
    'Keep full inventory private. Review metadata fields before returning only the approved projection; names/paths can themselves be sensitive.',
    'Review identified scripts privately. n8n and Linux/container schedules require their own read-only inventory.',
    'A process without a provider marker may still call Bright Data; compare executable/script ownership and account activity logs.')
$result | ConvertTo-Json -Depth 8
