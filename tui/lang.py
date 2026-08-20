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
    "crawlers.containers": {"en": "Containers", "zh": "容器"},
    "crawlers.running": {"en": "running", "zh": "运行中"},
    "crawlers.enabled": {"en": "enabled", "zh": "启用"},
    "crawlers.toggle_hint": {"en": "toggle", "zh": "启停"},
    "crawlers.k.refresh": {"en": "refresh status + sources", "zh": "刷新状态与源清单"},
    "crawlers.k.stack": {"en": "crawl stack on/off (compose stop/up)", "zh": "采集栈开/关"},
    "crawlers.k.timer": {"en": "self-heal timer on/off", "zh": "自愈定时器开/关"},
    "crawlers.k.restart": {"en": "restart the api container", "zh": "重启 api 容器"},
    "crawlers.k.heal": {"en": "run the heal command now", "zh": "立即执行自愈命令"},
    "crawlers.k.toggle": {"en": "enable/disable the highlighted source", "zh": "启用/停用高亮源"},
    "crawlers.k.move": {"en": "move in the source list", "zh": "在源列表中移动"},
    "crawlers.k.back": {"en": "back to the dashboard", "zh": "返回主页"},
    "crawlers.log": {"en": "Log", "zh": "日志"},
    "crawlers.no_ops": {"en": "(no operations yet)", "zh": "（暂无操作）"},
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
    "galahad.report_prompt": {
        "en": "Analyze this daily market dataset: market-state read, "
        "cross-market linkages, notable signals and risks, and what to "
        "verify tomorrow. Data follows.",
        "zh": "分析这份每日市场数据:给出市场状态判读、跨市场联动、值得注意的"
        "信号与风险,以及明天需要验证什么。数据如下。",
    },
    "sentiment.history_hint": {
        "en": "(READ-ONLY)  |  ←/→ history  [O] ask GALAHAD  [Esc] Back",
        "zh": "(只读)  |  ←/→ 历史  [O] 问 GALAHAD  [Esc] 返回",
    },
}


def tr(language: str, key: str, default: str | None = None) -> str:
    entry = STRINGS.get(key)
    if entry is None:
        return default if default is not None else key
    return entry.get(language) or entry.get("en") or (default or key)
