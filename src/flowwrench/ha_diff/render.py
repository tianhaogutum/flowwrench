"""Human-readable diagnostic output; JSON output is the full evidence contract."""

from __future__ import annotations

import json
from html import escape


def _visible(value: str) -> str:
    """Escape terminal control characters in untrusted field names and file labels."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


def _value(ref: dict) -> str:
    if not ref["present"]:
        return "<not recorded>"
    rendered = json.dumps(
        ref.get("compared_value", ref["value"]), ensure_ascii=False, sort_keys=True
    )
    return rendered if len(rendered) <= 180 else rendered[:177] + "..."


def _change_lines(change: dict) -> list[str]:
    lines = [
        (
            f"  [{change['category']}] {_visible(change['field']) or '(step)'}: "
            f"{_value(change['left'])} -> {_value(change['right'])}"
        )
    ]
    for side in ("left", "right"):
        ref = change[side]
        if ref["present"]:
            lines.append(f"    {side}: {_visible(ref['source'])}#{_visible(ref['pointer'])}")
    return lines


def render_text(report: dict, *, show_equal: bool = False) -> str:
    summary = report["summary"]
    lines = [
        f"Home Assistant trace comparison: {report['status']}",
        (
            f"Coverage: {report['coverage']} | {summary['different_rows']} differing step(s) / "
            f"{summary['aligned_rows']} aligned row(s)"
        ),
        (
            "First differing recorded step: "
            + (
                report["first_divergence"]
                or (
                    "not uniquely established"
                    if summary["different_rows"]
                    else "none in compared step fields"
                )
            )
        ),
    ]
    if report["first_control_divergence"]:
        lines.append(
            "First condition/branch/error/path difference: " + report["first_control_divergence"]
        )
    if report["first_divergence_candidates"] and not report["first_divergence"]:
        lines.append("Earliest candidates: " + ", ".join(report["first_divergence_candidates"]))
    for heading, key in (
        ("Configuration", "configuration_changes"),
        ("Run outcome", "run_changes"),
    ):
        if report[key]:
            lines.extend(("", heading))
            for change in report[key]:
                lines.extend(_change_lines(change))
    for row in report["steps"]:
        if not row["changes"] and not show_equal:
            continue
        first = " <-- FIRST" if row["id"] == report["first_divergence"] else ""
        lines.extend(("", f"{row['id']}: {row['status']}{first}"))
        if "configuration" in row:
            for side, config in row["configuration"].items():
                if config["label"]:
                    reference = config["label_reference"]
                    lines.append(
                        f"  {side} step: {_visible(config['label'])} "
                        f"({_visible(reference['source'])}#{_visible(reference['pointer'])})"
                    )
        for change in row["changes"]:
            lines.extend(_change_lines(change))
    if report["warnings"]:
        lines.extend(("", "Evidence limits"))
        lines.extend(f"- {_visible(warning)}" for warning in report["warnings"])
    lines.extend(("", report["interpretation"]))
    return "\n".join(lines) + "\n"


def _html_ref(ref: dict) -> str:
    if not ref["present"]:
        return '<span class="missing">Not recorded</span>'
    value = json.dumps(ref.get("compared_value", ref["value"]), ensure_ascii=False, indent=2)
    shown = escape(value if len(value) <= 240 else value[:237] + "…")
    reference = escape(ref["source"] + "#" + ref["pointer"])
    details = (
        f"<details><summary>Full compared value</summary><pre>{escape(value)}</pre></details>"
        if len(value) > 240
        else ""
    )
    filtered = (
        "<small>Value after the explicit ignore policy.</small>" if "compared_value" in ref else ""
    )
    return f'<pre>{shown}</pre>{filtered}{details}<code class="reference">{reference}</code>'


def _html_changes(changes: list[dict]) -> str:
    if not changes:
        return '<p class="muted">No difference in compared recorded fields.</p>'
    body = "".join(
        f'<tr><th scope="row"><span class="tag">{escape(change["category"])}</span>'
        f"<code>{escape(change['field'] or '(step)')}</code>"
        f"<small>{escape(change['kind'])}</small></th>"
        f"<td>{_html_ref(change['left'])}</td><td>{_html_ref(change['right'])}</td></tr>"
        for change in changes
    )
    return (
        '<div class="scroll"><table><thead><tr><th scope="col">Recorded field</th>'
        '<th scope="col">Left · baseline</th><th scope="col">Right · comparison</th>'
        f"</tr></thead><tbody>{body}</tbody></table></div>"
    )


def render_html(report: dict, *, show_equal: bool = False) -> str:
    """A self-contained, script-free report; never fetches or embeds input files."""
    summary = report["summary"]
    anchors = {row["id"]: f"step-{index}" for index, row in enumerate(report["steps"])}

    def first_link(key: str) -> str:
        row_id = report[key]
        if row_id is None:
            candidates = report[key + "_candidates"]
            if candidates:
                links = ", ".join(
                    f'<a href="#{anchors[candidate]}">{escape(candidate)}</a>'
                    for candidate in candidates
                )
                return "Not uniquely established. Recorded candidates: " + links
            return (
                "Not uniquely established"
                if summary["different_rows"]
                else "No step-level difference"
            )
        return f'<a href="#{anchors[row_id]}">{escape(row_id)}</a>'

    warnings = "".join(f"<li>{escape(warning)}</li>" for warning in report["warnings"])
    source_rows = "".join(
        f"<li><strong>{side.title()}</strong>: <code>{escape(item['source'])}</code> · "
        f"{item['recorded_steps']} recorded steps · <code>{escape(item['run_id'])}</code></li>"
        for side, item in report["inputs"].items()
    )
    sections = []
    for title, key in (
        ("Configuration changes", "configuration_changes"),
        ("Run outcome changes", "run_changes"),
    ):
        if report[key]:
            sections.append(f"<section><h2>{title}</h2>{_html_changes(report[key])}</section>")
    for row in report["steps"]:
        if not row["changes"] and not show_equal:
            continue
        is_first = row["id"] == report["first_divergence"]
        label = (
            ' <span class="tag first-label">First recorded divergence</span>' if is_first else ""
        )
        status = escape(row["status"].replace("_", " "))
        config_details = ""
        if "configuration" in row:
            descriptions = []
            for side, config in row["configuration"].items():
                reference = config["reference"]
                source = (
                    f"<code>{escape(reference['source'] + '#' + reference['pointer'])}</code>"
                    if reference
                    else escape(config["reason"])
                )
                descriptions.append(
                    f"<li><strong>{side.title()}</strong>: {escape(config['label'] or 'Step configuration')}"
                    f" · {escape(config['status'])} · {source}"
                    f"{(' · unresolved: ' + escape(config['unresolved_path'])) if config['unresolved_path'] else ''}</li>"
                )
            config_details = (
                "<details><summary>Step configuration references</summary><ul>"
                + "".join(descriptions)
                + "</ul></details>"
            )
        sections.append(
            f'<section id="{anchors[row["id"]]}" class="step {"first" if is_first else ""}">'
            f'<h2><code>{escape(row["id"])}</code>{label}</h2><p class="muted">{status}</p>'
            f"{config_details}{_html_changes(row['changes'])}</section>"
        )
    policy = escape(json.dumps(report["policy"], ensure_ascii=False, indent=2))
    partial = (
        '<p class="partial">Partial evidence: some steps or fields are missing or unexamined.</p>'
        if report["coverage"] == "partial"
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Home Assistant trace comparison</title>
<style>
:root {{ color-scheme: light dark; --bg:#f3f5f8; --card:#fff; --fg:#172132; --muted:#516079; --line:#d8dfe8; --accent:#126c9b; --first:#e9f6ff; --warning:#fff0ca; }}
@media (prefers-color-scheme:dark) {{ :root {{ --bg:#10151e; --card:#192230; --fg:#e3eaf4; --muted:#aebbd0; --line:#37455a; --accent:#7ecbff; --first:#19364d; --warning:#44371c; }} }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.55 system-ui,sans-serif; }}
main {{ max-width:1240px; margin:auto; padding:32px 20px 70px; }} h1 {{ margin:4px 0 12px; font-size:30px; line-height:1.2; }} h2 {{ font-size:19px; margin:0 0 10px; }}
header,section,aside {{ margin:0 0 20px; padding:24px; background:var(--card); border:1px solid var(--line); border-radius:12px; }}
.eyebrow,.muted,small {{ color:var(--muted); }} .eyebrow {{ letter-spacing:.08em; text-transform:uppercase; font-size:12px; }}
.stats {{ display:flex; gap:12px; flex-wrap:wrap; margin:16px 0; }} .stats div {{ padding:10px 14px; border:1px solid var(--line); border-radius:8px; }} .stats b {{ display:block; font-size:22px; }}
.first {{ background:var(--first); border:2px solid var(--accent); scroll-margin-top:20px; }} a {{ color:var(--accent); overflow-wrap:anywhere; }}
.tag {{ display:block; font-size:12px; font-weight:600; color:var(--accent); margin-bottom:6px; }} .first-label {{ display:inline; margin-left:10px; }}
.scroll {{ overflow-x:auto; }} table {{ width:100%; border-collapse:collapse; table-layout:fixed; min-width:620px; }} th,td {{ text-align:left; vertical-align:top; border-top:1px solid var(--line); padding:14px 12px; }} th:first-child {{ width:28%; }} td {{ width:36%; }}
code,pre {{ font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:12px; overflow-wrap:anywhere; }} pre {{ white-space:pre-wrap; margin:0 0 10px; max-height:24em; overflow:auto; }} small,.reference {{ display:block; }} .reference {{ margin-top:10px; color:var(--muted); }}
.missing {{ color:var(--muted); font-style:italic; }} .partial {{ padding:12px; border-radius:6px; background:var(--warning); }} summary {{ cursor:pointer; }} details {{ margin:10px 0; }} ul {{ padding-left:22px; }}
@media(max-width:640px) {{ main {{ padding:16px 10px; }} header,section,aside {{ padding:16px; }} h1 {{ font-size:25px; }} }}
@media print {{ body {{ background:white; color:black; }} main {{ max-width:none; padding:0; }} section,header,aside {{ break-inside:avoid; }} .scroll {{ overflow:visible; }} pre {{ max-height:none; }} }}
</style></head><body><main>
<header><div class="eyebrow">Home Assistant · offline evidence comparison</div>
<h1>{escape(report["status"].replace("_", " ").title())}</h1>
<p>{escape(report["identity"]["domain"])} · <code>{escape(report["identity"]["item_id"])}</code></p>
{partial}<div class="stats"><div><b>{summary["different_rows"]}</b> differing steps</div><div><b>{summary["field_changes"]}</b> recorded field changes</div><div><b>{summary["aligned_rows"]}</b> aligned rows</div></div>
<p><strong>Earliest differing recorded step:</strong> {first_link("first_divergence")}</p>
<p><strong>First condition / branch / error / path difference:</strong> {first_link("first_control_divergence")}</p>
<p class="muted">{escape(report["interpretation"])}</p><details><summary>Input files and comparison policy</summary><ul>{source_rows}</ul><pre>{policy}</pre></details></header>
<aside><h2>Evidence limits</h2>{f"<ul>{warnings}</ul>" if warnings else "<p>No additional input warnings.</p>"}<p class="muted">Order uses recorded step start times. Variable deltas are compared without reconstructing Home Assistant's execution engine. Reports can contain private input values.</p></aside>
{"".join(sections)}<footer class="muted">Generated locally by flowwrench ha-diff · {escape(report["schema"])}</footer>
</main></body></html>"""
