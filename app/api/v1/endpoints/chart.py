"""
Chart visualization endpoints.

  GET /api/v1/chart/{query_id}       — JSON chart data for a query
  GET /api/v1/chart/{query_id}/view  — HTML page rendering the chart via Highcharts

The view page embeds its data server-side rather than fetching it back from
the API. That removes a hardcoded http://127.0.0.1:8000 call (which could
never have worked off a developer machine) and means the page does not need
to carry a credential in JavaScript in order to load its own data.
"""
from __future__ import annotations

import html
import json
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

import services.db_service as db_svc
from api.deps import require_auth, require_auth_or_token

router = APIRouter(prefix="/chart", tags=["Chart"])


def _normalize_charts(raw) -> dict | None:
    """
    Handle both storage shapes:
      • legacy rows: bare list of chart specs
      • current rows: {"charts": [...], "rationale": "..."}
    Returns the dict shape, or None if there is nothing to render.
    """
    if not raw:
        return None
    if isinstance(raw, list):
        return {"charts": raw, "rationale": ""} if raw else None
    if isinstance(raw, dict):
        return raw if raw.get("charts") else None
    return None


def _render(template: str, values: dict[str, str]) -> str:
    """
    Substitute __PLACEHOLDER__ tokens in a single pass.

    Sequential str.replace() calls would let an earlier substitution's own
    content be reinterpreted as a later placeholder — e.g. a query whose text
    literally contains "__CHART_DATA__".
    """
    pattern = re.compile("|".join(re.escape(k) for k in values))
    return pattern.sub(lambda m: values[m.group(0)], template)


@router.get("/{query_id}", dependencies=[Depends(require_auth)])
def get_chart_data(query_id: str):
    """Return the raw chart JSON stored for this query."""
    row = db_svc.get_charts_by_query_id(query_id)
    if not row:
        raise HTTPException(status_code=404, detail="Query not found")
    payload = _normalize_charts(row.get("charts"))
    if payload is None:
        raise HTTPException(status_code=404, detail="No chart data for this query")
    return payload


# Opened directly in a browser tab or iframe, neither of which can set an
# Authorization header — accepts the credential as ?token= as well.
@router.get(
    "/{query_id}/view",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth_or_token)],
)
def view_chart(query_id: str):
    """Serve a self-contained HTML page that renders the chart with Highcharts."""
    row = db_svc.get_charts_by_query_id(query_id)
    if not row:
        raise HTTPException(status_code=404, detail="Query not found")
    payload = _normalize_charts(row.get("charts"))
    if payload is None:
        raise HTTPException(status_code=404, detail="No chart data for this query")

    title = (row.get("original_query") or "Chart Preview")[:120]

    # The query text and the chart payload are user- and LLM-authored. Escape
    # both for their destination context: HTML text for the title, and
    # <-neutralised JSON so the payload cannot close its own <script> block.
    return _render(
        _CHART_HTML_TEMPLATE,
        {
            "__TITLE__": html.escape(title),
            "__QUERY_ID__": html.escape(query_id),
            "__CHART_DATA__": json.dumps(payload, default=str).replace("<", "\\u003c"),
        },
    )


_CHART_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>__TITLE__</title>
  <script src="https://code.highcharts.com/highcharts.js"></script>
  <script src="https://code.highcharts.com/modules/exporting.js"></script>
  <script src="https://code.highcharts.com/modules/export-data.js"></script>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #f5f5f5; padding: 24px; color: #333;
    }
    .header { margin-bottom: 24px; }
    .header h1 { font-size: 20px; font-weight: 600; color: #1a1a1a; }
    .header p { font-size: 13px; color: #666; margin-top: 4px; }
    .charts-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(540px, 1fr));
      gap: 20px;
    }
    .chart-card {
      background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);
      padding: 16px; min-height: 400px;
    }
    .rationale {
      margin-top: 20px; padding: 12px 16px; background: #f0f4ff;
      border-left: 3px solid #4a7cf7; border-radius: 4px; font-size: 13px; color: #555;
    }
    .error { color: #c0392b; text-align: center; padding: 60px 20px; }
  </style>
</head>
<body>
  <div class="header">
    <h1>__TITLE__</h1>
    <p>Query ID: <code>__QUERY_ID__</code></p>
  </div>
  <div class="charts-grid" id="charts-grid"></div>
  <div id="rationale"></div>

  <script id="chart-data" type="application/json">__CHART_DATA__</script>
  <script>
    (function () {
      const grid = document.getElementById('charts-grid');
      let data;

      try {
        data = JSON.parse(document.getElementById('chart-data').textContent);
      } catch (err) {
        grid.innerHTML = '<p class="error">Failed to load charts.</p>';
        return;
      }

      if (!data.charts || data.charts.length === 0) {
        grid.innerHTML = '<p class="error">No chart data available.</p>';
        return;
      }

      data.charts.forEach((spec, idx) => {
        const card = document.createElement('div');
        card.className = 'chart-card';
        card.id = 'chart-' + idx;
        grid.appendChild(card);

        Highcharts.chart(card.id, {
          chart:       { type: spec.type || 'column' },
          title:       { text: spec.title || '' },
          subtitle:    { text: spec.subtitle || '' },
          xAxis:       spec.xAxis || {},
          yAxis:       spec.yAxis || {},
          series:      spec.series || [],
          legend:      spec.legend || { enabled: true },
          tooltip:     spec.tooltip || {},
          plotOptions: spec.plotOptions || {},
          credits:     { enabled: false },
        });
      });

      if (data.rationale) {
        // textContent, not innerHTML — the rationale is LLM-generated text.
        const box = document.createElement('div');
        box.className = 'rationale';
        const label = document.createElement('strong');
        label.textContent = 'Rationale: ';
        box.appendChild(label);
        box.appendChild(document.createTextNode(data.rationale));
        document.getElementById('rationale').appendChild(box);
      }
    })();
  </script>
</body>
</html>
"""
