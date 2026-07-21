from __future__ import annotations

import html
import statistics
from collections import Counter

from .complexity import CorpusComplexity, QueryComplexity


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _tags(items: tuple[str, ...], empty: str = "не обнаружены") -> str:
    if not items:
        return f'<span class="empty-value">{_escape(empty)}</span>'
    return "".join(f'<span class="tag">{_escape(item)}</span>' for item in items)


def _metric(label: str, value: object) -> str:
    return (
        '<div class="metric">'
        f'<span class="metric-value">{_escape(value)}</span>'
        f'<span class="metric-label">{_escape(label)}</span>'
        "</div>"
    )


def _bar_rows(items: list[tuple[str, int]], *, limit: int | None = None) -> str:
    visible = items[:limit] if limit is not None else items
    maximum = max((value for _, value in visible), default=1)
    rows = []
    for label, value in visible:
        width = 0 if maximum == 0 else max(2, round(value / maximum * 100))
        rows.append(
            '<div class="bar-row">'
            f'<span class="bar-label" title="{_escape(label)}">{_escape(label)}</span>'
            '<span class="bar-track">'
            f'<span class="bar-fill" style="width:{width}%"></span>'
            "</span>"
            f'<strong>{value}</strong>'
            "</div>"
        )
    return "".join(rows) or '<p class="empty-value">Нет данных</p>'


def _parse_explanation(query: QueryComplexity) -> str:
    if not query.parsed:
        return (
            '<ol class="parse-route">'
            '<li><b>Парсер остановился.</b> SQLGlot не смог построить AST.</li>'
            f'<li><b>Причина.</b> {_escape(query.error or "неизвестная ошибка")}</li>'
            "</ol>"
        )

    roots = ", ".join(query.statement_types)
    steps = [
        f"<li><b>Корень.</b> {query.statement_count} выражение; тип AST: {_escape(roots)}.</li>"
    ]
    if query.cte_count:
        steps.append(
            f"<li><b>WITH.</b> Выделено {query.cte_count} CTE: "
            f"{_escape(', '.join(query.cte_names))}.</li>"
        )
    steps.append(
        f"<li><b>Источники.</b> {len(query.table_names)} уникальных базовых таблиц, "
        f"{query.table_reference_count} обращений и {query.join_count} JOIN.</li>"
    )
    if query.subquery_count or query.set_operation_count:
        steps.append(
            f"<li><b>Вложенность.</b> {query.subquery_count} подзапросов "
            f"(глубина {query.max_subquery_depth}) и {query.set_operation_count} "
            "операций объединения множеств.</li>"
        )
    steps.append(
        f"<li><b>Секции.</b> WHERE: {query.where_count}; HAVING: {query.having_count}; "
        f"GROUP BY: {query.group_count}; ORDER BY: {query.order_count}.</li>"
    )
    if query.window_count or query.aggregate_functions:
        functions = ", ".join(
            dict.fromkeys(query.window_functions + query.aggregate_functions)
        )
        steps.append(
            f"<li><b>Вычисления.</b> Окон: {query.window_count}; CASE: {query.case_count}; "
            f"функции: {_escape(functions or 'не обнаружены')}.</li>"
        )
    return f'<ol class="parse-route">{"".join(steps)}</ol>'


def _condition_label(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        word = "условие"
    elif count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        word = "условия"
    else:
        word = "условий"
    return f"{count} {word}"


def _literal_usage_rows(query: QueryComplexity) -> str:
    if not query.literal_usages:
        return '<p class="empty-value">Значения, связанные с выражениями, не обнаружены.</p>'
    rows = []
    for usage in query.literal_usages:
        if usage.lineage.columns:
            columns = _tags(
                tuple(column.qualified_name for column in usage.lineage.columns)
            )
        else:
            reason = usage.lineage.reason or "литерал не зависит от колонки"
            columns = (
                f'<span class="unresolved-column" title="{_escape(reason)}">'
                "Без базовой колонки</span>"
            )
        rows.append(
            '<div class="usage-row">'
            f'<span><b class="mobile-label">Место</b><span class="context-badge context-{_escape(usage.clause_context.casefold())}">{_escape(usage.clause_context)}</span></span>'
            f'<span class="usage-column"><b class="mobile-label">Колонка</b>{columns}</span>'
            f'<span><b class="mobile-label">Оператор</b><span class="usage-operator">{_escape(usage.operator_or_function)}</span></span>'
            f'<span class="usage-values"><b class="mobile-label">Значения</b>{_tags(usage.values)}</span>'
            f'<span><b class="mobile-label">Формат</b>{_escape(", ".join(usage.pattern_formats))}</span>'
            f'<span class="usage-count"><b class="mobile-label">Повторы</b>{_escape(_condition_label(usage.condition_count))}</span>'
            "</div>"
        )
    return (
        '<div class="usage-table">'
        '<div class="usage-head"><span>Место</span><span>Таблица · колонка</span><span>Оператор</span><span>Значения</span><span>Формат</span><span>Повторы</span></div>'
        f'{"".join(rows)}</div>'
    )


def _query_card(query: QueryComplexity) -> str:
    open_attribute = " open" if query.rank <= 3 else ""
    status = "parsed" if query.parsed else "error"
    score = query.score if query.parsed else "—"
    table_counts = tuple(
        f"{name} ×{count}" if count > 1 else name
        for name, count in query.table_reference_counts
    )
    error_block = ""
    if query.error:
        error_block = f'<p class="error-message">{_escape(query.error)}</p>'
    return f"""
    <article class="query-card tier-{_escape(query.tier)}" data-tier="{_escape(query.tier)}"
             data-rank="{query.rank}" data-score="{_escape(score)}" data-status="{status}">
      <details{open_attribute}>
        <summary>
          <span class="rank">#{query.rank:02d}</span>
          <span class="query-identity">
            <span class="query-name">{_escape(query.name)}</span>
            <span class="query-subtitle">{_escape(query.tier_label)} · {_escape(query.statement_types[0] if query.statement_types else "PARSE ERROR")}</span>
          </span>
          <span class="score-block"><strong>{_escape(score)}</strong><small>баллов</small></span>
          <span class="summary-counts">
            <span>{query.cte_count}<small>CTE</small></span>
            <span>{query.subquery_count}<small>SUBQ</small></span>
            <span>{query.join_count}<small>JOIN</small></span>
            <span>{query.window_count}<small>WIN</small></span>
          </span>
          <span class="disclosure" aria-hidden="true">⌄</span>
        </summary>
        <div class="query-body">
          {error_block}
          <section class="metric-grid" aria-label="Метрики запроса">
            {_metric("Узлы AST", query.node_count)}
            {_metric("Макс. глубина", query.max_depth)}
            {_metric("SELECT", query.select_count)}
            {_metric("Таблицы", len(query.table_names))}
            {_metric("Подзапросы", query.subquery_count)}
            {_metric("Set operations", query.set_operation_count)}
            {_metric("CASE", query.case_count)}
            {_metric("Оконные функции", query.window_count)}
          </section>
          <div class="detail-grid">
            <section class="explanation-panel">
              <h3>Как разобран запрос</h3>
              {_parse_explanation(query)}
            </section>
            <section class="inventory-panel">
              <div class="inventory-group"><h3>Базовые таблицы</h3><div class="tag-list">{_tags(table_counts)}</div></div>
              <div class="inventory-group"><h3>CTE</h3><div class="tag-list">{_tags(query.cte_names)}</div></div>
              <div class="inventory-group"><h3>JOIN</h3><div class="tag-list">{_tags(query.join_types)}</div></div>
              <div class="inventory-group"><h3>Set operations</h3><div class="tag-list">{_tags(query.set_operation_types)}</div></div>
              <div class="inventory-group"><h3>Окна и агрегаты</h3><div class="tag-list">{_tags(query.window_functions + query.aggregate_functions)}</div></div>
            </section>
          </div>
          <section class="literal-panel">
            <div class="section-heading">
              <h3>Значения и условия</h3>
              <span class="section-note">{sum(usage.condition_count for usage in query.literal_usages)} условий в AST</span>
            </div>
            <p class="literal-help">Здесь литералы связаны с физическими колонками и местом использования. Константы результата показаны отдельно как «Без базовой колонки».</p>
            {_literal_usage_rows(query)}
          </section>
          <section class="sql-panel">
            <div class="section-heading"><h3>Исходный SQL</h3><button class="copy-sql" type="button">Скопировать</button></div>
            <pre><code>{_escape(query.sql.strip())}</code></pre>
          </section>
        </div>
      </details>
    </article>"""


def render_html(corpus: CorpusComplexity) -> str:
    parsed_queries = [query for query in corpus.queries if query.parsed]
    scores = [query.score for query in parsed_queries]
    median_score = round(statistics.median(scores)) if scores else 0
    maximum_score = max(scores, default=0)
    tier_counts = Counter(query.tier for query in corpus.queries)
    tier_chart = [
        ("Экстремальная", tier_counts["extreme"]),
        ("Высокая", tier_counts["high"]),
        ("Средняя", tier_counts["medium"]),
        ("Базовая", tier_counts["basic"]),
        ("Ошибки", tier_counts["error"]),
    ]
    construct_chart = list(corpus.aggregate_counts)
    top_tables = list(corpus.table_counts)
    table_references = sum(value for _, value in corpus.table_counts)
    cards = "".join(_query_card(query) for query in corpus.queries)
    success_rate = (
        corpus.files_parsed / corpus.files_seen * 100 if corpus.files_seen else 0
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Разбор сложности TPC-DS</title>
  <style>
    :root {{
      --paper: #f3efe4;
      --paper-deep: #e8e0d1;
      --ink: #1b1d18;
      --muted: #68675f;
      --line: #c8bead;
      --brick: #b83c2b;
      --brick-dark: #7e271d;
      --olive: #626c50;
      --amber: #c4852d;
      --shadow: 0 18px 45px rgba(47, 36, 23, .09);
      --serif: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif;
      --sans: "Avenir Next", Avenir, "Trebuchet MS", sans-serif;
      --mono: "SFMono-Regular", Menlo, Monaco, Consolas, monospace;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        linear-gradient(rgba(39, 35, 27, .035) 1px, transparent 1px),
        var(--paper);
      background-size: 100% 28px;
      font-family: var(--sans);
      font-size: 15px;
      line-height: 1.55;
    }}
    button, input, select {{ font: inherit; }}
    button {{ cursor: pointer; }}
    .masthead {{
      position: relative;
      overflow: hidden;
      color: #f8f1e5;
      background: var(--ink);
      border-bottom: 7px solid var(--brick);
    }}
    .masthead::after {{
      content: "AST / 99";
      position: absolute;
      right: -1vw;
      bottom: -64px;
      color: rgba(255,255,255,.045);
      font: 900 clamp(110px, 20vw, 280px)/1 var(--sans);
      letter-spacing: -.09em;
      pointer-events: none;
    }}
    .masthead-inner, main, .toolbar-inner, .footer-inner {{
      width: min(1440px, calc(100% - 40px));
      margin: 0 auto;
    }}
    .masthead-inner {{ position: relative; z-index: 1; padding: 62px 0 50px; }}
    .eyebrow {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin: 0 0 18px;
      color: #dbcdb8;
      font: 700 12px/1 var(--sans);
      letter-spacing: .16em;
      text-transform: uppercase;
    }}
    .eyebrow::before {{ content: ""; width: 45px; border-top: 3px solid var(--brick); }}
    h1 {{
      max-width: 980px;
      margin: 0;
      font: 600 clamp(42px, 7vw, 94px)/.94 var(--serif);
      letter-spacing: -.045em;
    }}
    .dek {{ max-width: 820px; margin: 24px 0 0; color: #d7cebf; font-size: 18px; }}
    .source-strip {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px 28px;
      margin-top: 30px;
      color: #aead9f;
      font: 12px/1.4 var(--mono);
    }}
    main {{ padding: 42px 0 80px; }}
    .overview {{ display: grid; grid-template-columns: 1.1fr .9fr; gap: 22px; }}
    .overview > * {{ min-width: 0; }}
    .summary-board, .chart-panel, .method-note {{
      background: rgba(250,247,239,.82);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
    }}
    .summary-board {{ grid-row: span 2; padding: 28px; }}
    .section-kicker {{ margin: 0 0 18px; color: var(--brick); font: 800 11px/1 var(--sans); letter-spacing: .18em; text-transform: uppercase; }}
    .big-stats {{ display: grid; grid-template-columns: repeat(2, 1fr); border: 1px solid var(--line); }}
    .big-stat {{ min-height: 128px; padding: 20px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }}
    .big-stat:nth-child(2n) {{ border-right: 0; }}
    .big-stat:nth-last-child(-n+2) {{ border-bottom: 0; }}
    .big-stat strong {{ display: block; font: 600 48px/1 var(--serif); letter-spacing: -.04em; }}
    .big-stat span {{ display: block; margin-top: 10px; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
    .chart-panel {{ padding: 24px 26px; }}
    .chart-panel h2, .method-note h2 {{ margin: 0 0 16px; font: 600 25px/1.05 var(--serif); }}
    .bar-row {{ display: grid; grid-template-columns: minmax(95px, 145px) 1fr 38px; align-items: center; gap: 10px; margin: 9px 0; font-size: 12px; }}
    .bar-label {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .bar-track {{ height: 7px; overflow: hidden; background: var(--paper-deep); }}
    .bar-fill {{ display: block; height: 100%; background: var(--brick); }}
    .bar-row:nth-child(even) .bar-fill {{ background: var(--olive); }}
    .method-note {{ margin-top: 22px; padding: 26px 30px; border-left: 6px solid var(--brick); }}
    .formula {{ margin: 16px 0 10px; padding: 14px 16px; overflow-x: auto; color: #f7eee1; background: var(--ink); font: 13px/1.7 var(--mono); white-space: nowrap; }}
    .method-note p {{ margin: 8px 0; color: var(--muted); }}
    .toolbar {{ position: sticky; top: 0; z-index: 10; margin-top: 38px; color: #fff; background: rgba(27,29,24,.97); border-bottom: 3px solid var(--brick); backdrop-filter: blur(12px); }}
    .toolbar-inner {{ display: grid; grid-template-columns: minmax(260px, 1fr) 210px auto auto 90px; gap: 10px; padding: 12px 0; align-items: center; }}
    .toolbar input, .toolbar select {{ width: 100%; min-height: 42px; color: #f8f1e5; background: #292b25; border: 1px solid #52544a; border-radius: 0; padding: 9px 12px; outline: none; }}
    .toolbar input:focus, .toolbar select:focus {{ border-color: #df9a82; box-shadow: 0 0 0 2px rgba(223,154,130,.18); }}
    .toolbar button {{ min-height: 42px; color: #f8f1e5; background: transparent; border: 1px solid #67695f; padding: 8px 13px; }}
    .toolbar button:hover {{ background: var(--brick); border-color: var(--brick); }}
    #visible-count {{ text-align: right; color: #c5c1b7; font: 12px/1 var(--mono); }}
    .query-list {{ margin-top: 24px; }}
    .query-card {{ margin: 13px 0; background: rgba(250,247,239,.92); border: 1px solid var(--line); border-left: 6px solid var(--olive); box-shadow: 0 5px 20px rgba(55,42,27,.045); }}
    .query-card.tier-extreme {{ border-left-color: var(--brick); }}
    .query-card.tier-high {{ border-left-color: var(--amber); }}
    .query-card.tier-error {{ border-left-color: #8b1a1a; }}
    .query-card[hidden] {{ display: none; }}
    summary {{ display: grid; grid-template-columns: 65px minmax(180px, 1fr) 92px 280px 28px; gap: 16px; align-items: center; min-height: 96px; padding: 16px 20px; cursor: pointer; list-style: none; }}
    summary::-webkit-details-marker {{ display: none; }}
    summary:hover {{ background: rgba(232,224,209,.45); }}
    .rank {{ color: var(--brick); font: 700 20px/1 var(--mono); }}
    .query-name {{ display: block; font: 600 27px/1.05 var(--serif); }}
    .query-subtitle {{ display: block; margin-top: 7px; color: var(--muted); font-size: 11px; letter-spacing: .08em; text-transform: uppercase; }}
    .score-block {{ text-align: right; border-right: 1px solid var(--line); padding-right: 18px; }}
    .score-block strong {{ display: block; font: 600 30px/1 var(--serif); }}
    .score-block small {{ color: var(--muted); text-transform: uppercase; letter-spacing: .08em; }}
    .summary-counts {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 9px; }}
    .summary-counts > span {{ text-align: center; font: 700 17px/1 var(--mono); }}
    .summary-counts small {{ display: block; margin-top: 7px; color: var(--muted); font: 9px/1 var(--sans); letter-spacing: .1em; }}
    .disclosure {{ font: 25px/1 var(--sans); transition: transform .2s ease; }}
    details[open] .disclosure {{ transform: rotate(180deg); }}
    .query-body {{ padding: 2px 24px 28px; border-top: 1px solid var(--line); }}
    .metric-grid {{ display: grid; grid-template-columns: repeat(8, 1fr); margin: 24px 0; border: 1px solid var(--line); }}
    .metric {{ min-width: 0; padding: 15px 10px; text-align: center; border-right: 1px solid var(--line); }}
    .metric:last-child {{ border-right: 0; }}
    .metric-value {{ display: block; font: 600 27px/1 var(--serif); }}
    .metric-label {{ display: block; margin-top: 8px; color: var(--muted); font-size: 9px; letter-spacing: .06em; text-transform: uppercase; }}
    .detail-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 28px; }}
    .detail-grid h3, .sql-panel h3, .literal-panel h3 {{ margin: 0 0 12px; font: 600 20px/1.1 var(--serif); }}
    .parse-route {{ margin: 0; padding-left: 22px; }}
    .parse-route li {{ margin: 8px 0; padding-left: 5px; color: var(--muted); }}
    .parse-route b {{ color: var(--ink); }}
    .inventory-panel {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px 22px; }}
    .inventory-group h3 {{ margin-bottom: 8px; color: var(--muted); font: 800 10px/1 var(--sans); letter-spacing: .12em; text-transform: uppercase; }}
    .tag-list {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .tag {{ padding: 4px 7px; background: var(--paper-deep); border: 1px solid #d5c9b8; font: 11px/1.3 var(--mono); }}
    .empty-value {{ color: #8b877e; font-style: italic; }}
    .literal-panel {{ margin-top: 30px; padding-top: 24px; border-top: 1px solid var(--line); }}
    .section-note {{ color: var(--muted); font: 11px/1 var(--mono); }}
    .literal-help {{ max-width: 900px; margin: -2px 0 15px; color: var(--muted); font-size: 12px; }}
    .usage-table {{ border: 1px solid var(--line); }}
    .usage-head, .usage-row {{ display: grid; grid-template-columns: 100px minmax(220px, 1.35fr) 115px minmax(190px, 1fr) minmax(125px, .75fr) 100px; gap: 12px; align-items: start; }}
    .usage-head {{ padding: 9px 12px; color: #e7dfd0; background: var(--ink); font-size: 9px; font-weight: 800; letter-spacing: .1em; text-transform: uppercase; }}
    .usage-row {{ padding: 12px; border-top: 1px solid var(--line); font-size: 11px; }}
    .usage-row:nth-child(odd) {{ background: rgba(232,224,209,.3); }}
    .usage-row > span {{ min-width: 0; }}
    .usage-column, .usage-values {{ display: flex; flex-wrap: wrap; gap: 5px; }}
    .context-badge, .usage-operator {{ display: inline-block; padding: 4px 6px; font: 700 10px/1.2 var(--mono); }}
    .context-badge {{ color: #fff; background: var(--olive); }}
    .context-select {{ background: #6d675e; }}
    .context-join_on {{ background: #8d5f28; }}
    .context-case {{ background: #78613d; }}
    .usage-operator {{ color: var(--brick-dark); background: #ead8cd; }}
    .unresolved-column {{ color: #786f62; font-style: italic; border-bottom: 1px dotted #786f62; }}
    .usage-count {{ font-weight: 700; }}
    .mobile-label {{ display: none; }}
    .sql-panel {{ margin-top: 28px; }}
    .section-heading {{ display: flex; align-items: center; justify-content: space-between; }}
    .copy-sql {{ color: var(--brick-dark); background: transparent; border: 1px solid var(--line); padding: 6px 10px; font-size: 11px; }}
    .copy-sql:hover {{ color: #fff; background: var(--brick); border-color: var(--brick); }}
    pre {{ max-height: 560px; margin: 0; padding: 20px; overflow: auto; color: #e7dfd0; background: #20221e; border-top: 4px solid var(--brick); tab-size: 2; }}
    code {{ font: 12px/1.65 var(--mono); white-space: pre; }}
    .error-message {{ padding: 12px 15px; color: #7d1818; background: #f2d9d3; border: 1px solid #d4a69d; font-family: var(--mono); }}
    .no-results {{ display: none; margin: 35px 0; padding: 40px; text-align: center; border: 1px dashed var(--line); }}
    footer {{ color: #b8b3a8; background: var(--ink); }}
    .footer-inner {{ padding: 32px 0; font-size: 12px; }}
    @media (max-width: 980px) {{
      .overview {{ grid-template-columns: minmax(0, 1fr); }}
      .summary-board {{ grid-row: auto; }}
      .toolbar-inner {{ grid-template-columns: 1fr 170px auto; }}
      .toolbar button {{ display: none; }}
      summary {{ grid-template-columns: 54px 1fr 78px 24px; }}
      .summary-counts {{ display: none; }}
      .metric-grid {{ grid-template-columns: repeat(4, 1fr); }}
      .metric:nth-child(4) {{ border-right: 0; }}
      .metric:nth-child(-n+4) {{ border-bottom: 1px solid var(--line); }}
      .detail-grid {{ grid-template-columns: 1fr; }}
      .usage-table {{ border: 0; }}
      .usage-head {{ display: none; }}
      .usage-row {{ grid-template-columns: 110px 1fr; margin: 10px 0; border: 1px solid var(--line); }}
      .usage-column, .usage-values {{ grid-column: 1 / -1; }}
      .mobile-label {{ display: block; width: 100%; margin-bottom: 5px; color: var(--muted); font: 800 8px/1 var(--sans); letter-spacing: .1em; text-transform: uppercase; }}
    }}
    @media (max-width: 620px) {{
      .masthead-inner, main, .toolbar-inner, .footer-inner {{ width: min(100% - 22px, 1440px); }}
      .masthead-inner {{ padding: 42px 0 36px; }}
      h1 {{ font-size: 44px; }}
      .overview {{ gap: 12px; }}
      .summary-board, .chart-panel, .method-note {{ padding: 20px; }}
      .toolbar-inner {{ grid-template-columns: 1fr 110px; }}
      #visible-count {{ display: none; }}
      summary {{ grid-template-columns: 48px 1fr 62px; gap: 8px; padding: 12px; }}
      .disclosure {{ display: none; }}
      .query-name {{ font-size: 22px; }}
      .score-block {{ padding-right: 8px; }}
      .query-body {{ padding: 2px 14px 18px; }}
      .metric-grid {{ grid-template-columns: repeat(2, 1fr); }}
      .metric {{ border-bottom: 1px solid var(--line); }}
      .metric:nth-child(2n) {{ border-right: 0; }}
      .metric:nth-last-child(-n+2) {{ border-bottom: 0; }}
      .inventory-panel {{ grid-template-columns: 1fr; }}
    }}
    @media print {{
      body {{ background: #fff; }}
      .toolbar, .copy-sql {{ display: none !important; }}
      .masthead {{ color: #000; background: #fff; border-bottom-color: #000; }}
      .dek, .source-strip {{ color: #333; }}
      main {{ width: 100%; padding-top: 20px; }}
      .query-card {{ break-inside: avoid; box-shadow: none; }}
      details:not([open]) > *:not(summary) {{ display: block; }}
      pre {{ max-height: none; white-space: pre-wrap; }}
    }}
  </style>
</head>
<body>
  <header class="masthead">
    <div class="masthead-inner">
      <p class="eyebrow">SQLGlot · структурный аудит корпуса</p>
      <h1>Разбор сложности TPC-DS</h1>
      <p class="dek">Все запросы ранжированы от самых насыщенных конструкциями к самым простым. Каждая карточка показывает не только счётчики, но и маршрут, по которому SQL был разложен в AST.</p>
      <div class="source-strip">
        <span>ИСТОЧНИК: {_escape(corpus.source_label)}</span>
        <span>COMMIT: {_escape(corpus.source_commit)}</span>
        <span>DIALECT: {_escape(corpus.dialect)}</span>
      </div>
    </div>
  </header>

  <main>
    <section class="overview" aria-label="Сводная статистика">
      <div class="summary-board">
        <p class="section-kicker">Корпус целиком</p>
        <div class="big-stats">
          <div class="big-stat"><strong>{corpus.files_seen}</strong><span>SQL-файлов</span></div>
          <div class="big-stat"><strong>{success_rate:.0f}%</strong><span>успешно разобрано</span></div>
          <div class="big-stat"><strong>{maximum_score}</strong><span>максимальный балл</span></div>
          <div class="big-stat"><strong>{median_score}</strong><span>медианный балл</span></div>
        </div>
      </div>
      <div class="chart-panel">
        <h2>Конструкции во всех запросах</h2>
        {_bar_rows(construct_chart)}
      </div>
      <div class="chart-panel">
        <h2>Уровни сложности</h2>
        {_bar_rows(tier_chart)}
      </div>
    </section>

    <section class="overview" style="margin-top:22px" aria-label="Таблицы корпуса">
      <div class="chart-panel">
        <h2>Чаще всего упоминаемые таблицы</h2>
        {_bar_rows(top_tables, limit=12)}
      </div>
      <aside class="method-note" style="margin-top:0">
        <p class="section-kicker">Методика</p>
        <h2>Структурная сложность, не стоимость выполнения</h2>
        <div class="formula">0.1 × узлы AST + 4 × CTE + 6 × подзапросы + 6 × set operations + 2 × JOIN + 4 × окна + 2 × CASE + 3 × макс. глубина</div>
        <p>Оценка нужна для сортировки и сравнения синтаксической насыщенности. Без данных Greenplum, статистики и EXPLAIN она не предсказывает время выполнения.</p>
        <p>Всего обращений к базовым таблицам: <strong>{table_references}</strong>.</p>
      </aside>
    </section>
  </main>

  <div class="toolbar">
    <div class="toolbar-inner">
      <input id="query-search" type="search" placeholder="Номер, таблица, CTE, функция или фрагмент SQL…" aria-label="Поиск по запросам">
      <select id="tier-filter" aria-label="Фильтр сложности">
        <option value="all">Все уровни</option>
        <option value="extreme">Экстремальная</option>
        <option value="high">Высокая</option>
        <option value="medium">Средняя</option>
        <option value="basic">Базовая</option>
        <option value="error">Ошибки</option>
      </select>
      <button id="expand-all" type="button">Раскрыть</button>
      <button id="collapse-all" type="button">Свернуть</button>
      <span id="visible-count">{len(corpus.queries)} / {len(corpus.queries)}</span>
    </div>
  </div>

  <main style="padding-top:0">
    <section id="query-list" class="query-list" aria-label="Запросы от сложных к простым">
      {cards}
      <div id="no-results" class="no-results">Ничего не найдено. Измените поиск или уровень сложности.</div>
    </section>
  </main>

  <footer><div class="footer-inner">Отчёт автономен: SQL, стили и фильтрация находятся в одном HTML-файле.</div></footer>
  <script>
    (() => {{
      const cards = [...document.querySelectorAll('.query-card')];
      const search = document.getElementById('query-search');
      const tier = document.getElementById('tier-filter');
      const count = document.getElementById('visible-count');
      const noResults = document.getElementById('no-results');

      function applyFilters() {{
        const needle = search.value.trim().toLocaleLowerCase('ru');
        const selectedTier = tier.value;
        let visible = 0;
        cards.forEach((card) => {{
          const matchesText = !needle || card.textContent.toLocaleLowerCase('ru').includes(needle);
          const matchesTier = selectedTier === 'all' || card.dataset.tier === selectedTier;
          card.hidden = !(matchesText && matchesTier);
          if (!card.hidden) visible += 1;
        }});
        count.textContent = `${{visible}} / ${{cards.length}}`;
        noResults.style.display = visible ? 'none' : 'block';
      }}

      search.addEventListener('input', applyFilters);
      tier.addEventListener('change', applyFilters);
      document.getElementById('expand-all').addEventListener('click', () => {{
        cards.filter((card) => !card.hidden).forEach((card) => card.querySelector('details').open = true);
      }});
      document.getElementById('collapse-all').addEventListener('click', () => {{
        cards.forEach((card) => card.querySelector('details').open = false);
      }});
      document.querySelectorAll('.copy-sql').forEach((button) => {{
        button.addEventListener('click', async () => {{
          const sql = button.closest('.sql-panel').querySelector('code').textContent;
          await navigator.clipboard.writeText(sql);
          const original = button.textContent;
          button.textContent = 'Скопировано';
          setTimeout(() => button.textContent = original, 1200);
        }});
      }});
    }})();
  </script>
</body>
</html>
"""
