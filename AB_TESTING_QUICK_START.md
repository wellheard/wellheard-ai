# A/B Testing Quick Start

## 5-Minute Setup

### 1. Create an Experiment
```bash
curl -X POST http://localhost:8000/v1/ab-test/create \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "speed_test_v2",
    "description": "Speaking speed 0.95x vs 1.0x",
    "metric": "transfer_rate",
    "variant_a": {"speed": 0.95},
    "variant_b": {"speed": 1.0},
    "min_samples_per_variant": 20
  }'
```

### 2. Get Test Status (Watch Progress)
```bash
# Watch results update in real-time
watch -n 2 "curl -s 'http://localhost:8000/v1/ab-test/status?experiment_name=speed_test_v2' \
  -H 'Authorization: Bearer $API_KEY' | jq '.winner'"
```

### 3. In Your Call Handler: Apply Overrides
```python
# When starting a call
variant_response = await manager.assign_variant(call_id, "speed_test_v2")
config_overrides = variant_response["config_overrides"]
# Apply: agent_config.speed = config_overrides.get("speed", 0.97)
```

### 4. After Grading: Record Result
```bash
curl -X POST http://localhost:8000/v1/ab-test/record-result \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"call_id\": \"$CALL_ID\",
    \"experiment_name\": \"speed_test_v2\",
    \"grade_score\": 78.5,
    \"transfer_attempted\": true,
    \"transfer_completed\": true,
    \"latency_p95_ms\": 450.0,
    \"latency_avg_ms\": 320.0,
    \"total_turns\": 6,
    \"duration_seconds\": 125.3,
    \"cost_usd\": 0.42
  }"
```

### 5. When Winner Declared
```bash
# Get winner
curl -s 'http://localhost:8000/v1/ab-test/status?experiment_name=speed_test_v2' \
  -H "Authorization: Bearer $API_KEY" | jq '.winner'

# Output:
# {
#   "has_winner": true,
#   "winner": "variant_b",
#   "p_value": 0.032,
#   "confidence": 0.968,
#   ...
# }

# Implement the winner
# Then stop the experiment
curl -X POST "http://localhost:8000/v1/ab-test/stop?experiment_name=speed_test_v2" \
  -H "Authorization: Bearer $API_KEY"
```

---

## Pre-Built Experiments

These 4 experiments are automatically created:

| Name | Tests | Metric | Hypothesis |
|------|-------|--------|-----------|
| `speed_test` | 0.97x vs 1.0x speech speed | transfer_rate | Slower = more trust |
| `temperature_test` | 0.7 vs 0.8 | transfer_rate | Higher temp = more natural |
| `max_tokens_test` | 40 vs 50 tokens | grade_score | More tokens = better context |
| `prompt_length_test` | Full vs 30% shorter prompt | grade_score | Shorter = faster |

---

## Metrics Reference

### Transfer Rate (Binary)
- **What**: % of calls that resulted in successful transfer
- **Formula**: `transfer_count / total_calls`
- **Better**: Higher
- **Test**: Z-test for proportions
- **Use case**: Testing changes that affect prospect interest (speed, warmth, clarity)

### Grade Score (0-100)
- **What**: Call quality score from grader
- **Better**: Higher
- **Test**: Welch's t-test
- **Use case**: Testing changes that affect overall quality

### Latency P95 (milliseconds)
- **What**: 95th percentile round-trip latency
- **Better**: Lower (faster)
- **Test**: Welch's t-test
- **Use case**: Testing changes affecting response speed

---

## Config Overrides Reference

```python
# Supported config parameters
{
    "speed": 0.97,           # 0.8-1.2 (1.0 = normal)
    "temperature": 0.7,      # 0.0-1.0 (consistency)
    "max_tokens": 40,        # Response length
    "system_prompt": "..."   # Full prompt text
}
```

---

## Status Response Format

```json
{
  "name": "speed_test_v2",
  "status": "running|complete|pending",
  "winner": {
    "has_winner": true,
    "winner": "variant_a|variant_b",
    "p_value": 0.032,        // Lower = more significant
    "confidence": 0.968      // 1 - p_value
  },
  "variant_a": {
    "sample_count": 25,
    "transfer_rate": 0.72,
    "grade_score_mean": 76.3
  },
  "variant_b": {
    "sample_count": 25,
    "transfer_rate": 0.80,
    "grade_score_mean": 79.1
  }
}
```

**When to declare winner**:
- `has_winner: true` → You have a winner!
- `has_winner: false` → Keep running, need more samples

---

## Common Issues

| Problem | Solution |
|---------|----------|
| "Experiment not found" | Create it first: POST /v1/ab-test/create |
| "Result not recorded" | Ensure call_id was assigned (POST /v1/ab-test/assign) |
| P-value won't drop below 0.05 | Need more samples OR effect size too small |
| "Insufficient samples" in winner | Run longer: need min_samples_per_variant in both |

---

## Pro Tips

1. **Use description field to document hypothesis**
   ```json
   "description": "Speed 0.95x expected to increase comprehension → higher transfer rate"
   ```

2. **Lower min_samples_per_variant for quick tests**
   ```json
   "min_samples_per_variant": 10  // instead of default 20
   ```

3. **Monitor progress with jq**
   ```bash
   watch "curl -s $URL | jq '.variant_a.results.sample_count, .variant_b.results.sample_count'"
   ```

4. **Test one variable at a time**
   - Only change speed, keep everything else same
   - Only change temperature, keep everything else same
   - Otherwise you won't know which change helped

5. **Run overnight/weeklong**
   - Overnight: tests ~50-100 calls (enough for some metrics)
   - Week: tests ~500-1000 calls (high confidence)
   - Time of day matters: try to balance testing across different times

---

## Full Documentation

See **AB_TESTING_GUIDE.md** for:
- Complete API reference
- Integration instructions
- Statistical methods
- Best practices
- Future enhancements
