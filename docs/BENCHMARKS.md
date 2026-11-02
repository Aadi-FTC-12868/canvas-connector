# Hosted synthetic workload

The benchmark starts real loopback HTTP workers with encrypted shared storage,
64 isolated synthetic accounts, actual MCP authentication, and a mocked upstream
profile response delayed by 20 ms. It makes 1,000 requests with client concurrency
32 and checks every returned account identity. No Canvas credentials or network
requests are used. PostgreSQL mode creates and removes a random test schema.

Two local snapshots on macOS arm64 / Python 3.12.7:

| Store | Workers | Successful / attempted | Requests per second | p50 | p95 | p99 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| SQLite | 1 | 1,000 / 1,000 | 403.20 | 77.48 ms | 99.94 ms | 114.92 ms |
| SQLite | 2 | 1,000 / 1,000 | 400.97 | 72.62 ms | 140.91 ms | 165.23 ms |

There were no transport, protocol, or identity errors. Two workers did not improve
throughput in this workload; the second snapshot had higher tail latency. These
single snapshots are not a controlled scaling study or evidence of a regression.
Do not infer a production capacity number from them. Shared store operations and
per-worker connection serialization remain contention points. More workers do
not remove those limits.

Latency includes server queueing and excludes time waiting for the benchmark's
client semaphore. This measures a small authenticated profile response, not
Canvas throttling, school-network latency, dashboards with many upstream calls,
PDF extraction, uploads, or OAuth onboarding. Those workloads need deployment
measurements. Normal grant and body limits remain enabled.

## Reproduce

```sh
python scripts/benchmark_hosted.py --workers 1 --concurrency 32 --requests 1000 --output dist/benchmark-one.json
python scripts/benchmark_hosted.py --workers 2 --concurrency 32 --requests 1000 --output dist/benchmark-two.json
```

For PostgreSQL, set `CONNECTOR_BENCHMARK_POSTGRES_URL` privately to a disposable
database, then run:

```sh
python scripts/benchmark_hosted.py --database postgres --workers 2 --concurrency 32 --requests 1000 --output dist/benchmark-postgres.json
```

CI runs this PostgreSQL workload and includes its JSON result with the release
artifacts. Compare measurements on equivalent hardware and workloads; do not
compare a CI PostgreSQL result directly against the local SQLite snapshots.
