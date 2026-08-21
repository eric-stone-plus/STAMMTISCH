"""Engine bridge — connects TUI to quantkit for real quant operations."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _missing_ohlcv(df: Any) -> bool:
    """True when fetch_ohlcv returned None or a zero-row frame.

    `df.empty` raises AttributeError when df is None — callers must use
    this helper (or an explicit None check) instead.
    """
    return df is None or getattr(df, "empty", True)


def _normalize_symbol(symbol: str) -> str:
    """Resolve a typed ticker to the primary Yahoo-style symbol.

    A-share 6-digit codes keep the old suffixes (600098 → 600098.SS).
    HK / JP / KR bare codes get .HK / .T / .KS; US tickers pass through.
    """
    from .symbols import normalize_symbol
    return normalize_symbol(symbol)


@dataclass
class BacktestSummary:
    total_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    trades: int
    final_equity: float
    cost_bps: float
    equity_series: Any = None
    returns_series: Any = None
    positions_series: Any = None


@dataclass
class IndicatorSummary:
    rsi: float
    macd: float
    macd_signal: float
    macd_hist: float
    bb_upper: float
    bb_mid: float
    bb_lower: float
    sma_20: float
    sma_50: float
    sma_200: float
    atr_14: float
    vol_20: float


@dataclass
class FactorSummary:
    feature_cols: list[str]
    n_samples: int
    ml_available: bool
    importance: list[tuple[str, float]] = field(default_factory=list)


@dataclass
class PortfolioSummary:
    total_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    trades: int
    n_assets: int
    avg_turnover: float
    avg_gross_exposure: float


@dataclass
class GateReport:
    gates: list[dict[str, Any]]
    all_passed: bool
    n_passed: int
    n_total: int


class QuantEngine:
    """Wraps quantkit functions for TUI consumption."""

    @classmethod
    def from_config(cls, config: Any) -> QuantEngine:
        """Build from a workstation Config; missing attrs stay unset."""
        return cls(
            data_dir=getattr(config, "data_dir", None),
            data_proxy_url=getattr(config, "data_proxy_url", "") or None,
            egress_proxy_url=getattr(config, "egress_proxy_url", "") or None,
            egress_switch_cmd=getattr(config, "egress_switch_cmd", "") or None,
        )

    def __init__(self, data_dir: str | None = None, data_proxy_url: str | None = None,
                 egress_proxy_url: str | None = None,
                 egress_switch_cmd: str | None = None):
        self.data_dir = Path(data_dir) if data_dir else Path.home() / ".quant_cache"
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # Unwritable cache dir: commands degrade with per-call errors.
        self._egress_proxy_url = (egress_proxy_url or "").strip()
        self._egress_switch_cmd = (egress_switch_cmd or "").strip()
        self._egress_active = False
        # Always-on manual override: route yfinance through a fixed egress
        # from the first call. Optional and host-specific: unset means
        # direct, exactly the previous behaviour.
        if data_proxy_url:
            self._egress_apply(data_proxy_url)
        # Reactive per-site rotation (preferred): fetches stay direct
        # until a provider rate-limits this exit IP; then the configured
        # switch command rotates THAT provider's host onto the egress
        # class and only the affected provider's fetches retry through
        # it. Other providers keep their direct route.

    def _egress_apply(self, proxy_url: str) -> None:
        try:
            import yfinance as yf
            yf.set_config(proxy={"http": proxy_url, "https": proxy_url})
            self._egress_active = True
        except Exception:
            pass  # older yfinance without set_config: fetch stays direct

    def _is_rate_limit(self, error: BaseException) -> bool:
        text = str(error).lower()
        return any(marker in text for marker in
                   ("too many requests", "rate limit", "rate limited", "429"))

    def _egress_rotate(self) -> bool:
        """Rotate the rate-limited provider's hosts onto the egress class."""
        if not (self._egress_proxy_url and self._egress_switch_cmd):
            return False
        import subprocess
        for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
            try:
                subprocess.run(
                    shlex.split(self._egress_switch_cmd.format(host=host)),
                    capture_output=True, timeout=30)
            except Exception:
                pass  # rotation is best-effort; the retry decides success
        self._egress_apply(self._egress_proxy_url)
        return True

    def _fetch_ohlcv(self, symbol: str, market: str, start: str,
                     end: str | None = None, force: bool = False):
        """quantkit fetch with reactive per-site egress rotation.

        Direct by default; a rate-limited fetch (provider hourly quotas
        are per exit IP) rotates the affected provider onto the
        configured egress and retries once through it. Anything else
        propagates untouched.
        """
        from quantkit.data import fetch_ohlcv
        try:
            return fetch_ohlcv(symbol, market=market, start=start, end=end,
                               data_dir=str(self.data_dir), force_refresh=force)
        except Exception as exc:
            if (not self._egress_active and self._is_rate_limit(exc)
                    and self._egress_rotate()):
                return fetch_ohlcv(symbol, market=market, start=start, end=end,
                                   data_dir=str(self.data_dir), force_refresh=force)
            raise

    @property
    def available(self) -> bool:
        try:
            import quantkit  # noqa: F401
            return True
        except Exception:
            # A broken install (missing transitive dep, etc.) must not crash
            # the TUI; every command degrades to {"ok": False} instead.
            return False

    def fetch_data(self, symbol: str, market: str = "auto", start: str = "2020-01-01", end: str | None = None, force: bool = False) -> dict[str, Any]:
        symbol = _normalize_symbol(symbol.strip().upper())
        try:
            df = self._fetch_ohlcv(symbol, market=market, start=start, end=end,
                                   force=force)
            if _missing_ohlcv(df):
                return {"ok": False, "error": f"No data for {symbol}"}
            last = df.iloc[-1]
            return {
                "ok": True, "symbol": symbol, "rows": len(df),
                "columns": list(df.columns),
                "first_date": str(df.index[0]), "last_date": str(df.index[-1]),
                "last_close": float(last.get("close", 0)),
                "last_volume": float(last.get("volume", 0)),
                "df": df,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def run_backtest(self, symbol: str, strategy: str = "dual_ma", fast: int = 20, slow: int = 50,
                     start: str = "2020-01-01", cost_tier: str = "low") -> dict[str, Any]:
        symbol = _normalize_symbol(symbol.strip().upper())
        try:
            from quantkit.backtest import run_long_only, dual_ma_signal, rsi_mean_reversion_signal

            df = self._fetch_ohlcv(symbol, market="auto", start=start)
            if _missing_ohlcv(df):
                return {"ok": False, "error": f"No data for {symbol}"}

            close = df["close"]
            if strategy == "dual_ma":
                signal = dual_ma_signal(close, fast=fast, slow=slow)
            elif strategy == "rsi_mr":
                signal = rsi_mean_reversion_signal(close)
            else:
                # Unknown strategy is a user typo, not a dual_ma request.
                return {"ok": False, "error": f"unknown strategy '{strategy}'"}

            result = run_long_only(close, signal, cost_tier=cost_tier)
            summary = BacktestSummary(
                total_return=result.total_return, cagr=result.cagr,
                sharpe=result.sharpe, max_drawdown=result.max_drawdown,
                win_rate=result.win_rate, trades=result.trades,
                final_equity=result.stats.get("final_equity", 1.0),
                cost_bps=result.stats.get("cost_bps_effective", 5.0),
                equity_series=result.equity, returns_series=result.returns,
                positions_series=result.positions,
            )
            return {"ok": True, "summary": summary, "stats": result.stats}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def compute_indicators(self, symbol: str, start: str = "2020-01-01") -> dict[str, Any]:
        symbol = _normalize_symbol(symbol.strip().upper())
        try:
            from quantkit.indicators import add_core_indicators

            df = self._fetch_ohlcv(symbol, market="auto", start=start)
            if _missing_ohlcv(df):
                return {"ok": False, "error": f"No data for {symbol}"}

            feat = add_core_indicators(df)
            last = feat.iloc[-1]
            summary = IndicatorSummary(
                rsi=float(last.get("rsi_14", 0)), macd=float(last.get("macd", 0)),
                macd_signal=float(last.get("macd_signal", 0)), macd_hist=float(last.get("macd_hist", 0)),
                bb_upper=float(last.get("bb_upper", 0)), bb_mid=float(last.get("bb_mid", 0)),
                bb_lower=float(last.get("bb_lower", 0)), sma_20=float(last.get("sma_20", 0)),
                sma_50=float(last.get("sma_50", 0)), sma_200=float(last.get("sma_200", 0)),
                atr_14=float(last.get("atr_14", 0)), vol_20=float(last.get("vol_20", 0)),
            )
            return {"ok": True, "summary": summary, "last_price": float(last.get("close", 0))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def build_factors(self, symbol: str, start: str = "2020-01-01") -> dict[str, Any]:
        symbol = _normalize_symbol(symbol.strip().upper())
        try:
            from quantkit.factors import build_feature_frame, combine_factors

            df = self._fetch_ohlcv(symbol, market="auto", start=start)
            if _missing_ohlcv(df):
                return {"ok": False, "error": f"No data for {symbol}"}

            feat = build_feature_frame(df)
            score = combine_factors(feat)
            summary = FactorSummary(
                feature_cols=[c for c in feat.columns if feat[c].dtype in ("float64", "int64")],
                n_samples=len(feat.dropna()), ml_available=True,
            )
            return {"ok": True, "summary": summary, "score": score}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def run_portfolio(self, symbols: list[str], strategy: str = "momentum",
                      start: str = "2020-01-01", rebalance: str = "M",
                      lookback: int = 60) -> dict[str, Any]:
        symbols = [_normalize_symbol(s.strip().upper()) for s in symbols]
        try:
            from quantkit.portfolio import fetch_price_panel, run_portfolio as run_port
            from quantkit.portfolio import momentum_panel, signal_to_weights, dual_ma_panel

            prices = fetch_price_panel(symbols, start=start, data_dir=str(self.data_dir))
            if _missing_ohlcv(prices):
                return {"ok": False, "error": "No price data"}

            if strategy == "momentum":
                signals = momentum_panel(prices, lookback=lookback)
            elif strategy == "dual_ma":
                signals = dual_ma_panel(prices)
            else:
                # Unknown strategy is a user typo, not a momentum request.
                return {"ok": False, "error": f"unknown strategy '{strategy}'"}

            weights = signal_to_weights(signals, long_only=True, max_weight=0.3)
            result = run_port(prices, weights, rebalance=rebalance)
            summary = PortfolioSummary(
                total_return=result.total_return, cagr=result.cagr,
                sharpe=result.sharpe, max_drawdown=result.max_drawdown,
                win_rate=result.win_rate, trades=result.trades,
                n_assets=result.stats.get("n_assets", len(symbols)),
                avg_turnover=result.stats.get("avg_turnover", 0),
                avg_gross_exposure=result.stats.get("avg_gross_exposure", 0),
            )
            return {"ok": True, "summary": summary, "stats": result.stats}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def evaluate_gates(self, metrics: dict[str, Any]) -> dict[str, Any]:
        try:
            from quantkit.gates import evaluate_gates as eval_gates
            result = eval_gates(metrics)
            gates_list = []
            all_passed = True
            n_passed = 0
            # quantkit returns GateResult dataclasses (gate/passed/
            # failures/missing/metrics); tolerate dicts from other
            # revisions so the TUI never assumes one shape.
            for g in result.get("gates", []):
                if isinstance(g, dict):
                    gate_id = g.get("gate", g.get("gate_id", "?"))
                    passed = bool(g.get("passed", False))
                    detail = g.get("reason", g.get("detail", ""))
                    value = g.get("value")
                    threshold = g.get("threshold")
                else:
                    gate_id = getattr(g, "gate", "?")
                    passed = bool(getattr(g, "passed", False))
                    detail = "; ".join(getattr(g, "failures", []) or [])
                    value = getattr(g, "metrics", {})
                    threshold = None
                if passed:
                    n_passed += 1
                else:
                    all_passed = False
                gates_list.append({
                    "gate_id": gate_id, "passed": passed,
                    "detail": detail, "value": value, "threshold": threshold,
                })
            report = GateReport(gates=gates_list, all_passed=all_passed, n_passed=n_passed, n_total=len(gates_list))
            return {"ok": True, "report": report}
        except Exception as e:
            return {"ok": False, "error": str(e)}


class CasinoEngine:
    """Wraps wagerkit (casino/gambling math) for TUI consumption.

    wagerkit is an optional dependency, imported lazily through its
    `session` facade; every method degrades to {"ok": False, ...} when
    it is not installed in the TUI python.
    """

    @property
    def available(self) -> bool:
        try:
            import wagerkit.session  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def _unavailable() -> dict[str, Any]:
        return {"ok": False, "error": "wagerkit not installed in the TUI python"}

    def holdem_equity(self, hole: list[str], board: list[str] | None = None,
                      opponents: int = 1, iters: int = 20000) -> dict[str, Any]:
        """hole/board cards as short strings, e.g. ["AS", "KD"]."""
        try:
            from wagerkit import session
            r = session.holdem_equity(hole, board or [], opponents=opponents, iters=iters)
            return {"ok": True, "result": r}
        except ImportError:
            return self._unavailable()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def game_edges(self, game: str | None = None) -> dict[str, Any]:
        """Bet-level edge rows for one game, or the full menu when game=None."""
        try:
            from wagerkit import session
            rows = session.game_edges(game) if game else session.game_menu()
            return {"ok": True, "rows": rows}
        except ImportError:
            return self._unavailable()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def kelly(self, p: float, b: float) -> dict[str, Any]:
        """Kelly sizing for win prob p and net odds b."""
        try:
            from wagerkit import session
            return {"ok": True, "result": session.kelly_stake(p, b)}
        except ImportError:
            return self._unavailable()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def bj_suggest(self, hole: list[str], up: str) -> dict[str, Any]:
        """Blackjack basic-strategy action, e.g. (["AS", "7S"], "9")."""
        try:
            from wagerkit import session
            return {"ok": True, "result": session.blackjack_suggest(hole, up)}
        except ImportError:
            return self._unavailable()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def bj_ev(self) -> dict[str, Any]:
        """Basic-strategy EV for the blackjack rule presets (MC, seeded)."""
        try:
            from wagerkit import session
            return {"ok": True, "rows": session.blackjack_ev()}
        except ImportError:
            return self._unavailable()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def slots(self) -> dict[str, Any]:
        """Exact PAR analysis of the illustrative slot machine."""
        try:
            from wagerkit import session
            return {"ok": True, "result": session.slots_example()}
        except ImportError:
            return self._unavailable()
        except Exception as e:
            return {"ok": False, "error": str(e)}
