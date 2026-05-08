# Deployment Plan

## Services

1. Public/internal API service: FastAPI
2. Risk engine: Python library/service
3. Market data adapter: WebSocket/polling provider integrations
4. Trigger engine: evaluates portfolios on market events
5. Liquidation engine: creates orders or recommendations
6. Audit store: JSONL locally, Postgres/S3/Kinesis in production
7. Simulation runner: historical and synthetic backtesting

## Event flow

```text
Portfolio update / loan update / market data event
        ↓
Risk engine evaluation
        ↓
Margin state decision
        ↓
Audit log
        ↓
Webhook / client execution endpoint if action is required
```

## Data privacy modes

- Cloud mode: client sends anonymized portfolio state
- Stateless mode: raw holdings are evaluated but not persisted
- Private mode: container runs in client's environment, close to portfolio data

## Execution modes

- Advisory: return recommendation only
- Execution: send liquidation instruction to client endpoint

## Production hardening needed next

- Auth and tenant isolation
- Policy version storage
- Persistent account state
- Market data provider integrations
- WebSocket stream consumer
- Webhook retry logic
- Postgres audit store
- Scenario/backtest report generator
- Deployment IaC
