"""Static HTML report: what did my fleet actually do — and what did it lie about.

Reads the run log (written by one process) and the checker verdicts stored inside
it (written by another), and renders a single self-contained HTML file: per run,
what the fleet claimed vs. what the independent checker found. No CDN, no JS, no
external assets — one file an operator can hand to a compliance function.

The inline-CSS discipline here is carried over from the run-inspection tooling this
package grew out of; the body — claimed-vs-verified, not database tables — is new.
"""

from __future__ import annotations

import html as html_lib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runlog import RunRecord, list_run_records


def _find_check_verdicts(run: RunRecord) -> list[dict[str, Any]]:
    """Pull every recorded checker verdict out of a run's sub-invocations."""
    verdicts = []
    for sub in run.sub_invocations:
        if sub.tool == "fleetproof" and sub.subcmd == "check":
            payload = sub.load_output()
            if isinstance(payload, dict) and "checks" in payload:
                verdicts.append(payload)
    return verdicts


def _run_status(run: RunRecord, verdicts: list[dict[str, Any]]) -> str:
    """Classify a run for the report: 'contradicted', 'verified', or 'unverified'."""
    if not verdicts:
        return "unverified"
    if any(v.get("verdict") == "fail" for v in verdicts):
        return "contradicted"
    return "verified"


# One enriched run: (record, its checker verdicts, its status).
_Enriched = tuple[RunRecord, list[dict[str, Any]], str]


def _session_blocks(enriched: list[_Enriched]) -> list[tuple[str, str | None, list[_Enriched]]]:
    """Partition enriched runs into render blocks, newest activity first.

    Runs sharing a ``session_id`` collapse into one ``("session", id, [...])``
    block (chronological inside). Runs without a session id — including every
    record written before session grouping existed — each render as their own
    ``("run", None, [item])`` block, exactly as before. Blocks are ordered by
    their most-recent run so the newest activity leads, matching the flat
    newest-first ordering legacy reports had.
    """
    by_sid: dict[str | None, list[_Enriched]] = {}
    for item in enriched:
        sid = item[0].session_id or None
        by_sid.setdefault(sid, []).append(item)

    blocks: list[tuple[str, str | None, list[_Enriched]]] = []
    for sid, items in by_sid.items():
        if sid is None:
            continue
        items_sorted = sorted(items, key=lambda it: it[0].started_at or "")
        blocks.append(("session", sid, items_sorted))
    for item in by_sid.get(None, []):
        blocks.append(("run", None, [item]))

    def _recency(block: tuple[str, str | None, list[_Enriched]]) -> str:
        return max((it[0].started_at or "" for it in block[2]), default="")

    blocks.sort(key=_recency, reverse=True)
    return blocks


def build_report(runs: list[RunRecord] | None = None) -> str:
    """Render the full HTML report for the given runs (default: all runs)."""
    if runs is None:
        runs = list_run_records()

    enriched = []
    for run in runs:
        verdicts = _find_check_verdicts(run)
        enriched.append((run, verdicts, _run_status(run, verdicts)))

    n_total = len(enriched)
    n_contradicted = sum(1 for _, _, s in enriched if s == "contradicted")
    n_verified = sum(1 for _, _, s in enriched if s == "verified")
    n_unverified = sum(1 for _, _, s in enriched if s == "unverified")

    blocks = _session_blocks(enriched)
    n_sessions = sum(1 for kind, _, _ in blocks if kind == "session")

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    parts = [_HTML_HEAD.format(title=html_lib.escape("FleetProof — Run Report"))]
    parts.append("<h1>FleetProof — Run Report</h1>")
    session_meta = f" &nbsp; <b>Sessions:</b> {n_sessions}" if n_sessions else ""
    parts.append(
        f"<p class='meta'><b>Generated:</b> {html_lib.escape(generated)} &nbsp; "
        f"<b>Runs:</b> {n_total}{session_meta}</p>"
    )

    parts.append("<div class='cards'>")
    if n_sessions:
        parts.append(_stat_card("Sessions", str(n_sessions), "neutral"))
    parts.append(_stat_card("Runs", str(n_total), "neutral"))
    parts.append(_stat_card("Independently verified", str(n_verified), "ok"))
    parts.append(_stat_card("Claim contradicted", str(n_contradicted), "bad"))
    parts.append(_stat_card("Unverified", str(n_unverified), "warn"))
    parts.append("</div>")

    if n_contradicted:
        parts.append(
            "<p class='lead bad-text'>"
            f"{n_contradicted} run(s) reported done, but the independent checker "
            "found a blocking failure. Those are below, marked "
            "<span class='badge bad'>contradicted</span>.</p>"
        )
    elif n_total == 0:
        parts.append("<p class='lead'>No runs recorded yet.</p>")
    else:
        parts.append(
            "<p class='lead ok-text'>No contradicted claims in this window.</p>"
        )

    for kind, sid, items in blocks:
        if kind == "session":
            parts.append(_render_session(sid, items))
        else:
            parts.append(_render_run(*items[0]))

    parts.append(_HTML_FOOT)
    return "\n".join(parts)


def _render_session(session_id: str | None, items: list[_Enriched]) -> str:
    """Render one session as a container: a header with time range, then its runs."""
    starts = sorted(it[0].started_at for it in items if it[0].started_at)
    lo = starts[0][:19] if starts else "-"
    hi = starts[-1][:19] if starts else "-"
    n_contra = sum(1 for it in items if it[2] == "contradicted")
    n_verified = sum(1 for it in items if it[2] == "verified")
    if n_contra:
        status = "contradicted"
    elif n_verified:
        status = "verified"
    else:
        status = "unverified"

    parts = [f"<section class='session {status}'>"]
    parts.append(
        f"<h2 class='session-h'>Session <code>{html_lib.escape(str(session_id))}</code> "
        f"{_badge(status)}</h2>"
    )
    parts.append(
        "<p class='meta'>"
        f"<b>runs in session:</b> {len(items)} &nbsp; "
        f"<b>time range:</b> {html_lib.escape(lo)} → {html_lib.escape(hi)}"
        "</p>"
    )
    parts.append("<div class='session-body'>")
    for run, verdicts, run_status in items:
        parts.append(_render_run(run, verdicts, run_status))
    parts.append("</div>")
    parts.append("</section>")
    return "\n".join(parts)


def _stat_card(label: str, value: str, tone: str) -> str:
    return (
        f"<div class='card {tone}'><div class='card-val'>{html_lib.escape(value)}</div>"
        f"<div class='card-lbl'>{html_lib.escape(label)}</div></div>"
    )


def _badge(status: str) -> str:
    tone = {"contradicted": "bad", "verified": "ok", "unverified": "warn"}.get(status, "neutral")
    return f"<span class='badge {tone}'>{html_lib.escape(status)}</span>"


def _render_run(run: RunRecord, verdicts: list[dict[str, Any]], status: str) -> str:
    parts = [f"<section class='run {status}'>"]
    parts.append(
        f"<h2>{html_lib.escape(run.run_id)} {_badge(status)}</h2>"
    )
    parts.append(
        "<p class='meta'>"
        f"<b>root tool:</b> {html_lib.escape(run.root_tool or '-')} &nbsp; "
        f"<b>started:</b> {html_lib.escape((run.started_at or '-')[:19])} &nbsp; "
        f"<b>sub-invocations:</b> {len(run.sub_invocations)} &nbsp; "
        f"<b>failed sub-invocations:</b> {run.failed_count}"
        "</p>"
    )

    if not verdicts:
        parts.append(
            "<p class='note'>No independent checker verdict recorded for this run. "
            "The fleet's claim here has not been verified out-of-band.</p>"
        )
    for v in verdicts:
        parts.append(_render_verdict(v))

    if run.sub_invocations:
        parts.append(_render_sub_table(run))

    parts.append("</section>")
    return "\n".join(parts)


def _render_verdict(v: dict[str, Any]) -> str:
    summary = v.get("summary", {})
    verdict = v.get("verdict", "?")
    tone = "bad" if verdict == "fail" else "ok"
    parts = [
        f"<h3>Checker verdict: <span class='badge {tone}'>{html_lib.escape(verdict)}</span> "
        f"<span class='count'>({summary.get('passed', 0)}/{summary.get('total', 0)} passed, "
        f"{summary.get('blocking_failed', 0)} blocking)</span></h3>"
    ]
    parts.append("<table class='checks'><thead><tr>")
    parts.append("<th>check</th><th>expected</th><th>result</th><th>detail</th><th>blocking</th>")
    parts.append("</tr></thead><tbody>")
    for c in v.get("checks", []):
        passed = c.get("passed")
        blocking = c.get("blocking")
        row_cls = "row-pass" if passed else ("row-fail" if blocking else "row-warn")
        result = "pass" if passed else ("FAIL" if blocking else "warn")
        parts.append(f"<tr class='{row_cls}'>")
        parts.append(f"<td><code>{html_lib.escape(str(c.get('id', '?')))}</code></td>")
        parts.append(f"<td>{html_lib.escape(str(c.get('expectation', '-')))}</td>")
        parts.append(f"<td>{html_lib.escape(result)}</td>")
        parts.append(f"<td>{html_lib.escape(str(c.get('detail', '-')))}</td>")
        parts.append(f"<td>{'yes' if blocking else 'no'}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _render_sub_table(run: RunRecord) -> str:
    parts = ["<h3>Recorded invocations</h3>"]
    parts.append("<table class='rows'><thead><tr>")
    parts.append("<th>tool</th><th>subcmd</th><th>started</th><th>exit</th>"
                 "<th>duration</th><th>evidence</th>")
    parts.append("</tr></thead><tbody>")
    for s in run.sub_invocations:
        if s.exit_code is None:
            outcome = "?"
        elif s.exit_code == 0 and not s.exception_type:
            outcome = "0"
        else:
            outcome = f"{s.exit_code}{' / ' + s.exception_type if s.exception_type else ''}"
        dur = f"{s.duration_ms:.1f}ms" if s.duration_ms is not None else "-"
        parts.append("<tr>")
        parts.append(f"<td>{html_lib.escape(s.tool)}</td>")
        parts.append(f"<td>{html_lib.escape(s.subcmd)}</td>")
        parts.append(f"<td>{html_lib.escape((s.started_at or '-')[:19])}</td>")
        parts.append(f"<td>{html_lib.escape(outcome)}</td>")
        parts.append(f"<td>{html_lib.escape(dur)}</td>")
        parts.append(f"<td><code>{html_lib.escape(s.record_dir.name)}</code></td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def write_report(output: Path, runs: list[RunRecord] | None = None) -> Path:
    """Render and write the report to ``output``; returns the path written."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_report(runs), encoding="utf-8")
    return output


# === HTML chrome (inline; no external deps) ===

_HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                       Helvetica, Arial, sans-serif;
          margin: 2em auto; max-width: 1100px; padding: 0 1em;
          color: #222; background: #fdfdfd; line-height: 1.45; }}
  h1, h2, h3 {{ color: #1a1a1a; }}
  h1 {{ border-bottom: 2px solid #888; padding-bottom: 0.3em; }}
  h2 {{ margin-top: 0.2em; font-size: 1.1em; }}
  h3 {{ margin-top: 1em; color: #555; font-size: 1em; }}
  .meta {{ color: #666; font-size: 0.9em; }}
  .count {{ color: #888; font-weight: normal; font-size: 0.9em; }}
  .lead {{ font-size: 1.05em; margin: 1em 0; }}
  .note {{ color: #777; font-style: italic; }}
  .bad-text {{ color: #a11; }}
  .ok-text {{ color: #171; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 1em; margin: 1.2em 0; }}
  .card {{ flex: 1 1 140px; border: 1px solid #ddd; border-radius: 6px;
           padding: 0.8em 1em; background: #fff; }}
  .card-val {{ font-size: 1.8em; font-weight: 700; }}
  .card-lbl {{ color: #666; font-size: 0.85em; }}
  .card.ok .card-val {{ color: #171; }}
  .card.bad .card-val {{ color: #a11; }}
  .card.warn .card-val {{ color: #a60; }}
  .badge {{ display: inline-block; font-size: 0.7em; font-weight: 700;
            text-transform: uppercase; letter-spacing: 0.04em;
            padding: 2px 7px; border-radius: 10px; vertical-align: middle; }}
  .badge.ok {{ background: #e3f5e3; color: #171; }}
  .badge.bad {{ background: #fbe3e3; color: #a11; }}
  .badge.warn {{ background: #fbf1dd; color: #a60; }}
  .badge.neutral {{ background: #eee; color: #555; }}
  section.session {{ margin-top: 1.8em; padding: 1em 1.2em 1.2em;
                     border-radius: 8px; border: 1px solid #d3d3d3;
                     background: #f4f5f7; }}
  section.session > .session-h {{ margin-top: 0; font-size: 1.15em; }}
  section.session.contradicted {{ border-left: 6px solid #c33; }}
  section.session.verified {{ border-left: 6px solid #3a3; }}
  section.session.unverified {{ border-left: 6px solid #d9a441; }}
  .session-body section.run {{ margin-top: 1em; }}
  section.run {{ margin-top: 1.6em; padding: 1em 1.2em; border-radius: 6px;
                 border: 1px solid #e2e2e2; background: #fff; }}
  section.run.contradicted {{ border-left: 5px solid #c33; }}
  section.run.verified {{ border-left: 5px solid #3a3; }}
  section.run.unverified {{ border-left: 5px solid #d9a441; }}
  table {{ border-collapse: collapse; margin: 0.5em 0 1em 0;
           font-size: 0.9em; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 4px 8px; text-align: left;
            vertical-align: top; }}
  th {{ background: #f0f0f0; font-weight: 600; }}
  tr.row-fail td {{ background: #fdecec; }}
  tr.row-warn td {{ background: #fdf6e8; }}
  code {{ font-family: ui-monospace, "SF Mono", Consolas, monospace;
          background: #f0f0f0; padding: 1px 4px; border-radius: 3px; }}
  table.rows td {{ font-family: ui-monospace, "SF Mono", Consolas, monospace;
                   overflow-wrap: anywhere; }}
</style>
</head>
<body>
"""

_HTML_FOOT = """
</body>
</html>
"""
