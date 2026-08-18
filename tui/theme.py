"""nmtui-style theme — clean, minimal, functional."""

THEME_CSS = """
Screen {
    background: #000000;
    color: #a0a0a0;
}

Static, Label, ListItem {
    color: #a0a0a0;
}

/* ── Panels ───────────────────────────────────────────────────── */
.panel {
    background: #000000;
    border: solid #505050;
    padding: 0 1;
    margin: 0 0 0 0;
}
.panel-title {
    text-style: bold;
    color: #ffffff;
    background: #303030;
    dock: top;
    height: 1;
    margin: -1 -1 0 -1;
    padding: 0 1;
}
.panel-green { border: solid #2e7d32; }
.panel-green .panel-title { color: #66bb6a; background: #1b3a1b; }
.panel-amber { border: solid #9e7c1a; }
.panel-amber .panel-title { color: #ffd54f; background: #3a3010; }
.panel-red { border: solid #c62828; }
.panel-red .panel-title { color: #ef5350; background: #3a1010; }

/* ── Header bars ──────────────────────────────────────────────── */
.header-bar {
    height: 1;
    color: #ffffff;
    background: #303030;
    text-style: bold;
    margin: 0 0 0 0;
    padding: 0 1;
}

/* ── DataTable ────────────────────────────────────────────────── */
DataTable { background: #000000; }
DataTable > .datatable--header { background: #303030; text-style: bold; color: #ffffff; }
DataTable > .datatable--cursor { background: #1a3a5c; color: #ffffff; }

/* ── Input / Select ───────────────────────────────────────────── */
Input { background: #000000; border: solid #505050; color: #a0a0a0; }
Input:focus { border: solid #4fc3f7; }
Select { background: #000000; border: solid #505050; }
Select:focus { border: solid #4fc3f7; }

/* ── Scrollbar ────────────────────────────────────────────────── */
ScrollBar { background: #000000; }
ScrollBar > .scrollbar--thumb { background: #303030; }

/* ── Chat ─────────────────────────────────────────────────────── */
#chat-messages { background: #000000; border: solid #505050; }
#chat-input { background: #000000; border: solid #4fc3f7; color: #a0a0a0; }

/* ── OptionList ───────────────────────────────────────────────── */
OptionList { background: #000000; border: solid #505050; }
OptionList > .option-list--option-highlighted { background: #1a3a5c; color: #ffffff; }

/* ── Buttons ──────────────────────────────────────────────────── */
Button { background: #303030; color: #ffffff; border: solid #505050; }
Button:focus { background: #1a3a5c; border: solid #4fc3f7; }

/* ── Status classes ───────────────────────────────────────────── */
.status-amber { color: #ffd54f; }
.status-green { color: #66bb6a; }
.status-red { color: #ef5350; }
.status-cyan { color: #4fc3f7; }
.status-gray { color: #a0a0a0; }
"""
