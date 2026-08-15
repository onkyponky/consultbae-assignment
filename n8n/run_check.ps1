<#
    Posts a CSV to the n8n duplicate-check webhook and prints the alert in a
    readable form instead of raw JSON.

    The n8n workflow must be running and armed (click "Execute workflow" in
    the editor first - the test webhook listens for exactly one request).

        .\n8n\run_check.ps1
        .\n8n\run_check.ps1 -Csv n8n\test_incoming.csv
#>

param(
    [string]$Csv = "n8n\incoming_batch.csv",
    [string]$Webhook = "http://localhost:5678/webhook-test/duplicate-check"
)

if (-not (Test-Path $Csv)) {
    Write-Host "  Cannot find $Csv" -ForegroundColor Red
    exit 1
}

$rows = (Import-Csv $Csv | Measure-Object).Count
Write-Host ""
Write-Host "  Sending $rows rows from $Csv" -ForegroundColor DarkGray

# curl.exe does the multipart upload. Windows PowerShell 5.1 has no -Form.
$raw = curl.exe -s -F "file=@$Csv" $Webhook

if (-not $raw) {
    Write-Host ""
    Write-Host "  No response from n8n." -ForegroundColor Red
    Write-Host "  Click 'Execute workflow' in the editor, then run this again." -ForegroundColor DarkGray
    Write-Host ""
    exit 1
}

try { $r = $raw | ConvertFrom-Json } catch {
    Write-Host "  n8n did not return JSON:" -ForegroundColor Red
    Write-Host "  $raw"
    exit 1
}

# n8n answers an unarmed test webhook with a valid JSON error, which parses
# fine and then produces a table full of blanks. Catch it explicitly.
if ($null -eq $r.alert) {
    Write-Host ""
    if ($r.message) {
        Write-Host "  n8n says: $($r.message)" -ForegroundColor Red
        if ($r.hint) { Write-Host "  $($r.hint)" -ForegroundColor DarkGray }
    } else {
        Write-Host "  Unexpected response from n8n:" -ForegroundColor Red
        Write-Host "  $raw"
    }
    Write-Host ""
    exit 1
}

Write-Host ""
Write-Host "  DUPLICATE CHECK against the merged database" -ForegroundColor Cyan
Write-Host "  ---------------------------------------------------------------"
Write-Host "  $($r.alert)" -ForegroundColor Yellow
Write-Host "  checked $($r.checked)    already known $($r.duplicates)    new $($r.new)"
Write-Host ""

if ($r.duplicates -gt 0) {
    $r.alerts | ForEach-Object {
        [pscustomobject]@{
            'Incoming name' = $_.name
            'Sent as'       = if ($_.submitted_phone) { $_.submitted_phone } else { $_.submitted_email }
            'Matched on'    = $_.matched_on
            'Already in DB' = "#$($_.collides_with.id) $($_.collides_with.full_name)"
        }
    } | Format-Table -AutoSize
}

if ($r.new -gt 0) {
    Write-Host "  Not in the database - these would be new people:" -ForegroundColor Green
    $r.new_people | ForEach-Object {
        $key = if ($_.phone) { $_.phone } else { $_.email }
        Write-Host "     $($_.name.PadRight(18)) $key"
    }
    Write-Host ""
}
