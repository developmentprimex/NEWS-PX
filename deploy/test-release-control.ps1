$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'release-control.ps1')
function Assert-Test($value, $message) { if (-not $value) { throw $message } }
function Assert-Rejected([scriptblock]$Action) {
    $rejected = $false
    try { & $Action } catch { $rejected = $true }
    Assert-Test $rejected 'Unsafe release was accepted.'
}
$now = [datetime]::UtcNow
$sha = 'a' * 40
$permit = @{commitSha=$sha; approvalRef='LOCAL-TEST'; approvedBy='fixture'; expiresUtc=$now.AddHours(1).ToString('o'); evidence=@{}}
$gates = @('runtimeInventoryVerified','freshBackupRehearsalVerified','restartPathsHeld','accountConsumersIsolated','pausedRollbackReviewed','workflowApprovalVerified')
foreach ($gate in $gates) { $permit[$gate]=$true; $permit.evidence[$gate]='fixture-evidence' }
Assert-NewsReleasePermit $permit $sha $now
Assert-Rejected { Assert-NewsReleasePermit $permit ('b' * 40) $now }
foreach ($gate in $gates) {
    $permit[$gate]=$false
    Assert-Rejected { Assert-NewsReleasePermit $permit $sha $now }
    $permit[$gate]=$true
}
$permit.expiresUtc=$now.AddMinutes(-1).ToString('o')
Assert-Rejected { Assert-NewsReleasePermit $permit $sha $now }
$temp = Join-Path $PSScriptRoot ('.release-test-' + [guid]::NewGuid().ToString('N') + '.env')
try {
    $paused = @('NEWS_ENABLE_COLLECTION=false','NEWS_LINKEDIN_FETCH_ENABLED=false','NEWS_BRIGHTDATA_CANARY_LIVE_ENABLED=false','NEWS_AUTO_IMPORT_SNAPSHOT=false','BRIGHTDATA_API_TOKEN=')
    Set-Content -LiteralPath $temp -Value $paused
    Set-Content -LiteralPath $temp -Value ($paused[0..3] + @('BRIGHTDATA_API_TOKEN="','UNRELATED=synthetic-secret','#"'))
    Assert-Rejected { Assert-NewsPausedConfiguration $temp @{} }
    Set-Content -LiteralPath $temp -Value $paused
    Assert-NewsPausedConfiguration $temp @{}
    Set-Content -LiteralPath $temp -Value ($paused + @('export NEWS_ENABLE_COLLECTION=true','export BRIGHTDATA_API_TOKEN=synthetic-secret'))
    Assert-Rejected { Assert-NewsPausedConfiguration $temp @{} }
    Set-Content -LiteralPath $temp -Value $paused
    Assert-Rejected { Assert-NewsPausedConfiguration $temp @{'Machine/NEWS_ENABLE_COLLECTION'='true'} }
    Assert-Rejected { Assert-NewsPausedConfiguration $temp @{'User/BRIGHTDATA_API_TOKEN'='synthetic-secret'} }
    Set-Content -LiteralPath $temp -Value ($paused + 'BRIGHTDATA_API_TOKEN=synthetic-secret')
    Assert-Rejected { Assert-NewsPausedConfiguration $temp @{} }
    Set-Content -LiteralPath $temp -Value 'NEWS_ENABLE_COLLECTION=true'
    Assert-Rejected { Assert-NewsPausedConfiguration $temp @{} }
    # Execute the actual sync entrypoint: it must not fetch, deploy or start cleanup.
    function Invoke-RestMethod { throw 'Sync attempted network access.' }
    function Start-Process { throw 'Sync attempted a background process.' }
    function git { throw 'Sync attempted Git mutation.' }
    & (Join-Path $PSScriptRoot 'sync.ps1')
    Write-Output 'PASS: SHA/approval/evidence/expiry gates, paused environment, inherited credentials, sync isolation.'
} finally { Remove-Item -LiteralPath $temp -ErrorAction SilentlyContinue }
