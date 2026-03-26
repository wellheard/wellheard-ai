# WellHeard AI — Production Deployment Guide

**Goal:** Get WellHeard AI live on the internet with the same (or better) latency as local, ready for voice AI integration.

---

## The #1 Rule: Co-Location

The single most important thing for voice AI latency is **putting your server in the same region as your providers**. Every region hop adds 30-70ms. With 3 providers in the pipeline (STT, LLM, TTS), that's 90-210ms of avoidable latency.

**All our providers have infrastructure in US-East:**

| Provider | Location | Latency from US-East |
|---|---|---|
| Deepgram (STT) | AWS us-east-1 | <10ms |
| Groq (LLM) | Dallas, TX (Equinix) | ~15ms |
| Cartesia (TTS) | North America | <20ms |
| Telnyx (SIP) | Global PoPs, auto-routes | <30ms |

**Deploy in US-East (Virginia/Ashburn).** This is non-negotiable for latency.

---

## 3 Deployment Options (Pick One)

### Option A: Fly.io (Recommended — Fastest to Production)

**Cost:** ~$15-40/month infrastructure
**Time to deploy:** 15 minutes
**Best for:** Getting live fast, auto-scaling, zero ops

```bash
# 1. Install Fly CLI
curl -L https://fly.io/install.sh | sh

# 2. Sign up & authenticate
flyctl auth signup

# 3. From the wellheard-ai directory:
cd wellheard-ai

# 4. Set your API keys as secrets
flyctl secrets set \
  HV_API_KEY="your-secure-api-key" \
  HV_DEEPGRAM_API_KEY="your-deepgram-key" \
  HV_GROQ_API_KEY="your-groq-key" \
  HV_GOOGLE_API_KEY="your-google-key" \
  HV_CARTESIA_API_KEY="your-cartesia-key" \
  HV_TELNYX_API_KEY="your-telnyx-key" \
  HV_TELNYX_CONNECTION_ID="your-connection-id" \
  HV_TELNYX_PHONE_NUMBER="+1234567890"

# 5. Deploy (fly.toml is already configured)
flyctl deploy

# 6. Check it's running
flyctl status
curl https://wellheard-ai.fly.dev/v1/health
```

**That's it.** Your API is live at `https://wellheard-ai.fly.dev/v1/`.

**Scaling:**
```bash
# Scale to 3 machines for more concurrent calls
flyctl scale count 3

# Check logs
flyctl logs
```

---

### Option B: Any VPS (Hetzner, DigitalOcean, AWS EC2)

**Cost:** $20-60/month (Hetzner cheapest at ~$20)
**Time to deploy:** 30 minutes
**Best for:** Full control, cheapest long-term, existing infrastructure

```bash
# 1. Get a VPS in US-East
#    Hetzner: Ashburn, VA — CX22 ($4.49/mo) or CX32 ($8.49/mo)
#    DigitalOcean: NYC1 — $24/mo Droplet
#    AWS: us-east-1 — t3.medium ($30/mo)

# 2. SSH into your server
ssh root@your-server-ip

# 3. Install Docker
curl -fsSL https://get.docker.com | sh

# 4. Clone the repo
git clone <your-repo>/wellheard-ai
cd wellheard-ai

# 5. Configure
cp config/.env.example config/.env
nano config/.env  # Fill in all API keys

# 6. Create SSL certs (free via Let's Encrypt)
mkdir -p config/certs
apt install certbot -y
certbot certonly --standalone -d your-domain.com
cp /etc/letsencrypt/live/your-domain.com/fullchain.pem config/certs/
cp /etc/letsencrypt/live/your-domain.com/privkey.pem config/certs/

# 7. Launch
docker compose -f docker-compose.prod.yml up -d

# 8. Verify
curl https://your-domain.com/v1/health
```

**Skip SSL for testing:** If you just want to test without a domain, comment out the nginx service in docker-compose.prod.yml and access port 8000 directly.

---

### Option C: Pipecat Cloud (Zero Ops)

**Cost:** Contact Daily.co for pricing
**Time to deploy:** 10 minutes
**Best for:** Teams that don't want to manage any infrastructure

Pipecat Cloud (by Daily.co) is the managed hosting platform built specifically for Pipecat apps. Your exact same code runs there with zero changes.

```bash
# 1. Sign up at pipecat.ai/cloud
# 2. Install Pipecat CLI
pip install pipecat-cli

# 3. Deploy
pipecat deploy --app wellheard-ai

# 4. Done — they handle scaling, monitoring, multi-region
```

---

## What About LiveKit?

**For Fly.io deploys:** Use LiveKit Cloud (managed) to avoid running your own media server. Sign up at livekit.io, get API keys, point your config at their URL.

**For VPS deploys:** LiveKit runs as part of the Docker Compose stack — it's already included in `docker-compose.prod.yml`.

**For Pipecat Cloud:** Daily.co provides their own WebRTC transport — LiveKit not needed.

---

## Provider Setup Checklist

Before deploying, you need accounts with each provider. Here's exactly what to do:

### 1. Deepgram (STT + Budget TTS) — 5 minutes

1. Go to [console.deepgram.com](https://console.deepgram.com)
2. Sign up (free $200 credit)
3. Create an API key → copy to `HV_DEEPGRAM_API_KEY`
4. For production: Contact Deepgram about "Dedicated" deployment in us-east-1 for lowest latency

### 2. Groq (Budget LLM) — 2 minutes

1. Go to [console.groq.com](https://console.groq.com)
2. Sign up (free tier: 30 requests/min)
3. Create an API key → copy to `HV_GROQ_API_KEY`
4. For production: Upgrade to paid tier for higher rate limits
5. For lowest latency: Ask about Enterprise plan for regional endpoint pinning (Dallas)

### 3. Google AI Studio (Quality LLM) — 3 minutes

1. Go to [aistudio.google.com](https://aistudio.google.com)
2. Sign in with Google account
3. Get API key → copy to `HV_GOOGLE_API_KEY`
4. Free tier: 15 requests/min on Gemini Flash

### 4. Cartesia (Quality TTS) — 3 minutes

1. Go to [play.cartesia.ai](https://play.cartesia.ai)
2. Sign up
3. Get API key → copy to `HV_CARTESIA_API_KEY`
4. Browse voices → copy a voice ID to `HV_CARTESIA_VOICE_ID`

### 5. Telnyx (Phone Calls) — 10 minutes

1. Go to [portal.telnyx.com](https://portal.telnyx.com)
2. Sign up and add billing
3. Buy a phone number → copy to `HV_TELNYX_PHONE_NUMBER`
4. Create a "SIP Connection" (TeXML or Call Control) → copy Connection ID
5. Copy your API key → `HV_TELNYX_API_KEY`
6. **Important:** In SIP Connection settings, set AnchorSite to "Latency" for automatic routing to nearest PoP

---

## Latency: What to Expect

### Deployed on Fly.io (US-East), All Providers in US

```
Stage                    Target      Expected
─────────────────────    ─────────   ──────────
Network (caller→Telnyx)  <50ms       30-50ms
Telnyx → Your server     <20ms       10-20ms
STT (Deepgram Nova-3)    <300ms      150-250ms
LLM (Groq Llama)         <500ms      300-490ms
TTS (Cartesia Sonic)     <100ms      40-90ms
Network (server→caller)  <50ms       30-50ms
─────────────────────    ─────────   ──────────
TOTAL                    <800ms      560-950ms
```

**Streaming overlap reduces perceived latency:** The LLM starts generating while STT is still finalizing. TTS starts speaking while the LLM is still generating. So the caller hears the first word of the response 200-400ms after they stop speaking.

### What Kills Latency (Avoid These)

| Mistake | Penalty | Fix |
|---|---|---|
| Deploying in EU while providers are in US | +60-120ms per hop | Deploy in US-East |
| Using REST instead of WebSocket for STT/TTS | +200-300ms | Already using WebSocket |
| Cold starts (serverless) | +500-3000ms | Set `auto_stop_machines = false` |
| No connection pooling | +100-200ms first call | Already using persistent connections |
| Sequential pipeline (wait for full STT before LLM) | +300-600ms | Already streaming in parallel |

---

## Scaling Guide

| Concurrent Calls | Machines | Monthly Infra Cost |
|---|---|---|
| 1-25 | 1× shared-cpu-2x | ~$15/mo |
| 25-50 | 2× shared-cpu-2x | ~$30/mo |
| 50-100 | 3× shared-cpu-2x | ~$45/mo |
| 100-200 | 4× shared-cpu-4x | ~$120/mo |
| 200+ | Kubernetes cluster | Contact us |

**Auto-scaling on Fly.io:** Already configured in `fly.toml`. When connections hit 80 per machine, Fly automatically spins up another.

**The real cost is API usage, not infrastructure.** At 100,000 minutes/month:

| Component | Monthly Cost |
|---|---|
| Infrastructure (Fly.io) | $45 |
| Deepgram STT | $770 |
| Groq or Gemini LLM | $20-90 |
| Deepgram Aura or Cartesia TTS | $1,000 |
| Telnyx telephony | $700 |
| **Total** | **~$2,535-2,605** |
| **Dasha.ai equivalent** | **$8,000** |

---

## Monitoring in Production

### Built-in Dashboard

```bash
# Platform-wide metrics
curl -H "Authorization: Bearer YOUR_KEY" https://your-server/v1/dashboard

# Per-call metrics
curl -H "Authorization: Bearer YOUR_KEY" https://your-server/v1/calls/CALL_ID
```

### Fly.io Monitoring

```bash
# Live logs
flyctl logs

# Machine status
flyctl status

# Metrics dashboard
flyctl dashboard
```

### Alerts to Set Up

1. **Latency alert:** If P95 > 1000ms for 5 minutes
2. **Cost alert:** If daily spend > expected budget × 1.5
3. **Error rate:** If any provider error rate > 5%
4. **Concurrent calls:** If approaching machine capacity (80%)

---

## CI/CD: Automatic Deployments

A GitHub Actions workflow is included at `.github/workflows/deploy.yml`. It runs all 48 tests then deploys to Fly.io on every push to `main`.

**Setup:**
```bash
# 1. Get a Fly API token
flyctl tokens create deploy

# 2. Add it to GitHub repo secrets
# Go to: GitHub → Settings → Secrets → Actions
# Add: FLY_API_TOKEN = <your token>

# 3. Push to main — it auto-deploys
git push origin main
```

---

## Quick Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| High latency (>1s) | Wrong region | Check `flyctl status` — must be `iad` |
| Calls fail to connect | Telnyx misconfigured | Verify SIP Connection and phone number |
| "not_configured" in health | Missing API key | Check `flyctl secrets list` |
| WebSocket disconnects | Nginx timeout | Already configured for 24h timeout |
| Cost higher than expected | LLM token usage | Check dashboard, lower `max_tokens` |
