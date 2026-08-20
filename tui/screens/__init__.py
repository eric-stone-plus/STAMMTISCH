"""Screens — dashboard-first workstation.

Split by screen family; this package facade re-exports the previous
single-module surface so `from tui.screens import X` keeps working.
"""

from __future__ import annotations

from .modals import ConfirmScreen, HelpScreen, KeyHelpScreen
from .sessions import (
    _ASK_INTRO,
    _INPUT_HISTORY_CAP,
    _SESSION_KEEP,
    _ask_dir,
    _atomic_write_json,
    _atomic_write_text,
    _created_stamp,
    _intake_session_parts,
    _load_json,
    _new_ask_session_id,
    _session_path,
    _thought_line,
    delete_ask_session,
    format_run_session_summary,
    list_ask_sessions,
    load_ask_session,
    load_input_history,
    render_ask_session,
    run_session_parts,
    run_session_title,
    save_ask_session,
    save_input_history,
)
from .chat import AskSessionScreen, ChatInput, ChatScreen
from .dashboard import GENERAL_ITEMS, DashboardScreen, PipelineList, RegistryTable
from .domains import (
    SECURITY_ZONES,
    DomainBrowserScreen,
    FuturesScreen,
    SecurityScreen,
    ShippingScreen,
    _RowTable,
    _open_browser_chart,
    security_zone,
)
from .daily_intake import DailyIntakeScreen, _galahad_report_analysis, _history_store, _report_digest
from .runs import (
    PipelineRunScreen,
    PipelineViewScreen,
    RunInspectorScreen,
    ValidateScreen,
    _pipeline_summary,
)
from .config_screen import ConfigScreen

__all__ = [
    "AskSessionScreen",
    "KeyHelpScreen",
    "ChatInput",
    "ChatScreen",
    "ConfigScreen",
    "ConfirmScreen",
    "DailyIntakeScreen",
    "DashboardScreen",
    "DomainBrowserScreen",
    "FuturesScreen",
    "HelpScreen",
    "PipelineList",
    "PipelineRunScreen",
    "PipelineViewScreen",
    "RegistryTable",
    "RunInspectorScreen",
    "SecurityScreen",
    "ShippingScreen",
    "ValidateScreen",
    "security_zone",
    "run_session_title",
]
