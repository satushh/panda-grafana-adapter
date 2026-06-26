# panda-grafana-adapter

Turn [ethPandaOps `panda`](https://github.com/ethpandaops/panda) into **Grafana datasources** — chart the whole devnet fleet's **metrics** and **logs** in your own Grafana, with no cluster access and no token juggling.

`panda` already handles the hard parts: OIDC auth + automatic token refresh + the authenticated proxy to ethPandaOps' VictoriaMetrics (metrics) and ClickHouse (logs). This adapter is a thin HTTP shim that maps Grafana's **Prometheus** and **Loki** APIs onto `panda` subcommands:

```
Grafana ──(Prometheus API /api/v1/*)──▶ adapter ──▶ panda prometheus …  ──▶ panda-server ─▶ ethPandaOps (VictoriaMetrics)
        ──(Loki API     /loki/api/v1/*)─▶         ──▶ panda clickhouse … ──▶              ─▶              (ClickHouse otel_logs)
```

One process, two datasources. Read-only. Stores no secrets.

## Why

`panda` is a CLI; Grafana wants HTTP datasources. Pointing Grafana straight at the ethPandaOps proxy would mean copying a bearer token that **expires every ~24h**. Going through `panda` means its auto-refreshing auth is reused — set up once, keeps working.

## Requirements

- `panda` installed, on `PATH`, and authenticated (`panda auth status`).
- The **panda server** running (`panda server status` → healthy). `panda` routes queries through it; it runs as a Docker container, so **Docker must be up**. If Docker/panda-server is down, the adapter returns 502s until they're back (it recovers automatically).
- Python 3.8+ (standard library only — no pip installs).

## Run

```bash
python3 panda_grafana_adapter.py --port 9119
# flags: --datasource devnets  --clickhouse clickhouse-raw  --table external.otel_logs
#        --bind 0.0.0.0  --panda $(which panda)  --timeout 90
```

Run it **on the host** where `panda` is authenticated (it shells out to `panda`).

### Keep it running (macOS launchd)

`~/Library/LaunchAgents/com.ethpandaops.panda-grafana-adapter.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.ethpandaops.panda-grafana-adapter</string>
  <key>ProgramArguments</key><array>
    <string>/opt/homebrew/bin/python3</string>
    <string>/Users/you/panda-grafana-adapter/panda_grafana_adapter.py</string>
    <string>--port</string><string>9119</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/path/to/panda/dir:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/panda-grafana-adapter.log</string>
  <key>StandardOutPath</key><string>/tmp/panda-grafana-adapter.log</string>
</dict></plist>
```
```bash
launchctl load ~/Library/LaunchAgents/com.ethpandaops.panda-grafana-adapter.plist
launchctl kickstart -k gui/$(id -u)/com.ethpandaops.panda-grafana-adapter   # restart after edits
```
The `PATH` must include the dir containing `panda` (`dirname $(which panda)`).

## Wire into Grafana

Two datasources, both pointing at the adapter (Docker Desktop/Mac resolves `host.docker.internal` automatically; on Linux add `extra_hosts: ["host.docker.internal:host-gateway"]` to the Grafana service):

```yaml
# provisioning/datasources/*.yaml
apiVersion: 1
datasources:
  - uid: ethpandaops-devnets        # METRICS
    name: EthPandaOps Devnets
    type: prometheus
    access: proxy
    url: http://host.docker.internal:9119
    jsonData: { httpMethod: POST, timeInterval: 12s }
  - uid: ethpandaops-devnet-logs    # LOGS
    name: EthPandaOps Devnet Logs
    type: loki
    access: proxy
    url: http://host.docker.internal:9119
```

Scope queries with labels: `network`, `consensus_client`, `execution_client`, `instance`, etc. (metrics); `network`, `host`, `container`, `cl`, `el`, `level`, `instance` (logs).

## Endpoints

**Prometheus** (`type: prometheus`): `/api/v1/query`, `/query_range`, `/labels`, `/label/<name>/values` (honors `match[]` so `label_values(metric{…}, label)` template vars work), `/status/buildinfo`. `/series`, `/metadata`, `/query_exemplars` stubbed.

**Loki** (`type: loki`): `/loki/api/v1/query_range` & `/query` (log **stream selectors** `{…} |= "x" |~ "re"` → log streams; level is parsed from the line), `/labels`, `/label/<name>/values`. Pure scalar/vector metric queries (e.g. the health probe `vector(1)+vector(1)`) are delegated to the Prometheus backend.

## Example dashboards

`dashboards/` (also provisionable in Grafana):
- **devnet6-comprehensive** — everything for one devnet on a single page (overview, fleet health, participation, ePBS builder/payloads, networking, per-node drilldown, logs)
- **devnet6-prysm-health** — fixed Prysm fleet view (head, finalized, participation, slots-behind, peers, gossip score)
- **devnet-fleet-cl-health** — parameterized by `$network` + multi-select `$consensus_client`, with cross-client comparison
- **devnet-prysm-node** — per-node drilldown (`$network`/`$instance`) **with an embedded logs panel**
- **devnet-prysm-att-fc** — attestation participation, inclusion delay, reorgs, forkchoice empty/full nodes

## Limitations

- **LogQL metric queries** over streams (`count_over_time({…}[5m])`, `rate(…)`) aren't translated — log-volume histograms render empty; log lines themselves work fully.
- One `panda` process is spawned per request (~100–300 ms). Fine for personal dashboards; for heavy use, call the panda-server MCP endpoint instead of the CLI.
- Depends on Docker + panda-server being up (see Requirements).
- Binds `0.0.0.0` so containers can reach it — a read-only relay of non-sensitive devnet telemetry, but LAN-reachable; firewall if needed.

## License

MIT — see [LICENSE](LICENSE).
