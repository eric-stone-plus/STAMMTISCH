"""`stammtisch config` — manage the TUI workstation config from the CLI.

Stdlib-only on purpose: this runs even without the Textual dependency and
edits the same file the TUI reads/writes (default
~/.config/stammtisch/config.json, override with STAMMTISCH_CONFIG).

Usage:
  stammtisch config                 show current config (API key masked)
  stammtisch config set-key [KEY]   set the AI API key (prompts if omitted)
  stammtisch config unset-key       remove the AI API key
  stammtisch config set KEY VALUE   set any config key (see below)
  stammtisch config get KEY         print one value
  stammtisch config unset KEY       remove a key (falls back to default)
  stammtisch config path            print the config file path
  stammtisch config edit            open the config file in $EDITOR
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from typing import Any

from .config import DEFAULT_CONFIG, Config

# Keys whose values must be integers.
_INT_KEYS = {
    "default_fast",
    "default_slow",
    "default_lookback",
    "intake_timeout_seconds",
}
# Keys stored as comma-separated lists.
_LIST_KEYS = {"recent_symbols", "recent_strategies", "futures_symbols", "security_symbols"}
_CHOICE_KEYS = {"ohlcv_mode": frozenset({"live", "validated"})}
# Keys masked in `show`/`get` output.
_SECRET_KEYS = frozenset({"ai_api_key", "eia_api_key"})

USAGE = """stammtisch config — TUI workstation configuration

USAGE:
  stammtisch config
  stammtisch config set-key [KEY]
  stammtisch config unset-key
  stammtisch config set KEY VALUE
  stammtisch config get KEY
  stammtisch config unset KEY
  stammtisch config path
  stammtisch config edit

EXIT CODES: 0 ok · 2 usage"""


def mask_secret(value: str) -> str:
    """Mask an API key: sk-12****ef. Empty stays empty."""
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _prompt_secret(prompt: str = "AI API key: ") -> str:
    """Read a secret with getpass (hidden echo), stdin fallback for non-tty."""
    try:
        import getpass
        return getpass.getpass(prompt).strip()
    except (EOFError, OSError):
        return sys.stdin.readline().strip()


def _format_list(value: list) -> str:
    """Render a list config value; entries may be dicts (e.g. plugins)."""
    if not value:
        return "(empty)"
    return ", ".join(
        item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        for item in value
    )


def _show(config: Config) -> int:
    print(f"Config file: {config.path}")
    print()
    for key in DEFAULT_CONFIG:
        value = config.get(key)
        if key in _SECRET_KEYS:
            shown: Any = mask_secret(str(value))
        elif isinstance(value, list):
            shown = _format_list(value)
        else:
            shown = value
        env_note = " (env)" if key in config._from_env else ""
        print(f"  {key:<22} {shown}{env_note}")
    return 0


def _set_key(config: Config, args: list[str]) -> int:
    key = args[0] if args else _prompt_secret()
    if not key:
        print("stammtisch config: no key provided; nothing changed", file=sys.stderr)
        return 2
    config.set("ai_api_key", key)
    print(f"AI API key saved to {config.path} (0600).")
    return 0


def _unset_key(config: Config) -> int:
    config.set("ai_api_key", "")
    print("AI API key removed.")
    return 0


def _coerce_value(key: str, raw: str) -> Any:
    """Validate + coerce a raw CLI value for the given config key."""
    if key in _INT_KEYS:
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{key} requires an integer, got '{raw}'")
    if key in _LIST_KEYS:
        return [part.strip() for part in raw.split(",") if part.strip()]
    if key in _CHOICE_KEYS:
        value = raw.strip().lower()
        if value not in _CHOICE_KEYS[key]:
            choices = " or ".join(repr(item) for item in sorted(_CHOICE_KEYS[key]))
            raise ValueError(f"{key} requires {choices}, got '{raw}'")
        return value
    return raw


def _set(config: Config, args: list[str]) -> int:
    key, raw = args[0], args[1]
    if key not in DEFAULT_CONFIG:
        print(f"stammtisch config: unknown key '{key}'. Known keys:", file=sys.stderr)
        print("  " + ", ".join(DEFAULT_CONFIG), file=sys.stderr)
        return 2
    try:
        value = _coerce_value(key, raw)
    except ValueError as e:
        print(f"stammtisch config: {e}", file=sys.stderr)
        return 2
    config.set(key, value)
    print(f"{key} = {config.get(key)} (saved)")
    return 0


def _get(config: Config, args: list[str]) -> int:
    key = args[0]
    if key not in DEFAULT_CONFIG:
        print(f"stammtisch config: unknown key '{key}'", file=sys.stderr)
        return 2
    value = config.get(key)
    if key in _SECRET_KEYS:
        print(mask_secret(str(value)))
    elif isinstance(value, list):
        print(_format_list(value) if value else "")
    else:
        print(value)
    return 0


def _unset(config: Config, args: list[str]) -> int:
    key = args[0]
    if key not in DEFAULT_CONFIG:
        print(f"stammtisch config: unknown key '{key}'", file=sys.stderr)
        return 2
    config.set(key, DEFAULT_CONFIG[key])
    print(f"{key} reset to default ({DEFAULT_CONFIG[key]!r})")
    return 0


def _path(config: Config) -> int:
    print(config.path)
    return 0


def _edit(config: Config) -> int:
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        print(f"No $EDITOR set. Config file: {config.path}", file=sys.stderr)
        return 2
    if not config.path.exists():
        config.save()  # Create the file so the editor has something to open.
    # $EDITOR commonly carries arguments ("code --wait"): split them, and
    # treat a missing editor binary as a clean error, not a traceback.
    argv = shlex.split(editor) + [str(config.path)]
    try:
        return subprocess.call(argv)
    except OSError as e:
        print(f"stammtisch config: cannot launch editor: {e}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return _show(Config())
    if args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    sub = args[0]
    rest = args[1:]
    config = Config()

    if sub == "set-key":
        return _set_key(config, rest) if len(rest) <= 1 else _usage("set-key takes at most one KEY")
    if sub == "unset-key":
        return _no_args(rest, _unset_key, config, sub)
    if sub == "set":
        return _set(config, rest) if len(rest) == 2 else _usage("set requires KEY and VALUE")
    if sub == "get":
        return _get(config, rest) if len(rest) == 1 else _usage("get requires KEY")
    if sub == "unset":
        return _unset(config, rest) if len(rest) == 1 else _usage("unset requires KEY")
    if sub == "path":
        return _no_args(rest, _path, config, sub)
    if sub == "edit":
        return _no_args(rest, _edit, config, sub)
    print(f"stammtisch config: unknown subcommand '{sub}'", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


def _no_args(args: list[str], fn, config: Config, sub: str) -> int:
    if args:
        return _usage(f"'{sub}' takes no arguments")
    return fn(config)


def _usage(message: str) -> int:
    print(f"stammtisch config: {message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
