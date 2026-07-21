from __future__ import annotations

import html
from collections import Counter
from typing import Any, Mapping

from .catalog_stats import CatalogReport


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _format_int(value: object) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def _distribution(values: Mapping[str, int], *, empty: str = "нет") -> str:
    if not values:
        return f'<span class="empty">{_escape(empty)}</span>'
    maximum = max(values.values(), default=1)
    parts = []
    for label, value in values.items():
        width = max(4, round(value / maximum * 100)) if maximum else 0
        parts.append(
            '<span class="dist-item">'
            f'<span class="dist-label">{_escape(label)}</span>'
            f'<span class="dist-bar"><i style="width:{width}%"></i></span>'
            f'<strong>{_format_int(value)}</strong>'
            "</span>"
        )
    return "".join(parts)


def _tokens(values: Mapping[str, int], *, kind: str = "neutral") -> str:
    if not values:
        return '<span class="empty">нет</span>'
    return "".join(
        f'<span class="token token-{_escape(kind)}"><b>{_escape(label)}</b>'
        f'<small>{_format_int(value)}</small></span>'
        for label, value in values.items()
    )


def _value_cards(values: list[Mapping[str, Any]]) -> str:
    if not values:
        return '<p class="empty zero-note">Значения не обнаружены</p>'
    cards = []
    for index, value in enumerate(values, start=1):
        raw_examples = value.get("raw_examples") or []
        templates = []
        if value.get("pattern_template"):
            templates.append(str(value["pattern_template"]))
        raw_line = ""
        if raw_examples or templates:
            fragments = []
            if raw_examples:
                fragments.append("SQL: " + ", ".join(str(item) for item in raw_examples))
            if templates:
                fragments.append("шаблон: " + ", ".join(templates))
            raw_line = f'<span class="value-raw">{_escape(" · ".join(fragments))}</span>'
        features = [
            name for name, enabled in (value.get("regex_features") or {}).items() if enabled
        ]
        feature_line = (
            f'<span class="feature-line">{_escape(", ".join(features))}</span>'
            if features
            else ""
        )
        cards.append(
            '<div class="value-card">'
            f'<span class="value-rank">{index:02d}</span>'
            '<span class="value-main">'
            f'<code>{_escape(value.get("value", ""))}</code>'
            f'<span class="value-family">{_escape(value.get("pattern_family", ""))}</span>'
            f'{raw_line}{feature_line}'
            "</span>"
            '<span class="value-count">'
            f'<strong>{_format_int(value.get("source_row_count", 0))}</strong>'
            '<small>взвеш. использований</small>'
            f'<span>{_format_int(value.get("distinct_query_count", 0))} запросов</span>'
            "</span>"
            "</div>"
        )
    return "".join(cards)


def _column_row(column: Mapping[str, Any]) -> str:
    active = column.get("usage_status") == "active"
    contexts = column.get("context_counts") or {}
    patterns = column.get("pattern_family_counts") or {}
    operators = column.get("operator_counts") or {}
    values = column.get("top_values") or []
    search_bits = [
        column.get("qualified_name", ""),
        *contexts.keys(),
        *patterns.keys(),
        *operators.keys(),
    ]
    for value in values:
        search_bits.extend(
            [
                value.get("value", ""),
                value.get("pattern_template", "") or "",
                *(value.get("raw_examples") or []),
            ]
        )
    search_data = " ".join(str(bit) for bit in search_bits).casefold()
    context_data = " ".join(contexts)
    pattern_data = " ".join(patterns)
    status_label = "есть связанные значения" if active else "связанные значения не найдены"
    detail_open = " open" if active and int(column.get("source_row_count", 0)) >= 20 else ""
    return f"""
      <div class="column-row {'is-active' if active else 'is-unused'}"
           data-active="{'active' if active else 'unused'}"
           data-contexts="{_escape(context_data)}"
           data-patterns="{_escape(pattern_data)}"
           data-search="{_escape(search_data)}">
        <details{detail_open}>
          <summary class="column-summary">
            <span class="column-status" title="{_escape(status_label)}"></span>
            <span class="column-name">
              <code>{_escape(column.get('qualified_name', ''))}</code>
              <small>{_escape(status_label)}</small>
            </span>
            <span class="column-kpi"><strong>{_format_int(column.get('source_row_count', 0))}</strong><small>использований</small></span>
            <span class="column-kpi"><strong>{_format_int(column.get('distinct_query_count', 0))}</strong><small>запросов</small></span>
            <span class="column-kpi"><strong>{_format_int(len(values))}</strong><small>значений в топе</small></span>
            <span class="column-chevron" aria-hidden="true">⌄</span>
          </summary>
          <div class="column-detail">
            <div class="column-distributions">
              <section><h4>Секции SQL</h4><div class="distribution">{_distribution(contexts)}</div></section>
              <section><h4>Операторы</h4><div class="token-list">{_tokens(operators, kind='operator')}</div></section>
              <section><h4>Типы значений и масок</h4><div class="token-list">{_tokens(patterns, kind='pattern')}</div></section>
            </div>
            <section class="values-section">
              <div class="minor-heading"><h4>Популярные значения, маски и regex</h4><span>до {len(values)} позиций</span></div>
              <div class="value-list">{_value_cards(values)}</div>
            </section>
            <p class="examples"><b>Примеры запросов:</b> {_escape(', '.join(column.get('example_query_ids') or []) or 'нет')}</p>
          </div>
        </details>
      </div>"""


def _table_card(table: Mapping[str, Any], *, index: int) -> str:
    columns = table.get("columns") or []
    open_attribute = " open" if index < 3 else ""
    return f"""
    <article class="table-card" data-table="{_escape(table.get('qualified_name', ''))}">
      <details class="table-details"{open_attribute}>
        <summary class="table-summary">
          <span class="table-index">{index + 1:02d}</span>
          <span class="table-name"><small>физическая таблица</small><code>{_escape(table.get('qualified_name', ''))}</code></span>
          <span class="table-kpi"><strong>{_format_int(table.get('active_column_count', 0))}</strong><small>активных / {_format_int(table.get('column_count', 0))}</small></span>
          <span class="table-kpi"><strong>{_format_int(table.get('source_row_count', 0))}</strong><small>использований</small></span>
          <span class="table-kpi"><strong>{_format_int(table.get('distinct_query_count', 0))}</strong><small>запросов</small></span>
          <span class="table-chevron" aria-hidden="true">⌄</span>
        </summary>
        <div class="table-body">
          <div class="table-profile">
            <section><h4>Где используется</h4><div class="distribution">{_distribution(table.get('context_counts') or {})}</div></section>
            <section><h4>Чем сравнивается</h4><div class="token-list">{_tokens(table.get('operator_counts') or {}, kind='operator')}</div></section>
            <section><h4>Форматы</h4><div class="token-list">{_tokens(table.get('pattern_family_counts') or {}, kind='pattern')}</div></section>
          </div>
          <div class="column-header"><span>Колонка</span><span>Использования</span><span>Запросы</span><span>Топ</span></div>
          <div class="column-list">{''.join(_column_row(column) for column in columns)}</div>
        </div>
      </details>
    </article>"""


def _quality_rows(quality: Mapping[str, Any]) -> str:
    groups = quality.get("groups") or []
    if not groups:
        return '<p class="quality-clean">Все условия однозначно связаны с физическими колонками.</p>'
    rows = []
    for group in groups:
        columns = group.get("base_columns") or []
        rows.append(
            '<div class="quality-row">'
            f'<span class="quality-status">{_escape(group.get("lineage_status", ""))}</span>'
            '<span class="quality-main">'
            f'<code>{_escape(", ".join(columns) or "без кандидатов")}</code>'
            f'<span>{_escape(group.get("lineage_reason") or "причина не указана")}</span>'
            f'<small>{_escape(group.get("clause_context", ""))} · {_escape(group.get("operator_or_function", ""))} · '
            f'{_escape(", ".join(group.get("values") or []))}</small>'
            "</span>"
            f'<strong>{_format_int(group.get("source_row_count", 0))}</strong>'
            "</div>"
        )
    return "".join(rows)


def render_catalog_html(report: CatalogReport) -> str:
    payload = report.to_dict()
    metadata = payload["metadata"]
    summary = payload["summary"]
    quality = payload["quality"]
    tables = sorted(
        payload["tables"],
        key=lambda table: (
            -int(table.get("source_row_count", 0)),
            str(table.get("qualified_name", "")),
        ),
    )
    contexts: Counter[str] = Counter()
    patterns: Counter[str] = Counter()
    for table in tables:
        contexts.update(table.get("context_counts") or {})
        patterns.update(table.get("pattern_family_counts") or {})
    resolution_rate = float(summary.get("lineage_resolution_rate", 0)) * 100
    context_options = "".join(
        f'<option value="{_escape(name)}">{_escape(name)} · {_format_int(count)}</option>'
        for name, count in contexts.most_common()
    )
    pattern_options = "".join(
        f'<option value="{_escape(name)}">{_escape(name)} · {_format_int(count)}</option>'
        for name, count in patterns.most_common()
    )
    cards = "".join(_table_card(table, index=index) for index, table in enumerate(tables))
    status_tags = "".join(
        f'<span><b>{_escape(status)}</b>{_format_int(count)}</span>'
        for status, count in (summary.get("lineage_status_counts") or {}).items()
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Статистика использования SQL · дата-каталог</title>
  <style>
    :root {{
      --paper:#f1ecdf; --paper-light:#fbf8f0; --paper-deep:#e4dbca;
      --ink:#1c201d; --muted:#6c6a61; --line:#c7bcaa;
      --brick:#ad392a; --brick-dark:#78251c; --olive:#5f6c50;
      --gold:#bd812c; --blue:#3c6571; --shadow:0 16px 38px rgba(53,43,28,.08);
      --serif:"Iowan Old Style","Palatino Linotype",Georgia,serif;
      --sans:"Avenir Next",Avenir,"Trebuchet MS",sans-serif;
      --mono:"SFMono-Regular",Menlo,Monaco,Consolas,monospace;
    }}
    * {{ box-sizing:border-box; }}
    html {{ scroll-behavior:smooth; }}
    body {{ margin:0; color:var(--ink); background:linear-gradient(rgba(45,39,29,.035) 1px,transparent 1px),var(--paper); background-size:100% 29px; font:15px/1.5 var(--sans); }}
    code {{ font-family:var(--mono); overflow-wrap:anywhere; }}
    button,input,select {{ font:inherit; }}
    button {{ cursor:pointer; }}
    .masthead {{ position:relative; overflow:hidden; color:#f6efe2; background:var(--ink); border-bottom:7px solid var(--brick); }}
    .masthead::after {{ content:"CATALOG"; position:absolute; right:-2vw; bottom:-50px; color:rgba(255,255,255,.04); font:900 clamp(90px,18vw,250px)/1 var(--sans); letter-spacing:-.08em; }}
    .masthead-inner,.page,.toolbar-inner,.footer-inner {{ width:min(1540px,calc(100% - 42px)); margin:0 auto; }}
    .masthead-inner {{ position:relative; z-index:1; padding:58px 0 46px; }}
    .eyebrow {{ display:flex; align-items:center; gap:12px; margin:0 0 17px; color:#d9cbb7; font:800 11px/1 var(--sans); letter-spacing:.18em; text-transform:uppercase; }}
    .eyebrow::before {{ content:""; width:48px; border-top:3px solid var(--brick); }}
    h1 {{ max-width:1050px; margin:0; font:600 clamp(40px,6.5vw,88px)/.95 var(--serif); letter-spacing:-.045em; }}
    .dek {{ max-width:900px; margin:23px 0 0; color:#d7cec0; font-size:18px; }}
    .source-line {{ display:flex; flex-wrap:wrap; gap:10px 28px; margin-top:29px; color:#aaa99f; font:12px/1.5 var(--mono); }}
    .page {{ padding:38px 0 78px; }}
    .summary-grid {{ display:grid; grid-template-columns:1.2fr repeat(4,1fr); background:rgba(251,248,240,.9); border:1px solid var(--line); box-shadow:var(--shadow); }}
    .summary-lead {{ min-height:154px; padding:25px; border-right:1px solid var(--line); }}
    .summary-lead small,.summary-card small {{ display:block; color:var(--muted); font-size:10px; letter-spacing:.12em; text-transform:uppercase; }}
    .summary-lead strong {{ display:block; margin:8px 0 12px; color:var(--brick); font:600 42px/1 var(--serif); }}
    .resolution-track {{ height:8px; overflow:hidden; background:var(--paper-deep); }}
    .resolution-track i {{ display:block; height:100%; background:var(--olive); }}
    .status-tags {{ display:flex; flex-wrap:wrap; gap:6px 14px; margin-top:12px; }}
    .status-tags span {{ color:var(--muted); font-size:10px; }} .status-tags b {{ margin-right:5px; color:var(--ink); }}
    .summary-card {{ min-height:154px; padding:25px 20px; border-right:1px solid var(--line); }}
    .summary-card:last-child {{ border-right:0; }}
    .summary-card strong {{ display:block; margin-top:17px; font:600 43px/1 var(--serif); letter-spacing:-.04em; }}
    .summary-card span {{ display:block; margin-top:10px; color:var(--muted); font-size:12px; }}
    .method-note {{ display:grid; grid-template-columns:1fr auto; gap:24px; align-items:center; margin-top:20px; padding:22px 26px; background:rgba(251,248,240,.75); border:1px solid var(--line); border-left:6px solid var(--brick); }}
    .method-note h2 {{ margin:0 0 6px; font:600 24px/1.1 var(--serif); }} .method-note p {{ margin:0; color:var(--muted); }}
    .method-note code {{ padding:9px 12px; color:#f7eee2; background:var(--ink); font-size:11px; }}
    .toolbar {{ position:sticky; top:0; z-index:20; margin-top:30px; color:white; background:rgba(28,32,29,.97); border-bottom:3px solid var(--brick); backdrop-filter:blur(12px); }}
    .toolbar-inner {{ display:grid; grid-template-columns:minmax(280px,1fr) 190px 190px 210px auto; gap:9px; align-items:center; padding:11px 0; }}
    .toolbar input,.toolbar select {{ width:100%; min-height:43px; padding:8px 11px; color:#f8f1e6; background:#292d29; border:1px solid #555a52; border-radius:0; outline:none; }}
    .toolbar input:focus,.toolbar select:focus {{ border-color:#e4a08c; box-shadow:0 0 0 2px rgba(228,160,140,.18); }}
    #visible-column-count {{ min-width:104px; color:#c9c4ba; text-align:right; font:11px/1.3 var(--mono); }}
    .catalog-head {{ display:flex; justify-content:space-between; gap:20px; align-items:end; margin:34px 0 16px; }}
    .catalog-head h2,.quality-section h2 {{ margin:0; font:600 38px/1 var(--serif); letter-spacing:-.03em; }}
    .catalog-head p {{ max-width:560px; margin:0; color:var(--muted); text-align:right; }}
    .table-card {{ margin:13px 0; background:rgba(251,248,240,.9); border:1px solid var(--line); border-left:6px solid var(--olive); box-shadow:0 6px 20px rgba(54,42,24,.05); }}
    .table-card[hidden] {{ display:none; }}
    summary {{ list-style:none; }} summary::-webkit-details-marker {{ display:none; }}
    .table-summary {{ display:grid; grid-template-columns:48px minmax(260px,1fr) repeat(3,125px) 28px; gap:14px; align-items:center; min-height:98px; padding:15px 19px; cursor:pointer; }}
    .table-summary:hover,.column-summary:hover {{ background:rgba(228,219,202,.38); }}
    .table-index {{ color:var(--brick); font:700 17px/1 var(--mono); }}
    .table-name small {{ display:block; margin-bottom:7px; color:var(--muted); font-size:9px; letter-spacing:.14em; text-transform:uppercase; }}
    .table-name code {{ font-size:18px; font-weight:700; }}
    .table-kpi {{ text-align:right; border-left:1px solid var(--line); padding-left:14px; }} .table-kpi strong {{ display:block; font:600 28px/1 var(--serif); }} .table-kpi small {{ color:var(--muted); font-size:9px; text-transform:uppercase; }}
    .table-chevron,.column-chevron {{ font-size:24px; transition:transform .2s; }} details[open]>.table-summary .table-chevron,details[open]>.column-summary .column-chevron {{ transform:rotate(180deg); }}
    .table-body {{ border-top:1px solid var(--line); }}
    .table-profile {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:24px; padding:20px 24px; background:rgba(228,219,202,.22); border-bottom:1px solid var(--line); }}
    h4 {{ margin:0 0 11px; font-size:10px; letter-spacing:.13em; text-transform:uppercase; }}
    .distribution {{ display:flex; flex-direction:column; gap:6px; }}
    .dist-item {{ display:grid; grid-template-columns:92px 1fr 35px; gap:8px; align-items:center; font-size:10px; }} .dist-label {{ overflow:hidden; text-overflow:ellipsis; }}
    .dist-bar {{ height:5px; background:var(--paper-deep); }} .dist-bar i {{ display:block; height:100%; background:var(--brick); }}
    .token-list {{ display:flex; flex-wrap:wrap; gap:6px; }} .token {{ display:inline-flex; gap:8px; align-items:center; padding:5px 8px; border:1px solid var(--line); background:var(--paper-light); font:10px/1 var(--mono); }} .token small {{ color:var(--muted); }} .token-pattern {{ border-color:#a9b69c; }} .token-operator {{ border-color:#bca688; }}
    .column-header {{ display:grid; grid-template-columns:minmax(300px,1fr) 140px 140px 100px; gap:12px; padding:10px 73px 10px 51px; color:var(--muted); background:var(--ink); font-size:9px; letter-spacing:.12em; text-transform:uppercase; }}
    .column-header span:not(:first-child) {{ text-align:right; }}
    .column-row {{ border-bottom:1px solid var(--line); }} .column-row:last-child {{ border-bottom:0; }} .column-row[hidden] {{ display:none; }}
    .column-summary {{ display:grid; grid-template-columns:18px minmax(260px,1fr) 140px 140px 100px 24px; gap:12px; align-items:center; min-height:72px; padding:10px 18px; cursor:pointer; }}
    .column-status {{ width:8px; height:8px; background:var(--olive); border-radius:50%; box-shadow:0 0 0 4px rgba(95,108,80,.12); }} .is-unused .column-status {{ background:#aaa295; box-shadow:none; }}
    .column-name code {{ display:block; font-size:13px; }} .column-name small {{ display:block; margin-top:3px; color:var(--muted); font-size:9px; }} .is-unused .column-name code {{ color:#77746c; }}
    .column-kpi {{ text-align:right; }} .column-kpi strong {{ display:block; font:600 22px/1 var(--serif); }} .column-kpi small {{ color:var(--muted); font-size:8px; text-transform:uppercase; }}
    .column-detail {{ padding:21px 24px 25px 48px; background:#f8f4ea; border-top:1px dashed var(--line); }}
    .column-distributions {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:26px; }}
    .values-section {{ margin-top:22px; }} .minor-heading {{ display:flex; justify-content:space-between; gap:12px; align-items:center; margin-bottom:8px; }} .minor-heading h4 {{ margin:0; }} .minor-heading span {{ color:var(--muted); font-size:9px; }}
    .value-list {{ border-top:1px solid var(--line); }}
    .value-card {{ display:grid; grid-template-columns:42px minmax(220px,1fr) 150px; gap:12px; align-items:center; padding:11px 6px; border-bottom:1px solid var(--line); }}
    .value-rank {{ color:var(--brick); font:700 11px/1 var(--mono); }} .value-main code {{ display:block; font-size:12px; }} .value-family {{ display:inline-block; margin-top:5px; padding:2px 6px; color:var(--brick-dark); background:#ead8cb; font:9px/1.3 var(--mono); }}
    .value-raw,.feature-line {{ display:block; margin-top:5px; color:var(--muted); font:9px/1.35 var(--mono); }} .feature-line {{ color:var(--blue); }}
    .value-count {{ text-align:right; }} .value-count strong {{ display:block; font:600 21px/1 var(--serif); }} .value-count small,.value-count span {{ display:block; color:var(--muted); font-size:8px; }}
    .examples {{ margin:16px 0 0; color:var(--muted); font:10px/1.5 var(--mono); }} .empty {{ color:var(--muted); font-style:italic; font-size:10px; }} .zero-note {{ margin:10px 0; }}
    .quality-section {{ margin-top:44px; padding:28px; background:rgba(251,248,240,.9); border:1px solid var(--line); border-top:6px solid var(--brick); box-shadow:var(--shadow); }}
    .quality-intro {{ max-width:850px; color:var(--muted); }} .quality-list {{ margin-top:18px; border-top:1px solid var(--line); }}
    .quality-row {{ display:grid; grid-template-columns:110px minmax(260px,1fr) 70px; gap:16px; align-items:center; padding:13px 5px; border-bottom:1px solid var(--line); }}
    .quality-status {{ padding:5px 7px; color:#fff; background:var(--brick); font:9px/1 var(--mono); text-align:center; text-transform:uppercase; }} .quality-main code,.quality-main span,.quality-main small {{ display:block; }} .quality-main code {{ font-size:11px; }} .quality-main span {{ color:var(--muted); font-size:10px; }} .quality-main small {{ margin-top:3px; color:var(--blue); }} .quality-row>strong {{ text-align:right; font:600 24px/1 var(--serif); }}
    .quality-clean {{ padding:18px; color:var(--olive); background:#edf0e8; }}
    footer {{ color:#c9c1b5; background:var(--ink); }} .footer-inner {{ display:flex; justify-content:space-between; gap:20px; padding:24px 0; font:10px/1.5 var(--mono); }}
    @media (max-width:1080px) {{
      .summary-grid {{ grid-template-columns:repeat(2,1fr); }} .summary-lead {{ grid-column:span 2; border-right:0; border-bottom:1px solid var(--line); }} .summary-card:nth-child(odd) {{ border-right:0; }}
      .toolbar-inner {{ grid-template-columns:1fr 1fr 1fr; }} .toolbar input {{ grid-column:span 2; }}
      .table-summary {{ grid-template-columns:40px minmax(220px,1fr) 105px 105px 26px; }} .table-summary .table-kpi:nth-of-type(5) {{ display:none; }}
      .column-header {{ display:none; }} .column-summary {{ grid-template-columns:18px minmax(220px,1fr) 105px 105px 24px; }} .column-summary .column-kpi:nth-of-type(5) {{ display:none; }}
    }}
    @media (max-width:760px) {{
      .masthead-inner,.page,.toolbar-inner,.footer-inner {{ width:min(100% - 24px,1540px); }} .masthead-inner {{ padding:38px 0 32px; }} .dek {{ font-size:15px; }}
      .summary-grid {{ display:block; }} .summary-lead,.summary-card {{ min-height:0; border:0; border-bottom:1px solid var(--line); }} .summary-card strong {{ margin-top:8px; }}
      .method-note {{ display:block; }} .method-note code {{ display:block; margin-top:14px; overflow:auto; }}
      .toolbar {{ position:relative; }} .toolbar-inner {{ display:grid; grid-template-columns:1fr; }} .toolbar input {{ grid-column:auto; }} #visible-column-count {{ text-align:left; }}
      .catalog-head {{ display:block; }} .catalog-head p {{ margin-top:10px; text-align:left; }}
      .table-summary {{ grid-template-columns:34px 1fr 24px; min-height:82px; }} .table-summary .table-kpi {{ display:none; }}
      .table-profile,.column-distributions {{ grid-template-columns:1fr; }}
      .column-summary {{ grid-template-columns:14px 1fr 24px; }} .column-summary .column-kpi {{ display:none; }}
      .column-detail {{ padding:18px 15px; }} .value-card {{ grid-template-columns:30px 1fr; }} .value-count {{ grid-column:2; text-align:left; }}
      .quality-row {{ grid-template-columns:1fr; }} .quality-row>strong {{ text-align:left; }} .footer-inner {{ display:block; }}
    }}
    @media print {{ .toolbar {{ display:none; }} .table-card,.quality-section {{ box-shadow:none; break-inside:avoid; }} body {{ background:#fff; }} details {{ display:block; }} }}
  </style>
</head>
<body>
  <header class="masthead">
    <div class="masthead-inner">
      <p class="eyebrow">SQL lineage · значения · маски · regex</p>
      <h1>Статистика использования SQL</h1>
      <p class="dek">Каталожный срез по каждой физической таблице и колонке: где она участвует, чем сравнивается и какие значения или шаблоны встречаются чаще всего.</p>
      <div class="source-line"><span>источник: {_escape(metadata.get('source_label'))}</span><span>диалект: {_escape(metadata.get('dialect'))}</span><span>формат: {_escape(metadata.get('format_version'))}</span><span>сформировано: {_escape(metadata.get('generated_at'))}</span></div>
    </div>
  </header>
  <main class="page">
    <section class="summary-grid" aria-label="Сводка">
      <div class="summary-lead"><small>Однозначность lineage</small><strong>{resolution_rate:.1f}%</strong><div class="resolution-track"><i style="width:{resolution_rate:.2f}%"></i></div><div class="status-tags">{status_tags}</div></div>
      <div class="summary-card"><small>Таблицы</small><strong>{_format_int(summary.get('table_count'))}</strong><span>полный DDL-инвентарь</span></div>
      <div class="summary-card"><small>Колонки</small><strong>{_format_int(summary.get('column_count'))}</strong><span>{_format_int(summary.get('active_column_count'))} со значениями</span></div>
      <div class="summary-card"><small>Запросы</small><strong>{_format_int(summary.get('query_count'))}</strong><span>{_format_int(summary.get('parsed_query_count'))} разобрано</span></div>
      <div class="summary-card"><small>Условия</small><strong>{_format_int(summary.get('resolved_condition_count'))}</strong><span>с физической колонкой</span></div>
    </section>
    <section class="method-note"><div><h2>Как читать результат</h2><p>В популярность колонки попадают только однозначно разрешённые условия. Серые колонки входят в DDL, но не встретились. Неоднозначные случаи сохранены ниже отдельно.</p></div><code>query_text → details.jsonl → catalog JSON → HTML</code></section>
    <div class="toolbar">
      <div class="toolbar-inner">
        <input id="catalog-search" type="search" placeholder="Таблица, колонка, значение, маска или regex…" autocomplete="off">
        <select id="activity-filter"><option value="all">Все колонки</option><option value="active">С найденными значениями</option><option value="unused">Без найденных значений</option></select>
        <select id="context-filter"><option value="all">Все секции SQL</option>{context_options}</select>
        <select id="pattern-filter"><option value="all">Все типы значений</option>{pattern_options}</select>
        <span id="visible-column-count">{_format_int(summary.get('column_count'))} колонок</span>
      </div>
    </div>
    <div class="catalog-head"><h2>Таблицы и колонки</h2><p>Таблицы отсортированы по частоте использования; внутри сохранён полный алфавитный состав колонок из DDL.</p></div>
    <section id="catalog-list">{cards}</section>
    <section class="quality-section" id="quality">
      <h2>Неоднозначные и неразрешённые случаи</h2>
      <p class="quality-intro">Эти {_format_int(quality.get('source_row_count'))} взвешенных использований требуют проверки схемы или SQL. Они намеренно не добавлены к популярности ни одной физической колонки.</p>
      <div class="quality-list">{_quality_rows(quality)}</div>
    </section>
  </main>
  <footer><div class="footer-inner"><span>Данные отчёта агрегированы заранее; SQL в браузере не разбирается.</span><span>{_escape(metadata.get('source_commit') or 'без commit источника')}</span></div></footer>
  <script>
    (() => {{
      const search = document.getElementById('catalog-search');
      const activity = document.getElementById('activity-filter');
      const context = document.getElementById('context-filter');
      const pattern = document.getElementById('pattern-filter');
      const counter = document.getElementById('visible-column-count');
      const tables = Array.from(document.querySelectorAll('.table-card'));
      const normalize = value => value.trim().toLocaleLowerCase('ru');
      function applyFilters() {{
        const term = normalize(search.value);
        let visibleColumns = 0;
        tables.forEach(table => {{
          let tableVisible = 0;
          table.querySelectorAll('.column-row').forEach(column => {{
            const textMatch = !term || normalize(column.dataset.search).includes(term);
            const activityMatch = activity.value === 'all' || column.dataset.active === activity.value;
            const contextMatch = context.value === 'all' || column.dataset.contexts.split(' ').includes(context.value);
            const patternMatch = pattern.value === 'all' || column.dataset.patterns.split(' ').includes(pattern.value);
            const visible = textMatch && activityMatch && contextMatch && patternMatch;
            column.hidden = !visible;
            if (visible) {{ tableVisible += 1; visibleColumns += 1; }}
          }});
          table.hidden = tableVisible === 0;
          if (term && tableVisible) table.querySelector('.table-details').open = true;
        }});
        counter.textContent = `${{visibleColumns.toLocaleString('ru-RU')}} колонок`;
      }}
      [search, activity, context, pattern].forEach(control => control.addEventListener('input', applyFilters));
      applyFilters();
    }})();
  </script>
</body>
</html>"""
