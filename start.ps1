# Starts the bot and opens a chat with it.
# Run:  .\start.ps1

Set-Location $PSScriptRoot

Write-Host "Starting the bot..." -ForegroundColor Cyan
Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList "app.py" -WindowStyle Minimized
Start-Sleep -Seconds 6

Write-Host "Opening chat. Type 'quit' to leave." -ForegroundColor Cyan
.venv\Scripts\python.exe chat.py

Write-Host "Stopping the bot..." -ForegroundColor Cyan
Get-NetTCPConnection -LocalPort 3978 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    ForEach-Object { Stop-Process -Id $_ -Force }
Write-Host "Done." -ForegroundColor Green
