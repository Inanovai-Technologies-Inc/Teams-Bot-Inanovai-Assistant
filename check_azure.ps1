# Checks whether your Azure access is ready for the Teams bot.
# Run:  .\check_azure.ps1

$ErrorActionPreference = "Continue"
$env:Path += ";C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin"

function Line { Write-Host ("-" * 60) }
$ok = $true

Line
Write-Host " Checking your Azure access" -ForegroundColor Cyan
Line

# 1. Signed in?
$acct = az account show --output json 2>$null | ConvertFrom-Json
if (-not $acct) {
    Write-Host " NOT SIGNED IN" -ForegroundColor Red
    Write-Host "   Run this first:  az login"
    Line
    exit 1
}
$upn = $acct.user.name
Write-Host " Signed in as : $upn" -ForegroundColor Green

# 2. Subscription?
$subs = az account list --output json 2>$null | ConvertFrom-Json
if (-not $subs -or $subs.Count -eq 0) {
    Write-Host " Subscription : NONE  <-- blocked" -ForegroundColor Red
    $ok = $false
} else {
    Write-Host " Subscription : $($subs[0].name)" -ForegroundColor Green
}

# 3. Contributor role?
if ($ok) {
    $roles = az role assignment list --assignee $upn --all --output json 2>$null | ConvertFrom-Json
    $names = $roles | ForEach-Object { $_.roleDefinitionName } | Sort-Object -Unique
    if ($names -contains "Contributor" -or $names -contains "Owner") {
        Write-Host " Azure role   : $($names -join ', ')" -ForegroundColor Green
    } else {
        $shown = if ($names) { $names -join ', ' } else { "none" }
        Write-Host " Azure role   : $shown  <-- need Contributor" -ForegroundColor Red
        $ok = $false
    }
}

# 4. Can you actually create something? The only test that really counts.
if ($ok) {
    Write-Host " Live test    : creating resource group 'teams-bot-rg'..." -NoNewline
    $rg = az group create --name teams-bot-rg --location centralindia --output json 2>$null | ConvertFrom-Json
    if ($rg) {
        Write-Host " OK" -ForegroundColor Green
    } else {
        Write-Host " FAILED" -ForegroundColor Red
        Write-Host "   Contributor has not taken effect yet. Sign out and in, then retry."
        $ok = $false
    }
}

Line
if ($ok) {
    Write-Host " AZURE IS READY" -ForegroundColor Green
    Write-Host " Still to confirm by hand: Teams 'Upload an app' button."
} else {
    Write-Host " NOT READY YET" -ForegroundColor Yellow
    Write-Host " Ask Prashant to finish the steps, then run this again."
}
Line
