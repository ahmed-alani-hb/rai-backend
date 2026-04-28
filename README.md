# RAI — backend

FastAPI middleware between the RAI mobile app and ERPNext / Odoo. Handles
auth (username/password → API key), AI provider routing (Claude / Groq
Llama 3.3 / Gemini / OpenAI), tool-calling for ERP queries, and
server-side Arabic voice transcription via Groq Whisper.

## Stack

- Python 3.12, FastAPI, uvicorn
- Anthropic, OpenAI (also serves as the Groq client), google-genai SDKs
- ERPNext REST API (Frappe v15+)
- Deployed on Google Cloud Run, Netherlands region

## Local dev

```bash
cd backend
python -m venv .venv && source .venv/bin/activate     # or .\.venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt
cp .env.example .env
# Fill in the API keys you want to use (only one is required)
uvicorn app.main:app --reload --port 8000
```

Quick health check: `curl http://localhost:8000/api/v1/health`

## Deploy

`docs/DEPLOY_CLOUD_RUN.md` walks through the Google Cloud Run setup
(custom domain, Secret Manager, deploy script).

```powershell
# Windows: assumes gcloud CLI installed and authenticated
.\deploy_cloud_run.ps1
```

## Repo layout

```
app/
├── api/         # FastAPI routes — chat, dashboard, voice, health, debug
├── core/        # Settings (pydantic-settings) + auth deps
├── models/      # Pydantic schemas shared with the Flutter client
├── prompts/     # System prompts in Arabic, Sorani Kurdish, English
└── services/    # AI router, ERPNext client, tools, cache, business context
```

## Companion repo

The Flutter mobile app lives in **rai-app** (separate repo so iOS builds
on Codemagic don't redeploy the backend on every commit).
