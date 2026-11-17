# Fixture provenance

Downloaded 2026-11-11. Upstream JSON files are preserved byte-for-byte under `upstream/`.

- `upstream/automation_saved_traces.json`: https://raw.githubusercontent.com/home-assistant/core/2026.11.1/tests/components/trace/fixtures/automation_saved_traces.json — real persisted automation traces from Home Assistant's own websocket tests (timestamps originate in 2021). Apache-2.0; copyright Home Assistant authors.
- `upstream/script_saved_traces.json`: https://raw.githubusercontent.com/home-assistant/core/2026.11.1/tests/components/trace/fixtures/script_saved_traces.json — real persisted script traces, same source tag/license.
- `upstream/testkit_trace_sample.json`: https://raw.githubusercontent.com/N4S4/homeassistant-automation-testkit/685f9506c597a3290a9d6cd2b4d8d8c4ed8507a2/tests/fixtures/trace_sample.json — a downloaded Home Assistant trace of `frontend.set_theme` failing with “Theme waves not found”, captured 2026-10-07. GPL-3.0; copyright upstream authors. Retained as test data only; no Testkit implementation code is incorporated.

`examples/ha-diff/theme-error.json` is the Testkit fixture unchanged; `examples/ha-diff/theme-success.json` is a clearly labelled counterfactual derived by removing the two recorded error messages and changing script_execution to finished. It is not claimed to be a separate real HA run. Tests construct further mutations in memory to isolate specific failure/edge cases.

Upstream licenses are included beside the fixtures. The adapter's original MIT notice is kept in `licenses/ha-trace-diff-MIT.txt`; flowwrench is Apache-2.0. Neither license applies to these upstream files: including upstream test data does not relicense them.

examples/ha-diff/motion-success.json and examples/ha-diff/motion-blocked.json are synthetic controlled examples in the verified native download shape. They demonstrate a recorded lux variable difference, true/false template condition outcomes and an action missing from the blocked run; no real household execution or causal replay is claimed.

A final integrity audit re-fetched all three pinned source URLs and confirmed byte-for-byte equality. SHA256SUMS records the exact fixture bytes.
