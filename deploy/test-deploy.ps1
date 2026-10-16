$ErrorActionPreference = 'Stop'
$testRoot = Join-Path $PSScriptRoot ('.test-' + [guid]::NewGuid().ToString('N'))
$resolved = [IO.Path]::GetFullPath($testRoot)
if (-not $resolved.StartsWith([IO.Path]::GetFullPath($PSScriptRoot) + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid test root.' }
New-Item -ItemType Directory -Path $testRoot | Out-Null
$root = $testRoot
$deployRoot = Join-Path $root '_deploy'
$stage = Join-Path $deployRoot 'stage\20261006000000-abcdef012345'
$backup = Join-Path $deployRoot 'backups\20261006000000-abcdef012345'
New-Item -ItemType Directory -Path $stage, $backup, (Join-Path $deployRoot 'node-cache') -Force | Out-Null
function Assert-Test($condition, [string]$message) { if (-not $condition) { throw $message } }
# Load the real deployment functions without executing a production deployment.
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'deploy.ps1'), [ref]$tokens, [ref]$errors)
Assert-Test (-not $errors.Count) 'Deployment script has syntax errors.'
foreach ($name in @('Assert-NewsPath', 'Move-NewsDirectory', 'Get-DependencyKey', 'Restore-NewsDirectories')) {
    $fn = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    . ([scriptblock]::Create($fn.Extent.Text))
}
. (Join-Path $PSScriptRoot 'cleanup.ps1') -Root $testRoot -FunctionsOnly
try {
    $manifests = Join-Path $testRoot 'manifests'
    New-Item -ItemType Directory -Path $manifests | Out-Null
    Set-Content -LiteralPath (Join-Path $manifests 'package.json') -Value '{"name":"test"}'
    Set-Content -LiteralPath (Join-Path $manifests 'package-lock.json') -Value 'lock-one'
    $key = Get-DependencyKey $manifests 'node24|npm11|AMD64'
    Assert-Test ($key -eq (Get-DependencyKey $manifests 'node24|npm11|AMD64')) 'Unchanged manifests must reuse cache.'
    Assert-Test ($key -ne (Get-DependencyKey $manifests 'node25|npm11|AMD64')) 'Runtime change must invalidate cache.'
    Set-Content -LiteralPath (Join-Path $manifests 'package-lock.json') -Value 'lock-two'
    Assert-Test ($key -ne (Get-DependencyKey $manifests 'node24|npm11|AMD64')) 'Lockfile change must invalidate cache.'
    $key = Get-DependencyKey $manifests 'node24|npm11|AMD64'
    Set-Content -LiteralPath (Join-Path $manifests '.npmrc') -Value 'legacy-peer-deps=true'
    Assert-Test ($key -ne (Get-DependencyKey $manifests 'node24|npm11|AMD64')) 'npm configuration must invalidate cache.'

    # Failed switch after backing up backend, but before touching frontend/venv.
    New-Item -ItemType Directory -Path (Join-Path $backup 'backend'), (Join-Path $root 'frontend'), (Join-Path $root '.venv') | Out-Null
    Set-Content -LiteralPath (Join-Path $backup 'backend\old.txt') -Value 'previous backend'
    Set-Content -LiteralPath (Join-Path $root 'frontend\live.txt') -Value 'untouched frontend'
    Set-Content -LiteralPath (Join-Path $root '.venv\live.txt') -Value 'untouched environment'
    $failed = Join-Path $stage 'failed'
    New-Item -ItemType Directory -Path $failed | Out-Null
    Restore-NewsDirectories @('backend', 'frontend', '.venv') $failed
    Assert-Test (Test-Path -LiteralPath (Join-Path $root 'backend\old.txt')) 'Partial switch must restore backend.'
    Assert-Test (Test-Path -LiteralPath (Join-Path $root 'frontend\live.txt')) 'Partial switch must preserve untouched frontend.'
    Assert-Test (Test-Path -LiteralPath (Join-Path $root '.venv\live.txt')) 'Partial switch must preserve untouched Python environment.'
    # Health check failure after a new frontend replaced the old one.
    Move-NewsDirectory (Join-Path $root 'frontend') (Join-Path $backup 'frontend')
    New-Item -ItemType Directory -Path (Join-Path $root 'frontend') | Out-Null
    Set-Content -LiteralPath (Join-Path $root 'frontend\new.txt') -Value 'failed frontend'
    Restore-NewsDirectories @('backend', 'frontend') $failed
    Assert-Test (Test-Path -LiteralPath (Join-Path $root 'frontend\live.txt')) 'Health failure must restore old frontend.'
    Assert-Test (Test-Path -LiteralPath (Join-Path $failed 'frontend\new.txt')) 'Failed frontend must be retained for diagnosis.'

    $shared = Join-Path $deployRoot 'node-cache\shared'
    New-Item -ItemType Directory -Path $shared | Out-Null
    Set-Content -LiteralPath (Join-Path $shared 'keep.txt') -Value 'shared dependency'
    New-Item -ItemType Junction -Path (Join-Path $stage 'node_modules') -Target $shared | Out-Null
    Assert-CleanupPath $stage
    $rejected = $false
    try { Assert-CleanupPath $shared } catch { $rejected = $true }
    Assert-Test $rejected 'Cleanup must reject dependency cache targets.'
    $rejected = $false
    try { Assert-CleanupPath (Join-Path $deployRoot 'stage') } catch { $rejected = $true }
    Assert-Test $rejected 'Cleanup must reject the parent stage directory.'

    $queue = Join-Path $deployRoot 'cleanup-queue'
    New-Item -ItemType Directory -Path $queue | Out-Null
    ConvertTo-Json -InputObject @($stage, $backup) | Set-Content -LiteralPath (Join-Path $queue 'request.json')
    $heldLock = [IO.File]::Open((Join-Path $queue 'worker.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try { & (Join-Path $PSScriptRoot 'cleanup.ps1') -Root $testRoot }
    finally { $heldLock.Dispose() }
    Assert-Test (Test-Path -LiteralPath $stage) 'A concurrent cleanup worker must not process the queue.'
    & (Join-Path $PSScriptRoot 'cleanup.ps1') -Root $testRoot
    Assert-Test (-not (Test-Path -LiteralPath $stage)) 'Cleanup must remove the queued stage.'
    Assert-Test (-not (Test-Path -LiteralPath $backup)) 'Cleanup must process multiple paths in the same request.'
    Assert-Test (Test-Path -LiteralPath (Join-Path $shared 'keep.txt')) 'Cleanup must preserve junction targets.'
    Assert-Test (-not (Test-Path -LiteralPath (Join-Path $queue 'request.json'))) 'Successful cleanup must drain its queue.'
    # A port collision must fail closed, never kill a process by generic name/port.
    $fn = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Stop-News' }, $true)
    . ([scriptblock]::Create($fn.Extent.Text))
    $tasks = @('NewsIntelligenceAPI', 'NewsIntelligenceWeb')
    $stoppedTasks = [Collections.Generic.List[string]]::new()
    $script:forcedProcessStops = 0
    function Stop-ScheduledTask { param($TaskName) $stoppedTasks.Add($TaskName) }
    function Start-Sleep { }
    function Get-NetTCPConnection { param($LocalPort) if ($LocalPort -eq 3101) { [pscustomobject]@{OwningProcess=123456} } }
    function Get-CimInstance { [pscustomobject]@{CommandLine='node_modules\next\dist\bin\next start --port 3101'} }
    function Stop-Process { $script:forcedProcessStops++ }
    $blocked = $false
    try { Stop-News } catch { $blocked = $true }
    Assert-Test $blocked 'Occupied ports must require owner review.'
    Assert-Test ($script:forcedProcessStops -eq 0) 'Release must not force-stop a process by port or command pattern.'
    Assert-Test (($stoppedTasks -join ',') -eq 'NewsIntelligenceAPI,NewsIntelligenceWeb') 'Only News tasks may be stopped.'
    $fn = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Start-News' }, $true)
    . ([scriptblock]::Create($fn.Extent.Text))
    function Assert-NewsPausedConfiguration { }
    function Get-NewsInheritedCollectionSettings { @{} }
    $script:taskStarts = 0
    function Start-ScheduledTask { $script:taskStarts++ }
    $blocked = $false
    try { Start-News } catch { $blocked = $true }
    Assert-Test ($blocked -and $script:taskStarts -eq 0) 'Occupied ports must prevent every restart path from starting another instance.'
    Write-Output 'PASS: dependency invalidation, partial/full rollback, cleanup boundaries, junction safety, worker lock.'
} finally {
    # Explicitly verified test directory within deploy; remove links without traversal.
    if (Test-Path -LiteralPath $testRoot) { Remove-CleanupTree $testRoot }
}

