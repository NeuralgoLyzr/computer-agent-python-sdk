# OTel destination recipes

`OtelSink` is **vendor-neutral OTLP**. Any backend that accepts OTLP/HTTP works without code changes — only two env vars differ. Use this page as a copy-paste reference.

## Two configuration surfaces

1. **Python sink layer** — where traces leave the worker. Controls `OtelSink` directly. Env vars: `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SERVICE_NAME`, `COMPUTERAGENT_OTEL`.
2. **Platform layer** — where the AgentOS SPA's Observability tab queries traces from. Controls the TS `agentos-server`. Env vars on that service: `TRACE_BACKEND` (`clickhouse` | `newrelic`), plus backend-specific credentials.

These are independent. Workers can write to one backend while the SPA reads from another — useful for fan-out (archive + query layer).

## Common destinations

### Local docker-compose collector → ClickHouse (default)

```bash
# Already the default in examples/e2e/run_live_demo.py
unset OTEL_EXPORTER_OTLP_ENDPOINT          # falls back to http://localhost:4318
unset OTEL_EXPORTER_OTLP_HEADERS
```

SPA Observability tab queries the same ClickHouse via `agentos-server`. Good for offline / air-gapped dev.

### New Relic (direct OTLP)

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp.nr-data.net
export OTEL_EXPORTER_OTLP_HEADERS=api-key=NRRX-YOUR-LICENSE-KEY
export OTEL_SERVICE_NAME=acme-assist-qa
```

Find spans at https://one.newrelic.com/ → APM & services → your service.

To make the SPA Observability tab read from New Relic instead, set on `agentos-server`:
```
TRACE_BACKEND=newrelic
NEW_RELIC_API_KEY=...
NEW_RELIC_ACCOUNT_ID=...
```

### Datadog (via local DD Agent OTLP intake)

```bash
# Requires Datadog Agent running with OTLP enabled. In datadog.yaml:
#   otlp_config:
#     receiver:
#       protocols:
#         http:
#           endpoint: 0.0.0.0:4318
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
unset OTEL_EXPORTER_OTLP_HEADERS              # DD Agent handles auth
export OTEL_SERVICE_NAME=acme-assist-qa
```

### Datadog (direct OTLP intake, no agent)

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://trace.agent.datadoghq.com
export OTEL_EXPORTER_OTLP_HEADERS=DD-API-KEY=YOUR-DD-API-KEY
```

Replace `datadoghq.com` with `datadoghq.eu` etc. for non-US sites.

### Honeycomb

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://api.honeycomb.io
export OTEL_EXPORTER_OTLP_HEADERS=x-honeycomb-team=YOUR-HONEYCOMB-API-KEY
export OTEL_SERVICE_NAME=acme-assist-qa
```

### Grafana Cloud Tempo

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://tempo-prod-XX-prod-XX-XXX.grafana.net:443
# Basic auth: base64("instance_id:api_key")
export OTEL_EXPORTER_OTLP_HEADERS=authorization=Basic\ XXXXXXXX
```

### AWS X-Ray (via ADOT collector)

Run the AWS Distro for OpenTelemetry collector with the X-Ray exporter, then point at it:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318  # the ADOT collector
```

### Self-hosted Jaeger / Tempo / SigNoz

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://<your-host>:4318
unset OTEL_EXPORTER_OTLP_HEADERS
```

### Console (debug / no backend)

```bash
unset OTEL_EXPORTER_OTLP_ENDPOINT
# Programmatic:
#   configure(sinks=[OtelSink(exporter="console")])
```

Spans print to stdout in OTLP-JSON. Useful for offline debugging or shape-verifying a new policy/middleware.

### Disable OTel entirely

```bash
export COMPUTERAGENT_OTEL=disabled
```

The OtelSink is not auto-attached; AgentOS Mongo writes still happen via `AgentRegistrySink`/`MongoMessageSink`.

## Programmatic equivalent

If you'd rather set everything in code (e.g. for multi-tenant workers that switch destinations per-request):

```python
from computeragent import configure
from computeragent.telemetry.sinks import OtelSink

configure(
    sinks=[
        OtelSink(
            exporter="otlp-http",
            endpoint="https://otlp.nr-data.net",
            headers={"api-key": os.environ["NR_LICENSE_KEY"]},
            # service_name="acme-assist-qa",  # optional override
        )
    ],
)
```

## Multi-destination fan-out

`OtelSink` writes to one destination at a time. To fan out to multiple backends, use the **otel-collector** in the middle — it accepts OTLP from your worker and re-exports to N downstream pipelines simultaneously. The bundled local collector already demonstrates this pattern (ClickHouse + debug log).

A representative multi-exporter `otel-collector-config.yaml` snippet:

```yaml
exporters:
  clickhouse: { endpoint: tcp://clickhouse:9000 }
  otlphttp/honeycomb:
    endpoint: https://api.honeycomb.io
    headers: { x-honeycomb-team: ${env:HONEYCOMB_KEY} }
  otlphttp/newrelic:
    endpoint: https://otlp.nr-data.net
    headers: { api-key: ${env:NR_LICENSE_KEY} }

service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [clickhouse, otlphttp/honeycomb, otlphttp/newrelic]
```

The worker still emits to one endpoint (`http://otel-collector:4318`); the collector fans out.
