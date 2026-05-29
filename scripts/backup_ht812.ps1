# Trigger HT812V2 config backup via the ht812_api service.
# Saves a timestamped XML file to ..\backups\ and prints the filename.

param (
    [string]$ApiBase   = $env:HT812_API_URL ?? "http://localhost:8000",
    [string]$BackupDir = $env:BACKUP_DIR    ?? (Join-Path $PSScriptRoot "..\backups")
)

$BackupDir = [IO.Path]::GetFullPath($BackupDir)

Write-Host "Triggering HT812 config backup via $ApiBase/ht812/config ..."

try {
    $response = Invoke-WebRequest `
        -Uri "$ApiBase/ht812/config" `
        -Method GET `
        -Headers @{ Accept = "application/xml" } `
        -ErrorAction Stop
} catch {
    Write-Error "API request failed: $_"
    exit 1
}

if ($response.StatusCode -ne 200) {
    Write-Error "API returned HTTP $($response.StatusCode)"
    exit 1
}

if (-not (Test-Path $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir | Out-Null
}

$timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd'T'HHmmss'Z'")
$localFile = Join-Path $BackupDir "ht812_config_$timestamp.xml"

[IO.File]::WriteAllBytes($localFile, $response.Content)

Write-Host "Saved: $localFile"
Write-Host "Size:  $($response.Content.Length) bytes"
