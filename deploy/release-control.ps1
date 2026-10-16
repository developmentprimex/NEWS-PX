# Functions only. Importing this file does not deploy, stop tasks or contact APIs.
function Assert-NewsReleasePermit($Permit, [string]$ExpectedSha, [datetime]$Now = [datetime]::UtcNow) {
    if ($ExpectedSha -notmatch '^[0-9a-f]{40}$' -or $Permit.commitSha -cne $ExpectedSha) { throw 'Release permit must match the exact reviewed commit.' }
    if (-not $Permit.approvalRef -or -not $Permit.approvedBy) { throw 'Explicit release approval is required.' }
    $expiry = [datetime]::Parse($Permit.expiresUtc).ToUniversalTime()
    if ($expiry -le $Now -or $expiry -gt $Now.AddHours(24)) { throw 'Release permit is expired or exceeds 24 hours.' }
    foreach ($gate in @('runtimeInventoryVerified', 'freshBackupRehearsalVerified', 'restartPathsHeld', 'accountConsumersIsolated', 'pausedRollbackReviewed', 'workflowApprovalVerified')) {
        if ($Permit.$gate -isnot [bool] -or $Permit.$gate -ne $true -or -not $Permit.evidence.$gate) { throw "Release evidence missing: $gate" }
    }
}

function Assert-NewsPausedConfiguration([string]$EnvFile, [hashtable]$Inherited) {
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        if ($line -match '^\s*(#.*)?$') { continue }
        # Fail closed rather than approximate python-dotenv's export, multiline,
        # interpolation or duplicate override semantics. Never print line contents.
        if ($line -notmatch '^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$') { throw 'Unsupported dotenv syntax requires private release review.' }
        $key = $matches[1].ToUpperInvariant()
        $value = $matches[2]
        if (($value.StartsWith("'") -and $value -notmatch "^'[^']*'\s*(?:#.*)?$") -or
            ($value.StartsWith('"') -and $value -notmatch '^"(?:[^"\\]|\\.)*"\s*(?:#.*)?$')) {
            throw 'Multiline or unmatched dotenv quotes require private release review.'
        }
        if ($key -in @('NEWS_ENABLE_COLLECTION','NEWS_LINKEDIN_FETCH_ENABLED','NEWS_BRIGHTDATA_CANARY_LIVE_ENABLED','NEWS_AUTO_IMPORT_SNAPSHOT','BRIGHTDATA_API_TOKEN') -and $values.ContainsKey($key)) { throw 'Repeated collection setting requires private release review.' }
        $values[$key] = $value.Trim('"', "'")
    }
    foreach ($name in @('NEWS_ENABLE_COLLECTION', 'NEWS_LINKEDIN_FETCH_ENABLED', 'NEWS_BRIGHTDATA_CANARY_LIVE_ENABLED', 'NEWS_AUTO_IMPORT_SNAPSHOT')) {
        if ($values[$name] -cne 'false') { throw "Paused release requires explicit false: $name" }
        foreach ($scope in @('Process', 'User', 'Machine')) {
            $value = $Inherited["$scope/$name"]
            if ($value -and $value -cne 'false') { throw "Conflicting inherited pause setting: $scope/$name" }
        }
    }
    if ($values['BRIGHTDATA_API_TOKEN']) { throw 'Paused candidate and legacy rollback require a credential-free application environment.' }
    foreach ($scope in @('Process', 'User', 'Machine')) {
        if ($Inherited["$scope/BRIGHTDATA_API_TOKEN"]) { throw 'Inherited provider credentials prevent a paused release.' }
    }
}

function Get-NewsInheritedCollectionSettings {
    $values = @{}
    foreach ($scope in @('Process', 'User', 'Machine')) {
        foreach ($name in @('NEWS_ENABLE_COLLECTION', 'NEWS_LINKEDIN_FETCH_ENABLED', 'NEWS_BRIGHTDATA_CANARY_LIVE_ENABLED', 'NEWS_AUTO_IMPORT_SNAPSHOT', 'BRIGHTDATA_API_TOKEN')) {
            $values["$scope/$name"] = [Environment]::GetEnvironmentVariable($name, $scope)
        }
    }
    return $values
}
