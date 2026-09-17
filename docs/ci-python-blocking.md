# Making the python CI step blocking

`ci.yml`'s python step runs with `continue-on-error: true` because 17 of the
524 tests import the optional **quantkit** dependency, which lives in a
PRIVATE sibling repo (`quant-core`) that a public runner cannot clone.
This note is the one-move flip plan for both ways out, plus the local
verifier that proves the gate is safe BEFORE you flip it.

## Local pre-flight (works today, no GitHub access needed)

```sh
scripts/verify-python-ci-env.sh bare              # expect: red, 17 quantkit-gated failures
scripts/verify-python-ci-env.sh --with-quantkit /path/to/quantkit
                                                  # expect: green (523 passed + 92 subtests)
```

The script builds a fresh venv exactly like the CI step does, adds quantkit
in the second mode, and exits 0 only when the suite is fully green. Run the
second command against whatever quantkit the chosen option below provides —
if it prints `RESULT: green`, the flip is safe.

## Option A — publish quantkit (preferred long-term)

Make `quant-core` installable from a public location (PyPI, or the public
`GALAHAD` repo already carrying it under `quantkit/`).

1. Publish (out of band, repo-side work).
2. In `.github/workflows/ci.yml`, python step:
   - add the install line:  `python -m pip install quantkit`
   - **delete one line:** `continue-on-error: true`
   - replace the step name with `python tests (blocking)`.
3. Delete the explanatory comment block above the step (it documents the
   non-blocking rationale that no longer applies).
4. Push and watch the run go green-blocking.

## Option B — private checkout via PAT (no publishing needed)

1. Create a fine-grained PAT with read-only Contents access to `quant-core`
   and add it as the `QUANT_CORE_TOKEN` secret in this repo.
2. In `.github/workflows/ci.yml`, python step, insert before the pip line:

   ```yaml
   - uses: actions/checkout@v4
     with:
       repository: eric-stone-plus/quant-core
       token: ${{ secrets.QUANT_CORE_TOKEN }}
       path: quant-core
   ```

   and install with `python -m pip install -e quant-core`.
   Caveat: the runner then needs a private dependency forever — anyone
   without the PAT cannot reproduce CI. Option A avoids that.
3. Same one-line flip: delete `continue-on-error: true`.
4. Before pushing, run the pre-flight script's second mode against the
   exact checkout the runner will use (same commit you just gave the PAT
   access to).

## Either way

After the flip, a red python suite blocks the PR — that is the point. The
cargo + zero-warnings gates are already blocking and stay untouched.
