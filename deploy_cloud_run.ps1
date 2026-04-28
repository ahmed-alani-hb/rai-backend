# Deploy ERP Thaki backend to Google Cloud Run.
# Run from backend/: .\deploy_cloud_run.ps1
#
# Prerequisites: gcloud CLI installed, project + secrets configured.
# See docs/DEPLOY_CLOUD_RUN.md for first-time setup.

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

Write-Host ""
Write-Host "Deploying ERP Thaki to Cloud Run (Netherlands)..." -ForegroundColor Cyan
Write-Host ""

# ^@^ at the start of --set-env-vars switches the delimiter from comma to @.
# Required because ALLOWED_ORIGINS contains commas inside the value.
gcloud run deploy erp-thaki `
  --source . `
  --region europe-west4 `
  --allow-unauthenticated `
  --memory 512Mi `
  --cpu 1 `
  --min-instances 0 `
  --max-instances 5 `
  --timeout 300 `
  --port 8000 `
  --set-env-vars "^@^APP_ENV=production@DEFAULT_AI_PROVIDER=groq@ALLOWED_ORIGINS=https://erp.honey-bird.net,http://localhost:8080" `
  --set-secrets "SECRET_KEY=erp-secret-key:latest,ANTHROPIC_API_KEY=erp-anthropic-key:latest,GROQ_API_KEY=erp-groq-key:latest,GEMINI_API_KEY=erp-gemini-key:latest"

if ($LASTEXITCODE -ne 0) {
    Write-Host "Deploy failed" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Deploy complete." -ForegroundColor Green
Write-Host ""

# Show the live URL
$URL = gcloud run services describe erp-thaki --region europe-west4 --format "value(status.url)"
Write-Host "Service URL: $URL" -ForegroundColor Cyan

Write-Host ""
Write-Host "Quick test:" -ForegroundColor Yellow
Write-Host "  curl $URL/api/v1/health" -ForegroundColor Yellow
Write-Host "  curl $URL/api/v1/config" -ForegroundColor Yellow
