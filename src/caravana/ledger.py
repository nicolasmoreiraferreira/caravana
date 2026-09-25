"""Histórico de execuções em SQLite (o "ledger").

Cada execução é uma linha em ``runs``; cada tentativa de passo é uma linha em
``steps``. O banco é a fonte da verdade para retomada e para os relatórios: um
processo interrompido no meio (``Ctrl+C``, queda de energia, ``kill -9``) deixa
o ledger consistente porque as transações são curtas e confirmadas a cada passo.

Decisões de projeto:

* modo WAL, para que ``caravana report`` leia enquanto uma execução grava;
* um único escritor (o motor) e leitores livres, sem ORM e sem servidor;
* nenhum dado sensível é gravado: valores de ``save_as`` passam por
  :func:`caravana.util.redact` antes de chegar aqui.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import LedgerError
from .util import utc_now

__all__ = ["Ledger", "RunRecord", "RunStats", "StepRecord"]

_SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    flow_name     TEXT NOT NULL,
    flow_version  TEXT NOT NULL,
    flow_hash     TEXT NOT NULL,
    config_path   TEXT NOT NULL,
    run_dir       TEXT NOT NULL,
    status        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    sessions      INTEGER NOT NULL DEFAULT 0,
    workers       INTEGER NOT NULL DEFAULT 1,
    generation    INTEGER NOT NULL DEFAULT 1,
    metadata      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS steps (
    run_id        TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    session_id    TEXT NOT NULL,
    step_id       TEXT NOT NULL,
    step_name     TEXT,
    action        TEXT NOT NULL,
    attempt       INTEGER NOT NULL,
    generation    INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    duration_ms   REAL,
    error_type    TEXT,
    error_message TEXT,
    data          TEXT,
    artifacts     TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id, generation, session_id, step_id, attempt)
);

CREATE TABLE IF NOT EXISTS artifacts (
    run_id     TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    step_id    TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,
    kind       TEXT NOT NULL,
    path       TEXT NOT NULL,
    bytes      INTEGER NOT NULL DEFAULT 0,
    sha256     TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id, session_id);
CREATE INDEX IF NOT EXISTS idx_steps_generation ON steps(run_id, generation);
CREATE INDEX IF NOT EXISTS idx_steps_status ON steps(run_id, status);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
"""


@dataclass(slots=True)
class RunRecord:
    """Uma execução registrada no histórico."""

    id: str
    flow_name: str
    flow_version: str
    flow_hash: str
    config_path: str
    run_dir: str
    status: str
    started_at: str
    finished_at: str | None
    sessions: int
    workers: int
    generation: int
    metadata: dict[str, Any]

    @property
    def started(self) -> datetime:
        """Início como ``datetime`` (UTC)."""
        return datetime.fromisoformat(self.started_at)

    @property
    def duration_s(self) -> float | None:
        """Duração em segundos, quando a execução terminou."""
        if not self.finished_at:
            return None
        return (datetime.fromisoformat(self.finished_at) - self.started).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        """Serializa para JSON (CLI ``--json``)."""
        payload = {
            "id": self.id,
            "flow": self.flow_name,
            "flow_version": self.flow_version,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": self.duration_s,
            "sessions": self.sessions,
            "workers": self.workers,
            "generation": self.generation,
            "run_dir": self.run_dir,
        }
        payload.update(self.metadata)
        return payload


@dataclass(slots=True)
class StepRecord:
    """Uma tentativa de passo registrada."""

    session_id: str
    step_id: str
    step_name: str | None
    action: str
    attempt: int
    status: str
    started_at: str
    finished_at: str | None
    duration_ms: float | None
    error_type: str | None
    error_message: str | None
    data: Any
    artifacts: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Serializa para JSON."""
        return {
            "session": self.session_id,
            "step": self.step_id,
            "step_name": self.step_name,
            "action": self.action,
            "attempt": self.attempt,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "data": self.data,
            "artifacts": self.artifacts,
        }


@dataclass(slots=True)
class RunStats:
    """Números agregados de uma execução, usados em relatórios e no README."""

    total_sessions: int = 0
    completed: int = 0
    degraded: int = 0
    failed: int = 0
    aborted: int = 0
    steps_ok: int = 0
    steps_failed: int = 0
    steps_retried: int = 0
    steps_skipped: int = 0
    duration_s: float = 0.0
    artifacts: int = 0
    bytes_written: int = 0

    @property
    def success_rate(self) -> float:
        """Fração de sessões concluídas sem falha definitiva.

        Sessões degradadas (que perderam passos tolerados) contam como sucesso:
        o resultado foi entregue, com lacunas declaradas no relatório.
        """
        finished = self.completed + self.degraded + self.failed + self.aborted
        return ((self.completed + self.degraded) / finished) if finished else 0.0

    @property
    def steps_total(self) -> int:
        """Total de tentativas de passo registradas."""
        return self.steps_ok + self.steps_failed

    def to_dict(self) -> dict[str, Any]:
        """Serializa para JSON."""
        return {
            "sessions": self.total_sessions,
            "completed": self.completed,
            "degraded": self.degraded,
            "failed": self.failed,
            "aborted": self.aborted,
            "steps_total": self.steps_total,
            "steps_ok": self.steps_ok,
            "steps_failed": self.steps_failed,
            "steps_retried": self.steps_retried,
            "steps_skipped": self.steps_skipped,
            "duration_s": self.duration_s,
            "artifacts": self.artifacts,
            "bytes_written": self.bytes_written,
            "success_rate": self.success_rate,
        }


def _classify_session(records: list[StepRecord]) -> tuple[list[str], bool]:
    """Classifica uma sessão a partir do histórico de tentativas.

    A classificação usa a **última tentativa de cada passo**, não todas: uma
    falha seguida de sucesso é uma nova tentativa bem-sucedida, não uma falha
    definitiva.

    Returns:
        ``(passos_com_falha_definitiva, houve_sucesso_depois_da_última_falha)``.
        O segundo valor distingue "o fluxo seguiu" (degradada) de "o fluxo
        parou aqui" (falhada).
    """
    last_by_step: dict[str, StepRecord] = {}
    for record in records:
        last_by_step[record.step_id] = record
    failed_steps = [
        step_id for step_id, record in last_by_step.items() if record.status == "failed"
    ]
    if not failed_steps:
        return [], False
    last_failure = max(
        (last_by_step[step_id] for step_id in failed_steps), key=lambda row: row.started_at
    )
    has_ok_after = any(
        record.status == "ok" and record.started_at > last_failure.started_at for record in records
    )
    return failed_steps, has_ok_after


class Ledger:
    """Acesso ao histórico SQLite.

    A conexão é compartilhada entre as threads de sessão (``check_same_thread=False``)
    e todo acesso passa por um ``RLock``: o SQLite serializa escritas de qualquer
    forma, e a trava evita que duas threads intercalem comandos na mesma conexão
    — um modo de falha silencioso e difícil de diagnosticar.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        self._conn.commit()

    def _migrate(self) -> None:
        """Adiciona colunas novas a bancos criados por versões anteriores.

        A migração é aditiva: nenhuma coluna é removida e nenhum dado é
        reescrito. Um histórico antigo continua legível e passa a reportar
        geração 1, que é exatamente o que ele é.
        """
        for table, column, definition in (
            ("runs", "generation", "INTEGER NOT NULL DEFAULT 1"),
            ("steps", "generation", "INTEGER NOT NULL DEFAULT 1"),
            ("artifacts", "generation", "INTEGER NOT NULL DEFAULT 1"),
        ):
            existing = {
                row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in existing:  # pragma: no cover - caminho de migração
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                self._conn.commit()

    # -- ciclo de vida -----------------------------------------------------
    def close(self) -> None:
        """Fecha a conexão."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except sqlite3.Error as exc:  # pragma: no cover - falha de disco
                self._conn.rollback()
                raise LedgerError(f"falha ao gravar no histórico: {exc}") from exc

    def _query(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[sqlite3.Row]:
        """Executa uma leitura sob a mesma trava usada pelas escritas."""
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    # -- execuções ---------------------------------------------------------
    def start_run(
        self,
        run_id: str,
        *,
        flow_name: str,
        flow_version: str,
        flow_hash: str,
        config_path: str,
        run_dir: str,
        sessions: int,
        workers: int,
        metadata: dict[str, Any] | None = None,
        resumed_from: str | None = None,
    ) -> RunRecord:
        """Registra o início de uma execução (ou de uma retomada)."""
        started = utc_now().isoformat()
        meta = dict(metadata or {})
        if resumed_from:
            meta["resumed_from"] = resumed_from
        existing = self.get_run(run_id)
        if existing is not None:
            # Retomada: o id já existe. Em vez de recriar a linha (o que
            # apagaria o histórico da tentativa anterior), reabrimos a mesma
            # execução e registramos quantas vezes ela foi retomada.
            previous = existing.metadata or {}
            meta = {**previous, **meta}
            meta["resume_count"] = int(previous.get("resume_count", 0)) + 1
            generation = existing.generation + 1
            with self._tx() as conn:
                conn.execute(
                    """
                    UPDATE runs SET status = 'running', started_at = ?, finished_at = NULL,
                                     sessions = ?, workers = ?, generation = ?, metadata = ?
                    WHERE id = ?
                    """,
                    (
                        started,
                        sessions,
                        workers,
                        generation,
                        json.dumps(meta, ensure_ascii=False),
                        run_id,
                    ),
                )
            reopened = self.get_run(run_id)
            assert reopened is not None  # acabou de ser atualizado
            return reopened
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, flow_name, flow_version, flow_hash, config_path, run_dir,
                                  status, started_at, sessions, workers, generation, metadata)
                VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, 1, ?)
                """,
                (
                    run_id,
                    flow_name,
                    flow_version,
                    flow_hash,
                    config_path,
                    run_dir,
                    started,
                    sessions,
                    workers,
                    json.dumps(meta, ensure_ascii=False),
                ),
            )
        return RunRecord(
            id=run_id,
            flow_name=flow_name,
            flow_version=flow_version,
            flow_hash=flow_hash,
            config_path=config_path,
            run_dir=run_dir,
            status="running",
            started_at=started,
            finished_at=None,
            sessions=sessions,
            workers=workers,
            generation=1,
            metadata=meta,
        )

    def generation(self, run_id: str) -> int:
        """Geração ativa de uma execução (1 na primeira passada, +1 por retomada)."""
        rows = self._query("SELECT generation FROM runs WHERE id = ?", (run_id,))
        return int(rows[0]["generation"]) if rows else 1

    def finish_run(self, run_id: str, status: str) -> None:
        """Marca o fim da execução com o estado final."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE id = ?",
                (status, utc_now().isoformat(), run_id),
            )

    def get_run(self, run_id: str) -> RunRecord | None:
        """Busca uma execução pelo id (aceita prefixo único, como o Git)."""
        rows = self._query("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not rows:
            matches = self._query(
                "SELECT * FROM runs WHERE id LIKE ? ORDER BY started_at DESC LIMIT 2",
                (f"{run_id}%",),
            )
            if len(matches) != 1:
                return None
            rows = matches
        return self._run_from_row(rows[0])

    def list_runs(self, limit: int = 20, status: str | None = None) -> list[RunRecord]:
        """Lista execuções recentes, opcionalmente filtrando por estado."""
        query = "SELECT * FROM runs"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        return [self._run_from_row(row) for row in self._query(query, params)]

    def latest_run(self) -> RunRecord | None:
        """Execução mais recente, qualquer estado."""
        runs = self.list_runs(limit=1)
        return runs[0] if runs else None

    def find_resumable(self, flow_hash: str | None = None) -> RunRecord | None:
        """Última execução interrompida compatível com o fluxo informado."""
        rows = self._query(
            "SELECT * FROM runs WHERE status IN ('interrupted', 'failed') "
            "ORDER BY started_at DESC LIMIT 20"
        )
        for row in rows:
            record = self._run_from_row(row)
            if flow_hash is None or record.flow_hash == flow_hash:
                return record
        return None

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            flow_name=row["flow_name"],
            flow_version=row["flow_version"],
            flow_hash=row["flow_hash"],
            config_path=row["config_path"],
            run_dir=row["run_dir"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            sessions=row["sessions"],
            workers=row["workers"],
            generation=int(row["generation"] or 1),
            metadata=json.loads(row["metadata"] or "{}"),
        )

    # -- passos ------------------------------------------------------------
    def record_step(
        self,
        run_id: str,
        *,
        session_id: str,
        step_id: str,
        step_name: str | None,
        action: str,
        attempt: int,
        status: str,
        started_at: str,
        duration_ms: float,
        error_type: str | None = None,
        error_message: str | None = None,
        data: Any = None,
        artifacts: list[str] | None = None,
    ) -> None:
        """Grava o resultado de uma tentativa de passo."""
        generation = self.generation(run_id)
        with self._tx() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO steps (run_id, session_id, step_id, step_name, action,
                                              attempt, generation, status, started_at, finished_at,
                                              duration_ms, error_type, error_message, data, artifacts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    session_id,
                    step_id,
                    step_name,
                    action,
                    attempt,
                    generation,
                    status,
                    started_at,
                    utc_now().isoformat(),
                    duration_ms,
                    error_type,
                    error_message,
                    json.dumps(data, ensure_ascii=False, default=str) if data is not None else None,
                    json.dumps(artifacts or [], ensure_ascii=False),
                ),
            )

    def steps_for(
        self,
        run_id: str,
        session_id: str | None = None,
        *,
        generation: int | None = None,
    ) -> list[StepRecord]:
        """Lista as tentativas registradas, na ordem de execução.

        Args:
            run_id: execução.
            session_id: limita a uma sessão.
            generation: geração específica. Por padrão, a geração ativa — assim
                uma execução retomada relata o estado atual, e não a soma das
                tentativas anteriores.
        """
        active = self.generation(run_id) if generation is None else generation
        query = "SELECT * FROM steps WHERE run_id = ? AND generation = ?"
        params: list[Any] = [run_id, active]
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        query += " ORDER BY started_at, attempt"
        rows = self._query(query, params)
        return [
            StepRecord(
                session_id=row["session_id"],
                step_id=row["step_id"],
                step_name=row["step_name"],
                action=row["action"],
                attempt=row["attempt"],
                status=row["status"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                duration_ms=row["duration_ms"],
                error_type=row["error_type"],
                error_message=row["error_message"],
                data=json.loads(row["data"]) if row["data"] else None,
                artifacts=json.loads(row["artifacts"] or "[]"),
            )
            for row in rows
        ]

    def last_attempt(self, run_id: str, session_id: str, step_id: str) -> StepRecord | None:
        """Última tentativa de um passo, usada no ponto de retomada."""
        rows = self.steps_for(run_id, session_id)
        matching = [row for row in rows if row.step_id == step_id]
        return matching[-1] if matching else None

    def checkpoint(self, run_id: str) -> dict[str, Any]:
        """Estado de retomada por sessão: onde parar e o que já foi concluído.

        Retorna ``{session_id: {"done": [passos concluídos], "failed": {passo: mensagem}}}``.
        Passos marcados como ``skipped`` também contam como concluídos, para que
        a retomada não repita trabalho descartado de propósito.
        """
        state: dict[str, dict[str, Any]] = {}
        for row in self.steps_for(run_id):
            entry = state.setdefault(row.session_id, {"done": [], "failed": {}})
            if row.status in {"ok", "skipped"} and row.step_id not in entry["done"]:
                entry["done"].append(row.step_id)
            if row.status == "failed":
                entry["failed"][row.step_id] = row.error_message or row.error_type or "falha"
        return state

    def resume_plan(self, run_id: str) -> dict[str, str]:
        """Para cada sessão, o próximo passo ainda não concluído.

        Uma sessão cujo último passo concluído é o passo de entrada reinicia do
        zero; uma sessão que parou no meio continua dali.
        """
        state = self.checkpoint(run_id)
        return {session: entry["done"][-1] for session, entry in state.items() if entry["done"]}

    # -- artefatos ---------------------------------------------------------
    def record_artifact(
        self,
        run_id: str,
        *,
        session_id: str,
        step_id: str,
        kind: str,
        path: str,
        size: int = 0,
        digest: str | None = None,
    ) -> None:
        """Registra um artefato produzido (screenshot, HTML, download, trace)."""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO artifacts (run_id, session_id, step_id, generation, kind, path, bytes,
                                       sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    session_id,
                    step_id,
                    self.generation(run_id),
                    kind,
                    path,
                    size,
                    digest,
                    utc_now().isoformat(),
                ),
            )

    def artifacts_for(
        self, run_id: str, kind: str | None = None, *, all_generations: bool = True
    ) -> list[dict[str, Any]]:
        """Lista os artefatos de uma execução.

        Por padrão inclui **todas** as gerações: evidência de uma tentativa
        anterior continua sendo evidência e não deve desaparecer quando a
        execução é retomada.
        """
        query = "SELECT * FROM artifacts WHERE run_id = ?"
        params: list[Any] = [run_id]
        if not all_generations:
            query += " AND generation = ?"
            params.append(self.generation(run_id))
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY created_at"
        return [dict(row) for row in self._query(query, params)]

    # -- agregações --------------------------------------------------------
    def stats(self, run_id: str) -> RunStats:
        """Calcula os números agregados de uma execução."""
        stats = RunStats()
        run = self.get_run(run_id)
        if run is None:
            return stats
        stats.duration_s = run.duration_s or 0.0

        rows = self.steps_for(run_id)
        sessions: dict[str, list[StepRecord]] = {}
        # Os contadores são por *passo*, não por tentativa. Um passo que falhou
        # e foi recuperado no retry é um passo bem-sucedido: contá-lo também
        # como falha faria o relatório dizer "12 ok, 6 com falha definitiva"
        # para uma execução em que nada falhou de verdade.
        by_step: dict[tuple[str, str], StepRecord] = {}
        for row in rows:
            sessions.setdefault(row.session_id, []).append(row)
            key = (row.session_id, row.step_id)
            previous = by_step.get(key)
            if previous is None or row.attempt >= previous.attempt:
                by_step[key] = row
            if row.attempt > 1:
                stats.steps_retried += 1

        for record in by_step.values():
            if record.status == "ok":
                stats.steps_ok += 1
            elif record.status == "failed":
                stats.steps_failed += 1
            elif record.status == "skipped":
                stats.steps_skipped += 1

        stats.total_sessions = len(sessions)
        for records in sessions.values():
            failed_steps, has_ok_after = _classify_session(records)
            if records[-1].status == "aborted":
                stats.aborted += 1
            elif not failed_steps:
                stats.completed += 1
            elif has_ok_after:
                # O fluxo seguiu depois da falha (passo tolerado ou desvio):
                # o resultado existe, com lacunas declaradas.
                stats.degraded += 1
            else:
                stats.failed += 1

        artifacts = self.artifacts_for(run_id)
        stats.artifacts = len(artifacts)
        stats.bytes_written = sum(int(item["bytes"] or 0) for item in artifacts)
        return stats

    def session_results(self, run_id: str) -> dict[str, dict[str, Any]]:
        """Estado final de cada sessão: último passo, dados coletados e erros."""
        results: dict[str, dict[str, Any]] = {}
        for record in self.steps_for(run_id):
            entry = results.setdefault(
                record.session_id,
                {
                    "status": "ok",
                    "last_step": None,
                    "data": {},
                    "error": None,
                    "tolerated": [],
                    "steps": 0,
                },
            )
            entry["last_step"] = record.step_id
            entry["steps"] += 1
            if record.status == "failed":
                entry["error"] = record.error_message or record.error_type
                entry["last_failure"] = record.step_id
            elif record.status == "aborted":
                entry["error"] = record.error_message or record.error_type
                entry["aborted"] = True
            if record.data is not None and record.status == "ok":
                entry["data"][record.step_id] = record.data

        # Decide o estado final de cada sessão com a mesma regra das
        # estatísticas, para que a tabela do relatório e os números do topo
        # nunca discordem.
        for session_id, entry in results.items():
            records = list(self.steps_for(run_id, session_id))
            failed_steps, has_ok_after = _classify_session(records)
            if entry.get("aborted"):
                entry["status"] = "aborted"
            elif not failed_steps:
                entry["status"] = "ok"
            elif has_ok_after:
                entry["status"] = "degraded"
                entry["tolerated"] = failed_steps
            else:
                entry["status"] = "failed"
            entry.pop("aborted", None)
        return results
