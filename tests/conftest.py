"""Fixtures compartilhadas: site de demonstração local e fábricas de fluxo.

Os testes de integração rodam contra o servidor de ``examples/demo-site``, que
sobe em uma porta livre no próprio processo de teste. Nada de rede externa:
o resultado é determinístico e roda em qualquer máquina, inclusive em CI.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "examples" / "demo-site"


def _load_demo_server() -> Any:
    """Importa ``examples/demo-site/server.py`` sem exigir pacote instalado."""
    spec = importlib.util.spec_from_file_location("caravana_demo_server", DEMO_DIR / "server.py")
    if spec is None or spec.loader is None:  # pragma: no cover - ambiente quebrado
        raise RuntimeError("não foi possível carregar o site de demonstração")
    module = importlib.util.module_from_spec(spec)
    sys.modules["caravana_demo_server"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def demo_server() -> Iterator[str]:
    """Sobe o site de demonstração uma vez por sessão e devolve a URL base."""
    module = _load_demo_server()
    server = module.build_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def free_port() -> int:
    """Porta TCP livre, para testes que precisam de um servidor efêmero."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def browser_available() -> bool:
    """Indica se o Chromium do Playwright está instalado nesta máquina."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


@pytest.fixture
def requires_browser(browser_available: bool) -> None:
    """Pula o teste quando o navegador não está disponível."""
    if not browser_available:
        pytest.skip("Chromium do Playwright não instalado")


def write_flow(path: Path, name: str, steps: list[dict[str, Any]], **flow: Any) -> Path:
    """Escreve um fluxo TOML a partir de uma lista de passos."""
    lines = ["[flow]", f'name = "{name}"', f'version = "{flow.get("version", "1.0.0")}"']
    if flow.get("description"):
        lines.append(f'description = "{flow["description"]}"')
    if flow.get("start"):
        lines.append(f'start = "{flow["start"]}"')
    for step in steps:
        lines.append("")
        lines.append("[[flow.steps]]")
        for key, value in step.items():
            lines.append(f"{key} = {_toml_value(value)}")
    path.write_text("\n".join(lines) + "\n", "utf-8")
    return path


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items())
        return "{ " + inner + " }"
    raise TypeError(f"valor TOML não suportado: {value!r}")


@pytest.fixture
def flow_factory(tmp_path: Path) -> Any:
    """Fábrica de arquivos de fluxo dentro do diretório temporário do teste."""
    counter = {"value": 0}

    def factory(
        steps: list[dict[str, Any]],
        name: str = "teste",
        **flow: Any,
    ) -> Path:
        counter["value"] += 1
        path = tmp_path / f"flow-{counter['value']}.toml"
        return write_flow(path, name, steps, **flow)

    return factory


@pytest.fixture
def config_factory(tmp_path: Path) -> Any:
    """Fábrica de configurações TOML que apontam para um fluxo e um servidor."""

    def factory(
        flow_path: Path,
        base_url: str = "",
        *,
        sessions: list[dict[str, Any]] | None = None,
        engine: dict[str, Any] | None = None,
        variables: dict[str, Any] | None = None,
        filename: str = "caravana.toml",
    ) -> Path:
        engine_lines = []
        for key, value in (engine or {}).items():
            engine_lines.append(f"{key} = {_toml_value(value)}")
        blocks = [
            "[flow]",
            f'path = "{flow_path.name}"',
            "",
            "[engine]",
            *engine_lines,
        ]
        if variables:
            blocks += ["", "[vars]"]
            blocks += [f"{key} = {_toml_value(value)}" for key, value in variables.items()]
        for session in sessions or [{"id": "s"}]:
            blocks += ["", "[[session]]"]
            for key, value in session.items():
                if key == "base_url" and base_url and value == "${base}":
                    value = base_url
                blocks.append(f"{key} = {_toml_value(value)}")
        path = tmp_path / filename
        path.write_text("\n".join(blocks) + "\n", "utf-8")
        return path

    return factory
