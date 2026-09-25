"""Configuração da execução: fluxo, sessões, políticas de retry e artefatos.

O arquivo de configuração é o contrato entre quem descreve o trabalho e o
motor. Ele é validado antes de qualquer recurso ser alocado: uma configuração
inconsistente falha em milissegundos, não depois de abrir quatro navegadores.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError, FlowValidationError
from .flow import Flow, load_flow

__all__ = ["Config", "SessionConfig", "load_config"]

_SCREENSHOT_MODES = frozenset({"always", "on_error", "never"})
_DEFAULT_UA = "Caravana/0.1 (+https://github.com/nicolasmoreiraferreira/caravana)"


@dataclass(slots=True)
class SessionConfig:
    """Definição de uma família de sessões isoladas.

    ``count`` cria N sessões independentes com o mesmo ponto de partida — cada
    uma com contexto de navegador próprio (cookies, storage e cache separados).
    """

    id: str
    base_url: str = ""
    count: int = 1
    start_url: str | None = None
    max_pages: int = 20
    reuse_profile: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    viewport: tuple[int, int] = (1280, 800)
    locale: str = "pt-BR"
    timezone_id: str = "America/Sao_Paulo"
    user_agent: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def label(self, index: int) -> str:
        """Identificador da instância: ``loja-1`` para ``count = 3``, ``loja`` para 1."""
        return f"{self.id}-{index + 1}" if self.count > 1 else self.id


@dataclass(slots=True)
class Config:
    """Configuração completa e já validada."""

    flow: Flow
    sessions: list[SessionConfig]
    variables: dict[str, str] = field(default_factory=dict)
    workers: int = 4
    max_attempts: int = 3
    retry_delay: float = 0.5
    timeout_ms: float = 30_000
    screenshots: str = "on_error"
    screenshot_quality: int = 70
    respect_robots: bool = True
    user_agent: str = _DEFAULT_UA
    base_dir: Path = Path()
    run_dir: Path = Path(".caravana/runs")

    @property
    def base_url(self) -> str:
        """URL base padrão: a primeira sessão que declarar uma."""
        for session in self.sessions:
            if session.base_url:
                return session.base_url.rstrip("/")
        return ""

    def describe(self) -> dict[str, Any]:
        """Resumo legível usado por ``caravana doctor`` e pelo cabeçalho de ``run``."""
        return {
            "fluxo": f"{self.flow.name} v{self.flow.version}",
            "passos": len(self.flow.steps),
            "sessões": sum(session.count for session in self.sessions),
            "famílias": [session.id for session in self.sessions],
            "workers": self.workers,
            "tentativas_por_passo": self.max_attempts,
            "timeout_ms": self.timeout_ms,
            "screenshots": self.screenshots,
            "respeita_robots": self.respect_robots,
        }


def _parse_session(raw: Any, index: int) -> SessionConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"[[session]] #{index}: esperado uma tabela TOML")
    session_id = str(raw.get("id") or "").strip()
    if not session_id:
        raise ConfigError(f"[[session]] #{index}: 'id' é obrigatório")
    viewport = raw.get("viewport", [1280, 800])
    if not isinstance(viewport, list) or len(viewport) != 2:
        raise ConfigError(f"sessão '{session_id}': 'viewport' deve ser [largura, altura]")
    headers = raw.get("headers", {})
    if not isinstance(headers, dict):
        raise ConfigError(f"sessão '{session_id}': 'headers' deve ser uma tabela TOML")
    known = {
        "id",
        "base_url",
        "count",
        "start_url",
        "max_pages",
        "reuse_profile",
        "headers",
        "viewport",
        "locale",
        "timezone",
        "user_agent",
    }
    return SessionConfig(
        id=session_id,
        base_url=str(raw.get("base_url", "")).rstrip("/"),
        count=int(raw.get("count", 1)),
        start_url=raw.get("start_url"),
        max_pages=int(raw.get("max_pages", 20)),
        reuse_profile=bool(raw.get("reuse_profile", False)),
        headers={str(k): str(v) for k, v in headers.items()},
        viewport=(int(viewport[0]), int(viewport[1])),
        locale=str(raw.get("locale", "pt-BR")),
        timezone_id=str(raw.get("timezone", "America/Sao_Paulo")),
        user_agent=raw.get("user_agent"),
        extra={k: v for k, v in raw.items() if k not in known},
    )


def _validate_sessions(sessions: list[SessionConfig]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for session in sessions:
        if session.id in seen:
            problems.append(f"sessão '{session.id}': id duplicado")
        seen.add(session.id)
        if session.count < 1:
            problems.append(f"sessão '{session.id}': 'count' precisa ser >= 1")
        # Uma base_url pode conter template (${var.base}) e só é resolvida em
        # tempo de execução; nesse caso a checagem de esquema fica para o motor.
        if (
            session.base_url
            and "${" not in session.base_url
            and not session.base_url.startswith(("http://", "https://"))
        ):
            problems.append(
                f"sessão '{session.id}': 'base_url' deve começar com http:// ou https://"
            )
        if session.max_pages < 1:
            problems.append(f"sessão '{session.id}': 'max_pages' precisa ser >= 1")
        width, height = session.viewport
        if width < 200 or height < 200:
            problems.append(f"sessão '{session.id}': viewport muito pequeno ({width}x{height})")
    return problems


def load_config(path: Path) -> Config:
    """Lê, valida e materializa a configuração a partir de um arquivo TOML."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"configuração não encontrada: {path}")
    try:
        payload = tomllib.loads(path.read_text("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML inválido: {exc}") from exc

    base_dir = path.parent
    flow_table = payload.get("flow", {})
    if not isinstance(flow_table, dict) or not flow_table.get("path"):
        raise ConfigError(f"{path}: defina [flow].path apontando para o arquivo do fluxo")
    flow_path = (base_dir / str(flow_table["path"])).resolve()
    flow = load_flow(flow_path)

    engine = payload.get("engine", {})
    if not isinstance(engine, dict):
        raise ConfigError(f"{path}: [engine] deve ser uma tabela TOML")
    sessions_raw = payload.get("session", [])
    if not isinstance(sessions_raw, list) or not sessions_raw:
        raise ConfigError(f"{path}: defina ao menos um bloco [[session]]")
    sessions = [_parse_session(raw, index) for index, raw in enumerate(sessions_raw, start=1)]

    variables_raw = payload.get("vars", {})
    if not isinstance(variables_raw, dict):
        raise ConfigError(f"{path}: [vars] deve ser uma tabela TOML")

    screenshots = str(engine.get("screenshots", "on_error"))
    if screenshots not in _SCREENSHOT_MODES:
        raise ConfigError(
            f"{path}: [engine].screenshots deve ser um de {sorted(_SCREENSHOT_MODES)}"
        )

    config = Config(
        flow=flow,
        sessions=sessions,
        variables={str(k): str(v) for k, v in variables_raw.items()},
        workers=max(1, int(engine.get("workers", 4))),
        max_attempts=max(1, int(engine.get("max_attempts", 3))),
        retry_delay=max(0.0, float(engine.get("retry_delay", 0.5))),
        timeout_ms=max(1.0, float(engine.get("timeout_ms", 30_000))),
        screenshots=screenshots,
        screenshot_quality=min(100, max(10, int(engine.get("screenshot_quality", 70)))),
        respect_robots=bool(engine.get("respect_robots", True)),
        user_agent=str(engine.get("user_agent", _DEFAULT_UA)),
        base_dir=base_dir,
        run_dir=Path(engine.get("run_dir", ".caravana/runs")),
    )

    problems = _validate_sessions(config.sessions)
    if problems:
        raise FlowValidationError(problems)
    return config
