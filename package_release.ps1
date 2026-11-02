$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$zipPath = Join-Path $PSScriptRoot "grant_crawler_release.zip"
if (Test-Path $zipPath) {
    Remove-Item $zipPath -Force
}

$filesToInclude = @(
    "app.py",
    "crawler.py",
    "database.py",
    "gemini_agent.py",
    "notifier.py",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    "Procfile",
    "railway.json",
    "render.yaml",
    ".gitignore",
    ".dockerignore",
    ".env.example",
    "start.ps1",
    "stop.ps1",
    "run.bat",
    "stop.bat",
    "README.md",
    "static"
)

Compress-Archive -Path $filesToInclude -DestinationPath $zipPath -Force
Write-Host "Created clean, secret-free release package at: $zipPath" -ForegroundColor Green
