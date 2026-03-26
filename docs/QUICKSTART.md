# WellHeard AI — Quickstart Integration Guide

**For: Any company replacing Dasha.ai**
**Version:** 1.0 | **Date:** March 2026

---

## What You Get

Two voice AI pipelines that beat Dasha.ai on both cost and latency:

| | Budget Pipeline | Quality Pipeline | Dasha.ai (current) |
|---|---|---|---|
| **Cost/min** | $0.025 | $0.026 | $0.08+ |
| **Avg latency** | ~700ms | ~490ms | ~1050ms |
| **Savings** | 69% cheaper | 68% cheaper | — |
| **STT** | Deepgram Nova-3 | Deepgram Nova-3 | Proprietary |
| **LLM** | Groq Llama 4 Scout | Gemini 2.5 Flash | GPT-4o-mini |
| **TTS** | Deepgram Aura-2 | Cartesia Sonic-2 | ElevenLabs |
| **Telephony** | Telnyx | Telnyx | Telnyx |

---

## 1. Setup (5 minutes)

### Option A: Docker (Recommended)

```bash
git clone <your-repo>/wellheard-ai
cd wellheard-ai

# Copy and fill in your API keys
cp config/.env.example config/.env
nano config/.env

# Start everything
docker compose up -d
```

### Option B: Local Python

```bash
git clone <your-repo>/wellheard-ai
cd wellheard-ai

pip install -e .
cp config/.env.example config/.env
# Edit config/.env with your API keys

python main.py
```

### Required API Keys

Get these keys (takes ~10 minutes total):

| Service | Sign Up | What For |
|---|---|---|
| **Deepgram** | [console.deepgram.com](https://console.deepgram.com) | STT (both modes) + TTS (budget) |
| **Groq** | [console.groq.com](https://console.groq.com) | LLM (budget mode) |
| **Google AI** | [aistudio.google.com](https://aistudio.google.com) | LLM (quality mode) |
| **Cartesia** | [play.cartesia.ai](https://play.cartesia.ai) | TTS (quality mode) |
| **Telnyx** | [portal.telnyx.com](https://portal.telnyx.com) | Phone calls |

---

## 2. Make Your First Call

### Start a Budget Call ($0.025/min)

```bash
curl -X POST http://localhost:8000/v1/calls \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "pipeline": "budget",
    "phone_number": "+15551234567",
    "agent": {
      "system_prompt": "You are a friendly appointment scheduler for Acme Dental. Be concise.",
      "greeting": "Hi! This is Acme Dental calling to confirm your appointment. Is this a good time?"
    }
  }'
```

### Start a Quality Call ($0.026/min)

```bash
curl -X POST http://localhost:8000/v1/calls \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "pipeline": "quality",
    "phone_number": "+15551234567",
    "agent": {
      "system_prompt": "You are a premium sales representative. Sound natural and warm.",
      "voice_id": "a0e99841-438c-4a64-b679-ae501e7d6091",
      "temperature": 0.8
    }
  }'
```

### Response

```json
{
  "call_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "active",
  "pipeline": "budget",
  "phone_number": "+15551234567",
  "agent_id": "default",
  "estimated_cost_per_minute": 0.025,
  "message": "Call started with budget pipeline"
}
```

---

## 3. API Reference (5 Endpoints)

### Base URL
```
http://your-server:8000/v1
```

### Authentication
```
Authorization: Bearer YOUR_API_KEY
```

---

### POST /v1/calls — Start a call

```json
{
  "pipeline": "budget | quality",
  "direction": "outbound | inbound",
  "phone_number": "+15551234567",
  "max_duration_seconds": 1800,
  "webhook_url": "https://your-app.com/webhook",
  "metadata": { "customer_id": "123", "campaign": "spring_2026" },
  "agent": {
    "agent_id": "sales_bot_v2",
    "system_prompt": "You are a sales agent...",
    "voice_id": "",
    "language": "en",
    "temperature": 0.7,
    "max_tokens": 256,
    "interruption_enabled": true,
    "greeting": "Hello! How can I help you today?",
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "book_appointment",
          "description": "Book an appointment for the caller",
          "parameters": {
            "type": "object",
            "properties": {
              "date": { "type": "string" },
              "time": { "type": "string" },
              "name": { "type": "string" }
            }
          }
        }
      }
    ]
  }
}
```

---

### DELETE /v1/calls/{call_id} — End a call

Returns final metrics:

```json
{
  "call_id": "550e8400-...",
  "pipeline_mode": "budget",
  "duration_seconds": 127.5,
  "turns": 8,
  "interruptions": 1,
  "avg_latency_ms": 695.0,
  "p95_latency_ms": 740.0,
  "total_cost_usd": 0.053,
  "cost_per_minute_usd": 0.025,
  "cost_breakdown": [
    { "provider": "deepgram_nova3", "component": "stt", "cost": 0.016 },
    { "provider": "groq_llama", "component": "llm", "cost": 0.0004 },
    { "provider": "deepgram_aura2", "component": "tts", "cost": 0.021 }
  ]
}
```

---

### GET /v1/calls/{call_id} — Get call status

Returns live metrics for an active call.

---

### GET /v1/health — Health check

```json
{
  "status": "healthy",
  "version": "1.0.0",
  "providers": {
    "budget_pipeline": "ready",
    "quality_pipeline": "ready",
    "telephony": "ready"
  },
  "active_calls": 12
}
```

---

### GET /v1/dashboard — Cost & performance dashboard

```json
{
  "total_calls": 1547,
  "active_calls": 12,
  "total_cost_usd": 89.42,
  "total_minutes": 3580.5,
  "avg_cost_per_minute": 0.025,
  "providers": {
    "deepgram_nova3": { "avg_latency_ms": 245, "p95_latency_ms": 310, "error_rate": 0.001 },
    "groq_llama":     { "avg_latency_ms": 420, "p95_latency_ms": 510, "error_rate": 0.002 },
    "deepgram_aura2": { "avg_latency_ms": 88,  "p95_latency_ms": 120, "error_rate": 0.001 }
  }
}
```

---

### WebSocket /v1/ws/{call_id} — Real-time events

```javascript
const ws = new WebSocket("ws://your-server:8000/v1/ws/CALL_ID");

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  // data.event = "transcript" | "response" | "audio" | "metrics" | "call_ended"
};

// Send barge-in interrupt
ws.send(JSON.stringify({ type: "interrupt" }));
```

---

## 4. Webhook Events

If you set `webhook_url`, you'll receive POST requests:

```json
{
  "event": "call.completed",
  "call_id": "550e8400-...",
  "duration_seconds": 127.5,
  "turns": 8,
  "total_cost_usd": 0.053,
  "transcript": [
    { "role": "agent", "text": "Hi! How can I help?" },
    { "role": "user", "text": "I'd like to schedule an appointment." },
    { "role": "agent", "text": "Sure! What date works for you?" }
  ]
}
```

---

## 5. When to Use Which Pipeline

| Use Case | Recommended | Why |
|---|---|---|
| Appointment reminders | **Budget** | Cost-sensitive, simple conversations |
| Lead qualification | **Budget** | High volume, straightforward scripts |
| Premium sales calls | **Quality** | Voice quality matters for conversion |
| Customer support | **Quality** | Better LLM reasoning for complex issues |
| Surveys & feedback | **Budget** | High volume, predictable flow |
| VIP customer outreach | **Quality** | Natural voice + emotion for rapport |

---

## 6. Code Examples

### Python

```python
import httpx

API_URL = "http://localhost:8000/v1"
API_KEY = "your-api-key"
headers = {"Authorization": f"Bearer {API_KEY}"}

# Start a call
response = httpx.post(f"{API_URL}/calls", headers=headers, json={
    "pipeline": "budget",
    "phone_number": "+15551234567",
    "agent": {
        "system_prompt": "You are a dental office scheduler.",
        "greeting": "Hi! Calling from Acme Dental about your appointment."
    }
})
call = response.json()
print(f"Call started: {call['call_id']}")

# Later: end the call
metrics = httpx.delete(f"{API_URL}/calls/{call['call_id']}", headers=headers).json()
print(f"Duration: {metrics['duration_seconds']}s")
print(f"Cost: ${metrics['total_cost_usd']:.4f}")
print(f"Avg latency: {metrics['avg_latency_ms']}ms")
```

### JavaScript / Node.js

```javascript
const API_URL = "http://localhost:8000/v1";
const API_KEY = "your-api-key";

// Start a call
const response = await fetch(`${API_URL}/calls`, {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${API_KEY}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    pipeline: "quality",
    phone_number: "+15551234567",
    agent: {
      system_prompt: "You are a premium sales agent. Sound professional and engaging.",
      voice_id: "a0e99841-438c-4a64-b679-ae501e7d6091",
    },
  }),
});
const call = await response.json();
console.log(`Call started: ${call.call_id}`);
```

### cURL — Check costs in real time

```bash
# Dashboard
curl -H "Authorization: Bearer YOUR_KEY" http://localhost:8000/v1/dashboard

# Single call metrics
curl -H "Authorization: Bearer YOUR_KEY" http://localhost:8000/v1/calls/CALL_ID
```

---

## 7. Architecture Overview

```
Phone Call
    │
    ▼
Telnyx SIP ──→ LiveKit Media Server ──→ Pipecat Agent
                                             │
                   ┌─────────────────────────┤
                   ▼                         ▼
            Silero VAD              Deepgram Nova-3 (STT)
          (voice detect)            streaming partials
                                         │
                                         ▼
                              ┌─── Budget ────┐  ┌── Quality ──┐
                              │  Groq Llama   │  │ Gemini Flash │
                              │  490ms TTFT   │  │  192ms TTFT  │
                              └───────┬───────┘  └──────┬───────┘
                                      │                  │
                              ┌───────┴───────┐  ┌──────┴───────┐
                              │ Deepgram Aura │  │   Cartesia   │
                              │   90ms TTFB   │  │  40ms TTFB   │
                              └───────┬───────┘  └──────┬───────┘
                                      │                  │
                                      ▼                  ▼
                               Audio back to caller via LiveKit
```

**Key:** All stages run in parallel (streaming). LLM starts before STT finishes. TTS starts before LLM finishes. This is why latency is ~700ms, not ~1500ms.

---

## 8. Cost Calculator

```
Monthly cost = minutes × rate

Budget:  minutes × $0.025/min
Quality: minutes × $0.026/min

Examples:
  10,000 min/mo  → Budget: $250    Quality: $260    (Dasha: $800)
  100,000 min/mo → Budget: $2,500  Quality: $2,600  (Dasha: $8,000)
  1M min/mo      → Budget: $25,000 Quality: $26,000 (Dasha: $80,000)
```

---

## Support

- **API Docs:** `http://your-server:8000/docs` (interactive Swagger UI)
- **Health Check:** `GET /v1/health`
- **Logs:** `docker compose logs -f wellheard`
