# Start TeamBots on a Windows laptop with Docker Desktop. Run from this folder:  .\start.ps1
# Uses docker-compose.local.yml (laptop / Docker Desktop). On a VPS see docs/deploy-vps.md.
$ErrorActionPreference = "Stop"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker Desktop is not installed or not on PATH." }
if (-not (Test-Path .env)) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $key = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    "TB_SECRET_KEY=$key" | Out-File -Encoding ascii .env
    Write-Host "Created .env with a new TB_SECRET_KEY. Keep this file: it decrypts your stored keys and secrets."
}
docker compose -f docker-compose.local.yml up -d --build
Write-Host "TeamBots runs at http://localhost:8080"
Write-Host "A model server on this laptop is reachable from the app as http://host.docker.internal:<port>/v1"
