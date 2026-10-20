param(
    [Parameter(Mandatory=$true)][string]$ExpectedServerAddress,
    [string]$InspectionRoot='E:\private\primex-reviewed-candidate',
    [string]$NewsRoot='E:\websites\news-intelligence',
    [string]$Pm2Home='C:\pm2-system'
)
$ErrorActionPreference='Stop'
# Inventory only. The sole write creates a new JSON file in the existing private folder.
# Command lines and PM2 state are parsed in memory, never exported verbatim.
function Assert-WorkerDiagnosticHost([string]$ExpectedServerAddress) {
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    $principal=New-Object Security.Principal.WindowsPrincipal($identity)
    if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Use an existing authorized elevated server console.'}
    if(-not (Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -EQ $ExpectedServerAddress)){throw 'Expected local server address not found.'}
}
function Assert-WorkerInspectionPath([string]$InspectionRoot,[string]$NewsRoot) {
    if(-not [IO.Path]::IsPathRooted($InspectionRoot) -or $InspectionRoot.StartsWith('\\')){throw 'A local private inspection directory is required.'}
    $full=[IO.Path]::GetFullPath($InspectionRoot).TrimEnd('\')
    $news=[IO.Path]::GetFullPath($NewsRoot).TrimEnd('\')
    if($full -eq $news -or $full.StartsWith($news+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Export cannot be inside the production application.'}
    if(-not (Test-Path -LiteralPath $full -PathType Container)){throw 'Existing private inspection directory required.'}
    $cursor=$full
    while($cursor){
        if((Get-Item -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Inspection path cannot traverse a reparse point.'}
        $parent=[IO.Directory]::GetParent($cursor)
        if($null -eq $parent){break};$cursor=$parent.FullName
    }
}
function Get-WorkerCommandTokens([string]$Command) {
    # Tokenize for metadata only, never evaluate a command or interpret shell operators.
    foreach($match in [regex]::Matches($Command,'"[^"\r\n]*"|''[^''\r\n]*''|[^\s]+')){
        $value=$match.Value
        if($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))){$value=$value.Substring(1,$value.Length-2)}
        $value
    }
}
function Get-WorkerModuleDetails([string[]]$Tokens) {
    $result=[ordered]@{module='';asgiTarget='';celeryApp='';celeryRole='';entrypoint='';entrypointResolution='NOT_VERIFIED'}
    if($Tokens.Count -lt 2){return $result}
    $index=1
    # Only interpreter/startup positions are candidates; options' arbitrary values are not scripts.
    while($index -lt $Tokens.Count){
        $token=$Tokens[$index]
        if($token -in @('-u','-B','-b','-bb','-E','-I','-s','-S','-O','-OO','-v','-q','/d','/c','/s','call','-NoProfile','-NonInteractive','-NoLogo')){$index++;continue}
        if($token -in @('-W','-X')){$index+=2;continue}
        if($token -eq '-c'){$result.entrypointResolution='INLINE_CODE_WITHHELD';return $result}
        if($token -eq '-File'){$index++;if($index -ge $Tokens.Count){return $result};$token=$Tokens[$index]}
        if($token -eq '-m'){
            $index++
            if($index -lt $Tokens.Count -and $Tokens[$index] -cmatch '^[A-Za-z_]\w*(?:\.\w+)*$'){$result.module=$Tokens[$index]}else{return $result}
            $index++;break
        }
        if($token -match '^(?:[A-Za-z]:\\|[\w./\\-]+)' -and $token -match '\.(?:py|ps1|cmd|bat|js|cjs|mjs)$' -and $token -notmatch '(?i)://|[\r\n]|\s--?|(?:password|token|secret|api[_-]?key)\s*[:=]'){
            $result.entrypoint=$token
            $result.entrypointResolution=if($token -match '^[A-Za-z]:\\'){'ABSOLUTE'}else{'RELATIVE_CWD_NOT_VERIFIED'}
        }
        return $result
    }
    while($index -lt $Tokens.Count){
        $token=$Tokens[$index]
        if($result.module -eq 'uvicorn'){
            if($index -lt $Tokens.Count -and $token -cmatch '^[A-Za-z_]\w*(?:\.\w+)*:[A-Za-z_]\w*$'){$result.asgiTarget=$token;break}
            break
        }
        if($result.module -eq 'celery'){
            if($token -in @('-A','--app')){
                $index++;if($index -lt $Tokens.Count -and $Tokens[$index] -cmatch '^[A-Za-z_]\w*(?:[.:]\w+)*$'){$result.celeryApp=$Tokens[$index]}else{break}
            }elseif($token -cmatch '^--app=([A-Za-z_]\w*(?:[.:]\w+)*)$'){$result.celeryApp=$Matches[1]
            }elseif($token -in @('worker','beat')){$result.celeryRole=$token;break
            }else{break} # Unrecognized options/values cannot manufacture a role.
        }else{break}
        $index++
    }
    $result
}
function Get-WorkerRuntimeEntrypoint($Row) {
    $descriptor=Get-WorkerModuleDetails -Tokens @(Get-WorkerCommandTokens ([string]$Row.CommandLine))
    $created=$null
    if($Row.CreationDate){try{$created=([datetime]$Row.CreationDate).ToUniversalTime().ToString('o')}catch{}}
    [ordered]@{pid=$Row.ProcessId;parentPid=$Row.ParentProcessId;name=$Row.Name;executable=$Row.ExecutablePath;
        createdUtc=$created;module=$descriptor.module;asgiTarget=$descriptor.asgiTarget;celeryApp=$descriptor.celeryApp;
        celeryRole=$descriptor.celeryRole;entrypoint=$descriptor.entrypoint;entrypointResolution=$descriptor.entrypointResolution}
}
function ConvertTo-WorkerSafeMetadata($Value,[string]$FieldName='') {
    if($null -eq $Value){return $null}
    if($Value -is [string]){
        if($FieldName -match '(?i)sha256$' -and $Value -cmatch '^[a-fA-F0-9]{64}$'){return $Value}
        if($Value.Length -gt 1024 -or $Value -match '(?i)://|bearer\s+|(?:password|passwd|token|secret|api[_-]?key)\s*[:=]|[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}|(?:[A-Za-z0-9_-]{32,})|[\r\n]'){
            $script:workerDiagnosticRedactions++;return '[REDACTED_METADATA]'
        }
        return $Value
    }
    if($Value -is [datetime]){return $Value.ToUniversalTime().ToString('o')}
    if($Value -is [System.Collections.IDictionary]){
        $safe=[ordered]@{};foreach($key in $Value.Keys){$safe[$key]=ConvertTo-WorkerSafeMetadata $Value[$key] ([string]$key)};return $safe
    }
    if($Value -is [System.Collections.IEnumerable]){
        $items=@(foreach($item in $Value){ConvertTo-WorkerSafeMetadata $item $FieldName});return ,$items
    }
    if($Value -is [System.Management.Automation.PSCustomObject]){
        $safe=[ordered]@{};foreach($property in $Value.PSObject.Properties){$safe[$property.Name]=ConvertTo-WorkerSafeMetadata $property.Value $property.Name};return $safe
    }
    if($Value -is [ValueType]){return $Value}
    return '[UNSUPPORTED_METADATA]'
}
function Read-ApprovedWorkerInventory([string]$Helper,[string]$NewsRoot) {
    $hostPath=Join-Path $PSHOME 'powershell.exe'
    $lines=@(& $hostPath -NoProfile -NonInteractive -File $Helper -NewsRoot $NewsRoot 2>$null)
    if($LASTEXITCODE -ne 0){throw 'Approved base inventory failed; no export created.'}
    try{($lines -join "`n") | ConvertFrom-Json}catch{throw 'Approved base inventory JSON invalid; no export created.'}
}
function Read-WorkerPm2Metadata([string]$Pm2Home) {
    $result=[ordered]@{status='NOT_PRESENT_AT_REVIEWED_PATH';apps=@();pidFiles=@();fileEvidence=@()}
    $dump=Join-Path $Pm2Home 'dump.pm2'
    try{
        if(Test-Path -LiteralPath $dump -PathType Leaf){
            $before=(Get-FileHash -LiteralPath $dump -Algorithm SHA256).Hash
            $saved=Get-Content -LiteralPath $dump -Raw | ConvertFrom-Json
            $result.apps=@(foreach($app in $saved){
                $arguments=if($app.args -is [string]){@(Get-WorkerCommandTokens $app.args)}else{@($app.args)}
                $details=Get-WorkerModuleDetails -Tokens (@('python.exe')+$arguments)
                [ordered]@{name=$app.name;pm_cwd=$app.pm_cwd;pm_exec_path=$app.pm_exec_path;exec_mode=$app.exec_mode;
                    autorestart=$app.autorestart;max_restarts=$app.max_restarts;restart_delay=$app.restart_delay;
                    cron_restart=$app.cron_restart;watch=$app.watch;savedPid=$app.pid;
                    module=$details.module;asgiTarget=$details.asgiTarget;celeryApp=$details.celeryApp;
                    celeryRole=$details.celeryRole;entrypoint=$details.entrypoint}
            })
            $after=(Get-FileHash -LiteralPath $dump -Algorithm SHA256).Hash
            $result.fileEvidence=@([ordered]@{file='dump.pm2';sha256=$after;unchangedWhileReading=($before -eq $after);
                modifiedUtc=(Get-Item -LiteralPath $dump).LastWriteTimeUtc.ToString('o')})
            $result.status=if($before -eq $after){'SAVED_FILES_NOT_LIVE_RPC'}else{'CHANGED_DURING_READ_NOT_VERIFIED'}
        }
        $pidRoot=Join-Path $Pm2Home 'pids'
        if(Test-Path -LiteralPath $pidRoot -PathType Container){
            $result.pidFiles=@(foreach($file in Get-ChildItem -LiteralPath $pidRoot -Filter '*.pid' -File){
                $number=(Get-Content -LiteralPath $file.FullName -Raw).Trim()
                if($number -match '^\d{1,10}$'){
                    [ordered]@{file=$file.Name;pid=[long]$number;modifiedUtc=$file.LastWriteTimeUtc.ToString('o')}
                }
            })
        }
    }catch{$result.status='READ_INCOMPLETE_NOT_VERIFIED'}
    $result
}
function Read-WorkerTaskDetails {
    foreach($task in Get-ScheduledTask | Where-Object {$_.TaskName -match 'NewsIntelligence|PrimeFlow|Codex.*Pm2'}){
        $info=$null;$infoStatus='PASS'
        try{$info=Get-ScheduledTaskInfo -TaskName $task.TaskName -TaskPath $task.TaskPath}catch{$infoStatus='NOT_VERIFIED'}
        [ordered]@{name=$task.TaskName;path=$task.TaskPath;state=[string]$task.State;
            enabled=$task.Settings.Enabled;multipleInstances=[string]$task.Settings.MultipleInstances;
            restartCount=$task.Settings.RestartCount;restartInterval=[string]$task.Settings.RestartInterval;
            startWhenAvailable=$task.Settings.StartWhenAvailable;executionTimeLimit=[string]$task.Settings.ExecutionTimeLimit;
            principalUserId=$task.Principal.UserId;logonType=[string]$task.Principal.LogonType;runLevel=[string]$task.Principal.RunLevel;
            infoStatus=$infoStatus;lastRun=$info.LastRunTime;nextRun=$info.NextRunTime;lastResult=$info.LastTaskResult;
            actions=@(foreach($action in $task.Actions){
                $parts=@('task.exe')+@(Get-WorkerCommandTokens ([string]$action.Arguments))
                $entry=Get-WorkerModuleDetails -Tokens $parts
                [ordered]@{executable=$action.Execute;workingDirectory=$action.WorkingDirectory;entrypoint=$entry.entrypoint;
                    entrypointResolution=$entry.entrypointResolution;newsMarker=([string]$action.Arguments -match 'news-intelligence');
                    providerMarker=([string]$action.Arguments -match 'brightdata|linkedin|check_due_sources')}
            });
            triggers=@(foreach($trigger in $task.Triggers){
                [ordered]@{type=$trigger.CimClass.CimClassName;enabled=$trigger.Enabled;start=$trigger.StartBoundary;end=$trigger.EndBoundary;
                    repeat=$trigger.Repetition.Interval;duration=$trigger.Repetition.Duration;stopAtDurationEnd=$trigger.Repetition.StopAtDurationEnd}
            })}
    }
}
function Get-WorkerRuntimeBundle([string]$ExpectedServerAddress,[string]$InspectionRoot,[string]$NewsRoot,[string]$Helper,[string]$Pm2Home) {
    Assert-WorkerDiagnosticHost $ExpectedServerAddress
    Assert-WorkerInspectionPath $InspectionRoot $NewsRoot
    $expected='D2F63F451E22537CADD59BE98A590B7D60FBBA8F5E70457000DA09554ACA4C91'
    if((Get-FileHash -LiteralPath $Helper -Algorithm SHA256).Hash -cne $expected){throw 'Approved helper hash mismatch; no inventory executed.'}
    $started=(Get-Date).ToUniversalTime().ToString('o')
    $workers=Read-ApprovedWorkerInventory $Helper $NewsRoot
    if($workers.productionModified -ne $false -or $workers.providerRequests -ne 0){throw 'Unexpected helper safety metadata.'}
    $approved=[ordered]@{capturedAt=$workers.capturedAt;productionModified=$workers.productionModified;providerRequestsByDiagnostics=$workers.providerRequests;
        processes=@($workers.processes | Select-Object pid,parentPid,name,executable,module,port,script,runtimeMarker,newsPath,providerMarker,laravelMarker,n8nMarker);
        services=@($workers.services | Select-Object name,state,pid,runtimeMarker,newsPath,providerMarker);
        tasks=@(foreach($task in $workers.tasks){[ordered]@{name=$task.name;path=$task.path;state=$task.state;multipleInstances=$task.multipleInstances;
            actions=@($task.actions | Select-Object executable,workingDirectory,newsPath,providerMarker,laravelMarker,n8nMarker)}});
        listeners=@($workers.listeners | Select-Object LocalAddress,LocalPort,OwningProcess);
        containers=@(foreach($container in $workers.containers){
            if($container -is [string]){$container=$container | ConvertFrom-Json};$container | Select-Object id,name,image,status
        });dockerExitCode=$workers.dockerExitCode;dockerStatus=$workers.docker;
        wslDistributions=@($workers.wslDistributions);wslExitCode=$workers.wslExitCode;wslStatus=$workers.wsl}
    $processRows=@(Get-CimInstance Win32_Process)
    $allProcessPids=@{};foreach($process in $processRows){$allProcessPids[[string]$process.ProcessId]=$true}
    $runtime=@($processRows | Where-Object {$_.Name -match 'python|node|cmd|powershell|uvicorn|celery'} | ForEach-Object {Get-WorkerRuntimeEntrypoint $_})
    $pm2=Read-WorkerPm2Metadata $Pm2Home
    $runtimeByPid=@{};foreach($row in $runtime){$runtimeByPid[[string]$row.pid]=$row}
    $bindings=@(foreach($listener in $approved.listeners){
        if($listener.LocalPort -in @(8001,3101,8081,8091,8000,8080,8010,3000)){
            $row=$runtimeByPid[[string]$listener.OwningProcess]
            [ordered]@{port=$listener.LocalPort;address=$listener.LocalAddress;pid=$listener.OwningProcess;
                parentPid=$row.parentPid;createdUtc=$row.createdUtc;entrypoint=$row.entrypoint;entrypointResolution=$row.entrypointResolution;
                module=$row.module;asgiTarget=$row.asgiTarget;presentInSupplement=($null -ne $row);
                caution='Sequential snapshots; verify creation time and executable before any maintenance.'}
        }
    })
    $pm2Pids=@(foreach($file in $pm2.pidFiles){
        $row=$runtimeByPid[[string]$file.pid]
        [ordered]@{file=$file.file;pid=$file.pid;modifiedUtc=$file.modifiedUtc;inFreshProcessSnapshot=$allProcessPids.ContainsKey([string]$file.pid);
            runtimeDetailsAvailable=($null -ne $row);
            processCreatedUtc=$row.createdUtc;processName=$row.name;entrypoint=$row.entrypoint;celeryApp=$row.celeryApp;
            celeryRole=$row.celeryRole;bindingStatus='CANDIDATE_NOT_VERIFIED'}
    })
    $taskStatus='PASS';$tasks=@()
    try{$tasks=@(Read-WorkerTaskDetails)}catch{$taskStatus='READ_INCOMPLETE_NOT_VERIFIED'}
    $bundle=[ordered]@{schemaVersion=1;captureStartedUtc=$started;capturedUtc=(Get-Date).ToUniversalTime().ToString('o');
        approvedHelperSha256=$expected;approvedInventory=$approved;runtimeEntrypoints=$runtime;portBindings=$bindings;
        pm2Status=$pm2.status;pm2Apps=$pm2.apps;pm2PidFiles=$pm2Pids;pm2FileEvidence=$pm2.fileEvidence;
        taskDetails=$tasks;taskDetailsStatus=$taskStatus;providerRequestsBySupplement=0;productionConfigurationChanged=$false;
        limitations=@('Raw command lines, arguments, PM2 environments, container labels and credentials are never exported.',
            'Relative entrypoints require cwd evidence. Unreadable metadata and stale PID files remain NOT_VERIFIED.',
            'Saved PM2 files are not live daemon state. No PM2 CLI, broker query or job execution is performed.',
            'Marker absence does not prove no account usage. External consumers and loaded credentials remain outside this export.',
            'Names and paths require private metadata review before transfer. No release or activation approval is implied.')}
    $script:workerDiagnosticRedactions=0
    $safe=ConvertTo-WorkerSafeMetadata $bundle
    $safe.metadataRedactions=$script:workerDiagnosticRedactions
    $safe
}
function Write-WorkerPrivateExport($Bundle,[string]$InspectionRoot,[string]$NewsRoot) {
    Assert-WorkerInspectionPath $InspectionRoot $NewsRoot
    $destination=Join-Path $InspectionRoot ('worker-runtime-approved-'+[guid]::NewGuid().ToString('N')+'.json')
    $bytes=[Text.Encoding]::UTF8.GetBytes(($Bundle | ConvertTo-Json -Depth 18))
    $stream=[IO.File]::Open($destination,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$stream.Write($bytes,0,$bytes.Length)}finally{$stream.Dispose()}
    [ordered]@{status='EXPORTED_PRIVATE_REVIEW_REQUIRED';exportPath=$destination;
        sha256=(Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash;
        runtimeRows=@($Bundle.runtimeEntrypoints).Count;taskRows=@($Bundle.taskDetails).Count;metadataRedactions=$Bundle.metadataRedactions}
}
try{
    $helper=Join-Path $PSScriptRoot 'read-only-worker-diagnostics.ps1'
    $bundle=Get-WorkerRuntimeBundle -ExpectedServerAddress $ExpectedServerAddress -InspectionRoot $InspectionRoot -NewsRoot $NewsRoot -Helper $helper -Pm2Home $Pm2Home
    Write-WorkerPrivateExport $bundle $InspectionRoot $NewsRoot | ConvertTo-Json -Depth 3
}catch{throw 'Diagnostic workflow stopped. No application/service/task changes were requested. Review permissions, helper hash and private paths locally; do not export raw errors or credentials.'}
