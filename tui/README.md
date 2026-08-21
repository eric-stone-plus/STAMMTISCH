# STAMMTISCH TUI

nmtui-style terminal workstation for STAMMTISCH — pipeline runs, evidence
inspection, quant engine commands, and optional AI analysis in one screen.

## Quick start

```sh
# From the repo root (preferred — unified launcher):
./stammtisch

# Or directly:
.venv/bin/python -m tui

# With a specific state root:
STAMMTISCH_HOME=/path/to/state .venv/bin/python -m tui

# Useful flags:
.venv/bin/python -m tui --state-root /path/to/state \
    --pipeline-dir pipelines/examples --binary target/release/stammtisch-core
```

## Requirements

- Python 3.11+ with the exact minimal UI dependencies in
  `requirements-tui.txt` (repo `.venv` recommended). Rebuild the base UI
  environment independently of optional quantitative packages:

  ```sh
  uv venv --clear --python 3.11 .venv
  uv pip sync --python .venv/bin/python --require-hashes requirements-tui.lock
  ```

  `requirements-tui.txt` pins the direct `textual` and `rich` requirements;
  `requirements-tui.lock` pins and hashes their complete Python 3.11/x86-64
  Linux dependency closure. Neither file freezes quantkit or any wider
  quantitative environment.
- Compiled `stammtisch-core` binary (auto-detected under `target/release|debug/`,
  or `--binary` / `STAMMTISCH_BIN`)
- Optional: `quantkit` importable (enables the quant engine commands;
  the TUI degrades gracefully without it). Install it editable into the
  TUI python after the minimal rebuild, or use a separately managed interpreter
  via `STAMMTISCH_PYTHON`, along with its data deps, e.g.
  `.venv/bin/pip install -e /path/to/quantkit numpy pandas akshare`
- Optional: AI provider API key (enables AI chat and run analysis)
- Optional: a forecast command (config `kronos_cmd`) connects the chart to a
  `SYMBOL --horizon N --json` adapter. Legacy
  `{"model": ..., "forecast": [...], "dates": [...]}` responses are retained
  for diagnostics but always produce `ABSTAIN`. A valid sealed
  `kronos.forecast.v2` receipt may be drawn as a dashed diagnostic curve with
  its non-`PASS` gate shown explicitly; it is never promoted to evidence or an
  actionable signal. Empty `kronos_cmd` disables the integration.
- Optional: a daily-data product command (config `intake_cmd`) enables `D`.
  STAMMTISCH invokes it without a shell and validates its evidence manifest,
  canonical dataset, report JSON, and report HTML under `workspace_root`.
  `intake_report_builder` defaults to `deepseek`, preserving the configured
  chat-model editorial backend; `deterministic` is the offline fallback.

## Daily data intake

`D` is data-first. It invokes the configured product directly, retains the
product's append-only capture evidence, validates source and market coverage,
and displays the accepted canonical records before opening the Chinese daily
report. The derivation order is fixed:

```text
source capture -> evidence manifest -> canonical dataset -> report JSON -> HTML
```

Report JSON or HTML is never treated as source input. Sentiment remains a
separate `S` surface. Source content is rendered in its original language;
STAMMTISCH's own labels and errors are English. The product command and data
workspace are configured per installation, so the public application does not
depend on a particular host, port, or checkout path.

## K-line timeseries (browser)

Not MCP. The chart is TradingView Lightweight Charts vendored under
`tui/static/` (Apache-2.0). A local stdlib HTTP server
(`tui/chart_server.py`) feeds it OHLCV from quantkit; `k` starts that
server on loopback using an OS-assigned port and opens the page. An explicit
`chart_port` may be configured when a stable local address is required.
The page shows candlesticks (THS colors: red up / green down), volume,
MA20, and a crosshair OHLC legend. A structurally valid Kronos v2 receipt may
appear as a dashed diagnostic forecast, always labeled with its non-`PASS`
gate. That curve remains audit-only and is not accepted as evidence. The
request horizon is read from `kronos_horizon` (default 20).

Validated mode treats the producer's symbol and MIC as one exact identity.
The browser carries both values from a search result (or an explicit
`SYMBOL@MIC` entry) through candle, comparison, history, URL, and forecast
requests. It never rewrites a bare index identifier into a listed-security
alias. Every drawn comparison leg is included in the visible, copyable
provenance badge with its own bar and manifest hashes.

A forecast over validated data is accepted for inspection only when the
browser supplies the current validated-bars reference, the server resolves
that reference back to the same accepted manifest, and the forecast receipt's
embedded input snapshot exactly matches a contiguous tail of those bars. The
reference binds `bars_sha256`, `output_sha256`, the full identity, and the
calendar-session digest. Missing, stale, or mismatched linkage stops before a
forecast can be presented as related to the chart.

## Polymarket prediction market (terminal)

Not MCP and not a browser tab. The CRYPTO row in the Plugins list opens a
read-only Gamma tape in the TUI: active contracts by 24h volume, local
filter, no orders. Set an explicit
HTTP proxy with `stammtisch config set polymarket_proxy_url URL` or
`STAMMTISCH_POLYMARKET_PROXY`. Direct access and ambient proxy variables are
disabled; missing or invalid configuration fails closed. Configure a stable
client-facing proxy URL, not a replaceable upstream worker endpoint.

## Energy watchlist (EIA, terminal)

The ENERGY row in the dashboard Plugins list opens a read-only watchlist of
EIA Open Data API v2 series curated for a coal desk that also watches crude
and gas: WTI/Brent spot, US crude stocks
ex-SPR, Henry Hub spot, Lower-48 working gas storage, China/India coal
trade and production, Saudi/Russia crude production, Japan gas imports, and
the EIA STEO price projections (the OUTLOOK rows headline the earliest
projection month against the latest actual). Row highlight shows recent
observations; `R` reloads.

The EIA API is free but requires a registered key
(https://www.eia.gov/opendata/ — throttled at roughly 5 requests/second per
key). Save it with `stammtisch config set eia_api_key KEY` or export
`EIA_API_KEY`. Requests go out only through an explicit HTTP proxy
(`energy_proxy_url` or `STAMMTISCH_ENERGY_PROXY`); direct access and
ambient proxy variables are disabled, and missing key/proxy fails closed
with an actionable status line. Per-series errors degrade to error rows
without taking down the board.

Scope notes: EIA petroleum/NG futures routes end at 2024-04-05 and are
deliberately unused; v2 has no weekly/monthly coal route, and international
coal is annual only (unit code MT = 1,000 metric tons, shown as kt).
International price markers for coal (Newcastle, API2/4) are licensed
products and out of scope.

## Security board (SECURITY)

The SECURITY row (the shipped example pipeline's workbench) opens an equity
watchlist board grouped by market zone — A-SHARE / HK / US / OTHER by
exchange suffix — with `←`/`→` switching, a recent-bars detail pane, and
`K` opening the browser K-line for the selected row. Symbols come from
`security_symbols` (Yahoo-style tickers through the quantkit path, e.g.
`601088.SS 1088.HK BTU`). When that list is empty (the full-market daily
screen mode), the board shows non-cut names from the latest persisted daily
decision and then recent symbols. The quant and daily-report hotkeys
(`A`/`B`/`D`/`H`/`E`/`F`/`P`/`S`/`T`) live on this screen.

## Domain boards (FUTURES / SHIPPING / CASINO)

Three dashboard plugins have dedicated screens; every other `plugins` entry
keeps the read-only directory browser.

- `FUTURES` is a category board (`←`/`→` switches categories) fed by two
  paths: provider-backed continuous tickers (`futures_symbols`, default
  `["BZ=F"]` = ICE Brent front month) through the existing quantkit
  provider path — full OHLCV, `K` opens the browser K-line — and
  exchange-settled contracts from a second adapter command (`futures_cmd`,
  e.g. SGX marine-fuel settlements: settle, forward curve, open interest).
  The quant workbench keys (`B` backtest, `F` fetch, `T` indicators,
  `P` portfolio) are available here too.
  An empty `futures_symbols` plus an empty `futures_cmd` restores the
  directory browser.

  Exchange-settled rows chart too: the adapter's bars export
  (`mktdaily.bars.v1` JSON files under an operator-local root) is served by
  the chart server as `SGX:<CODE>` symbols when `external_bars_root` is
  configured. Settlement-only contracts draw as degenerate daily candles
  (open=high=low=close=settle) — the honest shape of an exchange mark.
  Missing roots or malformed files fail closed; validated mode never
  serves them.
- `SHIPPING` is a category board (`←`/`→` switches boards) with four
  categories. `FFA` renders an exchange daily-settlement board (FFA
  time-charter and voyage baskets plus bunker fuels) produced by an
  operator-local adapter command (`shipping_cmd`, empty = directory
  browser). The command is invoked without a shell and must print exactly
  one JSON object:

```json
{"ok": true, "schema": "mktdaily.sgx-board.v1", "asof": "YYYY-MM-DD",
 "source": "...", "instruments": [
   {"code": "CWF", "group": "...", "name": "...", "unit": "USD/day",
    "front_month": "YYYY-MM", "settle": 0.0,
    "change": 0.0, "change_pct": 0.0,
    "curve": [{"month": "YYYY-MM", "settle": 0.0}],
    "recent": [{"date": "YYYY-MM-DD", "settle": 0.0}]}]}
```

`change`/`change_pct` may be null; extra keys pass through. The row
highlight drives a forward-curve table and a recent front-month settle
history. Nonzero exits, malformed JSON, and schema mismatches fail closed
(see `tui/domaindata.py` for the full contract).

  The remaining three categories are fed by one second adapter command
  (`spval_cmd`), which prints a single `stammtisch.spval-board.v2` JSON
  object (see `tui/domaindata.py`): `S&P VALUATION` renders the valuation
  board (baseline KPIs, scenario × price grid, MAX-BID factors, Greeks),
  `MARKET` the charter-cycle and route-TCE boards, and `RISK` the tail
  matrix, counterfactuals, and price × TCE sensitivity. The payload is
  fetched once and rendered per category; an empty `spval_cmd` renders a
  not-configured notice. `K` opens the browser K-line on the FFA board
  only.

- `CASINO` hosts the wagerkit race board (RACING was merged into it): the
  adapter command (`racing_cmd`, empty = directory browser) prints one
  `wagerkit.hkjc-board.v1` JSON object; the screen renders meetings and
  runners with model edge as diagnostics — never bet advice.


## Keys (dashboard)

| Key     | Action                          |
|---------|---------------------------------|
| `A`     | ASK                             |
| `Enter` | Inspect selected run            |
| `E`     | Edit config                     |
| `Esc`   | Go back                         |
| `Q`     | Quit                            |

The quant and daily-report keys (`B` backtest, `D` daily-data intake,
`H` report history, `F` fetch market data, `K` K-line timeseries,
`P` portfolio, `S` sentiment tape, `T` technical indicators) live inside
the SECURITY workbench; `B`/`F`/`T`/`P`/`K` are also on the FUTURES board.

The Quick Start sidebar is minimal (Ask, Edit config). Inspect is the run
table; gates and validate stay inside those flows.

## Configuration

File: `~/.config/stammtisch/config.json` (written with `0600` permissions).
Override the location with `STAMMTISCH_CONFIG`. Environment overrides (never
persisted): `DEEPSEEK_API_KEY`, `STAMMTISCH_HOME`,
`STAMMTISCH_INTAKE_CMD`, `STAMMTISCH_WORKSPACE_ROOT`,
`STAMMTISCH_POLYMARKET_PROXY`, `EIA_API_KEY`, and
`STAMMTISCH_ENERGY_PROXY`. K-line data defaults to the existing live
provider path (`ohlcv_mode: "live"`). Set `ohlcv_mode: "validated"` and
`validated_bars_root` (or `STAMMTISCH_OHLCV_MODE` and
`STAMMTISCH_VALIDATED_BARS_ROOT`) to require accepted offline consensus
manifests. Validated mode fails closed and never falls back to live data.
Accepted validated charts show shortened bar/manifest hashes in the bottom
status dock; hover for the full provenance or activate the badge to copy it.
The launcher picks the TUI
interpreter via `STAMMTISCH_PYTHON` → repo `.venv` → `python3`.

Managed from the CLI (stdlib only, works without the TUI deps):

```sh
stammtisch config                 # show config (API key masked)
stammtisch config set-key [KEY]   # save the AI key (prompts if omitted)
stammtisch config unset-key       # remove it
stammtisch config set KEY VALUE   # any config key, e.g. default_fast 30
stammtisch config get KEY         # print one value
stammtisch config unset KEY       # reset to default
stammtisch config path            # print the config file path
stammtisch config edit            # open it in $EDITOR
```

Inside the TUI, press `E` for the same settings in a form.

## Architecture

```
tui/
├── __init__.py     # Package metadata
├── __main__.py     # CLI entry point (argparse → run_tui)
├── app.py          # Textual App, global bindings, screen wiring
├── screens/        # Screen package (facade re-exports):
│   ├── dashboard.py      # Dashboard + run registry table
│   ├── domains.py        # Futures, shipping, security boards, plugin browser
│   ├── daily_intake.py   # Daily intake screen + report-history helpers
│   ├── runs.py           # Pipeline run, inspector, validate, pipeline view
│   ├── chat.py           # Chat + ask-session screens
│   ├── config_screen.py  # Workstation config editor
│   ├── sessions.py       # Session-record helpers (ask sessions, run titles)
│   └── modals.py         # Confirm and help modals
├── intake.py       # Fail-closed daily-data product adapter
├── domaindata.py   # Fail-closed domain board adapter (SHIPPING screen)
├── polymarket.py   # Read-only Polymarket Gamma tape (in-terminal)
├── energy.py       # Read-only EIA Open Data v2 watchlist (in-terminal)
├── analysis.py     # Quant screens: data fetch, backtest, indicators, portfolio, gates
├── widgets.py      # Custom widgets (stage flow, gate cards, HUD, typing text)
├── theme.py        # Grayscale nmtui-style CSS
├── driver.py       # stammtisch-core CLI driver (JSON envelope parsing)
├── engine.py       # quantkit bridge (optional, degrades gracefully)
├── ai_driver.py    # AI chat driver, provider-neutral (optional, stdlib urllib)
├── config.py       # File config + env overrides (0600, env never persisted)
└── config_cli.py   # `stammtisch config` subcommand (stdlib only)
```
