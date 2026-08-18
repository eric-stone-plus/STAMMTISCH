use std::process::exit;

fn main() {
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
                eprintln!("  status [RUN_ID] | inspect RUN_ID | reconcile |");
                eprintln!("  export RUN_ID --out DIR | verify --bundle DIR   [--json on any]");
            }
            e.exit_code()
        }
    };
    exit(code);
}
