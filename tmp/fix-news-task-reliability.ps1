$ErrorActionPreference = 'Stop'
$server = '192.168.10.8'
$taskNames = @('NewsIntelligenceAPI', 'NewsIntelligenceWeb')
$backupDirectory = Join-Path $PSScriptRoot ('news-task-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backupDirectory | Out-Null
$namespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'

function Read-NewsTask([string]$name) {
    $output = & schtasks.exe /Query /S $server /TN $name /XML
    if ($LASTEXITCODE -ne 0) { throw "Cannot read $name" }
    return [xml]($output -join "`n")
}

function Set-TaskElement($document, $parent, [string]$name, [string]$value) {
    $element = $parent.ChildNodes | Where-Object LocalName -eq $name | Select-Object -First 1
    if (-not $element) {
        $element = $document.CreateElement($name, $namespace)
        [void]$parent.AppendChild($element)
    }
    $element.InnerText = $value
}

# Save both original definitions before changing either task.
foreach ($name in $taskNames) {
    $document = Read-NewsTask $name
    if ($document.Task.Principals.Principal.UserId -ne 'S-1-5-18') { throw "Unexpected task account: $name" }
    $expected = if ($name -eq 'NewsIntelligenceAPI') { '/c E:\websites\news-intelligence\start-api.cmd' } else { '/c E:\websites\news-intelligence\start-web.cmd' }
    if ($document.Task.Actions.Exec.Arguments -ne $expected) { throw "Unexpected task action: $name" }
    $document.Save((Join-Path $backupDirectory ($name + '.before.xml')))
    Set-TaskElement $document $document.Task.Settings 'ExecutionTimeLimit' 'PT0S'
    $restart = $document.Task.Settings.RestartOnFailure
    if (-not $restart) {
        $restart = $document.CreateElement('RestartOnFailure', $namespace)
        [void]$document.Task.Settings.AppendChild($restart)
    }
    Set-TaskElement $document $restart 'Interval' 'PT1M'
    Set-TaskElement $document $restart 'Count' '10'
    $document.Save((Join-Path $backupDirectory ($name + '.after.xml')))
}

foreach ($name in $taskNames) {
    & schtasks.exe /Create /S $server /TN $name /XML (Join-Path $backupDirectory ($name + '.after.xml')) /RU SYSTEM /F
    if ($LASTEXITCODE -ne 0) { throw "Cannot update $name; originals are in $backupDirectory" }
    $actual = Read-NewsTask $name
    if ($actual.Task.Settings.ExecutionTimeLimit -ne 'PT0S' -or
        $actual.Task.Settings.RestartOnFailure.Interval -ne 'PT1M' -or
        $actual.Task.Settings.RestartOnFailure.Count -ne '10') {
        throw "Settings verification failed: $name"
    }
    Write-Output "VERIFIED $name : unlimited runtime; retry every minute, up to 10 attempts."
}
Write-Output "Backup: $backupDirectory"
