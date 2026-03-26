# WellHeard AI — Setup Checklist

**Everything you need to do, in order, to go live.**
Estimated total time: 45-60 minutes.

---

## Step 1: Deepgram Account (5 min)

Deepgram powers STT in both pipelines and TTS in the budget pipeline. It's the only provider required for both modes.

1. Go to **https://console.deepgram.com/signup**
2. Sign up (email or GitHub)
3. You get **$200 free credit** — enough for ~26,000 minutes of STT
4. In the dashboard, go to **API Keys** → click **Create a New API Key**
5. Name it "WellHeard Production", set permissions to **Member**
6. Copy the key

```
HV_DEEPGRAM_API_KEY=<paste here>
```

**No credit card required for the free tier.** The $200 credit lasts until used up.

---

## Step 2: Groq Account (2 min)

Groq powers the LLM in the **budget pipeline**. Their custom LPU chips give the fastest inference speed available.

1. Go to **https://console.groq.com**
2. Sign up (Google, GitHub, or email)
3. In the dashboard, go to **API Keys** → **Create API Key**
4. Copy the key

```
HV_GROQ_API_KEY=<paste here>
```

**Free tier:** 30 requests/minute, 14,400 requests/day. Enough for testing and light production. Paid tier removes limits.

---

## Step 3: Google AI Studio Account (3 min)

Google Gemini Flash powers the LLM in the **quality pipeline**. Fastest TTFT among major cloud LLMs.

1. Go to **https://aistudio.google.com/apikey**
2. Sign in with any Google account
3. Click **Create API Key**
4. Select a Google Cloud project (or create one — it's free)
5. Copy the key

```
HV_GOOGLE_API_KEY=<paste here>
```

**Free tier:** 15 requests/minute on Gemini 2.5 Flash. No credit card needed.

---

## Step 4: Cartesia Account (3 min)

Cartesia Sonic powers TTS in the **quality pipeline**. 40ms time-to-first-audio — fastest TTS available.

1. Go to **https://play.cartesia.ai**
2. Sign up
3. Go to **API Keys** in settings → create a key
4. Copy the key
5. Go to **Voices** → browse or clone a voice → copy the **Voice ID**

```
HV_CARTESIA_API_KEY=<paste here>
HV_CARTESIA_VOICE_ID=<paste the voice ID>
```

**Free tier:** 10,000 characters/month (roughly 10-15 minutes of speech). Paid plans start at $24/month.

---

## Step 5: Telnyx Account (10 min)

Telnyx handles the actual phone calls — SIP trunking at $0.007/min.

1. Go to **https://portal.telnyx.com/sign-up**
2. Sign up and **verify your email**
3. **Add a payment method** (required to buy phone numbers)
4. In the portal, go to **Numbers** → **Search & Buy** → buy a phone number (~$1/month)
5. Copy your phone number (E.164 format like `+15551234567`)
6. Go to **Voice** → **SIP Connections** → **Add SIP Connection**
   - Type: **Call Control** (recommended) or **TeXML**
   - Give it a name like "WellHeard"
   - Copy the **Connection ID** from the connection details
7. In the SIP Connection settings → **AnchorSite** → set to **"Latency"** (auto-routes to closest PoP)
8. Go to **Account** → **API Keys** → **Create Key** (Full Access)
9. Copy the API key (it starts with `KEY...`)

```
HV_TELNYX_API_KEY=<paste here>
HV_TELNYX_CONNECTION_ID=<paste here>
HV_TELNYX_PHONE_NUMBER=<paste here, e.g., +15551234567>
```

**Deposit:** Telnyx requires a small prepay balance (typically $2-20).

---

## Step 6: Fly.io Account (5 min)

Fly.io hosts the WellHeard server in US-East Virginia — co-located with all providers for lowest latency.

1. Install the Fly CLI:
```bash
curl -L https://fly.io/install.sh | sh
```

2. Sign up:
```bash
flyctl auth signup
```

3. **Add a credit card** (required for deployment, but free tier covers small workloads)

That's it — no other setup needed in the Fly dashboard. Everything else is done via CLI in Step 8.

---

## Step 7: Choose Your API Key (1 min)

Pick a secure API key that Brightcall (and other clients) will use to authenticate with WellHeard. Generate something strong:

```bash
openssl rand -hex 32
```

```
HV_API_KEY=<paste the generated key>
```

Share this key with any company that needs to call your API.

---

## Step 8: Deploy (10 min)

With all keys in hand, deploy:

```bash
# 1. Clone the repo
git clone <your-repo-url> wellheard-ai
cd wellheard-ai

# 2. Set all secrets on Fly.io (paste your real keys)
flyctl secrets set \
  HV_API_KEY="your-generated-api-key" \
  HV_DEEPGRAM_API_KEY="your-deepgram-key" \
  HV_GROQ_API_KEY="your-groq-key" \
  HV_GOOGLE_API_KEY="your-google-key" \
  HV_CARTESIA_API_KEY="your-cartesia-key" \
  HV_TELNYX_API_KEY="your-telnyx-key" \
  HV_TELNYX_CONNECTION_ID="your-connection-id" \
  HV_TELNYX_PHONE_NUMBER="+15551234567"

# 3. Deploy
flyctl deploy

# 4. Verify it's live
curl https://wellheard-ai.fly.dev/v1/health
```

Expected response:
```json
{
  "status": "healthy",
  "version": "1.0.0",
  "providers": {
    "budget_pipeline": "ready",
    "quality_pipeline": "ready",
    "telephony": "ready"
  },
  "active_calls": 0
}
```

---

## Step 9: Test a Live Call (2 min)

```bash
# Budget pipeline ($0.025/min)
curl -X POST https://wellheard-ai.fly.dev/v1/calls \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "pipeline": "budget",
    "phone_number": "+1YOUR_REAL_PHONE",
    "agent": {
      "system_prompt": "You are a test agent. Greet the caller and ask how you can help.",
      "greeting": "Hello! This is a test call from WellHeard. Can you hear me clearly?"
    }
  }'
```

Your phone should ring. Pick up and have a conversation to verify latency and voice quality.

---

## Step 10: Share with Brightcall (2 min)

Send Brightcall (or any client) three things:

1. **API endpoint:** `https://wellheard-ai.fly.dev/v1`
2. **API key:** the key you generated in Step 7
3. **Integration guide:** `docs/QUICKSTART.md` (already in the project)

They only need to make one API call to start using it:
```bash
curl -X POST https://wellheard-ai.fly.dev/v1/calls \
  -H "Authorization: Bearer THE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"pipeline": "budget", "phone_number": "+15551234567"}'
```

---

## Summary: All Keys You Need

| # | Key | Where to Get It | Required For |
|---|---|---|---|
| 1 | `HV_DEEPGRAM_API_KEY` | console.deepgram.com | Both pipelines (STT + budget TTS) |
| 2 | `HV_GROQ_API_KEY` | console.groq.com | Budget pipeline (LLM) |
| 3 | `HV_GOOGLE_API_KEY` | aistudio.google.com | Quality pipeline (LLM) |
| 4 | `HV_CARTESIA_API_KEY` | play.cartesia.ai | Quality pipeline (TTS) |
| 5 | `HV_CARTESIA_VOICE_ID` | play.cartesia.ai → Voices | Quality pipeline (voice selection) |
| 6 | `HV_TELNYX_API_KEY` | portal.telnyx.com | Phone calls |
| 7 | `HV_TELNYX_CONNECTION_ID` | portal.telnyx.com → SIP | Phone calls |
| 8 | `HV_TELNYX_PHONE_NUMBER` | portal.telnyx.com → Numbers | Caller ID |
| 9 | `HV_API_KEY` | You generate it | Client authentication |
| 10 | Fly.io account | fly.io | Hosting |

**Budget pipeline only?** You need keys 1, 2, 6, 7, 8, 9, 10 (skip 3, 4, 5).
**Quality pipeline only?** You need keys 1, 3, 4, 5, 6, 7, 8, 9, 10 (skip 2).
**Both pipelines?** You need all 10.

---

## Cost to Get Started

| Item | Cost |
|---|---|
| Deepgram | $0 (free $200 credit) |
| Groq | $0 (free tier) |
| Google AI | $0 (free tier) |
| Cartesia | $0 (free 10K chars) |
| Telnyx | ~$5 (prepay + phone number) |
| Fly.io | ~$0-5 (free tier covers testing) |
| **Total to launch** | **~$5** |
