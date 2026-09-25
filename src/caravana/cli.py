"""Interface de linha de comando.

Comandos:

``run``      executa o fluxo (com ``--resume``, ``--session``, ``--limit``);
``plan``     mostra o plano sem abrir navegador;
``list``     lista execuções do histórico;
``show``     detalha uma execução, com opção de imprimir o manifesto;
``report``   regrava/abre o relatório de uma execução;
``validate`` valida configuração e fluxo;
``doctor``   checa ambiente (Python, Playwright, navegadores, diretórios);
``version``  versão do motor.

Todo comando aceita ``--json`` onde faz sentido, para uso em scripts e CI.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from ._version import __version__
from .config import load_config
from .engine import Engine
from .errors import CaravanaError, ResumeError
from .events import Event, EventBus, JsonlSink
from .ledger import Ledger
from .report import build_report_html, build_report_markdown, build_report_text
from .templating import SecretStore
from .util import format_duration, human_bytes, redact, sha256_file, utc_now

app = typer.Typer(
    name="caravana",
    help="Orquestrador resiliente de sessões de navegador isoladas.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)

_CONFIG_OPTION = typer.Option(
    "caravana.toml", "--config", "-c", help="Arquivo de configuração do fluxo."
)
_RUN_DIR_OPTION = typer.Option(
    None, "--run-dir", help="Diretório base de execuções (padrão: .caravana/runs)."
)
_JSON_OPTION = typer.Option(False, "--json", help="Saída em JSON, para scripts e CI.")


def _fail(message: str, code: int = 2) -> NoReturn:
    err_console.print(f"[bold red]erro:[/] {message}")
    raise typer.Exit(code)


def _load(config_path: Path, run_dir: Path | None) -> tuple[Any, Path]:
    config = load_config(config_path)
    base = Path(run_dir) if run_dir else config.run_dir
    if not base.is_absolute():
        base = (config.base_dir / base).resolve()
    config.run_dir = base
    return config, base


class _ProgressPanel:
    """Painel de progresso alimentado pelo barramento de eventos.

    A interface nunca toca no motor: ela apenas lê eventos publicados. Isso
    permite trocar a apresentação (painel, JSON Lines, log em arquivo) sem
    alterar a execução.
    """

    def __init__(self, total: int, quiet: bool = False) -> None:
        self.total = total
        self.quiet = quiet
        self.progress = Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=28),
            MofNCompleteColumn(),
            TextColumn("[dim]{task.fields[detail]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
            disable=quiet,
        )
        self.task_id = self.progress.add_task("caravana", total=total, detail="iniciando")
        self._sessions: dict[str, str] = {}

    def __enter__(self) -> _ProgressPanel:
        self.progress.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.progress.stop()

    def handle(self, event: Event) -> None:
        if self.quiet:
            return
        detail = ""
        if event.kind == "run_started":
            self.progress.update(self.task_id, detail="executando")
        elif event.kind == "session_started":
            self._sessions[event.session] = "ativa"
            detail = f"{len(self._sessions)} ativa(s)"
        elif event.kind == "step_ok":
            detail = f"{event.session} · {event.step}"
            if event.duration_ms:
                detail += f" ({event.duration_ms:.0f}ms)"
        elif event.kind == "step_retry":
            detail = f"{event.session} · {event.step} (tentativa {event.attempt})"
        elif event.kind == "step_skipped":
            detail = f"{event.session} · {event.step} ignorado"
        elif event.kind == "step_failed":
            detail = f"{event.session} · {event.step} falhou"
            self.progress.console.log(
                Text.from_markup(f"[red]{event.session}[/] · "), Text(event.message)
            )
        elif event.kind == "session_finished":
            self._sessions.pop(event.session, None)
            self.progress.advance(self.task_id)
            detail = f"{event.session} {event.data.get('status', '')}"
        elif event.kind == "artifact":
            detail = f"artefato: {event.data.get('path', '')}"
        self.progress.update(self.task_id, detail=detail or "…")


@app.command("version")
def version_command(json_output: bool = _JSON_OPTION) -> None:
    """Mostra a versão do motor."""
    if json_output:
        console.print_json(json.dumps({"name": "caravana", "version": __version__}))
        return
    console.print(f"caravana {__version__}")


@app.command("plan")
def plan_command(
    config_path: Path = _CONFIG_OPTION,
    run_dir: Path | None = _RUN_DIR_OPTION,
    resume: str | None = typer.Option(None, "--resume", help="Id da execução a retomar."),
    session: list[str] = typer.Option([], "--session", help="Limita a famílias de sessão."),
    limit: int | None = typer.Option(None, "--limit", help="Executa apenas N sessões."),
    json_output: bool = _JSON_OPTION,
) -> None:
    """Mostra o que seria executado, sem abrir navegador."""
    try:
        config, base = _load(config_path, run_dir)
        engine = Engine(config, run_dir=base)
        plan = engine.plan(resume=resume, sessions=list(session) or None, limit=limit)
    except CaravanaError as exc:
        _fail(str(exc))

    if json_output:
        console.print_json(json.dumps(plan.to_dict(), ensure_ascii=False))
        return

    table = Table(title=f"plano · {plan.flow_name}", show_lines=False)
    table.add_column("sessão", style="cyan")
    table.add_column("retoma de", style="yellow")
    table.add_column("concluídos", justify="right")
    table.add_column("falhas anteriores", justify="right")
    for task in plan.tasks:
        table.add_row(
            task.session_id,
            task.resume_from or "—",
            str(len(task.done_steps)),
            str(len(task.failed_steps)),
        )
    console.print(table)
    console.print(
        f"run id: [bold]{plan.run_id}[/] · workers: {plan.workers} · sessões: {plan.total_sessions}"
    )
    if plan.resumed_from:
        console.print(f"retomando de: [bold]{plan.resumed_from}[/]")


@app.command("run")
def run_command(
    config_path: Path = _CONFIG_OPTION,
    run_dir: Path | None = _RUN_DIR_OPTION,
    resume: str | None = typer.Option(
        None, "--resume", help="Retoma a execução indicada a partir do último passo concluído."
    ),
    resume_last: bool = typer.Option(
        False, "--resume-last", help="Retoma automaticamente a última execução interrompida."
    ),
    session: list[str] = typer.Option([], "--session", help="Limita a famílias de sessão."),
    limit: int | None = typer.Option(None, "--limit", help="Executa apenas N sessões."),
    var: list[str] = typer.Option([], "--var", help="Sobrescreve uma variável: nome=valor."),
    secrets_file: Path | None = typer.Option(
        None, "--secrets-file", help="Arquivo NOME=valor com segredos (não versionar)."
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Sem painel de progresso."),
    json_output: bool = _JSON_OPTION,
    log: Path | None = typer.Option(None, "--log", help="Grava eventos em JSON Lines."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Só mostra o plano."),
) -> None:
    """Executa o fluxo em sessões isoladas."""
    try:
        config, base = _load(config_path, run_dir)
    except CaravanaError as exc:
        _fail(str(exc))

    for item in var:
        if "=" not in item:
            _fail(f"--var espera nome=valor (recebido: {item})")
        name, value = item.split("=", 1)
        config.variables[name.strip()] = value

    secrets = SecretStore()
    if secrets_file:
        try:
            secrets.load_from_file(secrets_file)
        except CaravanaError as exc:
            _fail(str(exc))

    if dry_run:
        return plan_command(config_path, run_dir, resume, session, limit, json_output)

    bus = EventBus()
    log_sink: JsonlSink | None = None
    if log:
        log_sink = JsonlSink(log)
        bus.add_sink(log_sink)
    if json_output:
        bus.add_sink(_JsonSink())
    panel = None if json_output else _ProgressPanel(total=0, quiet=quiet)

    engine = Engine(config, run_dir=base, bus=bus, secrets=secrets)
    stop_event = threading.Event()
    previous = signal.getsignal(signal.SIGINT)

    def _handle_sigint(_signum: int, _frame: Any) -> None:  # pragma: no cover - interativo
        if stop_event.is_set():
            err_console.print("[red]segunda interrupção: encerrando imediatamente[/]")
            raise KeyboardInterrupt
        stop_event.set()
        err_console.print(
            "[yellow]interrupção recebida; encerrando sessões e salvando ponto de retomada…[/]"
        )

    signal.signal(signal.SIGINT, _handle_sigint)

    try:
        if resume_last and not resume:
            with Ledger(base / "caravana.db") as ledger:
                candidate = ledger.find_resumable()
            if candidate is None:
                _fail("nenhuma execução interrompida compatível encontrada", code=1)
            resume = candidate.id
            console.print(f"retomando execução [bold]{resume}[/]")

        plan = engine.plan(resume=resume, sessions=list(session) or None, limit=limit)
        if panel is not None:
            panel.total = plan.total_sessions
            panel.progress.update(panel.task_id, total=plan.total_sessions)

        if not quiet and not json_output:
            console.print(
                Panel(
                    _describe(config, plan.total_sessions, plan.workers),
                    title=f"[bold cyan]caravana[/] {config.flow.name}",
                    border_style="cyan",
                )
            )

        with _BusPump(bus, panel):
            result = engine.run(
                resume=resume,
                sessions=list(session) or None,
                limit=limit,
                stop_event=stop_event,
            )
    except ResumeError as exc:
        _fail(str(exc), code=3)
    except CaravanaError as exc:
        _fail(str(exc))
    except KeyboardInterrupt:  # pragma: no cover - interativo
        _fail("execução cancelada", code=130)
    finally:
        signal.signal(signal.SIGINT, previous)
        if log_sink is not None:
            log_sink.close()

    if not json_output:
        _print_result(result)
    raise typer.Exit(0 if result.ok else 1)


class _JsonSink:
    """Escreve eventos como JSON Lines em stdout."""

    def handle(self, event: Event) -> None:
        payload = {"ts": event.timestamp, **event.to_dict()}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def close(self) -> None:
        return None


class _BusPump:
    """Consome o barramento enquanto a execução roda, atualizando o painel."""

    def __init__(self, bus: EventBus, panel: _ProgressPanel | None) -> None:
        self.bus = bus
        self.panel = panel
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _BusPump:
        if self.panel is not None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.bus.close()

    def _loop(self) -> None:
        assert self.panel is not None
        with self.panel:
            while not self._stop.is_set():
                for event in self.bus.drain(timeout=0.1):
                    self.panel.handle(event)
                if not self._stop.is_set():
                    time.sleep(0.02)


def _describe(config: Any, sessions: int, workers: int) -> str:
    lines = [
        f"fluxo      [bold]{config.flow.name}[/] v{config.flow.version} · {len(config.flow.steps)} passo(s)",
        f"sessões    {sessions} (workers: {workers})",
        f"tentativas {config.max_attempts} por passo · timeout {config.timeout_ms / 1000:.0f}s",
        f"artefatos  screenshots: {config.screenshots} · robots: "
        f"{'respeitado' if config.respect_robots else 'ignorado'}",
    ]
    if config.base_url:
        label = config.base_url if "${" not in config.base_url else f"{config.base_url} (template)"
        lines.append(f"base url   {label}")
    return "\n".join(lines)


def _print_result(result: Any) -> None:
    stats = result.stats
    style = "green" if result.ok else ("yellow" if result.interrupted else "red")
    summary = (
        f"sessões  {stats.completed} ok · {stats.degraded} degradada(s) · {stats.failed} falha · "
        f"{stats.aborted} interrompida\n"
        f"passos   {stats.steps_ok} ok · {stats.steps_failed} falha · {stats.steps_retried} retry\n"
        f"duração  {format_duration(stats.duration_s)}\n"
        f"arquivos {stats.artifacts} ({human_bytes(stats.bytes_written)})"
    )
    console.print(
        Panel(summary, title=f"[bold]{result.status}[/]", border_style=style, expand=False)
    )
    if result.reports:
        console.print("[dim]relatórios:[/]")
        for name, path in result.reports.items():
            console.print(f"  [cyan]{name:9}[/] {path}")
    if result.interrupted:
        console.print(
            f"[yellow]execução interrompida; retome com:[/] caravana run --resume {result.run_id}"
        )


@app.command("list")
def list_command(
    run_dir: Path | None = _RUN_DIR_OPTION,
    limit: int = typer.Option(15, "--limit", "-n", help="Quantidade de execuções."),
    status: str | None = typer.Option(None, "--status", help="Filtra por estado."),
    json_output: bool = _JSON_OPTION,
) -> None:
    """Lista as execuções registradas no histórico."""
    base = Path(run_dir) if run_dir else Path(".caravana/runs")
    ledger_path = base / "caravana.db"
    if not ledger_path.exists():
        if json_output:
            console.print_json("[]")
            return
        console.print("[yellow]nenhuma execução registrada ainda[/]")
        return
    with Ledger(ledger_path) as ledger:
        runs = ledger.list_runs(limit=limit, status=status)
        if json_output:
            console.print_json(json.dumps([run.to_dict() for run in runs], ensure_ascii=False))
            return
        if not runs:
            console.print("[yellow]nenhuma execução encontrada[/]")
            return
        table = Table(title="execuções", show_lines=False)
        table.add_column("run id", style="cyan", no_wrap=True)
        table.add_column("fluxo")
        table.add_column("estado")
        table.add_column("sessões", justify="right")
        table.add_column("início")
        table.add_column("duração", justify="right")
        for run in runs:
            color = {"completed": "green", "failed": "red"}.get(run.status, "yellow")
            table.add_row(
                run.id,
                run.flow_name,
                f"[{color}]{run.status}[/]",
                str(run.sessions),
                run.started_at.replace("T", " ")[:19],
                format_duration(run.duration_s or 0.0),
            )
        console.print(table)


@app.command("show")
def show_command(
    run_id: str = typer.Argument(None, help="Id da execução (aceita prefixo único)."),
    run_dir: Path | None = _RUN_DIR_OPTION,
    json_output: bool = _JSON_OPTION,
    manifest: bool = typer.Option(False, "--manifest", help="Imprime o manifesto completo."),
) -> None:
    """Mostra o resultado detalhado de uma execução."""
    base = Path(run_dir) if run_dir else Path(".caravana/runs")
    ledger_path = base / "caravana.db"
    if not ledger_path.exists():
        _fail(f"histórico não encontrado em {ledger_path}")
    with Ledger(ledger_path) as ledger:
        latest = ledger.latest_run()
        target = run_id or (latest.id if latest is not None else None)
        if target is None:
            _fail("nenhuma execução registrada", code=1)
        run = ledger.get_run(target)
        if run is None:
            _fail(f"execução '{target}' não encontrada", code=1)
        if manifest:
            path = Path(run.run_dir) / "manifest.json"
            if not path.exists():
                _fail(f"manifesto não encontrado: {path}", code=1)
            console.print_json(path.read_text("utf-8"))
            return
        if json_output:
            console.print_json(
                json.dumps(
                    {
                        "run": run.to_dict(),
                        "stats": ledger.stats(run.id).to_dict(),
                        "sessions": ledger.session_results(run.id),
                        "steps": [step.to_dict() for step in ledger.steps_for(run.id)],
                    },
                    ensure_ascii=False,
                )
            )
            return
        console.print(build_report_text(ledger, run.id))
        steps = ledger.steps_for(run.id)
        if steps:
            table = Table(title="passos", show_lines=False)
            table.add_column("sessão", style="cyan")
            table.add_column("passo")
            table.add_column("ação")
            table.add_column("tent.", justify="right")
            table.add_column("estado")
            table.add_column("duração", justify="right")
            for record in steps:
                color = {"ok": "green", "failed": "red", "skipped": "dim"}.get(record.status, "")
                table.add_row(
                    record.session_id,
                    record.step_id,
                    record.action,
                    str(record.attempt),
                    f"[{color}]{record.status}[/]",
                    format_duration((record.duration_ms or 0) / 1000),
                )
            console.print(table)


@app.command("report")
def report_command(
    run_id: str = typer.Argument(None, help="Id da execução."),
    run_dir: Path | None = _RUN_DIR_OPTION,
    config_path: Path = _CONFIG_OPTION,
    open_html: bool = typer.Option(False, "--open", help="Abre o HTML no navegador padrão."),
    format_: str = typer.Option(
        "markdown", "--format", "-f", help="Formato: markdown, html ou text."
    ),
) -> None:
    """Regrava os relatórios de uma execução a partir do histórico."""
    base = Path(run_dir) if run_dir else Path(".caravana/runs")
    ledger_path = base / "caravana.db"
    if not ledger_path.exists():
        _fail(f"histórico não encontrado em {ledger_path}")
    try:
        config = load_config(config_path)
    except CaravanaError:
        config = None
    with Ledger(ledger_path) as ledger:
        latest = ledger.latest_run()
        target = run_id or (latest.id if latest is not None else None)
        if target is None:
            _fail("nenhuma execução registrada", code=1)
        run = ledger.get_run(target)
        if run is None:
            _fail(f"execução '{target}' não encontrada", code=1)
        out_dir = Path(run.run_dir)
        markdown = build_report_markdown(ledger, run.id, config)
        html_report = build_report_html(ledger, run.id, config)
        (out_dir / "report.md").write_text(markdown, "utf-8")
        (out_dir / "report.html").write_text(html_report, "utf-8")
        console.print(f"relatórios regravados em [cyan]{out_dir}[/]")
        if format_ == "html":
            console.print(str(out_dir / "report.html"))
        elif format_ == "text":
            console.print(build_report_text(ledger, run.id))
        else:
            console.print(str(out_dir / "report.md"))
    if open_html:  # pragma: no cover - depende do ambiente do usuário
        webbrowser.open((out_dir / "report.html").as_uri())


@app.command("validate")
def validate_command(
    config_path: Path = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
    mermaid: bool = typer.Option(False, "--mermaid", help="Imprime o fluxo em Mermaid."),
) -> None:
    """Valida o arquivo de configuração e o fluxo, sem executar."""
    try:
        config = load_config(config_path)
    except CaravanaError as exc:
        if json_output:
            console.print_json(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            raise typer.Exit(1) from None
        _fail(str(exc), code=1)

    if mermaid:
        console.print(config.flow.mermaid())
        return
    if json_output:
        console.print_json(json.dumps({"ok": True, **config.describe()}, ensure_ascii=False))
        return
    table = Table(title=f"configuração válida · {config_path}", show_lines=False)
    table.add_column("chave", style="cyan")
    table.add_column("valor")
    for key, value in config.describe().items():
        table.add_row(key, str(value))
    console.print(table)
    console.print("[green]fluxo e configuração consistentes[/]")


@app.command("doctor")
def doctor_command(
    config_path: Path | None = typer.Option(None, "--config", "-c", help="Configuração opcional."),
    json_output: bool = _JSON_OPTION,
) -> None:
    """Verifica o ambiente: Python, dependências, navegadores e diretórios."""
    checks: list[tuple[str, bool, str]] = []
    python_ok = sys.version_info >= (3, 10)
    checks.append(("python", python_ok, f"{sys.version.split()[0]} (mínimo 3.10)"))

    # A ausência do Playwright é um *resultado* do diagnóstico, não um erro:
    # por isso a verificação usa importlib em vez de importar o pacote no topo.
    if importlib.util.find_spec("playwright") is None:
        checks.append(("playwright", False, "instale com: pip install caravana[playwright]"))
    else:
        module = importlib.import_module("playwright")
        checks.append(("playwright", True, f"pacote {getattr(module, '__version__', '?')}"))
    try:
        sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
        with sync_playwright() as playwright_api:
            browser = playwright_api.chromium.launch(headless=True)
            version = browser.version
            browser.close()
        checks.append(("chromium", True, f"navegador {version}"))
    except Exception as exc:
        checks.append(("chromium", False, f"{type(exc).__name__}: {str(exc)[:80]}"))

    writable = Path(".caravana")
    try:
        writable.mkdir(parents=True, exist_ok=True)
        probe = writable / ".doctor"
        probe.write_text("ok", "utf-8")
        probe.unlink()
        checks.append(("diretório", True, f"{writable.resolve()} gravável"))
    except OSError as exc:
        checks.append(("diretório", False, str(exc)[:80]))

    if config_path:
        try:
            config = load_config(config_path)
            sessions = sum(session.count for session in config.sessions)
            checks.append(
                (
                    "configuração",
                    True,
                    f"{config.flow.name}: {len(config.flow.steps)} passos, {sessions} sessão(ões)",
                )
            )
        except CaravanaError as exc:
            checks.append(("configuração", False, str(exc)[:120]))

    secrets = SecretStore()
    checks.append(
        (
            "segredos",
            True,
            f"{len(secrets.names())} definido(s)"
            + (f": {', '.join(secrets.names())}" if secrets.names() else ""),
        )
    )
    disk = shutil.disk_usage(".")
    checks.append(("disco", disk.free > 500 * 1024 * 1024, f"{human_bytes(disk.free)} livres"))

    if json_output:
        console.print_json(
            json.dumps(
                {
                    "ok": all(ok for _, ok, _ in checks),
                    "checks": [
                        {"name": name, "ok": ok, "detail": detail} for name, ok, detail in checks
                    ],
                },
                ensure_ascii=False,
            )
        )
    else:
        table = Table(title="caravana doctor", show_lines=False)
        table.add_column("item", style="cyan")
        table.add_column("estado")
        table.add_column("detalhe")
        for name, ok, detail in checks:
            table.add_row(name, "[green]ok[/]" if ok else "[red]falha[/]", detail)
        console.print(table)
    if not all(ok for _, ok, _ in checks):
        raise typer.Exit(1)


@app.command("verify")
def verify_command(
    run_id: str = typer.Argument(None, help="Id da execução a verificar."),
    run_dir: Path | None = _RUN_DIR_OPTION,
) -> None:
    """Confere os artefatos de uma execução contra os hashes do manifesto."""
    base = Path(run_dir) if run_dir else Path(".caravana/runs")
    ledger_path = base / "caravana.db"
    if not ledger_path.exists():
        _fail(f"histórico não encontrado em {ledger_path}")
    with Ledger(ledger_path) as ledger:
        latest = ledger.latest_run()
        target = run_id or (latest.id if latest is not None else None)
        if target is None:
            _fail("nenhuma execução registrada", code=1)
        run = ledger.get_run(target)
        if run is None:
            _fail(f"execução '{target}' não encontrada", code=1)
        manifest_path = Path(run.run_dir) / "manifest.json"
        if not manifest_path.exists():
            _fail(f"manifesto não encontrado: {manifest_path}", code=1)
        manifest = json.loads(manifest_path.read_text("utf-8"))
        problems = 0
        table = Table(title=f"verificação · {run.id}", show_lines=False)
        table.add_column("arquivo", style="cyan")
        table.add_column("estado")
        table.add_column("sha256", no_wrap=True)
        for entry in manifest.get("artifacts", []):
            path = Path(entry["path"])
            if not path.exists():
                table.add_row(path.name, "[red]ausente[/]", "—")
                problems += 1
                continue
            digest = sha256_file(path)
            if entry.get("sha256") and digest != entry["sha256"]:
                table.add_row(path.name, "[red]alterado[/]", digest[:16])
                problems += 1
            else:
                table.add_row(path.name, "[green]íntegro[/]", digest[:16])
        console.print(table)
    if problems:
        _fail(f"{problems} artefato(s) divergente(s)", code=1)
    console.print("[green]todos os artefatos conferem com o manifesto[/]")


def main() -> None:
    """Ponto de entrada do console script."""
    try:
        app()
    except CaravanaError as exc:  # pragma: no cover - caminho de erro global
        err_console.print(f"[bold red]erro:[/] {exc}")
        sys.exit(2)


if __name__ == "__main__":  # pragma: no cover
    main()


# utilitário exposto para uso programático (não é comando)
def _sanitize(text: str) -> str:
    return redact(text, SecretStore().values())


_ = (os, utc_now)
