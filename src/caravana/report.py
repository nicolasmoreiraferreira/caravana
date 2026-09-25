"""Relatórios auditáveis: Markdown, HTML autocontido, SVG e manifesto.

Um relatório do Caravana responde três perguntas, nesta ordem:

1. **O que aconteceu?** — estado final por sessão, com o passo exato onde parou.
2. **Por quê?** — mensagem de erro, tipo, tentativas e artefatos do momento da falha.
3. **Isso é confiável?** — hash do fluxo, versões, contagem de artefatos e o
   hash de cada arquivo, para que o resultado possa ser conferido depois.

O HTML não depende de rede (nenhum CDN, nenhuma fonte externa): pode ser
arquivado, anexado a um PR ou aberto meses depois sem perder a aparência.
"""

from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .config import Config
from .ledger import Ledger, RunRecord, RunStats
from .util import format_duration, human_bytes, sha256_file, utc_now

__all__ = [
    "build_badge_svg",
    "build_manifest",
    "build_report_html",
    "build_report_markdown",
    "build_report_text",
]

_STATUS_LABEL = {
    "ok": "sucesso",
    "degraded": "degradada",
    "completed_with_warnings": "concluída com avisos",
    "failed": "falha",
    "skipped": "ignorado",
    "aborted": "interrompido",
    "completed": "concluída",
    "interrupted": "interrompida",
    "running": "em execução",
}

_STATUS_CLASS = {
    "ok": "ok",
    "completed": "ok",
    "degraded": "warn",
    "completed_with_warnings": "warn",
    "failed": "fail",
    "aborted": "warn",
    "interrupted": "warn",
    "skipped": "skip",
    "running": "run",
}


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%".replace(".", ",")


def _label(status: str) -> str:
    return _STATUS_LABEL.get(status, status)


def _step_rows(ledger: Ledger, run_id: str) -> list[dict[str, Any]]:
    """Uma linha por passo, já resolvida para o estado final de cada sessão."""
    rows: list[dict[str, Any]] = []
    for session_id, records in _group_by_session(ledger, run_id).items():
        for record in records:
            rows.append(
                {
                    "session": session_id,
                    "step": record.step_id,
                    "name": record.step_name or record.step_id,
                    "action": record.action,
                    "attempt": record.attempt,
                    "status": record.status,
                    "duration_ms": record.duration_ms or 0.0,
                    "error": record.error_message,
                    "artifacts": record.artifacts,
                }
            )
    return rows


def _group_by_session(ledger: Ledger, run_id: str) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for record in ledger.steps_for(run_id):
        grouped.setdefault(record.session_id, []).append(record)
    return grouped


def build_report_markdown(ledger: Ledger, run_id: str, config: Config | None = None) -> str:
    """Gera o relatório em Markdown, com tabelas prontas para colar em uma issue."""
    run = ledger.get_run(run_id)
    if run is None:
        return f"# Execução não encontrada\n\n`{run_id}` não está no histórico.\n"
    stats = ledger.stats(run.id)
    sessions = ledger.session_results(run.id)
    artifacts = ledger.artifacts_for(run.id)
    duration = format_duration(stats.duration_s)

    lines: list[str] = []
    lines.append(f"# Relatório da execução `{run.id}`")
    lines.append("")
    lines.append(f"**Fluxo:** `{run.flow_name}` v{run.flow_version}  ")
    lines.append(f"**Estado:** `{run.status}` ({_label(run.status)})  ")
    lines.append(f"**Início:** {run.started_at}  ")
    lines.append(f"**Duração:** {duration}  ")
    lines.append(
        f"**Sessões:** {stats.completed} concluída(s), {stats.degraded} degradada(s), "
        f"{stats.failed} com falha, {stats.aborted} interrompida(s)  "
    )
    lines.append(
        f"**Passos:** {stats.steps_ok} ok, {stats.steps_failed} com falha definitiva, "
        f"{stats.steps_retried} nova(s) tentativa(s), {stats.steps_skipped} ignorado(s)  "
    )
    lines.append(f"**Artefatos:** {stats.artifacts} ({human_bytes(stats.bytes_written)})  ")
    lines.append(f"**Hash do fluxo:** `{run.flow_hash[:16]}…`  ")
    if config is not None:
        lines.append(
            f"**Configuração:** {config.workers} worker(s), {config.max_attempts} tentativa(s) "
            f"por passo, screenshots `{config.screenshots}`  "
        )
    lines.append("")

    lines.append("## Resumo")
    lines.append("")
    lines.append("| Sessão | Estado | Passos | Último passo | Erro |")
    lines.append("| --- | --- | --- | --- | --- |")
    for session_id, info in sorted(sessions.items()):
        error = (info.get("error") or "").replace("|", "\\|")[:120]
        lines.append(
            f"| `{session_id}` | {_label(info.get('status', 'ok'))} | {info.get('steps', 0)} | "
            f"`{info.get('last_step') or '—'}` | {error or '—'} |"
        )
    lines.append("")

    failures = [row for row in _step_rows(ledger, run.id) if row["status"] == "failed"]
    if failures:
        lines.append("## Falhas")
        lines.append("")
        for row in failures:
            lines.append(f"### `{row['session']}` · passo `{row['step']}` ({row['action']})")
            lines.append("")
            lines.append(f"- Tentativas: {row['attempt']}")
            lines.append(f"- Mensagem: {row['error'] or '—'}")
            if row["artifacts"]:
                lines.append("- Evidências:")
                for artifact in row["artifacts"]:
                    lines.append(f"  - `{artifact}`")
            lines.append("")

    lines.append("## Passos por sessão")
    lines.append("")
    for session_id, records in sorted(_group_by_session(ledger, run.id).items()):
        lines.append(f"### `{session_id}`")
        lines.append("")
        lines.append("| Passo | Ação | Tentativa | Estado | Duração |")
        lines.append("| --- | --- | --- | --- | --- |")
        for record in records:
            lines.append(
                f"| `{record.step_id}` | {record.action} | {record.attempt} | "
                f"{_label(record.status)} | {format_duration((record.duration_ms or 0) / 1000)} |"
            )
        lines.append("")

    if artifacts:
        lines.append("## Artefatos")
        lines.append("")
        lines.append("| Tipo | Sessão | Passo | Arquivo | Tamanho | SHA-256 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in artifacts:
            digest = (item.get("sha256") or "")[:12]
            lines.append(
                f"| {item['kind']} | `{item['session_id']}` | `{item['step_id']}` | "
                f"`{Path(item['path']).name}` | {human_bytes(int(item['bytes'] or 0))} | `{digest}` |"
            )
        lines.append("")

    lines.append("## Ambiente")
    lines.append("")
    lines.append("| Chave | Valor |")
    lines.append("| --- | --- |")
    for key, value in sorted((run.metadata or {}).items()):
        if isinstance(value, (str, int, float, bool)):
            lines.append(f"| {key} | {value} |")
    lines.append("")
    lines.append(f"_Gerado por Caravana em {utc_now().isoformat()}_")
    lines.append("")
    return "\n".join(lines)


def build_report_text(ledger: Ledger, run_id: str) -> str:
    """Versão curta em texto, para o terminal e para `caravana show`."""
    run = ledger.get_run(run_id)
    if run is None:
        return f"execução '{run_id}' não encontrada"
    stats = ledger.stats(run.id)
    lines = [
        f"execução    {run.id}",
        f"fluxo       {run.flow_name} v{run.flow_version} (hash {run.flow_hash[:12]})",
        f"estado      {run.status}",
        f"duração     {format_duration(stats.duration_s)}",
        f"sessões     {stats.completed} ok / {stats.degraded} degradada(s) / "
        f"{stats.failed} falha / {stats.aborted} interrompida",
        f"passos      {stats.steps_ok} ok / {stats.steps_failed} falha / {stats.steps_retried} retry",
        f"artefatos   {stats.artifacts} ({human_bytes(stats.bytes_written)})",
        f"diretório   {run.run_dir}",
    ]
    for session_id, info in sorted(ledger.session_results(run.id).items()):
        marker = {"ok": "✓", "degraded": "~"}.get(str(info.get("status")), "✗")
        error = f" — {info.get('error')}" if info.get("error") else ""
        lines.append(f"  {marker} {session_id}: {info.get('steps', 0)} passo(s){error}")
    return "\n".join(lines)


def _svg_bars(stats: RunStats) -> str:
    """Gráfico de barras em SVG puro, embutido no HTML sem bibliotecas."""
    bars = [
        ("ok", stats.steps_ok, "#34d399"),
        ("falha", stats.steps_failed, "#f87171"),
        ("retry", stats.steps_retried, "#fbbf24"),
        ("ignorado", stats.steps_skipped, "#94a3b8"),
    ]
    total = max(1, max(value for _, value, _ in bars))
    width, height, gap = 320, 120, 14
    bar_width = (width - gap * (len(bars) + 1)) / len(bars)
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Passos por estado">']
    for index, (label, value, color) in enumerate(bars):
        bar_height = max(2.0, (value / total) * (height - 34))
        x = gap + index * (bar_width + gap)
        y = height - 20 - bar_height
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" '
            f'rx="4" fill="{color}"><title>{label}: {value}</title></rect>'
        )
        parts.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{y - 4:.1f}" fill="#e2e8f0" font-size="10" '
            f'text-anchor="middle">{value}</text>'
        )
        parts.append(
            f'<text x="{x + bar_width / 2:.1f}" y="{height - 6}" fill="#94a3b8" font-size="9" '
            f'text-anchor="middle">{label}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def build_report_html(ledger: Ledger, run_id: str, config: Config | None = None) -> str:
    """Gera um relatório HTML autocontido (CSS e SVG embutidos, zero requisições)."""
    run = ledger.get_run(run_id)
    if run is None:  # pragma: no cover - caminho defensivo
        return f"<!doctype html><title>não encontrado</title><p>{html.escape(run_id)}</p>"
    stats = ledger.stats(run.id)
    sessions = ledger.session_results(run.id)
    artifacts = ledger.artifacts_for(run.id)
    rows = _step_rows(ledger, run.id)
    failures = [row for row in rows if row["status"] == "failed"]
    action_counts = Counter(row["action"] for row in rows)

    def esc(value: Any) -> str:
        return html.escape(str(value))

    cards = [
        ("Sessões", str(stats.total_sessions)),
        ("Concluídas", f"{stats.completed} ({_pct(stats.success_rate)})"),
        ("Degradadas", str(stats.degraded)),
        ("Falhas", str(stats.failed)),
        ("Passos ok", str(stats.steps_ok)),
        ("Novas tentativas", str(stats.steps_retried)),
        ("Duração", format_duration(stats.duration_s)),
        ("Artefatos", f"{stats.artifacts} · {human_bytes(stats.bytes_written)}"),
    ]
    cards_html = "".join(
        f'<div class="card"><span class="card-label">{esc(label)}</span>'
        f'<span class="card-value">{esc(value)}</span></div>'
        for label, value in cards
    )

    session_rows = "".join(
        f"<tr><td><code>{esc(session_id)}</code></td>"
        f'<td><span class="badge {_STATUS_CLASS.get(info.get("status", "ok"), "skip")}">'
        f"{esc(_label(info.get('status', 'ok')))}</span></td>"
        f"<td>{info.get('steps', 0)}</td>"
        f"<td><code>{esc(info.get('last_step') or '—')}</code></td>"
        f'<td class="err">{esc((info.get("error") or "—")[:220])}</td></tr>'
        for session_id, info in sorted(sessions.items())
    )

    steps_html = ""
    for session_id, records in sorted(_group_by_session(ledger, run.id).items()):
        body = "".join(
            f"<tr><td><code>{esc(record.step_id)}</code></td><td>{esc(record.action)}</td>"
            f"<td>{record.attempt}</td>"
            f'<td><span class="badge {_STATUS_CLASS.get(record.status, "skip")}">'
            f"{esc(_label(record.status))}</span></td>"
            f"<td>{format_duration((record.duration_ms or 0) / 1000)}</td>"
            f'<td class="err">{esc((record.error_message or "")[:200])}</td></tr>'
            for record in records
        )
        steps_html += (
            f"<details open><summary><code>{esc(session_id)}</code> "
            f'<span class="muted">{len(records)} passo(s)</span></summary>'
            f'<table class="grid"><thead><tr><th>Passo</th><th>Ação</th><th>Tentativa</th>'
            f"<th>Estado</th><th>Duração</th><th>Erro</th></tr></thead><tbody>{body}</tbody></table>"
            "</details>"
        )

    failures_html = (
        "".join(
            f'<div class="failure"><h3><code>{esc(row["session"])}</code> · '
            f'<code>{esc(row["step"])}</code> <span class="muted">{esc(row["action"])}</span></h3>'
            f"<p>{esc(row['error'] or '—')}</p>"
            + (
                "<ul>"
                + "".join(
                    f"<li><code>{esc(Path(path).name)}</code></li>" for path in row["artifacts"]
                )
                + "</ul>"
                if row["artifacts"]
                else ""
            )
            + "</div>"
            for row in failures
        )
        or '<p class="muted">Nenhuma falha registrada nesta execução.</p>'
    )

    artifacts_html = "".join(
        f"<tr><td>{esc(item['kind'])}</td><td><code>{esc(item['session_id'])}</code></td>"
        f"<td><code>{esc(item['step_id'])}</code></td><td><code>{esc(Path(item['path']).name)}</code></td>"
        f"<td>{human_bytes(int(item['bytes'] or 0))}</td>"
        f'<td><code class="digest">{esc((item.get("sha256") or "")[:16])}</code></td></tr>'
        for item in artifacts
    )

    actions_html = "".join(
        f'<span class="chip">{esc(action)} <b>{count}</b></span>'
        for action, count in action_counts.most_common()
    )

    config_note = ""
    if config is not None:
        config_note = (
            f"<tr><td>configuração</td><td><code>{config.workers} worker(s) · "
            f"{config.max_attempts} tentativa(s)/passo · screenshots "
            f"{esc(config.screenshots)}</code></td></tr>"
        )

    metadata_html = "".join(
        f"<tr><td>{esc(key)}</td><td><code>{esc(value)}</code></td></tr>"
        for key, value in sorted((run.metadata or {}).items())
        if isinstance(value, (str, int, float, bool))
    )

    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Caravana · execução {esc(run.id)}</title>
<style>
:root {{
  --bg: #0b1120; --panel: #111a2e; --panel-2: #16213a; --line: #22304d;
  --text: #e2e8f0; --muted: #94a3b8; --accent: #22d3ee; --ok: #34d399;
  --fail: #f87171; --warn: #fbbf24;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}}
.wrap {{ max-width: 1080px; margin: 0 auto; padding: 32px 20px 64px; }}
header {{ border-bottom: 1px solid var(--line); padding-bottom: 20px; margin-bottom: 24px; }}
h1 {{ font-size: 26px; margin: 0 0 8px; }}
h2 {{ font-size: 18px; margin: 32px 0 12px; color: var(--accent); }}
h3 {{ font-size: 15px; margin: 0 0 6px; }}
code, .digest {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }}
code {{ background: var(--panel-2); padding: 1px 6px; border-radius: 5px; }}
.muted {{ color: var(--muted); font-weight: 400; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }}
.card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px; }}
.card-label {{ display: block; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }}
.card-value {{ display: block; font-size: 22px; font-weight: 650; margin-top: 4px; }}
table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }}
th, td {{ text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }}
th {{ background: var(--panel-2); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }}
tr:last-child td {{ border-bottom: 0; }}
.grid {{ margin-top: 8px; }}
.badge {{ padding: 2px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; }}
.badge.ok {{ background: rgba(52,211,153,.16); color: var(--ok); }}
.badge.fail {{ background: rgba(248,113,113,.16); color: var(--fail); }}
.badge.warn {{ background: rgba(251,191,36,.16); color: var(--warn); }}
.badge.skip {{ background: rgba(148,163,184,.16); color: var(--muted); }}
.badge.run {{ background: rgba(34,211,238,.16); color: var(--accent); }}
.err {{ color: #fecaca; font-size: 13px; }}
details {{ margin: 10px 0; }}
summary {{ cursor: pointer; padding: 8px 0; }}
.failure {{ background: var(--panel); border: 1px solid var(--line); border-left: 3px solid var(--fail); border-radius: 10px; padding: 12px 14px; margin: 10px 0; }}
.chip {{ display: inline-block; background: var(--panel-2); border: 1px solid var(--line); border-radius: 999px; padding: 3px 10px; margin: 0 6px 6px 0; font-size: 12.5px; }}
.chip b {{ color: var(--accent); }}
.chart {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 10px; }}
footer {{ margin-top: 40px; color: var(--muted); font-size: 12.5px; border-top: 1px solid var(--line); padding-top: 16px; }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>Relatório de execução · Caravana</h1>
  <p class="muted">
    <code>{esc(run.id)}</code> · fluxo <code>{esc(run.flow_name)}</code> v{esc(run.flow_version)} ·
    hash <code>{esc(run.flow_hash[:16])}</code> · iniciada em {esc(run.started_at)}
  </p>
  <p><span class="badge {_STATUS_CLASS.get(run.status, "skip")}">{esc(_label(run.status))}</span></p>
</header>

<h2>Visão geral</h2>
<div class="cards">{cards_html}</div>

<h2>Distribuição de passos</h2>
<div class="chart">{_svg_bars(stats)}</div>
<p>{actions_html}</p>

<h2>Sessões</h2>
<table><thead><tr><th>Sessão</th><th>Estado</th><th>Passos</th><th>Último passo</th><th>Erro</th></tr></thead>
<tbody>{session_rows}</tbody></table>

<h2>Falhas</h2>
{failures_html}

<h2>Passos por sessão</h2>
{steps_html}

<h2>Artefatos</h2>
<table><thead><tr><th>Tipo</th><th>Sessão</th><th>Passo</th><th>Arquivo</th><th>Tamanho</th><th>SHA-256</th></tr></thead>
<tbody>{artifacts_html or '<tr><td colspan="6" class="muted">Nenhum artefato.</td></tr>'}</tbody></table>

<h2>Ambiente</h2>
<table><tbody>{config_note}{metadata_html}</tbody></table>

<footer>
  Gerado por Caravana {esc(utc_now().isoformat())} · relatório autocontido, sem dependências externas.
</footer>
</div>
</body>
</html>
"""


def build_badge_svg(stats: RunStats) -> str:
    """Gera um badge SVG com a taxa de sucesso, para colar em um README."""
    rate = stats.success_rate
    if stats.total_sessions == 0:
        color, text = "#94a3b8", "sem dados"
    elif rate >= 1.0:
        color, text = "#34d399", f"{stats.completed}/{stats.total_sessions} ok"
    elif rate >= 0.5:
        color, text = "#fbbf24", f"{stats.completed}/{stats.total_sessions} ok"
    else:
        color, text = "#f87171", f"{stats.completed}/{stats.total_sessions} ok"
    label, value = "caravana", f"{text} · {format_duration(stats.duration_s)}"
    label_width = 8 * len(label) + 20
    value_width = 7.2 * len(value) + 20
    total = label_width + value_width
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total:.0f}" height="20" role="img" aria-label="{label}: {value}">
  <title>{label}: {value}</title>
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r"><rect width="{total:.0f}" height="20" rx="3" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="{label_width:.0f}" height="20" fill="#0b1120"/>
    <rect x="{label_width:.0f}" width="{value_width:.0f}" height="20" fill="{color}"/>
    <rect width="{total:.0f}" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
    <text x="{label_width / 2:.0f}" y="14" fill="#010101" fill-opacity=".3">{label}</text>
    <text x="{label_width / 2:.0f}" y="13">{label}</text>
    <text x="{label_width + value_width / 2:.0f}" y="14" fill="#010101" fill-opacity=".3">{value}</text>
    <text x="{label_width + value_width / 2:.0f}" y="13">{value}</text>
  </g>
</svg>
"""


def build_manifest(
    *,
    ledger: Ledger,
    run: RunRecord,
    config: Config,
    stats: RunStats,
    engine_version: str,
) -> dict[str, Any]:
    """Monta o manifesto da execução: tudo que permite auditar o resultado depois.

    Inclui o hash de cada artefato. Se um arquivo for alterado após a execução,
    a comparação com o manifesto acusa a divergência.
    """
    artifacts = []
    for item in ledger.artifacts_for(run.id):
        path = Path(item["path"])
        entry = {
            "kind": item["kind"],
            "session": item["session_id"],
            "step": item["step_id"],
            "path": str(path),
            "name": path.name,
            "bytes": int(item["bytes"] or 0),
            "sha256": item["sha256"],
            "exists": path.exists(),
        }
        if path.exists() and not entry["sha256"]:
            entry["sha256"] = sha256_file(path)
        artifacts.append(entry)

    return {
        "schema": 1,
        "generated_at": utc_now().isoformat(),
        "engine": {"name": "caravana", "version": engine_version},
        "run": {
            "id": run.id,
            "status": run.status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "duration_s": run.duration_s,
            "run_dir": run.run_dir,
        },
        "flow": {
            "name": run.flow_name,
            "version": run.flow_version,
            "hash": run.flow_hash,
            "steps": [step.id for step in config.flow.steps],
        },
        "config": {
            "path": str(config.base_dir),
            "workers": config.workers,
            "max_attempts": config.max_attempts,
            "timeout_ms": config.timeout_ms,
            "screenshots": config.screenshots,
            "respect_robots": config.respect_robots,
            "sessions": [
                {"id": session.id, "count": session.count, "base_url": session.base_url}
                for session in config.sessions
            ],
        },
        "stats": stats.to_dict(),
        "artifacts": artifacts,
        "metadata": run.metadata,
    }


def manifest_summary(manifest: dict[str, Any]) -> str:
    """Resumo textual do manifesto (usado em ``caravana show --manifest``)."""
    return json.dumps(
        {
            "run": manifest["run"]["id"],
            "status": manifest["run"]["status"],
            "flow_hash": manifest["flow"]["hash"][:16],
            "artifacts": len(manifest["artifacts"]),
            "stats": manifest["stats"],
        },
        ensure_ascii=False,
        indent=2,
    )
