param(
    [string]$RepoUrl = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $RepoUrl) {
    $RepoUrl = Read-Host "Paste your new GitHub repository URL (e.g. https://github.com/username/grant-crawler.git)"
}

if (-not $RepoUrl.Trim()) {
    Write-Host "No repository URL provided. Exiting." -ForegroundColor Red
    exit 1
}

$RepoUrl = $RepoUrl.Trim()

# Configure remote origin
$remotes = git remote
if ($remotes -contains "origin") {
    git remote set-url origin $RepoUrl
} else {
    git remote add origin $RepoUrl
}

Write-Host "Pushing 'main' branch to $RepoUrl ..." -ForegroundColor Cyan
git branch -M main
git push -u origin main

Write-Host "Successfully pushed to GitHub: $RepoUrl" -ForegroundColor Green
