"""TUI chrome strings, bilingual (English / Simplified Chinese).

The workstation language is a config key (``language``: "en" | "zh")
toggled from the dashboard Quick Start list. Keys cover the chrome this
surface owns; anything missing falls back to the English default the
caller passes, so a missing translation never blanks the UI.
"""

from __future__ import annotations

STRINGS: dict[str, dict[str, str]] = {
    # Quick Start
    "quick.ask": {"en": "ASK GALAHAD", "zh": "问 GALAHAD"},
    "quick.config": {"en": "EDIT CONFIG", "zh": "编辑配置"},
    "quick.crawlers": {"en": "CRAWLERS", "zh": "爬虫面板"},
    "quick.language": {"en": "LANGUAGE", "zh": "语言"},
    "lang.name": {"en": "English", "zh": "简体中文"},
    "help.hint": {"en": "[?] HELP", "zh": "[?] 帮助"},
    "registry.label": {
        "en": "  Run Registry  |  click one  ·  Shift+click range  ·  Ctrl+A all  ·  Del delete",
        "zh": "  运行台账  |  单击选中  ·  Shift+单击 连选  ·  Ctrl+A 全选  ·  Del 删除",
    },
    # Crawler panel
    "crawlers.title": {"en": "CRAWLERS", "zh": "爬虫面板"},
    "crawlers.status": {"en": "Status", "zh": "状态"},
    "crawlers.endpoint": {"en": "Firecrawl endpoint", "zh": "Firecrawl 端点"},
    "crawlers.timer": {"en": "Self-heal timer", "zh": "自愈定时器"},
    "crawlers.egress": {"en": "Egress gateway", "zh": "出口网关"},
    "crawlers.intake": {"en": "Intake command", "zh": "采集命令"},
    "crawlers.up": {"en": "UP", "zh": "在线"},
    "crawlers.down": {"en": "DOWN", "zh": "离线"},
    "crawlers.on": {"en": "ON", "zh": "开"},
    "crawlers.off": {"en": "OFF", "zh": "关"},
    "crawlers.configured": {"en": "configured", "zh": "已配置"},
    "crawlers.not_configured": {"en": "not configured", "zh": "未配置"},
    "crawlers.sources": {"en": "Sources (fin_daily)", "zh": "采集源 (fin_daily)"},
    "crawlers.log": {"en": "Log", "zh": "日志"},
    "crawlers.no_ops": {"en": "  (no operations yet)", "zh": "  (尚无操作)"},
    # Config panel titles
    "config.ai": {"en": "AI Service", "zh": "AI 服务"},
    "config.workspace": {"en": "Workspace", "zh": "工作区"},
    "config.network": {"en": "Egress Proxies", "zh": "出口代理"},
    "proxy.off": {"en": "Direct (no proxy)", "zh": "全部直连 (不走代理)"},
    "proxy.all": {"en": "All via local egress gateway", "zh": "全部经本地出口网关"},
    "proxy.poly": {"en": "Market tape only", "zh": "仅行情走代理 (Polymarket)"},
    "proxy.energy": {"en": "Energy only", "zh": "仅能源走代理 (EIA)"},
    "proxy.custom": {"en": "Custom", "zh": "自定义"},
    "config.energy": {"en": "Energy (EIA)", "zh": "能源 (EIA)"},
    "config.intake": {"en": "Daily Data Intake", "zh": "每日数据采集"},
    "config.forecast": {"en": "Forecast", "zh": "预测"},
    "config.domains": {"en": "Domains", "zh": "板块"},
    "config.backtest": {"en": "Backtest Defaults", "zh": "回测默认"},
    # Sentiment
    "sentiment.history_hint": {
        "en": "(READ-ONLY)  |  ←/→ history  [O] report  [Esc] Back",
        "zh": "(只读)  |  ←/→ 历史  [O] 日报  [Esc] 返回",
    },
}


def tr(language: str, key: str, default: str | None = None) -> str:
    entry = STRINGS.get(key)
    if entry is None:
        return default if default is not None else key
    return entry.get(language) or entry.get("en") or (default or key)
