# Wave 6 — staging load + concurrency validation

Run directory: `/Users/alexkashkarian/Desktop/cosentus-voice-enginev2/backend/voice-agent/scripts/wave6-runs/b1-soak-2h`

## Overall summary

| Scenario | Status | Duration (s) | Calls | Accepted | Rejected 503 | Other | P95 latency (ms) |
|---|---|---|---|---|---|---|---|
| A | NOT RUN | — | — | — | — | — | — |
| B | NOT RUN | — | — | — | — | — | — |
| C | NOT RUN | — | — | — | — | — | — |
| D | NOT RUN | — | — | — | — | — | — |
| E | FAIL | 7321.3 | 1440 | 1387 | 0 | 53 | 1031.8 |

---

## Scenario A — NOT RUN

`/Users/alexkashkarian/Desktop/cosentus-voice-enginev2/backend/voice-agent/scripts/wave6-runs/b1-soak-2h/scenario_a.json` not found.

---

## Scenario B — NOT RUN

`/Users/alexkashkarian/Desktop/cosentus-voice-enginev2/backend/voice-agent/scripts/wave6-runs/b1-soak-2h/scenario_b.json` not found.

---

## Scenario C — NOT RUN

`/Users/alexkashkarian/Desktop/cosentus-voice-enginev2/backend/voice-agent/scripts/wave6-runs/b1-soak-2h/scenario_c.json` not found.

---

## Scenario D — NOT RUN

`/Users/alexkashkarian/Desktop/cosentus-voice-enginev2/backend/voice-agent/scripts/wave6-runs/b1-soak-2h/scenario_d.json` not found.

---

## Scenario E — FAIL

**Description.** 4-hour soak at 0.2 cps (12 cpm). Steady-state. Catches memory/FD leaks, vendor throttling under sustained load.

**Window.** 2026-05-18T22:32:52.325357+00:00 → 2026-05-19T18:58:32.919316+00:00 (7321.3 s).

**Scenario knobs.**

```json
{
  "soak_duration_secs": 7200,
  "soak_calls_per_sec": 0.2,
  "heartbeat_interval_secs": 60,
  "error_rate_threshold_pct": 1.0
}
```

**/start outcomes.**

| Key | Value |
|---|---|
| `count` | `1440` |
| `accepted_202` | `1387` |
| `rejected_503` | `0` |
| `other` | `{"timeout":32,"error":21}` |
| `latency_ms` | `{"p50":485.0,"p95":1031.8,"p99":10857.1}` |

**CloudWatch stats.**

```json
{
  "active_sessions_max": {
    "metric_name": "ActiveSessions",
    "namespace": "VoiceAgent/Pipeline",
    "statistic": "Maximum",
    "period_secs": 60,
    "datapoints": 1229,
    "min": 0.0,
    "max": 1.0,
    "avg": 0.01,
    "sum": 17.0
  },
  "active_sessions_avg": {
    "metric_name": "ActiveSessions",
    "namespace": "VoiceAgent/Pipeline",
    "statistic": "Average",
    "period_secs": 60,
    "datapoints": 1229,
    "min": 0.0,
    "max": 0.5,
    "avg": 0.0,
    "sum": 4.17
  },
  "ecs_cpu_avg": {
    "metric_name": "CPUUtilization",
    "namespace": "AWS/ECS",
    "statistic": "Average",
    "period_secs": 60,
    "datapoints": 1227,
    "min": 0.03,
    "max": 21.9,
    "avg": 0.82,
    "sum": 1003.08
  },
  "ecs_memory_avg": {
    "metric_name": "MemoryUtilization",
    "namespace": "AWS/ECS",
    "statistic": "Average",
    "period_secs": 60,
    "datapoints": 1227,
    "min": 6.56,
    "max": 32.4,
    "avg": 11.72,
    "sum": 14385.18
  },
  "drain_timeouts_sum": {
    "metric_name": "DrainTimeouts",
    "namespace": "VoiceAgent/Pipeline",
    "statistic": "Sum",
    "period_secs": 3600,
    "datapoints": 0,
    "min": 0.0,
    "max": 0.0,
    "avg": 0.0,
    "sum": 0.0
  },
  "memory_first_hour_avg": {
    "metric_name": "MemoryUtilization",
    "namespace": "AWS/ECS",
    "statistic": "Average",
    "period_secs": 60,
    "datapoints": 60,
    "min": 10.69,
    "max": 32.4,
    "avg": 17.07,
    "sum": 1024.46
  },
  "memory_last_hour_avg": {
    "metric_name": "MemoryUtilization",
    "namespace": "AWS/ECS",
    "statistic": "Average",
    "period_secs": 60,
    "datapoints": 59,
    "min": 10.01,
    "max": 22.06,
    "avg": 15.54,
    "sum": 916.87
  }
}
```

**ECS service state.**

```json
{
  "pre": {
    "desired_count": 2,
    "running_count": 2,
    "pending_count": 0,
    "deployments": 1,
    "primary_deployment_status": "COMPLETED",
    "task_definition_arn": "arn:aws:ecs:us-east-1:825269749545:task-definition/cosentus-voice-engine-staging:5",
    "running_task_count": 2
  },
  "post": {
    "desired_count": 2,
    "running_count": 2,
    "pending_count": 0,
    "deployments": 1,
    "primary_deployment_status": "COMPLETED",
    "task_definition_arn": "arn:aws:ecs:us-east-1:825269749545:task-definition/cosentus-voice-engine-staging:5",
    "running_task_count": 2
  },
  "cycles_logged": 119
}
```

**Checks.**

| Check | Status | Observed | Expected | Note |
|---|---|---|---|---|
| error_rate_under_threshold — Soak error rate exceeded threshold. | FAIL | `3.68` | `<= 1.0%` |  |
| memory_trend_stable — ECS memory utilization didn't grow > 20 pct points across the soak. | PASS | `-1.53` | `< 20` |  |
| no_drain_timeouts — No DrainTimeouts during soak. | PASS | `0.0` | `None` |  |
| no_task_replacement — ECS running task count unchanged across the soak. | PASS | `2 -> 2` | `None` |  |
| cpu_reasonable — ECS CPU max stayed under 60% during soak. | PASS | `21.90036544071821` | `< 60` |  |
| cycle_log_complete — Heartbeat log captured >= 90% of expected cycles. | PASS | `119` | `~120` |  |

**Notes.**

- Per-cycle log: /Users/alexkashkarian/Desktop/cosentus-voice-enginev2/backend/voice-agent/scripts/wave6-runs/b1-soak-2h/scenario_e.jsonl

---
