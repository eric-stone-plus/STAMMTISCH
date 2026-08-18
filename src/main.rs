use std::process::exit;

/// Exit when the TUI that spawned us is gone (unix only).
///
/// The TUI driver stops its core children on graceful shutdown, but a
/// kill -9 or a lost terminal skips those hooks and a mid-`run` core
/// would keep working on its own. The kernel reparents orphans, so
/// polling `parent_id()` detects the loss within a couple of seconds.
/// The env var is only set by the TUI driver; manual CLI runs have no
/// parent contract and are unaffected.
#[cfg(unix)]
fn spawn_parent_death_watch() {
    let Some(raw) = std::env::var("STAMMTISCH_PARENT_PID").ok() else {
        return;
    };
    let Ok(parent) = raw.trim().parse::<u32>() else {
        return;
    };
    std::thread::spawn(move || loop {
        std::thread::sleep(std::time::Duration::from_secs(2));
        if std::os::unix::process::parent_id() != parent {
            stammtisch::runner::seal_active_run_on_parent_loss();
            exit(0);
        }
    });
}

#[cfg(not(unix))]
fn spawn_parent_death_watch() {}

fn main() {
    spawn_parent_death_watch();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let code = match stammtisch::cmd::parse_args(&args) {
        Ok(cli) => stammtisch::cmd::dispatch(&cli),
        Err(e) => {
            // Parse errors must honor --json too: scan raw args.
            eprintln!("stammtisch: error: {e}");
            if args.iter().any(|a| a == "--json") {
                stammtisch::envelope::print(&stammtisch::envelope::err("args", &e));
            } else {
                eprintln!();
                eprintln!("stammtisch init | validate --pipeline FILE | run --pipeline FILE |");
                eprintln!("  status [RUN_ID] | inspect RUN_ID | reconcile | cancel |");
                eprintln!("  export RUN_ID --out DIR | verify --bundle DIR   [--json on any]");
            }
            e.exit_code()
        }
    };
    exit(code);
}
