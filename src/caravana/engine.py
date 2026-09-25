"""Motor de orquestração: N sessões isoladas em paralelo, com retomada.

Responsabilidades:

1. **Planejar** — transformar a configuração em tarefas (uma por sessão),
   calculando o ponto de retomada de cada uma antes de qualquer navegador abrir.
2. **Executar** — rodar as tarefas em um pool de threads limitado por
   ``engine.workers``. Cada thread cria o próprio navegador e contexto.
3. **Encerrar com segurança** — ao receber ``Ctrl+C``, sinaliza as sessões,
   espera o encerramento limpo e marca a execução como ``interrupted``, para
   que a próxima execução continue de onde parou.
4. **Registrar** — histórico SQLite, artefatos com hash, manifesto e relatórios.

Nada de estado compartilhado entre sessões: o pool entrega a cada tarefa uma
configuração própria, e o resultado volta como dados simples.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import platform
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._version import __version__
from .config import Config, SessionConfig
from .errors import ResumeError
from .events import Event, EventBus
from .ledger import Ledger, RunStats
from .report import (
    build_badge_svg,
    build_manifest,
    build_report_html,
    build_report_markdown,
)
from .session import SessionRunner
from .templating import SecretStore
from .util import (
    atomic_write_json,
    new_run_id,
    sha256_text,
    utc_now,
)

__all__ = ["Engine", "RunPlan", "RunResult", "TaskPlan"]


@dataclass(slots=True)
class TaskPlan:
    """Uma unidade de trabalho: uma sessão isolada."""

    session_id: str
    session_config: SessionConfig
    index: int
    resume_from: str | None = None
    done_steps: list[str] = field(default_factory=list)
    failed_steps: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class RunPlan:
    """Plano completo de uma execução, exibível sem abrir navegador."""

    run_id: str
    flow_name: str
    flow_hash: str
    tasks: list[TaskPlan]
    workers: int
    resumed_from: str | None = None

    @property
    def total_sessions(self) -> int:
        """Quantidade de sessões que serão executadas."""
        return len(self.tasks)

    def to_dict(self) -> dict[str, Any]:
        """Serializa o plano (usado por ``--dry-run --json``)."""
        return {
            "run_id": self.run_id,
            "flow": self.flow_name,
            "flow_hash": self.flow_hash,
            "workers": self.workers,
            "resumed_from": self.resumed_from,
            "tasks": [
                {
                    "session": task.session_id,
                    "resume_from": task.resume_from,
                    "done": len(task.done_steps),
                    "failed_before": sorted(task.failed_steps),
                }
                for task in self.tasks
            ],
        }


@dataclass(slots=True)
class RunResult:
    """Resultado consolidado de uma execução."""

    run_id: str
    status: str
    stats: RunStats
    run_dir: Path
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    reports: dict[str, Path] = field(default_factory=dict)
    plan: RunPlan | None = None
    interrupted: bool = False

    @property
    def ok(self) -> bool:
        """Verdadeiro quando todas as sessões terminaram sem falha."""
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        """Serializa o resultado para ``--json``."""
        return {
            "run_id": self.run_id,
            "status": self.status,
            "run_dir": str(self.run_dir),
            "stats": self.stats.to_dict(),
            "sessions": self.sessions,
            "reports": {key: str(value) for key, value in self.reports.items()},
        }


class Engine:
    """Coordena uma execução completa."""

    def __init__(
        self,
        config: Config,
        *,
        run_dir: Path | None = None,
        ledger_path: Path | None = None,
        bus: EventBus | None = None,
        secrets: SecretStore | None = None,
    ) -> None:
        self.config = config
        self.base_run_dir = Path(run_dir) if run_dir else config.run_dir
        self.ledger_path = Path(ledger_path) if ledger_path else self.base_run_dir / "caravana.db"
        self.bus = bus or EventBus()
        self.secrets = secrets or SecretStore()
        self.flow_hash = sha256_text(
            json.dumps(config.flow.to_dict(), sort_keys=True, ensure_ascii=False)
        )

    # -- planejamento ------------------------------------------------------
    def plan(
        self,
        *,
        resume: str | None = None,
        sessions: list[str] | None = None,
        limit: int | None = None,
    ) -> RunPlan:
        """Monta o plano de execução, incluindo pontos de retomada.

        Raises:
            ResumeError: quando a execução indicada não existe ou foi produzida
                por um fluxo diferente (retomar com outro fluxo misturaria
                históricos e mascararia regressões).
        """
        run_id = new_run_id(self.config.flow.name)
        resume_plan: dict[str, str] = {}
        checkpoint: dict[str, dict[str, Any]] = {}
        if resume:
            with Ledger(self.ledger_path) as ledger:
                record = ledger.get_run(resume)
                if record is None:
                    raise ResumeError(f"execução '{resume}' não encontrada em {self.ledger_path}")
                if record.flow_hash != self.flow_hash:
                    raise ResumeError(
                        f"a execução '{record.id}' foi feita com outra versão do fluxo; "
                        "retomar assim misturaria resultados incompatíveis"
                    )
                resume_plan = ledger.resume_plan(record.id)
                checkpoint = ledger.checkpoint(record.id)
                run_id = record.id

        tasks: list[TaskPlan] = []
        for session_config in self.config.sessions:
            if sessions and session_config.id not in sessions:
                continue
            for index in range(session_config.count):
                session_id = session_config.label(index)
                entry = checkpoint.get(session_id, {})
                tasks.append(
                    TaskPlan(
                        session_id=session_id,
                        session_config=session_config,
                        index=index,
                        resume_from=resume_plan.get(session_id),
                        done_steps=list(entry.get("done", [])),
                        failed_steps=dict(entry.get("failed", {})),
                    )
                )
        if limit is not None:
            tasks = tasks[:limit]
        return RunPlan(
            run_id=run_id,
            flow_name=self.config.flow.name,
            flow_hash=self.flow_hash,
            tasks=tasks,
            workers=min(self.config.workers, max(1, len(tasks))),
            resumed_from=resume,
        )

    # -- execução ----------------------------------------------------------
    def run(
        self,
        *,
        resume: str | None = None,
        sessions: list[str] | None = None,
        limit: int | None = None,
        stop_event: threading.Event | None = None,
        keep_ledger_open: bool = False,
    ) -> RunResult:
        """Executa o plano e devolve o resultado consolidado."""
        plan = self.plan(resume=resume, sessions=sessions, limit=limit)
        run_dir = self.base_run_dir / plan.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        stop_event = stop_event or threading.Event()

        ledger = Ledger(self.ledger_path)
        try:
            if not resume:
                ledger.start_run(
                    plan.run_id,
                    flow_name=self.config.flow.name,
                    flow_version=self.config.flow.version,
                    flow_hash=self.flow_hash,
                    config_path=str(self.config.base_dir),
                    run_dir=str(run_dir),
                    sessions=plan.total_sessions,
                    workers=plan.workers,
                    metadata={
                        "flow_source": str(self.config.flow.source or ""),
                        "engine": __version__,
                        "python": platform.python_version(),
                        "platform": platform.platform(),
                    },
                )
            else:
                ledger.start_run(
                    plan.run_id,
                    flow_name=self.config.flow.name,
                    flow_version=self.config.flow.version,
                    flow_hash=self.flow_hash,
                    config_path=str(self.config.base_dir),
                    run_dir=str(run_dir),
                    sessions=plan.total_sessions,
                    workers=plan.workers,
                    metadata={
                        "flow_source": str(self.config.flow.source or ""),
                        "engine": __version__,
                        "resumed": True,
                    },
                )

            self.bus.publish(
                Event(
                    kind="run_started",
                    message=f"execução {plan.run_id} — {plan.total_sessions} sessão(ões), "
                    f"{plan.workers} worker(s)",
                    data={
                        "run_id": plan.run_id,
                        "run_dir": str(run_dir),
                        "resumed_from": resume or "",
                    },
                )
            )
            started = time.perf_counter()
            interrupted = self._execute_tasks(plan, run_dir, ledger, stop_event)
            elapsed = time.perf_counter() - started

            stats = ledger.stats(plan.run_id)
            stats.duration_s = elapsed
            results = ledger.session_results(plan.run_id)
            status = self._final_status(stats, interrupted)
            ledger.finish_run(plan.run_id, status)

            result = RunResult(
                run_id=plan.run_id,
                status=status,
                stats=stats,
                run_dir=run_dir,
                sessions=results,
                plan=plan,
                interrupted=interrupted,
            )
            result.reports = self._write_outputs(result, ledger)
            self.bus.publish(
                Event(
                    kind="run_finished",
                    message=f"execução {status}: {stats.completed}/{stats.total_sessions} "
                    f"sessão(ões) concluída(s) em {elapsed:.1f}s",
                    data=result.to_dict(),
                )
            )
            return result
        finally:
            if not keep_ledger_open:
                ledger.close()

    def _final_status(self, stats: RunStats, interrupted: bool) -> str:
        """Estado da execução, do mais grave para o menos grave.

        ``completed_with_warnings`` existe para não esconder duas coisas
        diferentes sob o mesmo rótulo: "tudo certo" e "o resultado saiu, mas com
        lacunas declaradas" (passos tolerados). O código de saída segue 0 nos
        dois casos; o relatório mostra a diferença.
        """
        if interrupted or stats.aborted:
            return "interrupted"
        if stats.failed:
            return "failed"
        if stats.degraded:
            return "completed_with_warnings"
        return "completed"

    def _execute_tasks(
        self,
        plan: RunPlan,
        run_dir: Path,
        ledger: Ledger,
        stop_event: threading.Event,
    ) -> bool:
        interrupted = False
        if not plan.tasks:
            return False

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=plan.workers, thread_name_prefix="caravana"
        ) as pool:
            futures = {
                pool.submit(self._run_task, task, plan, run_dir, ledger, stop_event): task
                for task in plan.tasks
            }
            try:
                for future in concurrent.futures.as_completed(futures):
                    task = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        self.bus.publish(
                            Event(
                                kind="session_finished",
                                session=task.session_id,
                                level="error",
                                message=f"sessão encerrada com erro: {exc}",
                                data={"status": "failed"},
                            )
                        )
            except KeyboardInterrupt:
                interrupted = True
                stop_event.set()
                self.bus.publish(
                    Event(
                        kind="log",
                        level="warning",
                        message="interrupção solicitada; aguardando encerramento das sessões…",
                    )
                )
                for future in futures:
                    try:
                        future.result(timeout=30)
                    except Exception:
                        continue
        return interrupted

    def _run_task(
        self,
        task: TaskPlan,
        plan: RunPlan,
        run_dir: Path,
        ledger: Ledger,
        stop_event: threading.Event,
    ) -> None:
        """Executa uma sessão. Roda em thread própria: nada aqui é compartilhado."""
        runner = SessionRunner(
            config=self.config,
            session_config=task.session_config,
            session_id=task.session_id,
            index=task.index,
            run_id=plan.run_id,
            run_dir=run_dir,
            ledger=ledger,
            bus=self.bus,
            secrets=self.secrets,
            stop_event=stop_event,
        )
        started = time.perf_counter()
        try:
            with runner:
                status = runner.run(resume_from=task.resume_from)
                runner.save_profile()
        except Exception as exc:
            runner.status = "failed"
            runner.error = str(exc)[:500]
            status = "failed"
            ledger.record_step(
                plan.run_id,
                session_id=task.session_id,
                step_id=task.resume_from or self.config.flow.order()[0],
                step_name=None,
                action="session",
                attempt=1,
                status="failed",
                started_at=utc_now().isoformat(),
                duration_ms=(time.perf_counter() - started) * 1000,
                error_type=type(exc).__name__,
                error_message=str(exc)[:2000],
            )
        finally:
            with contextlib.suppress(Exception):  # pragma: no cover - disco cheio
                runner.write_summary()
            self.bus.publish(
                Event(
                    kind="session_finished",
                    session=task.session_id,
                    level="error" if status == "failed" else "info",
                    duration_ms=(time.perf_counter() - started) * 1000,
                    message=f"sessão {status}",
                    data={"status": status, "resumed_from": task.resume_from or ""},
                )
            )

    # -- saídas ------------------------------------------------------------
    def _write_outputs(self, result: RunResult, ledger: Ledger) -> dict[str, Path]:
        reports: dict[str, Path] = {}
        run = ledger.get_run(result.run_id)
        if run is None:  # pragma: no cover - execução recém-criada
            return reports

        reports["summary"] = result.run_dir / "summary.json"
        atomic_write_json(reports["summary"], result.to_dict())

        reports["markdown"] = result.run_dir / "report.md"
        reports["markdown"].write_text(
            build_report_markdown(ledger, result.run_id, self.config), encoding="utf-8"
        )
        reports["html"] = result.run_dir / "report.html"
        reports["html"].write_text(
            build_report_html(ledger, result.run_id, self.config), encoding="utf-8"
        )
        reports["badge"] = result.run_dir / "badge.svg"
        reports["badge"].write_text(build_badge_svg(result.stats), encoding="utf-8")

        manifest = build_manifest(
            ledger=ledger,
            run=run,
            config=self.config,
            stats=result.stats,
            engine_version=__version__,
        )
        reports["manifest"] = result.run_dir / "manifest.json"
        atomic_write_json(reports["manifest"], manifest)
        return reports
