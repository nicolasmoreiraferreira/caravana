"""Interpolação de variáveis e cofre de segredos.

Um fluxo é um arquivo de texto: nada de credencial escrita nele. Valores
dinâmicos aparecem como ``${escopo.nome}`` e são resolvidos em tempo de
execução a partir de quatro escopos:

===================  ==========================================================
Escopo               Origem
===================  ==========================================================
``var``              ``[sessions].vars`` e ``--var nome=valor`` na linha de comando
``secret``           variáveis de ambiente ``CARAVANA_SECRET_*`` ou ``--secrets``
``env``              variáveis de ambiente comuns
``run``              dados gerados pelo próprio motor (id, diretório, horário)
``session``          dados da sessão que executa o passo (id, índice, base_url)
===================  ==========================================================
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError, TemplateError

__all__ = ["SecretStore", "TemplateContext", "render", "render_value"]

_TOKEN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_\-]+)*)\}")
_SECRET_PREFIX = "CARAVANA_SECRET_"
_MISSING = object()


def _lookup(scope: Mapping[str, Any], path: list[str]) -> Any:
    current: Any = scope
    for part in path:
        if isinstance(current, Mapping):
            current = current.get(part, _MISSING)
        else:
            current = getattr(current, part, _MISSING)
        if current is _MISSING:
            return _MISSING
    return current


@dataclass(slots=True)
class SecretStore:
    """Mantém valores sensíveis fora dos arquivos de fluxo.

    Lê variáveis de ambiente com o prefixo ``CARAVANA_SECRET_`` e, opcionalmente,
    um arquivo ``.env``-like com ``NOME=valor``. O método :meth:`values` existe
    apenas para que o motor possa mascarar esses valores em logs e relatórios.
    """

    prefix: str = _SECRET_PREFIX
    _data: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.load_from_environ()

    def load_from_environ(self, environ: Mapping[str, str] | None = None) -> None:
        """Importa as variáveis de ambiente que seguem o prefixo configurado."""
        source = os.environ if environ is None else environ
        for key, value in source.items():
            if key.startswith(self.prefix):
                self._data[key[len(self.prefix) :]] = value

    def load_from_file(self, path: Path) -> None:
        """Importa segredos de um arquivo ``NOME=valor`` (linhas ``#`` são ignoradas)."""
        if not path.exists():
            raise ConfigError(f"arquivo de segredos não encontrado: {path}")
        for number, raw in enumerate(path.read_text("utf-8").splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ConfigError(f"{path}:{number}: esperado NOME=valor")
            name, value = line.split("=", 1)
            self._data[name.strip()] = value.strip().strip("'\"")

    def get(self, name: str) -> str:
        """Retorna o segredo ou falha com uma mensagem que não revela o valor."""
        try:
            return self._data[name]
        except KeyError:
            raise TemplateError(
                f"segredo '{name}' não definido (use CARAVANA_SECRET_{name})"
            ) from None

    def values(self) -> list[str]:
        """Lista os valores conhecidos, para mascaramento em textos de saída."""
        return [value for value in self._data.values() if value]

    def names(self) -> list[str]:
        """Lista os nomes disponíveis, sem expor valores (usado em ``caravana doctor``)."""
        return sorted(self._data)


@dataclass(slots=True)
class TemplateContext:
    """Escopos disponíveis durante a resolução de um template."""

    variables: dict[str, Any] = field(default_factory=dict)
    secrets: SecretStore = field(default_factory=SecretStore)
    run: dict[str, Any] = field(default_factory=dict)
    session: dict[str, Any] = field(default_factory=dict)

    def scopes(self) -> dict[str, Mapping[str, Any]]:
        """Monta o mapa de escopos usado por :func:`render`."""
        return {
            "var": self.variables,
            "secret": _SecretView(self.secrets),
            "env": os.environ,
            "run": self.run,
            "session": self.session,
        }


class _SecretView(Mapping[str, str]):
    """Visão somente-leitura do cofre, para que ``${secret.X}`` funcione como os demais escopos."""

    def __init__(self, store: SecretStore) -> None:
        self._store = store

    def __getitem__(self, key: str) -> str:
        return self._store.get(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._store.names())

    def __len__(self) -> int:
        return len(self._store.names())


def render(template: str, context: TemplateContext) -> str:
    """Resolve todas as expressões ``${...}`` de um texto.

    Escopos desconhecidos ou chaves ausentes levantam :class:`TemplateError` com
    o nome da variável — nunca com o valor, para não vazar segredo em log.
    """
    scopes = context.scopes()

    def replace(match: re.Match[str]) -> str:
        expression = match.group(1)
        parts = expression.split(".")
        scope_name, *rest = parts
        scope = scopes.get(scope_name)
        if scope is None:
            raise TemplateError(
                f"escopo desconhecido em ${{{expression}}}; use var, secret, env, run ou session"
            )
        if not rest:
            raise TemplateError(f"${{{expression}}} precisa de um nome depois do escopo")
        value = _lookup(scope, rest)
        if value is _MISSING:
            raise TemplateError(f"variável não definida: ${{{expression}}}")
        return str(value)

    return _TOKEN_RE.sub(replace, template)


def render_value(value: Any, context: TemplateContext) -> Any:
    """Resolve templates dentro de strings, listas e dicionários (recursivo)."""
    if isinstance(value, str):
        return render(value, context)
    if isinstance(value, list):
        return [render_value(item, context) for item in value]
    if isinstance(value, dict):
        return {key: render_value(item, context) for key, item in value.items()}
    return value
