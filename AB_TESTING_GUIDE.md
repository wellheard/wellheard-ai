# WellHeard A/B Testing Framework

Complete guide to the A/B testing system for systematically testing prompt variants, speaking speeds, temperature, max tokens, and other configurable parameters.

## Overview

The A/B testing framework:
- **Assigns** each new call to variant A or B (50/50 random)
- **Tracks** which variant each call_id belongs to
- **Records** results: grade scores, latency, turns, transfer rate
- **Computes** statistical significance (z-test for proportions, t-test for means)
- **Declares** a winner when p < 0.05 with minimum 20 calls per variant
- **Thread-safe** with per-experiment locking
- **In-memory storage** (upgradeable to Redis)

## Architecture

### Core Components

**`ab_testing.py`**: Main module containing:
- `ExperimentConfig`: Configuration for an A/B test
- `VariantConfig`: Configuration overrides for one variant
- `CallResult`: Results from a single call
- `ExperimentResult`: Aggregated results for one variant
- `WinnerResult`: Statistical test results
- `ABTestManager`: Main test management class
- Pre-built experiments: `create_speed_test()`, `create_temperature_test()`, etc.

### Workflow

```
1. Create Experiment
   POST /v1/ab-test/create
   ↓
2. For Each Call:
   a. Assign Variant
      POST /v1/ab-test/assign
      ↓
   b. Apply Config Overrides
      In call initialization (server.py)
      ↓
   c. Run Call
      Normal call flow
      ↓
   d. Record Result
      POST /v1/ab-test/record-result
   ↓
3. Monitor Status
   GET /v1/ab-test/status?experiment_name=speed_test
   ↓
4. When Winner Declared
   Review results → Stop experiment → Implement winner
   POST /v1/ab-test/stop
```

## API Reference

### 1. Create an Experiment

**Endpoint**: `POST /v1/ab-test/create`

**Request**:
```json
{
  "name": "my_temperature_test",
  "description": "Testing temperature 0.7 vs 0.8",
  "metric": "transfer_rate",
  "variant_a": {
    "temperature": 0.7,
    "max_tokens": 40
  },
  "variant_b": {
    "temperature": 0.8,
    "max_tokens": 40
  },
  "min_samples_per_variant": 20
}
```

**Valid Metrics**:
- `transfer_rate`: Binary (transfer completed or not). Uses z-test for proportions. **Higher is better.**
- `grade_score`: Continuous (0-100 scale). Uses t-test for means. **Higher is better.**
- `latency_p95`: Continuous (milliseconds). Uses t-test for means. **Lower is better.**
- `latency_avg`: Continuous (milliseconds). Uses t-test for means. **Lower is better.**

**Config Field Reference**:
- `system_prompt` (string): Override the agent's system prompt
- `temperature` (float): 0.0-1.0, controls creativity vs consistency
- `max_tokens` (int): Max response length in tokens
- `speed` (float): Speech playback speed (0.8-1.2, where 1.0 is normal)

**Response**:
```json
{
  "status": "created",
  "experiment_name": "my_temperature_test",
  "description": "Testing temperature 0.7 vs 0.8",
  "metric": "transfer_rate"
}
```

### 2. Assign a Variant

**Endpoint**: `POST /v1/ab-test/assign`

Called when initiating a call to determine which variant the call should use.

**Request**:
```json
{
  "call_id": "abc123def456",
  "experiment_name": "my_temperature_test"
}
```

**Response**:
```json
{
  "call_id": "abc123def456",
  "experiment_name": "my_temperature_test",
  "variant": "variant_a",
  "config_overrides": {
    "temperature": 0.7,
    "max_tokens": 40
  }
}
```

**Usage in Call Initialization**:
1. When starting a call, call this endpoint
2. Apply the `config_overrides` to the agent config
3. Proceed with the call using the modified config
4. Store the `variant` for later recording

### 3. Record Call Result

**Endpoint**: `POST /v1/ab-test/record-result`

Called after a call is graded to feed results back to the A/B test manager.

**Request**:
```json
{
  "call_id": "abc123def456",
  "experiment_name": "my_temperature_test",
  "grade_score": 78.5,
  "transfer_attempted": true,
  "transfer_completed": true,
  "latency_p95_ms": 450.0,
  "latency_avg_ms": 320.0,
  "total_turns": 6,
  "duration_seconds": 125.3,
  "cost_usd": 0.42
}
```

**Field Descriptions**:
- `grade_score`: Call quality score (0-100). Higher is better.
- `transfer_attempted`: Boolean. Was a transfer attempted?
- `transfer_completed`: Boolean. Did the transfer succeed?
- `latency_p95_ms`: 95th percentile round-trip latency
- `latency_avg_ms`: Average round-trip latency
- `total_turns`: Number of conversation turns
- `duration_seconds`: Total call duration
- `cost_usd`: Total cost of the call

**Response**:
```json
{
  "recorded": true,
  "call_id": "abc123def456",
  "experiment_name": "my_temperature_test"
}
```

### 4. Get Experiment Status

**Endpoint**: `GET /v1/ab-test/status`

Get current results and winner declaration.

**Query Parameters**:
- `experiment_name` (optional): If provided, returns status for that experiment. If omitted, returns status for all experiments.

**Response (Single Experiment)**:
```json
{
  "name": "my_temperature_test",
  "description": "Testing temperature 0.7 vs 0.8",
  "metric": "transfer_rate",
  "status": "complete",
  "variant_a": {
    "config": {
      "temperature": 0.7,
      "max_tokens": 40
    },
    "results": {
      "sample_count": 25,
      "grade_score_mean": 76.3,
      "grade_score_std": 8.2,
      "transfer_rate": 0.72,
      "latency_p95_mean_ms": 445.2,
      "latency_avg_mean_ms": 320.1,
      "avg_turns": 5.8,
      "avg_duration_seconds": 123.4,
      "total_cost_usd": 10.50
    }
  },
  "variant_b": {
    "config": {
      "temperature": 0.8,
      "max_tokens": 40
    },
    "results": {
      "sample_count": 25,
      "grade_score_mean": 79.1,
      "grade_score_std": 7.5,
      "transfer_rate": 0.80,
      "latency_p95_mean_ms": 452.1,
      "latency_avg_mean_ms": 325.3,
      "avg_turns": 5.9,
      "avg_duration_seconds": 125.2,
      "total_cost_usd": 10.75
    }
  },
  "winner": {
    "has_winner": true,
    "winner": "variant_b",
    "p_value": 0.042,
    "confidence": 0.958,
    "details": {
      "test": "z-test (two proportions)",
      "variant_a_rate": 0.72,
      "variant_b_rate": 0.80,
      "z_statistic": 2.054,
      "difference": 0.08
    }
  },
  "started_at": "2026-03-26T10:30:00",
  "ended_at": null
}
```

**Status Values**:
- `pending`: Experiment created but no calls recorded yet
- `running`: At least one call recorded, but need more samples
- `complete`: Both variants have sufficient samples (min_samples_per_variant met)

**Winner Declaration**:
- `has_winner`: Boolean. Is there a statistically significant winner?
- `winner`: Which variant won (or null if no winner)
- `p_value`: Statistical significance (lower is more confident). Must be < 0.05.
- `confidence`: 1 - p_value. Likelihood of winner (0-1 scale)

### 5. Stop an Experiment

**Endpoint**: `POST /v1/ab-test/stop`

Mark an experiment as complete and stop accepting new calls.

**Query Parameters**:
- `experiment_name` (required)

**Response**:
```json
{
  "stopped": true,
  "experiment_name": "my_temperature_test"
}
```

### 6. Delete an Experiment

**Endpoint**: `DELETE /v1/ab-test/{experiment_name}`

Permanently delete an experiment and all its results.

**Response**:
```json
{
  "deleted": true,
  "experiment_name": "my_temperature_test"
}
```

## Pre-Built Experiments

The framework comes with 4 pre-built experiments that are automatically initialized:

### 1. Speed Test
```python
create_speed_test()
# name: "speed_test"
# metric: "transfer_rate"
# variant_a: speed=0.97 (slower, current)
# variant_b: speed=1.0 (normal speed)
```

**Hypothesis**: Slightly slower speech increases comprehension and trust, potentially improving transfer rates.

### 2. Prompt Length Test
```python
create_prompt_length_test()
# name: "prompt_length_test"
# metric: "grade_score"
# variant_a: full system prompt (~1000 tokens)
# variant_b: shortened prompt (~700 tokens, 30% reduction)
```

**Hypothesis**: Shorter prompts reduce token overhead and may improve latency without sacrificing quality.

### 3. Temperature Test
```python
create_temperature_test()
# name: "temperature_test"
# metric: "transfer_rate"
# variant_a: temperature=0.7 (consistent)
# variant_b: temperature=0.8 (more creative)
```

**Hypothesis**: Slightly higher temperature improves response naturalness while maintaining consistency.

### 4. Max Tokens Test
```python
create_max_tokens_test()
# name: "max_tokens_test"
# metric: "grade_score"
# variant_a: max_tokens=40 (concise)
# variant_b: max_tokens=50 (slightly longer)
```

**Hypothesis**: Slightly longer responses provide better context without significantly impacting latency.

## Usage Examples

### Example 1: Create and Run a Temperature Test

```bash
# 1. Create the experiment
curl -X POST http://localhost:8000/v1/ab-test/create \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "temp_test_v2",
    "description": "Temperature 0.6 (more consistent) vs 0.75 (more natural)",
    "metric": "transfer_rate",
    "variant_a": {"temperature": 0.6},
    "variant_b": {"temperature": 0.75},
    "min_samples_per_variant": 30
  }'

# 2. Make calls with the experiment
# In your call initialization code, add:
ab_experiment = "temp_test_v2"
variant_response = await assign_variant(call_id, ab_experiment)
# variant_response.config_overrides contains {"temperature": 0.6 or 0.75}

# 3. After grading each call, record the result
curl -X POST http://localhost:8000/v1/ab-test/record-result \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "call_id": "call_123",
    "experiment_name": "temp_test_v2",
    "grade_score": 82.5,
    "transfer_attempted": true,
    "transfer_completed": true,
    "latency_p95_ms": 425.0,
    "latency_avg_ms": 310.0,
    "total_turns": 7,
    "duration_seconds": 134.2,
    "cost_usd": 0.44
  }'

# 4. Monitor results
curl -X GET "http://localhost:8000/v1/ab-test/status?experiment_name=temp_test_v2" \
  -H "Authorization: Bearer YOUR_API_KEY"

# 5. When winner declared (has_winner=true), implement it
# Then stop the experiment
curl -X POST "http://localhost:8000/v1/ab-test/stop?experiment_name=temp_test_v2" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

### Example 2: Testing Prompt Variation

```python
import httpx
import asyncio

async def test_new_prompt():
    manager = await get_ab_test_manager()

    # Define the new prompt (shortened version)
    short_prompt = """You are Becky, an insurance SDR on a live phone call.

STEPS:
1. Confirm interest
2. Ask about bank account
3. Transfer to agent

Keep responses under 15 words. End every response with a question."""

    # Create experiment
    from src.ab_testing import ExperimentConfig, VariantConfig

    config = ExperimentConfig(
        name="short_prompt_v3",
        description="Current prompt vs shortened version (50% less text)",
        metric="grade_score",
        variant_a=VariantConfig(system_prompt=None),  # Use current
        variant_b=VariantConfig(system_prompt=short_prompt),
        min_samples_per_variant=25,
    )

    await manager.create_experiment(config)
    print(f"Experiment '{config.name}' created")

    # Simulate 50 calls
    for i in range(50):
        call_id = f"test_call_{i}"
        variant = await manager.assign_variant(call_id, config.name)

        # Run call (simulate)
        grade = 75 + (5 if variant.value == "variant_b" else 0) + random.randint(-5, 5)

        # Record result
        await manager.record_result(
            call_id=call_id,
            experiment_name=config.name,
            grade_score=grade,
            transfer_attempted=True,
            transfer_completed=grade > 70,
            latency_p95_ms=400 + random.randint(-50, 50),
            latency_avg_ms=300 + random.randint(-30, 30),
            total_turns=6,
            duration_seconds=120 + random.randint(-10, 10),
            cost_usd=0.40,
        )

    # Get final status
    status = await manager.get_experiment_status(config.name)
    print(f"Winner: {status['winner']['winner']}")
    print(f"P-value: {status['winner']['p_value']}")
```

## Statistical Methods

### Z-Test for Proportions (Transfer Rate)

Used when testing binary metrics like transfer_rate.

**Test**: Two-proportion z-test
- Null hypothesis: H₀ p_a = p_b
- Alternative: H₁ p_a ≠ p_b
- Confidence level: 95% (α = 0.05)

**Formula**:
```
z = (p_a - p_b) / sqrt(p_pool * (1-p_pool) * (1/n_a + 1/n_b))
p_value = 2 * P(Z > |z|)
```

### Welch's T-Test for Means (Grade Score, Latency)

Used for continuous metrics like grade_score and latency.

**Test**: Two-sample Welch's t-test (does not assume equal variances)
- Null hypothesis: H₀ μ_a = μ_b
- Alternative: H₁ μ_a ≠ μ_b
- Confidence level: 95% (α = 0.05)

**Formula**:
```
t = (mean_a - mean_b) / sqrt(var_a/n_a + var_b/n_b)
df = (var_a/n_a + var_b/n_b)² / [(var_a/n_a)²/(n_a-1) + (var_b/n_b)²/(n_b-1)]
p_value = 2 * P(T > |t|)
```

### Significance Level & Sample Size

- **Significance level (α)**: 0.05 (95% confidence)
- **Minimum samples per variant**: 20 (default, configurable)
- **Winner declared when**: p_value < 0.05 AND both variants have ≥ min_samples

With n=20 per variant and typical effect sizes, you need ~40-80 calls total to reach significance.

## Integration with Call System

### Step 1: Add ab_test_experiment to StartCallRequest

In `src/api/models.py`, add optional field to `StartCallRequest`:
```python
@dataclass
class StartCallRequest:
    # ... existing fields ...
    ab_test_experiment: Optional[str] = None  # Name of A/B test experiment
```

### Step 2: Assign Variant in start_call()

Already done in `src/api/server.py`. The variant is assigned and config overrides are applied automatically.

### Step 3: Record Results After Grading

After calling `grade_call()`, record the result:
```python
# In grading endpoint or call completion handler
grade_report = grade_call(logs)
await manager.record_result(
    call_id=call_id,
    experiment_name=ab_test_experiment,
    grade_score=grade_report.overall_score,
    transfer_attempted=grade_report.transfer_attempted,
    transfer_completed=grade_report.transfer_qualified,
    latency_p95_ms=metrics.p95_total_latency,
    latency_avg_ms=metrics.avg_total_latency,
    total_turns=metrics.turns,
    duration_seconds=metrics.duration_seconds,
    cost_usd=metrics.total_cost,
)
```

## Best Practices

### 1. Test One Variable at a Time
Each experiment should change only ONE config parameter. This isolates the effect.

**Good**:
```python
# Tests only temperature
config = ExperimentConfig(
    variant_a=VariantConfig(temperature=0.7),
    variant_b=VariantConfig(temperature=0.8),
)
```

**Bad**:
```python
# Tests both temperature AND max_tokens
config = ExperimentConfig(
    variant_a=VariantConfig(temperature=0.7, max_tokens=40),
    variant_b=VariantConfig(temperature=0.8, max_tokens=50),
)
```

### 2. Use Meaningful Names
```python
# Good
"speed_test_slower_v1"
"temperature_0.6_vs_0.8"

# Bad
"test1"
"variant_experiment"
```

### 3. Monitor Sample Size
Watch the status endpoint to see when you reach significance:
```bash
watch -n 5 'curl -s http://localhost:8000/v1/ab-test/status | jq'
```

### 4. Document Hypotheses
In the description field, explain what you expect to happen:
```python
"Speed 0.95x (slower) vs 1.0x (normal): expect slower improves comprehension → higher transfer rate"
```

### 5. Preserve Winner Configs
When a winner is declared, save the winning config:
```bash
# Document winning config
curl -s http://localhost:8000/v1/ab-test/status?experiment_name=speed_test | jq '.winner'

# Then update your defaults in inbound_handler.py
SPEED = 0.95  # Was 0.97, now winner from speed_test
```

### 6. Account for Seasonality
If testing over different times of day/week/month, be aware that external factors (lead quality, time of day, etc.) may affect results. Run for at least 3-5 days to average out daily variance.

### 7. Sequential Testing
You can run multiple experiments in parallel, but be careful:
- They should test different parameters
- Each experiment needs independent call samples
- Or ensure stratification: make sure each variant A/B pair gets similar quality leads

## Troubleshooting

### Issue: "Experiment not found"
```
Response: {"detail": "Experiment 'speed_test' not found"}
```
**Solution**: Create the experiment first with POST /v1/ab-test/create

### Issue: Result not recorded
```json
{
  "recorded": false,
  "reason": "Call not found in experiment or already recorded"
}
```
**Solution**:
1. Ensure call_id was assigned to this experiment
2. Check that you're not recording the same call twice

### Issue: Winner never declared
```json
{
  "status": "running",
  "winner": {
    "has_winner": false,
    "p_value": 0.087
  }
}
```
**Solution**:
- Need more samples (p=0.087 is close, but > 0.05 threshold)
- Continue running the experiment

### Issue: High p-value despite large sample size
**Possible causes**:
1. True null effect (variants actually perform the same)
2. Effect size too small to detect with this sample size
3. High variance in metric (need to reduce noise)

**Solution**:
- Try a larger effect size (e.g., temp 0.6 vs 0.9 instead of 0.7 vs 0.8)
- Use lower-noise metrics (transfer_rate is less noisy than grade_score)
- Increase min_samples_per_variant for higher sensitivity

## Future Enhancements

- [ ] Redis persistence (survive server restarts)
- [ ] Bayesian A/B testing (faster convergence)
- [ ] Sequential testing (early stopping rules)
- [ ] Stratified assignment (control for lead quality)
- [ ] Multi-variant tests (A/B/C/D)
- [ ] Cohort analysis (performance by lead segment)
- [ ] Power analysis calculator (sample size estimation)
- [ ] Visualization dashboard (results charts)

## References

- Welch's t-test: https://en.wikipedia.org/wiki/Welch%27s_t-test
- Z-test for proportions: https://en.wikipedia.org/wiki/Proportion_test
- Statistical power: https://en.wikipedia.org/wiki/Statistical_power
- A/B testing best practices: https://www.optimizely.com/optimization-glossary/ab-testing/
