# Transfer Endpoints & Optimizer Integration Guide

## Files Created

1. **`src/transfer_endpoints.py`** (550 lines)
   - FastAPI router with 9 complete endpoints
   - Twilio webhook signature validation
   - Hold phrase TwiML streaming
   - Conference/agent event routing
   - Transfer status queries
   - Runtime configuration updates
   - Callback scheduling

2. **`src/transfer_optimizer.py`** (480 lines)
   - Production optimization wrapper
   - Pre-dial agent (predictive dialing)
   - Agent pool health tracking
   - Transfer quality scoring
   - Callback automation
   - Round-robin agent selection with health-based skipping

## Integration Steps

### Step 1: Import Router into Server

In `src/api/server.py`, add:

```python
from ..transfer_endpoints import router as transfer_router

# In create_app():
app.include_router(transfer_router)
```

### Step 2: Configure Twilio Webhook Base URL

In `config/settings.py`, add:

```python
twilio_webhook_base_url: str = Field(
    default="https://api.wellheard.ai",
    description="Base URL for Twilio webhooks"
)
```

### Step 3: Update Twilio Settings (if not already set)

Ensure these are in your environment or `.env`:

```
TWILIO_ACCOUNT_SID=your_account_sid
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_PHONE_NUMBER=your_twilio_number
```

### Step 4: Wire Up Transfer Optimizer (Optional but Recommended)

In orchestrator or call manager, use TransferOptimizer for predictive dialing:

```python
from src.transfer_optimizer import TransferOptimizer

optimizer = TransferOptimizer()

# Pre-dial when reaching qualify phase
if call_phase == "qualify":
    await optimizer.pre_dial_agent(call_id, call_phase)

# Trigger transfer
manager = await optimizer.initiate_optimized_transfer(
    call_id=call_id,
    prospect_call_sid=call_sid,
    contact_name=prospect_name,
    last_name=prospect_last_name,
    webhook_base_url=webhook_base_url,
)
```

## API Endpoints

### Endpoint 1: Trigger Transfer
```
POST /v1/transfer/{call_id}/trigger
Content-Type: application/json

{
  "prospect_call_sid": "CA1234567890...",
  "contact_name": "John",
  "last_name": "Doe",
  "agent_did": "+19802020160"  # Optional override
}

Returns:
{
  "call_id": "abc-123",
  "status": "initiated",
  "agent_did": "+19802020160"
}
```

### Endpoint 2: Get Transfer Status
```
GET /v1/transfer/{call_id}/status

Returns:
{
  "call_id": "abc-123",
  "state": "monitoring",
  "is_transferred": true,
  "hold_elapsed": 12.5,
  "agent_talk_time": 24.3,
  "metrics": { ... }
}
```

### Endpoint 3: Cancel Transfer
```
POST /v1/transfer/{call_id}/cancel
Content-Type: application/json

{
  "reason": "prospect_requested_callback"
}

Returns:
{
  "call_id": "abc-123",
  "status": "cancelled"
}
```

### Endpoint 4: Update Configuration
```
PUT /v1/transfer/config
Content-Type: application/json

{
  "primary_agent_did": "+19802020161",
  "agent_pool": ["+19802020160", "+19802020161"],
  "ring_timeout_seconds": 15,
  "max_hold_time_seconds": 120
}

Returns updated configuration
```

### Endpoint 5: Schedule Callback
```
POST /v1/transfer/{call_id}/callback
Content-Type: application/json

{
  "prospect_phone": "+1234567890",
  "prospect_name": "John Doe",
  "reason": "transfer_failed"
}

Returns:
{
  "callback_id": "cb-xyz-123",
  "status": "scheduled"
}
```

### Endpoint 6: List Callbacks
```
GET /v1/transfer/callbacks

Returns:
{
  "callbacks": [
    {
      "callback_id": "cb-xyz-123",
      "prospect_phone": "+1234567890",
      "prospect_name": "John Doe",
      "scheduled_at": 1711270800.0,
      "retry_count": 0
    }
  ],
  "total_pending": 1
}
```

### Endpoints 7-9: Twilio Webhooks (Auto-Routed)
- `POST /v1/transfer/conference-events` — Conference event webhook
- `POST /v1/transfer/agent-status` — Agent call status webhook
- `GET /v1/transfer/hold-twiml/{conference_name}` — Hold phrase TwiML

All validate Twilio request signature (X-Twilio-Signature header).

## Key Features

### 1. Hold Phrase Streaming (Fixes waitUrl="")
- **Problem**: Previous code had `waitUrl=""`, leaving prospects in silence
- **Solution**: GET /v1/transfer/hold-twiml endpoint returns TwiML with:
  - Scripted hold phrases from config (personalized selling points)
  - Professional Polly/Alice voice (TTS)
  - Automatic fallback to filler phrases
  - Configurable pause between repeats

Update prospect conference TwiML:
```xml
<Conference waitUrl="https://api.wellheard.ai/v1/transfer/hold-twiml/transfer-{call_id}">
```

### 2. Twilio Request Validation
- Validates X-Twilio-Signature header on all Twilio webhooks
- Prevents unauthorized/replay attacks
- Uses HMAC-SHA1 with auth token

### 3. Pre-Dial Agent (Predictive)
- Triggered when call reaches QUALIFY phase
- Dials agent into holding conference BEFORE prospect reaches transfer phase
- When transfer triggers: prospect moved into already-active conference
- **Result**: Zero ring-wait time for prospect

### 4. Agent Pool Health Tracking
- Round-robin selection with health awareness
- Tracks: answer_rate, avg_ring_time, voicemail_rate
- Auto-disables agents with <50% answer rate
- Skips agents in 30-min cooldown

### 5. Transfer Quality Scoring
- Scores transfers 0-100 based on:
  - Hold time (max 40 points)
  - Agent response time (max 30 points)
  - Prospect engagement (max 30 points)
- Qualifies transfers with 30s+ talk time
- Ratings: EXCELLENT, GOOD, FAIR, POOR

### 6. Runtime Configuration
- Update agent DIDs without restart
- Validate DID format (+1XXXXXXXXXX)
- Hot-reload applies immediately

## Testing

### Test Signature Validation
```python
from src.transfer_endpoints import validate_twilio_signature

# Valid signature (requires Twilio SDK)
valid = validate_twilio_signature(
    request_url="https://api.wellheard.ai/v1/transfer/conference-events?ConferenceSid=...",
    params={"ConferenceSid": "..."},
    signature="..."
)
```

### Test Transfer Optimizer
```python
from src.transfer_optimizer import TransferOptimizer

optimizer = TransferOptimizer()

# Select agent
agent = optimizer.select_best_agent()
print(f"Selected agent: {agent}")

# Score quality
quality = optimizer.score_transfer_quality({
    "total_hold_seconds": 12,
    "agent_answer_seconds": 8,
    "agent_talk_seconds": 45,
    "fail_reasons": []
})
print(f"Quality score: {quality['total_score']}/100")

# Track health
optimizer.record_agent_attempt(
    agent_did="+19802020160",
    success=True,
    ring_time=8.5
)
print(f"Pool health: {optimizer.get_pool_health()}")
```

## Configuration Reference

### TRANSFER_CONFIG (transfer_optimizer.py)
```python
TRANSFER_CONFIG = {
    "primary_agent_did": "+19802020160",
    "agent_pool": ["+19802020160"],
    "pre_dial_enabled": True,
    "pre_dial_trigger_phase": "qualify",
    "ring_timeout": 18,  # seconds
    "max_hold_time": 60,  # seconds
    "hold_phrase_pause": 8,  # seconds between phrases
    "whisper_enabled": True,
    "record_conference": True,
    "callback_retry_intervals": [300, 900, 3600],  # 5m, 15m, 1h
    "qualified_transfer_threshold": 30,  # seconds
}
```

### TransferConfig (warm_transfer.py)
```python
TransferConfig(
    agent_dids=["+19802020160"],
    ring_timeout_seconds=20,
    max_hold_time_seconds=90,
    post_transfer_monitor_seconds=30,
    record_conference=True,
    whisper_enabled=True,
    # ... hold phrases, handoff phrases, etc.
)
```

## Production Deployment Checklist

- [ ] Set `TWILIO_AUTH_TOKEN` in environment (for signature validation)
- [ ] Set `twilio_webhook_base_url` to production domain
- [ ] Configure Twilio phone number statusCallbacks to point to `/v1/transfer/conference-events` and `/v1/transfer/agent-status`
- [ ] Add agent DIDs to TRANSFER_CONFIG.agent_pool
- [ ] Enable recording (record_conference=True) for QA
- [ ] Set up Redis for callback persistence (currently in-memory)
- [ ] Monitor agent pool health via GET /v1/transfer/callbacks
- [ ] Test with test calls before production launch

## Troubleshooting

### Issue: "invalid_twilio_signature"
- Check that `TWILIO_AUTH_TOKEN` is set correctly
- Verify webhook URL matches what Twilio expects
- Check request body wasn't modified (signature is immutable)

### Issue: Hold phrases not playing
- Verify waitUrl is set in conference TwiML
- Check conference name is correct (routing)
- Confirm TwiML voice="alice" is available in your Twilio account
- Check logs for "hold_twiml_error"

### Issue: Agent pre-dial timing out
- Increase max_hold_time in config
- Check agent's phone isn't silenced
- Verify agent is reachable at configured DID

### Issue: Transfer state stuck
- Check agent status webhooks are reaching /v1/transfer/agent-status
- Verify X-Twilio-Signature is being validated
- Check logs for "conference_webhook_error"

## Architecture Diagram

```
Prospect Call
     │
     ├─ QUALIFY phase
     │  └─ pre_dial_agent() → agent dialed into pre-dial conference (waiting)
     │
     ├─ TRANSFER phase
     │  └─ initiate_optimized_transfer()
     │     ├─ If pre-dialed: instant bridge (0s)
     │     └─ Else: standard transfer with hold TwiML
     │
     ├─ Hold → GET /v1/transfer/hold-twiml/{conf_name}
     │  └─ Returns TwiML: <Say> + <Pause> loop
     │
     ├─ Agent status events
     │  └─ POST /v1/transfer/agent-status (signature validated)
     │     └─ Routes to WarmTransferManager.handle_agent_status()
     │
     ├─ Conference events
     │  └─ POST /v1/transfer/conference-events (signature validated)
     │     └─ Routes to WarmTransferManager.handle_conference_event()
     │
     └─ Completion
        ├─ Success → TRANSFERRED state
        └─ Failure → schedule callback → POST /v1/transfer/{call_id}/callback
```

