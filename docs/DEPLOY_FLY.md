# Deploying ERP Thaki backend to Fly.io

Free tier covers our use case: 3 always-on machines + 3GB persistent storage + 160GB egress per month. SSL certificates and custom domains are included free.

## One-time setup

### 1. Install flyctl

**Windows (PowerShell):**
```powershell
iwr https://fly.io/install.ps1 -useb | iex
```

Restart PowerShell after install. Verify:
```powershell
flyctl version
```

### 2. Sign up + login

```powershell
flyctl auth signup    # opens browser; sign up with GitHub/Google
flyctl auth login     # if already signed up
```

A credit card is required at signup but **will not be charged** while you stay within the free tier (3 small machines, 3GB volumes).

### 3. Launch the app

From the project's `backend/` directory:

```powershell
cd C:\dev\erp-thaki\backend
flyctl launch --no-deploy --copy-config
```

When prompted:
- **App name:** `erp-thaki` (or pick any unique name)
- **Region:** `fra` (Frankfurt) — closest to Iraq with good free-tier availability
- **Postgres / Redis / Sentry:** No (we don't need them for MVP)
- **Deploy now:** No (we'll set secrets first)

This generates a `fly.toml` (we already have one in the repo, the launch command will keep it).

### 4. Set secrets

Replace placeholders with your real keys:

```powershell
flyctl secrets set `
  SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" `
  ANTHROPIC_API_KEY="sk-ant-api03-YOUR-KEY" `
  GROQ_API_KEY="gsk_YOUR-KEY" `
  GEMINI_API_KEY="AIzaYour-Key" `
  ALLOWED_ORIGINS="https://erp.honey-bird.net,http://localhost:8080" `
  DEFAULT_AI_PROVIDER="groq"
```

(One command, line-continuation with backticks. PowerShell.)

Verify they're set:
```powershell
flyctl secrets list
```

### 5. Deploy

```powershell
flyctl deploy
```

Takes ~3-5 minutes. Watch the build logs. When done, get the URL:

```powershell
flyctl status
flyctl open
```

You should see your `{"name":"ERP الذكي", "version":"0.1.0", "status":"running"}` JSON at `https://erp-thaki.fly.dev`.

### 6. Custom domain (erp.honey-bird.net)

Get the public IP:

```powershell
flyctl ips list
```

In your DNS provider for honey-bird.net, add:
- **Type:** A
- **Name:** `erp`
- **Value:** `<the IPv4 address from above>`
- **TTL:** 300 (or default)

Optional — also add an AAAA record with the IPv6 address.

Wait ~5 minutes for DNS to propagate, then issue the SSL cert:

```powershell
flyctl certs create erp.honey-bird.net
flyctl certs show erp.honey-bird.net
```

Within 1-2 minutes the certificate auto-provisions via Let's Encrypt. Test:

```powershell
curl https://erp.honey-bird.net/api/v1/health
```

### 7. Update Flutter app to use production URL

In your phone's Flutter app login screen, replace `http://192.168.1.x:8000` with `https://erp.honey-bird.net`. That's it — same flow.

## Daily workflow after setup

To deploy a new code change:
```powershell
cd C:\dev\erp-thaki\backend
flyctl deploy
```

To see live logs:
```powershell
flyctl logs
```

To SSH into the running container:
```powershell
flyctl ssh console
```

To rotate a secret without redeploying code:
```powershell
flyctl secrets set ANTHROPIC_API_KEY="sk-ant-new-key"
# Triggers a rolling restart automatically
```

## Cost monitoring

```powershell
flyctl billing balance
flyctl billing show
```

Free tier resets monthly. As long as you're below 3 machines × 256MB (we use 512MB on 1 machine), and below 160GB egress, you stay free. Realistic monthly bandwidth for ERP Thaki at 5 customers: ~5GB.

## Upgrading to paid Hetzner later

When you sign your first paying customer and want a real server:

1. Spin up a Hetzner CX22 (€4/mo, 4GB RAM, plenty of headroom)
2. Run the same Docker image:
   ```bash
   docker run -d --restart=always --name erp-thaki -p 8000:8000 \
     --env-file .env \
     ghcr.io/your-org/erp-thaki:latest
   ```
3. Use Caddy or nginx + Let's Encrypt for SSL
4. Update DNS to point at the new IP

The Dockerfile is identical, so you can switch hosts in 30 minutes.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Build failed` during deploy | Check `flyctl logs` for the actual Python error |
| `App is not responding` | Probably a startup crash — `flyctl logs --no-tail` to see traceback |
| `SSL not working` | Wait 5 min. If still nothing, check DNS with `dig erp.honey-bird.net` |
| `flyctl deploy` slow first time | Normal — it's building from scratch. Subsequent deploys are ~30s |
| Phone can't reach the URL | Make sure HTTPS is reachable from cellular (not just Wi-Fi). Run `https://erp.honey-bird.net/api/v1/health` from a different network |
