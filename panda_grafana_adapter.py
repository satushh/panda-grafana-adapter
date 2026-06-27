#!/usr/bin/env python3
"""
panda-grafana-adapter
=====================
Expose ethPandaOps devnet telemetry (via the `panda` CLI) to Grafana as:

  * a **Prometheus** datasource  -> backed by `panda prometheus ...`   (metrics)
  * a **Loki** datasource        -> backed by `panda clickhouse ...`   (logs, otel_logs)

panda already handles OIDC auth + token refresh + the authenticated proxy to
ethPandaOps' VictoriaMetrics (metrics) and ClickHouse (logs). This adapter is a
thin translation layer that maps Grafana's Prometheus/Loki HTTP APIs onto panda
subcommands. One process, two datasources (route by path prefix).

Read-only. Stores no secrets. Must run on the host where `panda` is authenticated.

    python3 panda_grafana_adapter.py --port 9119

Grafana datasources (both point at the same base URL, e.g. http://host.docker.internal:9119):
  - type: prometheus   (serves /api/v1/*)
  - type: loki         (serves /loki/api/v1/*)
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

CFG = {"panda": "panda", "datasource": "devnets", "ch": "clickhouse-raw",
       "table": "external.otel_logs", "timeout": 90}

# ----------------------------------------------------------------------------
# panda invocation helpers
# ----------------------------------------------------------------------------
def _decode_panda(stdout, stderr):
    out = stdout or ""
    start = min([i for i in (out.find("{"), out.find("[")) if i != -1], default=-1)
    if start == -1:
        return False, (stderr or out or "no output from panda").strip()[:600]
    try:
        obj, _ = json.JSONDecoder().raw_decode(out[start:])
    except json.JSONDecodeError as e:
        return False, f"could not parse panda json: {e}"
    return True, obj


def run_prom(args):
    """`panda prometheus <args>` -> (ok, prometheus_response | err_str)."""
    cmd = [CFG["panda"], "--log-level", "error", "prometheus"] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=CFG["timeout"])
    except subprocess.TimeoutExpired:
        return False, "panda prometheus timed out"
    except FileNotFoundError:
        return False, f"panda binary not found: {CFG['panda']}"
    ok, obj = _decode_panda(p.stdout, p.stderr)
    if ok and isinstance(obj, dict) and obj.get("status") == "error":
        return False, obj.get("error", "panda status=error")
    return ok, obj


def prom_label_values_match(name, matches):
    """Resolve label_values(metric{...}, label): query each selector and collect
    distinct values of `name`. Lets Grafana template vars filter by metric."""
    vals = set()
    for sel in matches:
        ok, obj = run_prom(["query", CFG["datasource"], sel, "-o", "json"])
        if not ok:
            return False, obj
        for r in obj.get("data", {}).get("result", []):
            v = r.get("metric", {}).get(name)
            if v:
                vals.add(v)
    return True, sorted(vals)


def run_ch(sql):
    """`panda clickhouse query-raw <ch> <sql>` -> (ok, {columns,rows} | err_str)."""
    cmd = [CFG["panda"], "--log-level", "error", "clickhouse", "query-raw", CFG["ch"], sql]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=CFG["timeout"])
    except subprocess.TimeoutExpired:
        return False, "panda clickhouse timed out"
    except FileNotFoundError:
        return False, f"panda binary not found: {CFG['panda']}"
    ok, obj = _decode_panda(p.stdout, p.stderr)
    if ok and isinstance(obj, dict) and obj.get("error"):
        return False, str(obj.get("error"))[:600]
    return ok, obj

# ----------------------------------------------------------------------------
# Prometheus param normalization
# ----------------------------------------------------------------------------
def norm_step(s):
    s = (s or "60").strip()
    try:
        f = float(s)
        return f"{int(f) if f == int(f) else f}s"
    except ValueError:
        return s


def norm_ts(s):
    if not s:
        return None
    try:
        return str(int(float(s)))
    except ValueError:
        return s

# ----------------------------------------------------------------------------
# Loki: otel_logs mapping + LogQL subset
# ----------------------------------------------------------------------------
# ANSI-stripped Body; defined as a WITH-alias `clean` in every query that needs it.
CLEAN = r"replaceRegexpAll(Body, '\x1b\[[0-9;]*m', '')"
BODY_CLEAN = "clean"  # WITH-alias for the ANSI-stripped Body (defined per query)

# Normalised log level across all clients. SeverityText is unreliable (empty for
# besu/lodestar, scraped-from-message for nimbus), so parse the level token from
# each client's own line format (the client is known via ResourceAttributes), then
# fold abbreviations (WRN/ERRO/DBG/CRIT/...) into canonical WARN/ERROR/FATAL/INFO/…
_LVL_RAW = (
    "multiIf("
    "ResourceAttributes['container.name']='execution' AND ResourceAttributes['ethereum_el']='besu',"
    r" extract(clean, '\|\s*(TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s*\|'),"
    "ResourceAttributes['container.name']='execution' AND ResourceAttributes['ethereum_el'] IN ('ethrex','reth'),"
    r" extract(clean, 'Z\s+(TRACE|DEBUG|INFO|WARN|ERROR|FATAL)'),"
    "ResourceAttributes['container.name']='execution',"
    r" extract(clean, '^\s*(TRACE|DEBUG|INFO|WARN|ERROR|CRIT|EROR)'),"
    "ResourceAttributes['ethereum_cl']='nimbus',"
    r" extract(clean, '^\s*(TRC|DBG|INF|NTC|WRN|ERR|FAT|NOT)'),"
    "ResourceAttributes['ethereum_cl']='lighthouse',"
    r" extract(clean, '\d\d:\d\d:\d\d\.\d+\s+(TRCE|DEBG|INFO|WARN|ERRO|CRIT)'),"
    "ResourceAttributes['ethereum_cl']='prysm',"
    r" extract(clean, '\]\s+(TRACE|DEBUG|INFO|WARN|ERROR|FATAL|PANIC|DPANIC)'),"
    "ResourceAttributes['ethereum_cl']='teku',"
    r" extract(clean, '\.\d+\s+(TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+-'),"
    "ResourceAttributes['ethereum_cl']='lodestar',"
    r" extract(clean, '(?i)\]\s+(info|warn|error|debug|verbose|trace|silly)'),"
    r" extract(clean, '(?i)level=(\w+)'))"
)
LEVEL_EXPR = (
    "transform(upper(" + _LVL_RAW + "), "
    "['WARN','WRN','WARNING','ERROR','ERRO','ERR','EROR','FATAL','FATA','FAT','CRIT','CRITICAL','PANIC','DPANIC',"
    "'INFO','INF','DEBUG','DEBG','DBG','TRACE','TRC','TRCE','NOTICE','NTC','NOT'], "
    "['WARN','WARN','WARN','ERROR','ERROR','ERROR','ERROR','FATAL','FATAL','FATAL','FATAL','FATAL','FATAL','FATAL',"
    "'INFO','INFO','DEBUG','DEBUG','DEBUG','TRACE','TRACE','TRACE','NOTICE','NOTICE','NOTICE'], 'unknown')"
)

# Loki label name -> ClickHouse column/expression on external.otel_logs
LOKI_LABELS = {
    "network":   "ResourceAttributes['network']",
    "host":      "ResourceAttributes['host.name']",
    "container": "ResourceAttributes['container.name']",
    "cl":        "ResourceAttributes['ethereum_cl']",
    "el":        "ResourceAttributes['ethereum_el']",
    "service":   "ServiceName",
    "level":     LEVEL_EXPR,   # normalised, multi-client (parsed per format above)
    # synthetic label matching the Prometheus `instance` (network-host), so a
    # dashboard can drive metric + log panels off one $instance variable.
    "instance":  "concat(ResourceAttributes['network'], '-', ResourceAttributes['host.name'])",
}


def _sqlstr(v):
    return v.replace("\\", "\\\\").replace("'", "''")


def to_ns(s, default=None):
    if s is None or s == "":
        return default
    s = str(s)
    if s.isdigit():
        n = int(s)
        if n < 10**12:        # seconds
            return n * 10**9
        if n < 10**15:        # millis
            return n * 10**6
        return n              # nanos
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 10**9)
    except Exception:
        return default


def parse_logql(q):
    """Return (matchers: {label:(op,val)}, line_filters: [(op,val)]) for the
    common Grafana-generated LogQL subset: {a="x",b=~"y"} |= "z" != "w" |~ "re"."""
    q = (q or "").strip()
    matchers, filters, rest = {}, [], ""
    m = re.match(r'^\s*\{([^}]*)\}(.*)$', q, re.S)
    if m:
        for mm in re.finditer(r'(\w+)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"', m.group(1)):
            matchers[mm.group(1)] = (mm.group(2), mm.group(3))
        rest = m.group(2)
    else:
        rest = q
    for fm in re.finditer(r'(\|=|!=|\|~|!~)\s*(?:"((?:[^"\\]|\\.)*)"|`([^`]*)`)', rest):
        val = fm.group(2) if fm.group(2) is not None else fm.group(3)
        filters.append((fm.group(1), val))
    return matchers, filters


def loki_where(matchers, filters, start_ns, end_ns):
    w = [f"Timestamp >= fromUnixTimestamp64Nano({int(start_ns)})",
         f"Timestamp <= fromUnixTimestamp64Nano({int(end_ns)})"]
    for lbl, (op, val) in matchers.items():
        col = LOKI_LABELS.get(lbl)
        if not col:
            continue
        v = _sqlstr(val)
        if op == "=":
            w.append(f"{col} = '{v}'")
        elif op == "!=":
            w.append(f"{col} != '{v}'")
        elif op == "=~":
            w.append(f"match({col}, '{v}')")
        elif op == "!~":
            w.append(f"NOT match({col}, '{v}')")
    for op, val in filters:
        v = _sqlstr(val)
        if op == "|=":
            w.append(f"positionCaseInsensitive({BODY_CLEAN}, '{v}') > 0")
        elif op == "!=":
            w.append(f"positionCaseInsensitive({BODY_CLEAN}, '{v}') = 0")
        elif op == "|~":
            w.append(f"match({BODY_CLEAN}, '{v}')")
        elif op == "!~":
            w.append(f"NOT match({BODY_CLEAN}, '{v}')")
    return " AND ".join(w)


def loki_query(P, instant=False):
    q = (P.get("query", "") or "").strip()
    if not q.startswith("{"):
        # Not a log stream selector -> a metric LogQL expression.
        if "{" not in q:
            # Pure scalar/vector math (e.g. Grafana's health probe
            # `vector(1)+vector(1)`) is also valid PromQL -> delegate so the
            # returned value is correct.
            if instant:
                return run_prom(["query", CFG["datasource"], q, "-o", "json"])
            return run_prom(["query-range", CFG["datasource"], q,
                             "--start", norm_ts(P.get("start")) or "now-1h",
                             "--end", norm_ts(P.get("end")) or "now",
                             "--step", norm_step(P.get("step")), "-o", "json"])
        # LogQL metric over a stream selector (count_over_time/rate/...): not
        # supported here -> empty but valid so log-volume panels don't error.
        if instant:
            return True, {"status": "success", "data": {"resultType": "vector", "result": []}}
        return True, {"status": "success", "data": {"resultType": "matrix", "result": []}}
    matchers, filters = parse_logql(q)
    end_ns = to_ns(P.get("end"), default=time.time_ns())
    start_ns = to_ns(P.get("start"), default=end_ns - 3600 * 10**9)
    limit = min(int(P.get("limit") or 1000), 5000)
    order = "ASC" if P.get("direction") == "forward" else "DESC"
    where = loki_where(matchers, filters, start_ns, end_ns)
    sql = (f"WITH {CLEAN} AS clean "
           f"SELECT toString(toUnixTimestamp64Nano(Timestamp)) AS ts, clean AS line, "
           f"ResourceAttributes['host.name'] AS host, ResourceAttributes['container.name'] AS container, "
           f"ResourceAttributes['ethereum_cl'] AS cl, {LEVEL_EXPR} AS level "
           f"FROM {CFG['table']} WHERE {where} ORDER BY Timestamp {order} LIMIT {limit}")
    ok, res = run_ch(sql)
    if not ok:
        return False, res
    # panda clickhouse query-raw drops trailing empty cells, so rows can be
    # shorter than the column list -> pad defensively before positional access.
    cols = res.get("columns", [])
    ncol = len(cols)
    idx = {c: i for i, c in enumerate(cols)}
    streams = {}
    for r in res.get("rows", []):
        if len(r) < ncol:
            r = list(r) + [""] * (ncol - len(r))
        key = (r[idx["host"]], r[idx["container"]], r[idx["cl"]], r[idx["level"]])
        s = streams.setdefault(key, {"stream": {"host": key[0], "container": key[1],
                                                "cl": key[2], "level": key[3] or "unknown"},
                                     "values": []})
        s["values"].append([r[idx["ts"]], r[idx["line"]]])
    return True, {"status": "success",
                  "data": {"resultType": "streams", "result": list(streams.values())}}


def loki_label_values(name, P):
    col = LOKI_LABELS.get(name)
    if not col:
        return True, {"status": "success", "data": []}
    end_ns = to_ns(P.get("end"), default=time.time_ns())
    start_ns = to_ns(P.get("start"), default=end_ns - 3600 * 10**9)
    sql = (f"WITH {CLEAN} AS clean SELECT DISTINCT {col} AS v FROM {CFG['table']} "
           f"WHERE Timestamp >= fromUnixTimestamp64Nano({int(start_ns)}) "
           f"AND Timestamp <= fromUnixTimestamp64Nano({int(end_ns)}) AND {col} != '' "
           f"ORDER BY v LIMIT 2000")
    ok, res = run_ch(sql)
    if not ok:
        return False, res
    return True, {"status": "success", "data": [r[0] for r in res.get("rows", [])]}

# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _ok(self, data):
        self._json(200, {"status": "success", "data": data})

    def _err(self, msg, code=400):
        self._json(code, {"status": "error", "errorType": "bad_data", "error": msg})

    def _relay(self, ok, obj):
        self._json(200, obj) if ok else self._err(obj, 502)

    def _params(self):
        u = urlparse(self.path)
        params = parse_qs(u.query, keep_blank_values=True)
        if self.command == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            for k, v in parse_qs(self.rfile.read(n).decode() if n else "").items():
                params.setdefault(k, v)
        return u.path, {k: (v[0] if v else "") for k, v in params.items()}, params

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def _route(self):
        try:
            path, P, raw = self._params()
        except Exception as e:
            return self._err(f"bad request: {e}", 400)
        ds = CFG["datasource"]
        try:
            # -------- Prometheus (metrics) --------
            if path == "/api/v1/query":
                a = ["query", ds, P.get("query", "")]
                t = norm_ts(P.get("time"))
                if t:
                    a += ["--time", t]
                self._relay(*run_prom(a + ["-o", "json"]))
            elif path == "/api/v1/query_range":
                self._relay(*run_prom(["query-range", ds, P.get("query", ""),
                                       "--start", norm_ts(P.get("start")) or "now-1h",
                                       "--end", norm_ts(P.get("end")) or "now",
                                       "--step", norm_step(P.get("step")), "-o", "json"]))
            elif path == "/api/v1/labels":
                self._relay(*run_prom(["labels", ds, "-o", "json"]))
            elif path.startswith("/api/v1/label/") and path.endswith("/values"):
                name = unquote(path[len("/api/v1/label/"):-len("/values")])
                matches = raw.get("match[]") or []
                if matches:
                    ok, vals = prom_label_values_match(name, matches)
                    self._ok(vals) if ok else self._err(vals, 502)
                else:
                    self._relay(*run_prom(["label-values", ds, name, "-o", "json"]))
            elif path == "/api/v1/series":
                self._ok([])
            elif path == "/api/v1/metadata":
                self._ok({})
            elif path == "/api/v1/query_exemplars":
                self._ok([])
            elif path == "/api/v1/status/buildinfo":
                self._ok({"version": "2.50.1", "revision": "panda-grafana-adapter",
                          "branch": "", "features": {}})

            # -------- Loki (logs) --------
            elif path == "/loki/api/v1/query_range":
                self._relay(*loki_query(P, instant=False))
            elif path == "/loki/api/v1/query":
                self._relay(*loki_query(P, instant=True))
            elif path == "/loki/api/v1/labels":
                self._ok(list(LOKI_LABELS.keys()))
            elif path.startswith("/loki/api/v1/label/") and path.endswith("/values"):
                name = unquote(path[len("/loki/api/v1/label/"):-len("/values")])
                self._relay(*loki_label_values(name, P))
            elif path == "/loki/api/v1/series":
                self._ok([])
            elif path == "/loki/api/v1/index/stats":
                self._ok({"streams": 0, "chunks": 0, "bytes": 0, "entries": 0})
            elif path in ("/ready", "/loki/api/v1/status/buildinfo"):
                self._json(200, {"status": "success", "data": "ready"})

            # -------- health / root --------
            elif path in ("/", "/health", "/-/healthy", "/-/ready"):
                self._json(200, {"status": "success",
                                 "data": f"panda-grafana-adapter (metrics+logs) -> '{ds}' ok"})
            else:
                self._err(f"unsupported endpoint: {path}", 404)
        except Exception as e:
            self._err(f"adapter error: {e}", 500)

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description="panda -> Prometheus+Loki adapter for Grafana")
    ap.add_argument("--port", type=int, default=9119)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--datasource", default="devnets", help="panda prometheus datasource")
    ap.add_argument("--clickhouse", default="clickhouse-raw", help="panda clickhouse datasource")
    ap.add_argument("--table", default="external.otel_logs", help="otel logs table")
    ap.add_argument("--panda", default=shutil.which("panda") or "panda")
    ap.add_argument("--timeout", type=int, default=90)
    a = ap.parse_args()
    CFG.update(panda=a.panda, datasource=a.datasource, ch=a.clickhouse, table=a.table, timeout=a.timeout)
    srv = Server((a.bind, a.port), Handler)
    sys.stderr.write(f"panda-grafana-adapter on http://{a.bind}:{a.port}  "
                     f"metrics='{a.datasource}' logs='{a.clickhouse}:{a.table}' (panda: {a.panda})\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
