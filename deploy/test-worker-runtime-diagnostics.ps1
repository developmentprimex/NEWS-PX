$ErrorActionPreference = 'Stop'
$path = Join-Path $PSScriptRoot 'read-only-worker-runtime-diagnostics.ps1'
if (-not (Test-Path -LiteralPath $path)) { throw 'Consolidated runtime diagnostic script is missing' }
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Diagnostic syntax failed' }
# Load function definitions only: never execute the production entrypoint in tests.
foreach ($definition in $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]},$false)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
function Assert-Test($condition,[string]$message) { if (-not $condition) { throw $message } }
function Assert-Rejected([scriptblock]$Action) {
    $rejected=$false;try { & $Action | Out-Null } catch { $rejected=$true }
    Assert-Test $rejected 'Unsafe input accepted'
}
$secret='SYNTHETIC_API_TOKEN_not_a_real_credential_123456789'
$password='SYNTHETIC_PASSWORD_never_export'
$database='postgresql://fixture:private-password@127.0.0.1/private'
function New-Row([int]$Id,[string]$Command) {
    [pscustomobject]@{ProcessId=$Id;ParentProcessId=77;Name='python.exe';ExecutablePath='C:\Python\python.exe';
        CommandLine=$Command;CreationDate=[datetime]'2026-10-16T06:40:00Z'}
}
$rows=@(
    (New-Row 3644 ('python.exe "E:\websites\website-monitoring\monitor.py" --token '+$secret)),
    (New-Row 1064 ('python.exe E:\other\server.py --password '+$password+' --db '+$database)),
    (New-Row 3080 ('python.exe -m celery -A app.celery_app.celery_app worker --token '+$secret)),
    (New-Row 10776 'python.exe -m celery --app=app.celery_app.celery_app beat'),
    (New-Row 3556 'python.exe -m uvicorn app.main:app --port 8001'),
    (New-Row 99 ('python.exe --token "C:\secret\'+$secret+'.py"')),
    (New-Row 100 'python.exe -c "print(1)" --worker beat'),
    (New-Row 101 'python.exe worker.py')
)
$runtime=@($rows | ForEach-Object { Get-WorkerRuntimeEntrypoint $_ })
Assert-Test ($runtime[0].entrypoint -eq 'E:\websites\website-monitoring\monitor.py') 'Quoted absolute 8081 entrypoint missing'
Assert-Test ($runtime[1].entrypoint -eq 'E:\other\server.py') 'Unquoted absolute 8091 entrypoint missing'
Assert-Test ($runtime[2].celeryRole -eq 'worker' -and $runtime[3].celeryRole -eq 'beat') 'Celery role parsing failed'
Assert-Test ($runtime[2].celeryApp -eq 'app.celery_app.celery_app') 'Celery app missing'
Assert-Test ($runtime[4].asgiTarget -eq 'app.main:app') 'ASGI target missing'
Assert-Test (-not $runtime[5].entrypoint -and -not $runtime[6].celeryRole) 'Argument/inline code misidentified as entrypoint'
Assert-Test ($runtime[7].entrypointResolution -eq 'RELATIVE_CWD_NOT_VERIFIED') 'Relative script falsely resolved'
Assert-Test ([bool]$runtime[0].createdUtc) 'Creation timestamp missing'
$script:workerDiagnosticRedactions=0
$safe=ConvertTo-WorkerSafeMetadata ([ordered]@{runtime=$runtime;name=('token='+$secret);password=('password='+$password);uri=$database;envValue=$null})
$json=$safe | ConvertTo-Json -Depth 15
foreach ($value in @($secret,$password,$database)) { Assert-Test (-not $json.Contains($value)) 'Synthetic secret leaked' }
Assert-Test ($script:workerDiagnosticRedactions -eq 3) 'Sensitive metadata was not redacted'

# Exercise the real private-path guard and create-new writer against new local fixtures.
$fixtureRoot=Join-Path (Split-Path -Parent $PSScriptRoot) ('tmp\worker-runtime-test-'+[guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($fixtureRoot) | Out-Null
Assert-WorkerInspectionPath $fixtureRoot 'E:\fixture-news'
Assert-Rejected { Assert-WorkerInspectionPath $fixtureRoot (Split-Path -Parent $fixtureRoot) }
$export1=Write-WorkerPrivateExport $safe $fixtureRoot 'E:\fixture-news'
$export2=Write-WorkerPrivateExport $safe $fixtureRoot 'E:\fixture-news'
Assert-Test ($export1.exportPath -ne $export2.exportPath) 'Export overwrote existing evidence'
$exportJson=[IO.File]::ReadAllText($export1.exportPath)
foreach($value in @($secret,$password,$database)){Assert-Test (-not $exportJson.Contains($value)) 'Export leaked synthetic credentials'}

# Real PM2 projection on synthetic saved files: arbitrary env/labels are not copied.
$pm2Root=Join-Path $fixtureRoot 'pm2-fixture'
[IO.Directory]::CreateDirectory((Join-Path $pm2Root 'pids')) | Out-Null
$pm2Fixture=@([ordered]@{name='celery_worker';pm_cwd='C:\fixture\backend';pm_exec_path='C:\Python\python.exe';
    args=@('-m','celery','-A','app.celery_app.celery_app','worker','--token',$secret);
    env=@{PRIVATE_VALUE=$password;DATABASE_URL=$database};labels=@{hidden=$secret};autorestart=$true;watch=$false;max_restarts=10})
[IO.File]::WriteAllText((Join-Path $pm2Root 'dump.pm2'),($pm2Fixture | ConvertTo-Json -Depth 8))
[IO.File]::WriteAllText((Join-Path $pm2Root 'pids\celery_worker-0.pid'),'3080')
$pm2Result=Read-WorkerPm2Metadata $pm2Root
Assert-Test ($pm2Result.apps[0].celeryRole -eq 'worker') 'Saved Celery role lost'
Assert-Test ($pm2Result.pidFiles[0].pid -eq 3080) 'PM2 saved PID lost'
Assert-Test ($pm2Result.fileEvidence[0].unchangedWhileReading) 'PM2 dump stability check failed'
$pm2Json=$pm2Result | ConvertTo-Json -Depth 10
foreach($value in @($secret,$password,$database)){Assert-Test (-not $pm2Json.Contains($value)) 'PM2 secrets copied into projected fields'}
$sanitizedPm2=ConvertTo-WorkerSafeMetadata $pm2Result
Assert-Test ($sanitizedPm2.status -ne '[REDACTED_METADATA]') 'Normal PM2 saved-file status was redacted'

# Real task projector with mocked OS readers; task arguments/subscriptions never exported.
function Get-ScheduledTask {
    [pscustomobject]@{TaskName='NewsIntelligenceAPI';TaskPath='\';State='Running';
        Settings=[pscustomobject]@{Enabled=$true;MultipleInstances='IgnoreNew';RestartCount=10;RestartInterval='PT1M';StartWhenAvailable=$false};
        Principal=[pscustomobject]@{UserId='fixture-owner';LogonType='Password';RunLevel='Highest'};
        Actions=@([pscustomobject]@{Execute='powershell.exe';WorkingDirectory='E:\fixture';Arguments=('-NoProfile -File "E:\fixture\start.ps1" --token '+$secret)});
        Triggers=@([pscustomobject]@{CimClass=[pscustomobject]@{CimClassName='MSFT_TaskBootTrigger'};Enabled=$true;Subscription=$password;
            Repetition=[pscustomobject]@{Interval='PT5M'}})}
}
function Get-ScheduledTaskInfo { param($TaskName,$TaskPath) [pscustomobject]@{LastRunTime=[datetime]'2026-10-16T06:40:00Z';LastTaskResult=0} }
$taskResult=@(Read-WorkerTaskDetails)
Assert-Test ($taskResult[0].restartCount -eq 10 -and $taskResult[0].triggers[0].repeat -eq 'PT5M') 'Task restart/trigger metadata lost'
Assert-Test ($taskResult[0].actions[0].entrypoint -eq 'E:\fixture\start.ps1') 'Task launcher not resolved'
$taskJson=$taskResult | ConvertTo-Json -Depth 12
foreach($value in @($secret,$password,$database)){Assert-Test (-not $taskJson.Contains($value)) 'Task secret copied into projection'}

# Inventory test: every OS/process/filesystem reader is mocked; no native worker is run.
$script:fixtureReads=@();$script:fixtureBadHash=$false;$script:fixtureNotAdmin=$false
function Assert-WorkerDiagnosticHost { param($ExpectedServerAddress) if($script:fixtureNotAdmin){throw 'Not elevated'} }
function Assert-WorkerInspectionPath { param($InspectionRoot,$NewsRoot) }
function Get-FileHash { param($LiteralPath,$Algorithm) [pscustomobject]@{Hash=if($script:fixtureBadHash){'0'*64}else{'D2F63F451E22537CADD59BE98A590B7D60FBBA8F5E70457000DA09554ACA4C91'}} }
function Read-ApprovedWorkerInventory {
    param($Helper,$NewsRoot)
    $script:fixtureReads+='approved-helper'
    [pscustomobject]@{capturedAt='2026-10-16T06:40:00Z';productionModified=$false;providerRequests=0;
        processes=@([pscustomobject]@{pid=3644;parentPid=77;name='python.exe';executable='C:\Python\python.exe';module='';port='';script='';newsPath=$false;providerMarker=$false});
        services=@();tasks=@();listeners=@([pscustomobject]@{LocalAddress='0.0.0.0';LocalPort=8081;OwningProcess=3644});containers=@();docker='not installed on PATH';wslDistributions=@();unapprovedEnvironment=$password}
}
function Get-CimInstance { param($ClassName) Assert-Test ($ClassName -eq 'Win32_Process') 'Unexpected CIM scope';$script:fixtureReads+='processes';$rows;[pscustomobject]@{ProcessId=88;ParentProcessId=77;Name='custom-worker.exe';CreationDate=[datetime]'2026-10-16T06:40:00Z'} }
function Read-WorkerPm2Metadata { param($Pm2Home) [ordered]@{status='NOT_PRESENT';apps=@();pidFiles=@([ordered]@{file='custom-0.pid';pid=88;modifiedUtc='2026-10-16T06:40:00Z'});fileEvidence=@()} }
function Read-WorkerTaskDetails {
    $script:fixtureReads+='tasks'
    [ordered]@{name='NewsIntelligenceAPI';restartCount=10;restartInterval='PT1M';triggers=@([ordered]@{type='MSFT_TaskBootTrigger'})}
}
$bundle=Get-WorkerRuntimeBundle -ExpectedServerAddress '192.0.2.1' -InspectionRoot 'C:\private\fixture' -NewsRoot 'E:\fixture' -Helper 'C:\private\fixture\deploy\read-only-worker-diagnostics.ps1' -Pm2Home 'C:\pm2-fixture'
$bundleJson=$bundle | ConvertTo-Json -Depth 18
foreach ($value in @($secret,$password,$database)) { Assert-Test (-not $bundleJson.Contains($value)) 'Bundle leaked a secret' }
foreach ($field in @('runtimeEntrypoints','pm2Apps','pm2PidFiles','taskDetails','approvedInventory','portBindings')) {
    Assert-Test ($bundle.Contains($field)) "Required field missing: $field"
}
Assert-Test ($bundle.portBindings[0].pid -eq 3644 -and $bundle.portBindings[0].entrypoint -eq 'E:\websites\website-monitoring\monitor.py') 'Port-to-entrypoint join failed'
Assert-Test ($bundle.pm2PidFiles[0].inFreshProcessSnapshot -and -not $bundle.pm2PidFiles[0].runtimeDetailsAvailable) 'Native PID incorrectly described as absent from full snapshot'
Assert-Test ($script:fixtureReads.Count -eq 3) 'Unexpected mocked inventory operations'
$script:fixtureBadHash=$true
Assert-Rejected { Get-WorkerRuntimeBundle -ExpectedServerAddress '192.0.2.1' -InspectionRoot 'C:\private\fixture' -NewsRoot 'E:\fixture' -Helper 'fixture' -Pm2Home 'fixture' }
$script:fixtureBadHash=$false;$script:fixtureNotAdmin=$true
Assert-Rejected { Get-WorkerRuntimeBundle -ExpectedServerAddress '192.0.2.1' -InspectionRoot 'C:\private\fixture' -NewsRoot 'E:\fixture' -Helper 'fixture' -Pm2Home 'fixture' }

# Reject any unreviewed invocation: allowlisted reads, pure projection, one helper child.
$allowed=@('New-Object','Get-Date','Get-NetIPAddress','Where-Object','Get-FileHash','Get-CimInstance','Get-ScheduledTask','Get-ScheduledTaskInfo',
    'Get-Content','ConvertFrom-Json','ConvertTo-Json','Get-ChildItem','Get-Item','Test-Path','Join-Path','Split-Path',
    'Select-Object','ForEach-Object','Out-Null','Assert-WorkerDiagnosticHost','Assert-WorkerInspectionPath',
    'Read-ApprovedWorkerInventory','Get-WorkerRuntimeEntrypoint','Get-WorkerRuntimeBundle','Read-WorkerPm2Metadata',
    'Read-WorkerTaskDetails','ConvertTo-WorkerSafeMetadata','Get-WorkerCommandTokens','Get-WorkerModuleDetails',
    'Write-WorkerPrivateExport')
$commands=@($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.CommandAst]},$true))
foreach($command in $commands){
    $name=$command.GetCommandName()
    if(-not $name){Assert-Test ($command.Extent.Text -match '^& \$hostPath ') 'Unreviewed dynamic execution'}
    else{Assert-Test ($name -in $allowed) "Unreviewed command: $name"}
}
Assert-Test ($ast.Extent.Text -notmatch '(?i)ExecutionPolicy|Invoke-(?:WebRequest|RestMethod|Expression)|Start-Process|SetEnvironmentVariable|GetEnvironmentVariables') 'Forbidden execution/network/environment API'
Assert-Test ($ast.Extent.Text -match 'FileMode\]::CreateNew') 'Export must not overwrite a file'
Write-Output 'PASS: syntax, approved hash/admin gates, absolute/relative/inline parsing, Celery/ASGI roles, creation time, port join, synthetic secret exclusion and read-only command allowlist. All inventory calls mocked; no production/provider calls.'
