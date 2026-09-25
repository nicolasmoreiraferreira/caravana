"""Auxiliares de teste compartilhados (importáveis sem depender do conftest)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .conftest import write_flow as _base_write_flow

__all__ = ["write_flow"]


def _protect(value: Any) -> Any:
    r"""Marca strings com barra invertida para virarem literais no TOML.

    Expressões regulares contêm ``\d``, ``\s`` e afins, que o TOML
    interpretaria como sequência de escape inválida. Em TOML, o que um autor de
    fluxo escreveria é a string literal (aspas simples), que preserva as barras.
    """
    if isinstance(value, str):
        return f"LITERAL{value}" if "\\" in value else value
    if isinstance(value, dict):
        return {key: _protect(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_protect(item) for item in value]
    return value


def write_flow(path: Path, name: str, steps: list[dict[str, Any]], **flow: Any) -> Path:
    """Escreve um fluxo TOML a partir de uma lista de passos."""
    protected = [{key: _protect(value) for key, value in step.items()} for step in steps]
    target = _base_write_flow(path, name, protected, **flow)
    text = target.read_text("utf-8")
    text = re.sub(r'"LITERAL([^"]*)"', lambda match: f"'{match.group(1)}'", text)
    target.write_text(text, "utf-8")
    return target
