"""Self-contained human-readable quality report rendering."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from ..utils import atomic_write_text


def html_document(report: dict[str, Any]) -> str:
  """Render the deterministic self-contained HTML representation of a report."""
  rows: list[str] = []
  for track in report.get("tracks", []):
    transcription = track.get("transcription") or {}
    signal = track.get("signal") or {}
    rows.append(
        "<tr>"
        f"<td>{track.get('track_number')}</td>"
        f"<td>{html.escape(str(track.get('title', '')))}</td>"
        f"<td>{float(track.get('duration_seconds', 0)) / 60:.1f} min</td>"
        f"<td>{_rate(transcription.get('word_error_rate'))}</td>"
        f"<td>{_rate(transcription.get('character_error_rate'))}</td>"
        f"<td>{_number(signal.get('integrated_lufs'))}</td>"
        f"<td>{len(track.get('warnings', []))}</td>"
        "</tr>"
    )
  failures = "".join(
      f"<li>{html.escape(str(item))}</li>" for item in report.get("failures", [])
  ) or "<li>None</li>"
  warnings = "".join(
      f"<li>{html.escape(str(item))}</li>" for item in report.get("warnings", [])
  ) or "<li>None</li>"
  source = html.escape(json.dumps(report, ensure_ascii=False, indent=2))
  document = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>ebook-tts QA — {html.escape(str(report.get("book", {}).get("title", "")))}</title>
<style>
body {{ font: 16px/1.45 system-ui, sans-serif; max-width: 1100px; margin: 2rem auto; padding: 0 1rem; }}
table {{ width: 100%; border-collapse: collapse; }} th, td {{ border: 1px solid #bbb; padding: .45rem; text-align: left; }}
.pass {{ color: #087f23; }} .warn {{ color: #9a6700; }} .fail {{ color: #b42318; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #f4f4f4; padding: 1rem; }}
</style></head><body>
<h1>Quality report: {html.escape(str(report.get("book", {}).get("title", "")))}</h1>
<p>Status: <strong class="{html.escape(str(report.get('status', 'fail')))}">{html.escape(str(report.get('status', '')).upper())}</strong></p>
<p>Run <code>{html.escape(str(report.get('run_id', '')))}</code>; QA <code>{html.escape(str(report.get('qa_id', '')))}</code></p>
<h2>Failures</h2><ul>{failures}</ul>
<h2>Warnings</h2><ul>{warnings}</ul>
<h2>Tracks</h2><table><thead><tr><th>#</th><th>Title</th><th>Duration</th><th>WER</th><th>CER</th><th>LUFS</th><th>Warnings</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<details><summary>Complete JSON report</summary><pre>{source}</pre></details>
</body></html>'''
  return document


def render_html(report: dict[str, Any], path: Path) -> None:
  """Atomically publish a report's deterministic HTML representation."""
  atomic_write_text(path, html_document(report), mode=0o644)


def _rate(value: object) -> str:
  return "—" if value is None else f"{float(value) * 100:.2f}%"


def _number(value: object) -> str:
  return "—" if value is None else f"{float(value):.2f}"
