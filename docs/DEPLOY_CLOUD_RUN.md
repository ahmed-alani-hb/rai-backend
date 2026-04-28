# Deploying ERP Thaki backend to Google Cloud Run (FREE)

Cloud Run's free tier is the most generous option for our use case:
- **2 million requests/month** free
- **360,000 vCPU-seconds/month** free (≈100K typical requests)
- **180,000 GiB-seconds** memory free
- **Custom domains** with auto-managed SSL — free
- **No cold-start penalty for users** — first response in ~1-2s, subsequent are instant

For ERP Thaki at our scale (5-50 customers), monthly cost is genuinely **$0**.

## One-time setup

### 1. Install gcloud CLI

**Windows (PowerShell as admin):**
```powershell
(New-Object Net.WebClient).DownloadFile("https://dl.google.com/dl/cloudsdk/channels/rapid/GoogleCloudSDKInstaller.exe", "$env:Temp\GoogleCloudSDKInstaller.exe")
& $env:Temp\GoogleCloudSDKInstaller.exe
```

After install, restart PowerShell. Verify:
```powershell
gcloud --version
```

### 2. Create a Google Cloud project

```powershell
# Login (opens browser)
gcloud auth login

# Create the project (name it whatever)
gcloud projects create erp-thaki --name="ERP Thaki"

# Set it as default
gcloud config set project erp-thaki

# Enable Cloud Run + Cloud Build APIs
gcloud services enable run.googleapis.com cloudbuild.googleapis.com
```

You **must** enable billing on the project (Google requires a credit card even for the free tier — they bill $0 unless you exceed the free quotas, which won't happen at our scale). Visit:
https://console.cloud.google.com/billing/linkedaccount?project=erp-thaki

### 3. Set environment variables / secrets

Cloud Run uses Secret Manager for sensitive values:

```powershell
gcloud services enable secretmanager.googleapis.com

# Create each secret
echo "$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" | gcloud secrets create erp-secret-key --data-file=-
echo "sk-ant-api03-YOUR-KEY" | gcloud secrets create erp-anthropic-key --data-file=-
echo "gsk_YOUR-KEY" | gcloud secrets create erp-groq-key --data-file=-
echo "AIzaYour-Key" | gcloud secrets create erp-gemini-key --data-file=-
```

### 4. Build and deploy

From `backend/`:

```powershell
cd C:\dev\erp-thaki\backend

gcloud run deploy erp-thaki `
  --source . `
  --region europe-west1 `
  --allow-unauthenticated `
  --memory 512Mi `
  --cpu 1 `
  --min-instances 0 `
  --max-instances 5 `
  --timeout 300 `
  --port 8000 `
  --set-env-vars "APP_ENV=production,DEFAULT_AI_PROVIDER=groq,ALLOWED_ORIGINS=https://erp.honey-bird.net,http://localhost:8080" `
  --set-secrets "SECRET_KEY=erp-secret-key:latest,ANTHROPIC_API_KEY=erp-anthropic-key:latest,GROQ_API_KEY=erp-groq-key:latest,GEMINI_API_KEY=erp-gemini-key:latest"
```

**Region note:** `europe-west1` is **Frankfurt** — well-connected to Iraq with strong free-tier availability. Alternatives: `europe-west1` (Belgium), `me-central2` (Doha — closest geographically but smaller free quota).

First deploy takes 3-5 minutes. Subsequent deploys are ~30s.

When done you'll get a URL like:
```
https://erp-thaki-xxxxxxxxxx-uc.a.run.app
```

Test it:
```powershell
curl https://erp-thaki-xxxxxxxxxx-uc.a.run.app/api/v1/health
# {"status":"ok"}
```

### 5. Custom domain (erp.honey-bird.net)

```powershell
# Verify domain ownership first (one-time per Google account)
# Visit https://www.google.com/webmasters/verification and verify honey-bird.net

# Map the domain
gcloud run domain-mappings create --service erp-thaki --domain erp.honey-bird.net --region europe-west1

# Get the DNS records you need to add
gcloud run domain-mappings describe --domain erp.honey-bird.net --region europe-west1
```

The output shows DNS records (usually a CNAME). Add it at your DNS provider.

SSL provisions automatically within 15-60 minutes. Test:
```powershell
curl https://erp.honey-bird.net/api/v1/health
```

## Daily workflow

Deploy a code change:
```powershell
cd C:\dev\erp-thaki\backend
gcloud run deploy erp-thaki --source . --region europe-west1
```

View live logs:
```powershell
gcloud run services logs tail erp-thaki --region europe-west1
```

Update a secret without redeploy:
```powershell
echo "new-value" | gcloud secrets versions add erp-anthropic-key --data-file=-
# Then trigger a new revision
gcloud run services update erp-thaki --region europe-west1
```

## Cost monitoring

Cloud Console → Billing → "Reports" filtered by Cloud Run.

**Realistic costs at different scales** (assuming groq/gemini for most queries):

| Active users | Monthly Cloud Run cost |
|--------------|----------------------|
| 1-5 (testing) | $0 |
| 5-50 customers | $0 |
| 50-200 customers | $0-3 |
| 200-1000 customers | $5-15 |

You stay free until you have real revenue.

## Why min-instances=0 is fine

Cloud Run's "scale to zero" means there's a ~1-2s cold start when the app sits unused for >15 minutes. For a chat app, this is fine — first message takes 2s, rest are instant. If you do live demos and want zero cold start:

```powershell
# Scale up minimum (costs ~$5/mo per always-on instance)
gcloud run services update erp-thaki --min-instances 1 --region europe-west1
```

But honestly, the cold start is invisible in normal use because users open the app, type a question (3-5s), then send — which warms up the instance before they need a response.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Deploy fails with "billing not enabled" | Visit Cloud Console → Billing → link a card. Free tier still applies |
| `403 Forbidden` from the URL | Add `--allow-unauthenticated` to your deploy command |
| `Service unavailable` 503 | Check `gcloud run services logs tail erp-thaki --region europe-west1` for app errors |
| SSL not provisioning on custom domain | Wait 60 min. Verify DNS with `dig erp.honey-bird.net CNAME` |
| Slow first response | Normal cold start — set `--min-instances 1` if it bothers users |
