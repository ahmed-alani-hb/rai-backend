# ERP Thaki - Backend launcher for Windows
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Definition)

Write-Host ""
Write-Host "ERP Thaki - Backend launcher" -ForegroundColor Cyan
Write-Host ""

if ($env:VIRTUAL_ENV) {
    Write-Host "[INFO] Active venv detected. Close this PowerShell and open a fresh one for clean state." -ForegroundColor Yellow
    Write-Host ""
}

# 1) Find Python 3.10-3.13 (avoid 3.14, no wheels for some packages)
$pythonCmd = $null
$pythonVer = $null
foreach ($v in @("3.12","3.13","3.11","3.10")) {
    $out = (cmd /c "py -$v --version 2>&1") | Out-String
    if ($out.Trim() -match "Python\s+3\.\d+\.\d+") {
        $pythonCmd = "py -$v"
        $pythonVer = $Matches[0]
        break
    }
}

if (-not $pythonCmd) {
    Write-Host "[FAIL] Python 3.10-3.13 not found." -ForegroundColor Red
    Write-Host "Install with: py install 3.12" -ForegroundColor Yellow
    exit 1
}

Write-Host "[OK] Found $pythonVer (using: $pythonCmd)" -ForegroundColor Green

# 2) Virtual env
if (-not (Test-Path ".venv\Scripts\Activate.ps1")) {
    if (Test-Path ".venv") { Remove-Item -Recurse -Force .venv }
    Write-Host "Creating venv..." -ForegroundColor Yellow
    cmd /c "$pythonCmd -m venv .venv" | Out-Null
}

# Verify venv python is right version
$vpy = (cmd /c ".\.venv\Scripts\python.exe --version 2>&1") | Out-String
if ($vpy.Trim() -notmatch "Python\s+3\.(1[0-3])") {
    Write-Host "[WARN] Existing .venv has wrong Python ($($vpy.Trim())). Recreating..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force .venv
    cmd /c "$pythonCmd -m venv .venv" | Out-Null
}

& .\.venv\Scripts\Activate.ps1
Write-Host "[OK] venv activated" -ForegroundColor Green

# 3) Install requirements
$h = (Get-FileHash requirements.txt).Hash
$cached = if (Test-Path .venv\.req_hash) { Get-Content .venv\.req_hash } else { "" }
if ($h -ne $cached) {
    Write-Host "Installing requirements..." -ForegroundColor Yellow
    python -m pip install --upgrade pip 2>&1 | Out-Null
    python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] pip install failed." -ForegroundColor Red
        exit 1
    }
    Set-Content .venv\.req_hash $h
    Write-Host "[OK] requirements installed" -ForegroundColor Green
}

# 4) .env
if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    $charset = (48..57) + (65..90) + (97..122)
    $secret = -join (1..48 | ForEach-Object { [char]($charset | Get-Random) })
    (Get-Content .env) -replace 'SECRET_KEY=.*', "SECRET_KEY=$secret" | Set-Content .env
    Write-Host ""
    Write-Host "[INFO] .env created. Edit it: notepad .env" -ForegroundColor Yellow
    Write-Host "       Add: ANTHROPIC_API_KEY=sk-ant-..." -ForegroundColor Yellow
    Write-Host "Then run this script again." -ForegroundColor Cyan
    exit 0
}

# 5) Start server
Write-Host ""
Write-Host "Starting server at http://127.0.0.1:8000" -ForegroundColor Cyan
Write-Host "Stop: Ctrl+C"
Write-Host ""
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
