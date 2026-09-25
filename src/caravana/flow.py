"""Modelo de fluxo: passos, ações, grafo e validação estática.

Um fluxo é declarativo. Antes de qualquer navegador ser aberto, o motor
valida o grafo inteiro: identificadores, ações conhecidas, destinos de salto e
consistência de ``save_as``. Isso transforma erros de digitação em mensagens
imediatas em vez de falhas no meio de uma execução longa.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

from .errors import ConfigError, FlowValidationError

__all__ = ["KNOWN_ACTIONS", "Flow", "Step", "load_flow", "parse_flow"]

#: Ações suportadas. Mantido aqui (e não no registro de execução) para que a
#: validação estática não dependa de importar Playwright.
KNOWN_ACTIONS: frozenset[str] = frozenset(
    {
        "goto",
        "click",
        "fill",
        "type",
        "press",
        "wait_for",
        "wait",
        "extract",
        "extract_attr",
        "extract_all",
        "download",
        "screenshot",
        "login",
        "paginate",
        "assert_text",
        "assert_url",
        "http_get",
    }
)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")
_ERROR_POLICIES = frozenset({"fail", "continue", "skip_to"})


@dataclass(slots=True)
class Step:
    """Um passo do fluxo.

    Attributes:
        id: identificador único e estável (usado na retomada e no histórico).
        action: nome da ação registrada.
        name: rótulo humano, exibido em relatórios.
        params: argumentos da ação, ainda com templates não resolvidos.
        retries: tentativas extras além da primeira.
        retry_delay: espera entre tentativas, em segundos.
        timeout_ms: limite específico do passo (sobrepõe o padrão do motor).
        when: expressão simples de comparação; se falsa, o passo é ignorado.
        optional: quando verdadeiro, a falha definitiva não derruba a sessão.
        save_as: chave em ``session.data`` onde o resultado é armazenado.
        artifacts: quais artefatos guardar (``screenshot``, ``html``, ``trace``).
        next: destino explícito em caso de sucesso (sobrepõe a ordem declarada).
        on_error: ``fail``, ``continue`` ou ``skip_to:<passo>``.
    """

    id: str
    action: str
    name: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    retries: int = 2
    retry_delay: float = 0.5
    timeout_ms: float | None = None
    when: str | None = None
    optional: bool = False
    save_as: str | None = None
    artifacts: tuple[str, ...] = ()
    next: str | None = None
    on_error: str | None = None

    @property
    def label(self) -> str:
        """Rótulo para relatórios: ``nome (id)`` ou apenas o id."""
        return f"{self.name} ({self.id})" if self.name else self.id

    def to_dict(self) -> dict[str, Any]:
        """Serializa o passo para o histórico e para o fingerprint do fluxo."""
        return {
            "id": self.id,
            "action": self.action,
            "name": self.name,
            "params": self.params,
            "retries": self.retries,
            "retry_delay": self.retry_delay,
            "timeout_ms": self.timeout_ms,
            "when": self.when,
            "optional": self.optional,
            "save_as": self.save_as,
            "artifacts": list(self.artifacts),
            "next": self.next,
            "on_error": self.on_error,
        }


@dataclass(slots=True)
class Flow:
    """Fluxo completo: metadados, passos e grafo derivado."""

    name: str
    version: str = "1"
    description: str = ""
    start: str | None = None
    steps: list[Step] = field(default_factory=list)
    source: Path | None = None

    # -- consultas ---------------------------------------------------------
    def step_map(self) -> dict[str, Step]:
        """Mapa id → passo, na ordem declarada."""
        return {step.id: step for step in self.steps}

    def entry(self) -> Step:
        """Primeiro passo a executar."""
        mapping = self.step_map()
        if self.start:
            return mapping[self.start]
        if not self.steps:
            raise FlowValidationError(["o fluxo não possui passos"])
        return self.steps[0]

    def order(self) -> list[str]:
        """Sequência nominal dos passos, começando pela entrada."""
        if not self.steps:
            return []
        entry = self.entry()
        index = self.steps.index(entry)
        return [step.id for step in self.steps[index:] + self.steps[:index]]

    def to_dict(self) -> dict[str, Any]:
        """Representação completa, usada no fingerprint de compatibilidade."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "start": self.start,
            "steps": [step.to_dict() for step in self.steps],
        }

    def validate(self) -> None:
        """Valida o fluxo e levanta :class:`FlowValidationError` se houver problemas."""
        problems = _validate(self)
        if problems:
            raise FlowValidationError(problems)

    def mermaid(self) -> str:
        """Gera um diagrama Mermaid do fluxo, para documentação e PRs."""
        lines = ["flowchart TD"]
        for step in self.steps:
            shape = f'["{step.label}"]' if step.action != "extract" else f'[/"{step.label}"/]'
            lines.append(f"    {step.id.replace('-', '_')}{shape}")
        order = self.order()
        for current, following in pairwise(order):
            lines.append(f"    {current.replace('-', '_')} --> {following.replace('-', '_')}")
        for step in self.steps:
            if step.next:
                lines.append(
                    f"    {step.id.replace('-', '_')} -.->|sucesso| {step.next.replace('-', '_')}"
                )
            if step.on_error and step.on_error.startswith("skip_to:"):
                target = step.on_error.split(":", 1)[1].replace("-", "_")
                lines.append(f"    {step.id.replace('-', '_')} -.->|erro| {target}")
        return "\n".join(lines) + "\n"


def _coerce_step(raw: dict[str, Any], index: int) -> Step:
    if not isinstance(raw, dict):
        raise ConfigError(f"passo #{index}: esperado uma tabela TOML")
    step_id = str(raw.get("id") or "").strip()
    action = str(raw.get("action") or "").strip()
    artifacts_raw = raw.get("artifacts", [])
    if isinstance(artifacts_raw, str):
        artifacts: tuple[str, ...] = (artifacts_raw,)
    elif isinstance(artifacts_raw, list):
        artifacts = tuple(str(item) for item in artifacts_raw)
    else:
        raise ConfigError(f"passo #{index}: 'artifacts' deve ser texto ou lista")
    params = raw.get("params", {})
    if not isinstance(params, dict):
        raise ConfigError(f"passo '{step_id or index}': 'params' deve ser uma tabela TOML")
    return Step(
        id=step_id,
        action=action,
        name=raw.get("name"),
        params=dict(params),
        retries=int(raw.get("retries", 2)),
        retry_delay=float(raw.get("retry_delay", 0.5)),
        timeout_ms=float(raw["timeout_ms"]) if raw.get("timeout_ms") is not None else None,
        when=raw.get("when"),
        optional=bool(raw.get("optional", False)),
        save_as=raw.get("save_as"),
        artifacts=artifacts,
        next=raw.get("next"),
        on_error=raw.get("on_error"),
    )


def parse_flow(payload: dict[str, Any], source: Path | None = None) -> Flow:
    """Constrói um :class:`Flow` a partir de um dicionário já lido do TOML."""
    flow_table = payload.get("flow", payload)
    if not isinstance(flow_table, dict):
        raise ConfigError("tabela [flow] ausente ou inválida")
    steps_raw = flow_table.get("steps", [])
    if not isinstance(steps_raw, list):
        raise ConfigError("[flow].steps deve ser uma lista de tabelas")
    flow = Flow(
        name=str(flow_table.get("name") or (source.stem if source else "fluxo")),
        version=str(flow_table.get("version", "1")),
        description=str(flow_table.get("description", "")),
        start=flow_table.get("start"),
        steps=[_coerce_step(raw, index) for index, raw in enumerate(steps_raw, start=1)],
        source=source,
    )
    flow.validate()
    return flow


def load_flow(path: Path) -> Flow:
    """Lê e valida um arquivo ``.toml`` de fluxo."""
    if not path.exists():
        raise ConfigError(f"fluxo não encontrado: {path}")
    try:
        payload = tomllib.loads(path.read_text("utf-8"))
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - mensagem do parser
        raise ConfigError(f"{path}: TOML inválido: {exc}") from exc
    return parse_flow(payload, source=path)


def _validate(flow: Flow) -> list[str]:
    problems: list[str] = []
    if not flow.steps:
        problems.append("o fluxo não possui passos")
        return problems

    seen: dict[str, int] = {}
    for index, step in enumerate(flow.steps, start=1):
        if not step.id:
            problems.append(f"passo #{index}: 'id' é obrigatório")
        elif not _ID_RE.match(step.id):
            problems.append(
                f"passo '{step.id}': id deve usar apenas minúsculas, dígitos, '-' e '_'"
            )
        elif step.id in seen:
            problems.append(f"passo '{step.id}': id duplicado (passos {seen[step.id]} e {index})")
        else:
            seen[step.id] = index

        if not step.action:
            problems.append(f"passo '{step.id}': 'action' é obrigatório")
        elif step.action not in KNOWN_ACTIONS:
            problems.append(
                f"passo '{step.id}': ação desconhecida '{step.action}'; "
                f"disponíveis: {', '.join(sorted(KNOWN_ACTIONS))}"
            )
        if step.retries < 0:
            problems.append(f"passo '{step.id}': 'retries' não pode ser negativo")
        if step.retry_delay < 0:
            problems.append(f"passo '{step.id}': 'retry_delay' não pode ser negativo")
        if step.on_error and step.on_error != "fail" and step.on_error != "continue":
            if not step.on_error.startswith("skip_to:"):
                problems.append(
                    f"passo '{step.id}': 'on_error' deve ser fail, continue ou skip_to:<passo>"
                )
            else:
                policy = step.on_error.split(":", 1)[0]
                if policy not in _ERROR_POLICIES:  # pragma: no cover - defensivo
                    problems.append(f"passo '{step.id}': política de erro inválida")
        for artifact in step.artifacts:
            if artifact not in {"screenshot", "html", "trace", "download"}:
                problems.append(f"passo '{step.id}': artefato desconhecido '{artifact}'")

    ids = set(seen)
    if flow.start and flow.start not in ids:
        problems.append(f"[flow].start aponta para passo inexistente: '{flow.start}'")
    for step in flow.steps:
        if step.next and step.next not in ids:
            problems.append(
                f"passo '{step.id}': 'next' aponta para passo inexistente: '{step.next}'"
            )
        if step.on_error and step.on_error.startswith("skip_to:"):
            target = step.on_error.split(":", 1)[1]
            if target not in ids:
                problems.append(
                    f"passo '{step.id}': 'on_error' aponta para passo inexistente: '{target}'"
                )
        if step.save_as and not _ID_RE.match(str(step.save_as)):
            problems.append(f"passo '{step.id}': 'save_as' deve ser um identificador simples")
    return problems
