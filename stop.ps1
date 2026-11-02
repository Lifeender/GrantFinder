$ErrorActionPreference = "SilentlyContinue"
$connections = Get-NetTCPConnection -LocalPort 8050 -State Listen
if ($connections) {
    foreach ($c in $connections) {
        if ($c.OwningProcess -gt 0) {
            Stop-Process -Id $c.OwningProcess -Force
            Write-Host "Stopped Grant Crawler server (PID $($c.OwningProcess)) on port 8050." -ForegroundColor Green
        }
    }
} else {
    Write-Host "No Grant Crawler server is currently running on port 8050." -ForegroundColor Yellow
}
