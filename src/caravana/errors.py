"""Hierarquia de erros do Caravana.

Todos os erros levantados pelo motor herdam de :class:`CaravanaError`, o que
permite que a CLI traduza qualquer falha em uma mensagem curta e um código de
saída estável, sem depender de rastreamentos longos.
"""

from __future__ import annotations

__all__ = [
    "ActionError",
    "CaravanaError",
    "ConfigError",
    "FlowValidationError",
    "LedgerError",
    "ResumeError",
    "SessionError",
    "StepFailed",
    "TemplateError",
]


class CaravanaError(Exception):
    """Erro base do Caravana."""


class ConfigError(CaravanaError):
    """Configuração inválida (arquivo ausente, TOML malformado, chave desconhecida)."""


class FlowValidationError(CaravanaError):
    """O fluxo é sintaticamente válido, mas inconsistente (grafo, ids, ações)."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        detail = "\n".join(f"  - {problem}" for problem in self.problems)
        super().__init__(f"fluxo inválido ({len(self.problems)} problema(s)):\n{detail}")


class TemplateError(CaravanaError):
    """Uma expressão ``${...}`` não pôde ser resolvida."""


class ActionError(CaravanaError):
    """Uma ação falhou durante a execução (seletor ausente, timeout, etc.)."""


class SessionError(CaravanaError):
    """A sessão de navegador não pôde ser criada ou está irrecuperável."""


class LedgerError(CaravanaError):
    """Falha ao gravar ou ler o histórico de execuções."""


class ResumeError(CaravanaError):
    """A retomada pedida não é possível (execução ausente ou fluxo diferente)."""


class StepFailed(CaravanaError):
    """Falha definitiva de um passo, já esgotadas as tentativas.

    Carrega o identificador do passo e o erro original para que o motor possa
    registrar causa e consequência separadamente no histórico.
    """

    def __init__(self, step_id: str, attempts: int, cause: BaseException) -> None:
        self.step_id = step_id
        self.attempts = attempts
        self.cause = cause
        super().__init__(f"passo '{step_id}' falhou após {attempts} tentativa(s): {cause}")
