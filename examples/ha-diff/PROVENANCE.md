# Example provenance

- `motion-success.json`, `motion-blocked.json`: synthetic, controlled examples in the
  verified native Home Assistant download shape. Lux changes from 12 to 125, the
  recorded template condition changes from true to false, and the light action is
  absent from the blocked run. No real household execution or causal replay is claimed.
  Original data, Apache-2.0 like the rest of flowwrench.
- `motion-comparison.html`: the HTML report `flowwrench ha-diff` produces for the two
  motion files (`--format html`).
- `theme-error.json`: the Home Assistant Automation Testkit trace fixture
  (`tests/fixtures/trace_sample.json` at commit
  `685f9506c597a3290a9d6cd2b4d8d8c4ed8507a2` of
  https://github.com/N4S4/homeassistant-automation-testkit), unchanged: a downloaded
  trace of `frontend.set_theme` failing with "Theme waves not found". **GPL-3.0**,
  copyright the upstream authors; the license text is in
  [TESTKIT-LICENSE.txt](TESTKIT-LICENSE.txt). It is test/example data only; no Testkit
  code is used.
- `theme-success.json`: a clearly labelled counterfactual derived from `theme-error.json`
  by removing the two recorded error messages and changing `script_execution` to
  `finished`. It is not a separate real Home Assistant run and keeps the GPL-3.0
  provenance of its source.

See [tests/ha_diff/fixtures/PROVENANCE.md](../../tests/ha_diff/fixtures/PROVENANCE.md) for the
upstream test fixtures.
