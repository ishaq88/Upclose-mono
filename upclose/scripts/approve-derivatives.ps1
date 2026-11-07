# Operator helper: create + preview evidence derivatives for one session, then approve.
# Manual review gate: run without -Approve first, open every preview PNG, confirm plates,
# faces, people, addresses and reflections are covered, and only then rerun with -Approve.
# Usage: .\scripts\approve-derivatives.ps1 -SessionId <guid> [-Approve]
# OPERATOR_TOKEN comes from your shell env; it is never printed or stored.
param(
    [Parameter(Mandatory = $true)][string]$SessionId,
    [switch]$Approve
)
$ErrorActionPreference = 'Stop'
if (-not $env:OPERATOR_TOKEN) { throw 'Set OPERATOR_TOKEN in your shell first (same value as the API container).' }
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $previewDir = Join-Path $root '.tools\previews'
    New-Item -ItemType Directory -Force -Path $previewDir | Out-Null
    $rows = & docker compose -f infra/compose.yaml exec -T postgres psql -U upclose -d upclose -t -A -F '|' -c "SELECT rr.id, f.id FROM report_revisions rr JOIN reports r ON r.current_revision_id = rr.id JOIN findings f ON f.session_id = rr.session_id WHERE rr.session_id = '$SessionId' AND f.status IN ('verified','unverified')"
    if (-not $rows) { Write-Host 'No verified/unverified findings for this session.'; return }
    $headers = @{ 'X-Operator-Token' = $env:OPERATOR_TOKEN; 'Content-Type' = 'application/json' }
    foreach ($row in $rows) {
        $revisionId, $findingId = ($row -split '\|').Trim()
        $body = @{ session_id = $SessionId; revision_id = $revisionId; finding_id = $findingId; redactions = @() } | ConvertTo-Json -Compress
        $created = Invoke-RestMethod -Method Post -Uri 'http://localhost:8000/api/v1/operator/privacy/derivatives' -Headers $headers -Body $body
        $previewPath = Join-Path $previewDir "$findingId.png"
        Invoke-WebRequest -Uri "http://localhost:8000/api/v1/operator/privacy/derivatives/$($created.id)" -Headers @{ 'X-Operator-Token' = $env:OPERATOR_TOKEN } -OutFile $previewPath | Out-Null
        Write-Host "PREVIEW finding=$findingId file=$previewPath sha256=$($created.sha256)"
        if ($Approve) {
            $approveBody = @{ sha256 = $created.sha256; manual_review_receipt = 'permission-cleared-manually-reviewed-v1' } | ConvertTo-Json -Compress
            Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/v1/operator/privacy/derivatives/$($created.id)/approve" -Headers $headers -Body $approveBody | Out-Null
            Write-Host "APPROVED finding=$findingId"
        }
    }
    if (-not $Approve) { Write-Host 'Review every preview, then rerun with -Approve.' }
} finally {
    Pop-Location
}
