$ErrorActionPreference = "SilentlyContinue"
Set-Location $PSScriptRoot

Write-Host "Checking if port 8050 is already in use..." -ForegroundColor Cyan
$existing = Get-NetTCPConnection -LocalPort 8050 -State Listen
if ($existing) {
    foreach ($conn in $existing) {
        if ($conn.OwningProcess -gt 0) {
            Write-Host "Stopping existing server process (PID $($conn.OwningProcess))..." -ForegroundColor Yellow
            Stop-Process -Id $conn.OwningProcess -Force
        }
    }
    Start-Sleep -Seconds 1
}

$pythonExe = "python"
$localPy = "C:\Users\clayt\AppData\Local\Programs\Python\Python313\python.exe"
if (Test-Path $localPy) {
    $pythonExe = $localPy
}

Write-Host "Starting AI Grant Interviewer & Web Crawler on http://127.0.0.1:8050 ..." -ForegroundColor Green
Write-Host "Press Ctrl+C in this window (or click 'Shut Off Server' in the browser) to stop." -ForegroundColor Gray

Start-Process "http://127.0.0.1:8050"
& $pythonExe -m uvicorn app:app --host 127.0.0.1 --port 8050 --reload
