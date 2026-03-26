# Call Orchestrator Usage Guide

## Overview

The `CallOrchestrator` module provides pre-flight health checks and intelligent batch call orchestration for the WellHeard platform. It ensures infrastructure is healthy before launching campaigns and prevents wasting resources on doomed calls.

## Integration with server.py

### 1. Basic Initialization

```python
from call_orchestrator import CallOrchestrator

# Initialize with the active_calls registry from server.py
orchestrator = CallOrchestrator(active_calls_registry=active_calls)
```

### 2. Pre-flight Capacity Checks

Before launching calls, check if the system has capacity:

```python
# Check if system can handle 10 concurrent calls
capacity = await orchestrator.check_capacity(num_calls=10)

if not capacity.ready:
    # System not ready
    for issue in capacity.issues:
        logger.warning(f"Capacity issue: {issue}")
    # Don't launch calls
else:
    # System ready, launch calls
    logger.info(f"System ready. Available slots: {capacity.available_slots}")
```

### 3. Batch Campaign Launching

Launch calls in waves with automatic retry on failures:

```python
leads = [
    {"id": "lead-1", "phone": "+1234567890", "name": "John"},
    {"id": "lead-2", "phone": "+1234567891", "name": "Jane"},
    # ... more leads
]

result = await orchestrator.launch_campaign_batch(
    leads=leads,
    batch_size=10,                      # Calls per batch
    delay_between_batches_s=2.0,        # Wait between batches
    max_retries=3,                      # Retry failed batches
    retry_delay_s=5.0,                  # Delay between retries
)

print(f"Launched: {result.launched}, Failed: {result.failed}, Skipped: {result.skipped}")
```

### 4. System Status Monitoring

Get comprehensive system status for dashboards:

```python
status = await orchestrator.get_system_status()

# Returns:
# {
#     "active_calls": 15,
#     "max_concurrent": 50,
#     "capacity_utilization": 0.30,
#     "capacity_utilization_pct": 30.0,
#     "provider_health": {
#         "stt": "healthy",
#         "tts": "healthy",
#         "llm": "healthy",
#         "telephony": "healthy"
#     },
#     "avg_latency_ms": 285.5,
#     "p95_latency_ms": 450.2,
#     "issues": [],
#     "timestamp": 1711392000.123
# }
```

### 5. Recording Latencies

Track latency measurements for system monitoring:

```python
# Record a latency measurement
orchestrator.record_latency(latency_ms=285.5)
```

## Data Classes

### CapacityResult

Returned from `check_capacity()`:

```python
@dataclass
class CapacityResult:
    ready: bool                           # Can system handle N calls?
    available_slots: int                  # Slots available
    active_calls: int                     # Currently active calls
    max_concurrent: int                   # System max capacity
    provider_status: Dict[str, str]       # {"stt": "healthy", ...}
    issues: List[str]                     # Problems found
    
    def to_dict(self) -> dict:            # Convert to JSON
        ...
```

### LaunchResult

Returned from `launch_campaign_batch()`:

```python
@dataclass
class LaunchResult:
    launched: int                         # Successfully launched
    failed: int                           # Failed to launch
    skipped: int                          # Skipped due to capacity
    errors: List[str]                     # Error details
    
    def to_dict(self) -> dict:            # Convert to JSON
        ...
```

## Provider Health Checks

The orchestrator checks four provider types:

1. **STT (Speech-to-Text)**: Deepgram
   - Checks: API key configured, simulated connection test
   - Status: "healthy" / "degraded" / "down"

2. **TTS (Text-to-Speech)**: Cartesia or Deepgram
   - Checks: API key configured, simulated connection test
   - Status: "healthy" / "degraded" / "down"

3. **LLM**: Groq, OpenAI, or Gemini
   - Checks: API key configured, simulated health endpoint
   - Status: "healthy" / "degraded" / "down"

4. **Telephony**: Twilio or Telnyx
   - Checks: Credentials configured, simulated API validation
   - Status: "healthy" / "degraded" / "down"

## Caching

Provider health checks are cached for 30 seconds to avoid excessive pinging:

```python
HEALTH_CHECK_CACHE_TTL = 30  # seconds

# Results are automatically cached after each check
# Subsequent checks within 30s use cached results
```

## Configuration

Key settings in `config/settings.py`:

```python
# Maximum concurrent calls system can handle
MAX_CONCURRENT_CALLS = 50

# Provider API keys (checked for configuration)
deepgram_api_key
groq_api_key
openai_api_key
google_api_key
cartesia_api_key
twilio_account_sid
twilio_auth_token
twilio_phone_number
telnyx_api_key
telephony_provider  # "twilio" or "telnyx"
```

## Batch Launching Flow

```
1. Split leads into batches of batch_size
   ├─ For each batch:
   │  ├─ Check capacity: check_capacity(batch_size)
   │  │  └─ If not ready: retry up to max_retries times
   │  │     ├─ Wait retry_delay_s seconds
   │  │     └─ Check capacity again
   │  │
   │  ├─ If still not ready: skip batch, log error
   │  ├─ If ready: launch batch_size calls in parallel
   │  │  └─ Track launched/failed counts
   │  │
   │  └─ Wait delay_between_batches_s before next batch
   │
2. Return LaunchResult with aggregate counts
```

## Logging

Uses `structlog` for structured logging. Key events:

```
capacity_check                - Capacity check initiated
batch_preflight_check         - Batch preflight check started
batch_capacity_check_failed   - Capacity check failed, retrying
batch_capacity_check_failed_skipping - Capacity check failed after retries
batch_preflight_passed        - Batch ready to launch
batch_launched                - Batch launched
launch_campaign_batch_complete - Campaign complete
system_status_check           - System status checked
```

## Example: Complete Workflow

```python
from call_orchestrator import CallOrchestrator

# In server.py startup
orchestrator = CallOrchestrator(active_calls_registry=active_calls)

# In campaign endpoint
@app.post("/v1/campaigns/launch")
async def launch_campaign(request: CampaignRequest, api_key: str = Depends(verify_api_key)):
    """Launch outbound campaign with pre-flight checks."""
    
    # Pre-flight check
    capacity = await orchestrator.check_capacity(num_calls=len(request.leads))
    if not capacity.ready:
        raise HTTPException(status_code=503, detail="System not ready")
    
    # Launch campaign in batches
    result = await orchestrator.launch_campaign_batch(
        leads=request.leads,
        batch_size=10,
        delay_between_batches_s=2.0,
    )
    
    return {
        "campaign_id": f"campaign-{uuid.uuid4().hex[:8]}",
        "launched": result.launched,
        "failed": result.failed,
        "skipped": result.skipped,
    }

# In monitoring endpoint
@app.get("/v1/system/status")
async def system_status(api_key: str = Depends(verify_api_key)):
    """Get system status."""
    return await orchestrator.get_system_status()
```

## Error Handling

The orchestrator gracefully handles errors:

1. **Provider Health Check Failures**: Returns "down" status
2. **Capacity Check Failures**: Retries with backoff, then skips batch
3. **Call Launch Failures**: Continues with next batch, tracks in result
4. **Import Errors**: Falls back to simple call counting

## Extension Points

To integrate with real call launching:

1. Implement `_launch_single_call()` to create CallBridge and queue calls
2. Connect `_check_stt_health()` to Deepgram WebSocket test
3. Connect `_check_tts_health()` to Cartesia health endpoint
4. Connect `_check_llm_health()` to LLM provider health endpoints
5. Connect `_check_telephony_health()` to Twilio/Telnyx API validation

Current implementation has simulated placeholder calls that can be replaced.
