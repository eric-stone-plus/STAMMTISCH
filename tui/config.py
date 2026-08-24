"""Configuration management — file-based config with env var fallback."""

from __future__ import annotations

import copy
import json
import os
import shlex
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_DIR = Path.home() / ".config" / "stammtisch"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.json"
DEFAULT_WORKSPACE_ROOT = Path.home() / ".local" / "share" / "stammtisch" / "daily-data"


def default_config_file() -> Path:
    """Config path: STAMMTISCH_CONFIG env override, else the default location."""
    env = os.environ.get("STAMMTISCH_CONFIG")
    if env:
        return Path(env).expanduser()
    return DEFAULT_CONFIG_FILE

# Named OpenAI-compatible AI provider presets, shared by the config CLI
# (`stammtisch config use PROFILE`) and the TUI config screen provider
# dropdown. name -> (label, base_url, model). Per-profile API keys are
# remembered in the ai_profile_keys config dict, so switching back and
# forth never requires re-entering keys.
AI_PROFILES = {
    "glm": ("GLM official (bigmodel v4)", "https://open.bigmodel.cn/api/paas/v4", "glm-5.3"),
    "mimo": ("MiMo (Xiaomi Anthropic)", "https://token-plan-cn.xiaomimimo.com/anthropic", "mimo-v2.5-pro"),
    "deepseek": ("DeepSeek official", "https://api.deepseek.com/v1", "deepseek-v4-pro"),
}


def ai_profile_for_base_url(base_url: str) -> str | None:
    """Name of the preset whose base_url matches, if any."""
    base = base_url.rstrip("/")
    for name, (_label, preset_base, _model) in AI_PROFILES.items():
        if preset_base.rstrip("/") == base:
            return name
    return None


DEFAULT_CONFIG = {
    # TUI chrome language: "en" | "zh" (toggled from the dashboard).
    "language": "en",
    # Display timezone for user-facing timestamps (IANA name such as
    # "Asia/Shanghai"); empty keeps the stored UTC rendering.
    "display_timezone": "",
    "ai_api_key": "",
    "ai_base_url": "https://open.bigmodel.cn/api/paas/v4",
    "ai_model": "glm-5.3",
    # Per-profile API key memory for `stammtisch config use PROFILE`:
    # {profile_name: api_key}. Switching profiles restores the stored key.
    "ai_profile_keys": {},
    "state_root": "",
    "data_dir": str(Path.home() / ".quant_cache"),
    "data_proxy_url": "",
    # Reactive per-site rotation (preferred over always-on data_proxy_url).
    # Both must be set; empty means fetches stay direct. `{host}` in the
    # switch command is replaced with the rate-limited provider host.
    "egress_proxy_url": "",
    "egress_switch_cmd": "",
    "recent_symbols": [],
    "recent_strategies": [],
    "default_strategy": "dual_ma",
    "default_fast": 20,
    "default_slow": 50,
    "default_cost_tier": "low",
    "default_rebalance": "M",
    "default_lookback": 60,
    # Forecast adapter: `{cmd} SYMBOL --horizon N --json`. Legacy JSON is
    # diagnostic-only. A sealed Kronos v2 receipt may be drawn as an explicitly
    # labeled non-PASS diagnostic; it is never promoted to evidence. Empty = disabled.
    "kronos_cmd": "",
    "kronos_horizon": 20,
    # K-line OHLCV source. ``validated`` reads only verified offline consensus
    # manifests; it never falls back to the live quantkit provider.
    "ohlcv_mode": "live",
    "validated_bars_root": "",
    # Local chart server port (browser K-line timeseries viewport).
    "chart_port": 0,
    # Operator-local exchange series root for SGX:<CODE> chart symbols;
    # one mktdaily.bars.v1 JSON per code. Empty = SGX charts disabled.
    "external_bars_root": "",
    # Explicit HTTP proxy for the read-only Polymarket tape. Empty means
    # disabled; direct access and ambient proxy variables are never used.
    "polymarket_proxy_url": "",
    # ENERGY section (EIA Open Data API v2): free registered API key, sent
    # as the api_key query parameter. Empty = energy section disabled.
    "eia_api_key": "",
    # Explicit HTTP proxy for EIA API requests. Same fail-closed rule as
    # the Polymarket tape: empty = disabled, ambient proxies never used.
    "energy_proxy_url": "",
    # Daily-data product adapter. The command is tokenized without a shell and
    # receives --workspace-root, --json, and an optional --date argument.
    "intake_cmd": "",
    "workspace_root": str(DEFAULT_WORKSPACE_ROOT),
    "intake_timeout_seconds": 900,
    "intake_report_builder": "ai",
    # Legacy report-only compatibility. D uses the intake workspace instead.
    "reports_root": str(DEFAULT_WORKSPACE_ROOT / "legacy-reports"),
    # Report history index (SQLite). Empty follows <workspace_root>/history.db.
    "history_db": "",
    # Domain plugins rendered in the dashboard PLUGINS zone. Each entry is
    # {"label": str, "root": path}; entries are operator-local and never
    # shipped with defaults, keeping the repo host-agnostic.
    "plugins": [],
    # FUTURES plugin: Yahoo-style continuous futures tickers fetched and
    # charted through the existing quantkit path (BZ=F = ICE Brent front
    # month). Empty list disables the provider side of the screen.
    "futures_symbols": ["BZ=F"],
    # SECURITY board: Yahoo-style equity symbols grouped into market zones
    # by exchange suffix (.SS/.SZ/.BJ = A-SHARE, .HK = HK, bare = US).
    # Fetched and charted through the same quantkit path. When empty, the
    # board shows the latest non-cut daily decisions followed by recents.
    "security_symbols": [],
    # FUTURES plugin exchange-settled side: the command prints one
    # mktdaily.sgx-board.v1 JSON object on stdout (see tui/domaindata.py).
    # Empty = disabled. With both futures keys empty the plugin falls back
    # to the read-only directory browser.
    "futures_cmd": "",
    # SHIPPING plugin adapter: the command prints one mktdaily.sgx-board.v1
    # JSON object on stdout (see tui/domaindata.py). Empty = disabled; the
    # SHIPPING plugin then falls back to the directory browser.
    "shipping_cmd": "",
    # S&P VALUATION plugin adapter (SHIPPING screen, second board): the
    # command prints one stammtisch.spval-board.v1 JSON object on stdout
    # (see tui/domaindata.py). Empty = the S&P VALUATION category renders a
    # not-configured notice instead of a board.
    "spval_cmd": "",
    # RACING plugin adapter: the command prints one wagerkit.hkjc-board.v1
    # JSON object on stdout (see tui/racing.py). Empty = disabled; the
    # RACING plugin then falls back to the directory browser.
    "racing_cmd": "",
    # Crawler panel (operator-local, host-agnostic by default): the panel
    # is inert until these point at this workstation's crawling runtime.
    "crawler_url": "http://127.0.0.1:3002/",
    "crawler_compose_dir": "",
    "crawler_sources_conf": "",
    "crawler_heal_cmd": "",
}


class Config:
    """File-based configuration with environment variable override."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_config_file()
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        """Load config from file, falling back to defaults."""
        # Deep copy: the mutable default lists must never be shared with
        # self._data — in-place edits (add_recent_symbol) would otherwise
        # leak into DEFAULT_CONFIG and every later Config in the process.
        self._data = copy.deepcopy(DEFAULT_CONFIG)
        self._from_env: set[str] = set()
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    # Legacy migration: AI settings were stored under
                    # deepseek_* names before the ai_driver rename; carry
                    # values across when the new keys are absent so existing
                    # config files keep working (the next save drops the
                    # old names).
                    for legacy_key, current_key in (
                        ("deepseek_api_key", "ai_api_key"),
                        ("deepseek_base_url", "ai_base_url"),
                        ("deepseek_model", "ai_model"),
                    ):
                        if legacy_key in saved and current_key not in saved:
                            saved[current_key] = saved[legacy_key]
                    self._data.update(
                        {key: value for key, value in saved.items() if key in DEFAULT_CONFIG}
                    )
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass  # Use defaults on corrupt config

        # Environment variable overrides (never persisted to disk).
        # Current GLM names first; legacy DeepSeek names stay honored.
        env_key = (
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("XIAOMI_API_KEY")
            or os.environ.get("GLM_API_KEY")
            or os.environ.get("ZHIPU_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("DEEPSEEK_KEY")
        )
        if env_key:
            self._data["ai_api_key"] = env_key
            self._from_env.add("ai_api_key")

        env_root = os.environ.get("STAMMTISCH_HOME")
        if env_root:
            self._data["state_root"] = env_root
            self._from_env.add("state_root")

        env_intake_cmd = os.environ.get("STAMMTISCH_INTAKE_CMD")
        if env_intake_cmd:
            self._data["intake_cmd"] = env_intake_cmd
            self._from_env.add("intake_cmd")

        env_workspace = os.environ.get("STAMMTISCH_WORKSPACE_ROOT")
        if env_workspace:
            self._data["workspace_root"] = env_workspace
            self._from_env.add("workspace_root")

        env_polymarket_proxy = os.environ.get("STAMMTISCH_POLYMARKET_PROXY")
        if env_polymarket_proxy:
            self._data["polymarket_proxy_url"] = env_polymarket_proxy
            self._from_env.add("polymarket_proxy_url")

        env_eia_key = os.environ.get("EIA_API_KEY")
        if env_eia_key:
            self._data["eia_api_key"] = env_eia_key
            self._from_env.add("eia_api_key")

        env_energy_proxy = os.environ.get("STAMMTISCH_ENERGY_PROXY")
        if env_energy_proxy:
            self._data["energy_proxy_url"] = env_energy_proxy
            self._from_env.add("energy_proxy_url")

        env_ohlcv_mode = os.environ.get("STAMMTISCH_OHLCV_MODE")
        if env_ohlcv_mode:
            self._data["ohlcv_mode"] = env_ohlcv_mode
            self._from_env.add("ohlcv_mode")

        env_validated_root = os.environ.get("STAMMTISCH_VALIDATED_BARS_ROOT")
        if env_validated_root:
            self._data["validated_bars_root"] = env_validated_root
            self._from_env.add("validated_bars_root")

    def save(self) -> None:
        """Save current config to file (owner-only: may hold an API key).

        Written via temp file + atomic rename: a crash mid-write can never
        truncate the existing config.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in self._data.items() if k not in self._from_env}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        # Create owner-only from the start: open() would apply umask (often
        # 0644) and leave a readable window before any later chmod.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)  # a stale leftover tmp may carry wider perms
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._from_env.discard(key)
        self.save()

    def update(self, mapping: dict[str, Any]) -> None:
        """Batch-set multiple keys and persist once."""
        if not mapping:
            return
        for key, value in mapping.items():
            self._data[key] = value
            self._from_env.discard(key)
        self.save()

    @property
    def ai_api_key(self) -> str | None:
        key = self._data.get("ai_api_key", "")
        return key if key else None

    @property
    def eia_api_key(self) -> str | None:
        key = self._data.get("eia_api_key", "")
        return key if key else None

    @property
    def ai_base_url(self) -> str:
        return self._data.get("ai_base_url", DEFAULT_CONFIG["ai_base_url"])

    @property
    def ai_model(self) -> str:
        return self._data.get("ai_model", DEFAULT_CONFIG["ai_model"])

    # Legacy aliases for the pre-rename names; kept read-only so external
    # consumers (the private refine tooling) work against either name.
    @property
    def deepseek_api_key(self) -> str | None:
        return self.ai_api_key

    @property
    def deepseek_base_url(self) -> str:
        return self.ai_base_url

    @property
    def deepseek_model(self) -> str:
        return self.ai_model

    @property
    def state_root(self) -> str | None:
        root = self._data.get("state_root", "")
        return root if root else None

    @property
    def data_dir(self) -> str:
        return self._data.get("data_dir", DEFAULT_CONFIG["data_dir"])

    @property
    def data_proxy_url(self) -> str:
        return str(self._data.get("data_proxy_url", "") or "")

    @property
    def egress_proxy_url(self) -> str:
        return str(self._data.get("egress_proxy_url", "") or "")

    @property
    def egress_switch_cmd(self) -> str:
        return str(self._data.get("egress_switch_cmd", "") or "")

    @property
    def ohlcv_mode(self) -> str:
        """Configured K-line source mode, rejecting ambiguous values."""
        value = self._data.get("ohlcv_mode", DEFAULT_CONFIG["ohlcv_mode"])
        if not isinstance(value, str) or value.strip().lower() not in {
            "live",
            "validated",
        }:
            raise ValueError("ohlcv_mode must be 'live' or 'validated'")
        return value.strip().lower()

    @property
    def validated_bars_root(self) -> str:
        """Offline consensus-manifest root; empty means not configured."""
        value = self._data.get(
            "validated_bars_root", DEFAULT_CONFIG["validated_bars_root"]
        )
        if not isinstance(value, (str, os.PathLike)):
            raise ValueError("validated_bars_root must be a path string")
        return str(value).strip()

    @property
    def external_bars_root(self) -> str:
        """Operator-local exchange series root (SGX: charts); empty = off."""
        value = self._data.get(
            "external_bars_root", DEFAULT_CONFIG["external_bars_root"]
        )
        if not isinstance(value, (str, os.PathLike)):
            raise ValueError("external_bars_root must be a path string")
        return str(value).strip()

    @property
    def intake_argv(self) -> tuple[str, ...]:
        """Configured daily-data command as argv; a shell is never involved."""
        raw = self._data.get("intake_cmd", "")
        if isinstance(raw, list):
            return tuple(str(part) for part in raw if str(part))
        if not isinstance(raw, str) or not raw.strip():
            return ()
        try:
            return tuple(shlex.split(raw))
        except ValueError:
            return ()

    @property
    def workspace_root(self) -> str:
        value = self._data.get("workspace_root", DEFAULT_CONFIG["workspace_root"])
        return str(value or DEFAULT_CONFIG["workspace_root"])

    @property
    def history_db(self) -> str:
        """Report-history SQLite path; empty follows the intake workspace."""
        value = str(self._data.get("history_db") or "").strip()
        if value:
            return value
        return str(Path(self.workspace_root) / "history.db")

    @property
    def intake_timeout_seconds(self) -> int:
        value = self._data.get(
            "intake_timeout_seconds", DEFAULT_CONFIG["intake_timeout_seconds"]
        )
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return int(DEFAULT_CONFIG["intake_timeout_seconds"])

    @property
    def plugins(self) -> list[dict[str, str]]:
        """Configured domain plugins for the dashboard PLUGINS zone.

        Each entry is {"label": str, "root": path}; malformed entries are
        dropped silently (an operator-local typo must never break the
        dashboard). Labels are uppercased and roots are ``~``-expanded.
        """
        raw = self._data.get("plugins", DEFAULT_CONFIG["plugins"])
        if not isinstance(raw, list):
            return []
        plugins: list[dict[str, str]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            label = entry.get("label")
            root = entry.get("root")
            if not isinstance(label, str) or not label.strip():
                continue
            if not isinstance(root, (str, os.PathLike)) or not str(root).strip():
                continue
            plugins.append({
                "label": label.strip().upper(),
                "root": os.path.expanduser(str(root).strip()),
            })
        return plugins

    @property
    def futures_symbols(self) -> list[str]:
        """FUTURES board tickers; malformed entries are dropped silently."""
        raw = self._data.get("futures_symbols", DEFAULT_CONFIG["futures_symbols"])
        if not isinstance(raw, list):
            return []
        symbols: list[str] = []
        for entry in raw:
            if isinstance(entry, str) and entry.strip():
                symbol = entry.strip().upper()
                if symbol not in symbols:
                    symbols.append(symbol)
        return symbols

    @property
    def security_symbols(self) -> list[str]:
        """SECURITY board tickers; malformed entries are dropped silently."""
        raw = self._data.get("security_symbols", DEFAULT_CONFIG["security_symbols"])
        if not isinstance(raw, list):
            return []
        symbols: list[str] = []
        for entry in raw:
            if isinstance(entry, str) and entry.strip():
                symbol = entry.strip().upper()
                if symbol not in symbols:
                    symbols.append(symbol)
        return symbols

    @property
    def futures_argv(self) -> tuple[str, ...]:
        """Configured futures board command as argv; a shell is never involved."""
        raw = self._data.get("futures_cmd", "")
        if isinstance(raw, list):
            return tuple(str(part) for part in raw if str(part))
        if not isinstance(raw, str) or not raw.strip():
            return ()
        try:
            return tuple(shlex.split(raw))
        except ValueError:
            return ()

    @property
    def shipping_argv(self) -> tuple[str, ...]:
        """Configured shipping board command as argv; a shell is never involved."""
        raw = self._data.get("shipping_cmd", "")
        if isinstance(raw, list):
            return tuple(str(part) for part in raw if str(part))
        if not isinstance(raw, str) or not raw.strip():
            return ()
        try:
            return tuple(shlex.split(raw))
        except ValueError:
            return ()

    @property
    def spval_argv(self) -> tuple[str, ...]:
        """Configured S&P valuation board command as argv; no shell involved."""
        raw = self._data.get("spval_cmd", "")
        if isinstance(raw, list):
            return tuple(str(part) for part in raw if str(part))
        if not isinstance(raw, str) or not raw.strip():
            return ()
        try:
            return tuple(shlex.split(raw))
        except ValueError:
            return ()

    @property
    def racing_argv(self) -> tuple[str, ...]:
        """Configured racing board command as argv; a shell is never involved."""
        raw = self._data.get("racing_cmd", "")
        if isinstance(raw, list):
            return tuple(str(part) for part in raw if str(part))
        if not isinstance(raw, str) or not raw.strip():
            return ()
        try:
            return tuple(shlex.split(raw))
        except ValueError:
            return ()

    @property
    def recent_symbols(self) -> list[str]:
        return self._data.get("recent_symbols", [])

    def add_recent_symbol(self, symbol: str) -> None:
        """Add a symbol to recent list (max 20)."""
        recent = self.recent_symbols
        if symbol in recent:
            recent.remove(symbol)
        recent.insert(0, symbol)
        self._data["recent_symbols"] = recent[:20]
        self.save()

    @property
    def recent_strategies(self) -> list[str]:
        return self._data.get("recent_strategies", [])

    def add_recent_strategy(self, strategy: str) -> None:
        recent = self.recent_strategies
        if strategy in recent:
            recent.remove(strategy)
        recent.insert(0, strategy)
        self._data["recent_strategies"] = recent[:10]
        self.save()

    @property
    def default_strategy(self) -> str:
        return self._data.get("default_strategy", "dual_ma")

    @property
    def default_fast(self) -> int:
        return self._data.get("default_fast", 20)

    @property
    def default_slow(self) -> int:
        return self._data.get("default_slow", 50)

    @property
    def default_cost_tier(self) -> str:
        return self._data.get("default_cost_tier", "low")

    @property
    def default_rebalance(self) -> str:
        return self._data.get("default_rebalance", "M")

    @property
    def default_lookback(self) -> int:
        return self._data.get("default_lookback", 60)
