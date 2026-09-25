"""Testes de cada ação do catálogo, contra o site de demonstração.

Cada ação é exercitada isoladamente: o objetivo é garantir que a interface
declarativa (``params``) tem o comportamento documentado — inclusive nos casos
de borda, como campo ausente virando ``None`` em vez de derrubar o passo.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from caravana import Engine, EventBus, load_config

from ._helpers import write_flow

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _execute(
    flow_steps: list[dict[str, Any]],
    *,
    tmp_path: Path,
    base_url: str,
    engine: dict[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
    name: str = "acoes",
) -> tuple[Any, dict[str, Any], list[Any]]:
    """Executa um fluxo e devolve (resultado, dados da sessão, passos)."""
    flow_path = write_flow(tmp_path / "flow.toml", name, flow_steps)
    config_path = tmp_path / "caravana.toml"
    variables_block = ""
    if variables:
        variables_block = "\n[vars]\n" + "".join(
            f"{key} = {json.dumps(value)}\n" for key, value in variables.items()
        )
    config_path.write_text(
        f'[flow]\npath = "{flow_path.name}"\n\n[engine]\n'
        + "".join(f"{key} = {json.dumps(value)}\n" for key, value in (engine or {}).items())
        + variables_block
        + f'\n[[session]]\nid = "s"\nbase_url = "{base_url}"\n',
        "utf-8",
    )
    config = load_config(config_path)
    run_dir = tmp_path / "runs"
    config.run_dir = run_dir
    result = Engine(config, run_dir=run_dir, bus=EventBus()).run()

    from caravana import Ledger

    with Ledger(run_dir / "caravana.db") as ledger:
        data_file = next((run_dir / result.run_id / "data").glob("s.json"))
        data = json.loads(data_file.read_text("utf-8"))
        steps = ledger.steps_for(result.run_id)
    return result, data, steps


class TestNavigationActions:
    def test_goto_registra_url_titulo_e_status(self, demo_server: str, tmp_path: Path) -> None:
        result, data, _ = _execute(
            [
                {
                    "id": "abrir",
                    "action": "goto",
                    "params": {"url": "/page/2"},
                    "save_as": "pagina",
                }
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"
        assert data["data"]["pagina"]["status"] == 200
        assert data["data"]["pagina"]["title"].startswith("Página 2")

    def test_goto_http_404_falha(self, demo_server: str, tmp_path: Path) -> None:
        result, _, steps = _execute(
            [{"id": "abrir", "action": "goto", "params": {"url": "/nao-existe"}, "retries": 0}],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "failed"
        assert "HTTP 404" in (steps[-1].error_message or "")

    def test_goto_url_relativa_sem_base_falha(self, tmp_path: Path) -> None:
        result, _, steps = _execute(
            [{"id": "abrir", "action": "goto", "params": {"url": "/page/1"}, "retries": 0}],
            tmp_path=tmp_path,
            base_url="",
        )
        assert result.status == "failed"
        assert "sem base_url" in (steps[-1].error_message or "")

    def test_wait_aguarda_o_tempo_pedido(self, demo_server: str, tmp_path: Path) -> None:
        import time

        started = time.perf_counter()
        result, _, _ = _execute(
            [{"id": "pausa", "action": "wait", "params": {"ms": 400}}],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"
        assert time.perf_counter() - started >= 0.35

    def test_wait_for_elemento_ausente_falha(self, demo_server: str, tmp_path: Path) -> None:
        result, _st, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "esperar",
                    "action": "wait_for",
                    "params": {"selector": "#inexistente"},
                    "timeout_ms": 500,
                    "retries": 0,
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "failed"

    def test_wait_for_com_state_attached(self, demo_server: str, tmp_path: Path) -> None:
        result, _, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "esperar",
                    "action": "wait_for",
                    "params": {"selector": "#catalog", "state": "attached"},
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"


class TestInteractionActions:
    def test_fill_e_press_enviam_formulario(self, demo_server: str, tmp_path: Path) -> None:
        result, data, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/login"}},
                {
                    "id": "usuario",
                    "action": "fill",
                    "params": {"selector": "#user", "value": "demo"},
                },
                {
                    "id": "senha",
                    "action": "fill",
                    "params": {"selector": "#password", "value": "demo"},
                },
                {
                    "id": "enviar",
                    "action": "press",
                    "params": {"selector": "#password", "key": "Enter"},
                },
                {
                    "id": "conferir",
                    "action": "extract",
                    "params": {"fields": {"segredo": "#secret"}},
                    "save_as": "painel",
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed", data
        assert data["data"]["painel"]["segredo"] == "conteúdo autenticado"

    def test_type_digita_com_atraso(self, demo_server: str, tmp_path: Path) -> None:
        result, _, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/login"}},
                {
                    "id": "digitar",
                    "action": "type",
                    "params": {"selector": "#user", "text": "demo", "delay_ms": 5},
                },
                {
                    "id": "valor",
                    "action": "extract_attr",
                    "params": {"selector": "#user", "attr": "value"},
                    "save_as": "digitado",
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"
        assert result.stats.steps_ok == 3

    def test_click_com_wait_for(self, demo_server: str, tmp_path: Path) -> None:
        result, _, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "proxima",
                    "action": "click",
                    "params": {"selector": "#next", "wait_for": "#catalog"},
                },
                {
                    "id": "pagina",
                    "action": "extract_attr",
                    "params": {"selector": "#catalog", "attr": "data-page"},
                    "save_as": "pagina",
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"

    def test_click_sem_seletor_falha(self, demo_server: str, tmp_path: Path) -> None:
        result, _, steps = _execute(
            [{"id": "clicar", "action": "click", "params": {}, "retries": 0}],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "failed"
        assert "exige 'selector'" in (steps[-1].error_message or "")

    def test_fill_sem_seletor_falha(self, demo_server: str, tmp_path: Path) -> None:
        result, _, steps = _execute(
            [{"id": "preencher", "action": "fill", "params": {"value": "x"}, "retries": 0}],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "failed"
        assert "exige 'selector'" in (steps[-1].error_message or "")


class TestExtractionActions:
    def test_extract_campo_ausente_vira_none(self, demo_server: str, tmp_path: Path) -> None:
        """Página real tem campo opcional: ausência não deve derrubar o passo."""
        result, data, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "extrair",
                    "action": "extract",
                    "params": {
                        "fields": {"titulo": "#catalog h1", "promocao": ".selo-promocional"}
                    },
                    "save_as": "registro",
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"
        assert data["data"]["registro"]["titulo"] == "Página 1 de 3"
        assert data["data"]["registro"]["promocao"] is None

    def test_extract_com_regex_extrai_grupo(self, demo_server: str, tmp_path: Path) -> None:
        result, data, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/item/7"}},
                {
                    "id": "extrair",
                    "action": "extract",
                    "params": {
                        "fields": {
                            "sku": {"selector": ".sku", "regex": "SKU-(\\d+)"},
                            "preco": {"selector": ".price", "regex": "R\\$\\s*([\\d,]+)"},
                        }
                    },
                    "save_as": "detalhe",
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"
        assert data["data"]["detalhe"]["sku"] == "0007"
        assert data["data"]["detalhe"]["preco"] == "349,90"

    def test_extract_com_escopo_e_lista(self, demo_server: str, tmp_path: Path) -> None:
        result, data, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "extrair",
                    "action": "extract",
                    "params": {
                        "selector": "#catalog",
                        "fields": {
                            "nomes": {"selector": "article.product .name", "all": True},
                            "titulo": "h1",
                        },
                    },
                    "save_as": "catalogo",
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"
        assert len(data["data"]["catalogo"]["nomes"]) == 5
        assert data["data"]["catalogo"]["titulo"] == "Página 1 de 3"

    def test_extract_escopo_inexistente_falha(self, demo_server: str, tmp_path: Path) -> None:
        result, _, steps = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "extrair",
                    "action": "extract",
                    "params": {"selector": "#nao-existe", "fields": {"x": "h1"}},
                    "retries": 0,
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "failed"
        assert "seletor raiz" in (steps[-1].error_message or "")

    def test_extract_sem_fields_falha(self, demo_server: str, tmp_path: Path) -> None:
        result, _, steps = _execute(
            [{"id": "extrair", "action": "extract", "params": {}, "retries": 0}],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "failed"
        assert "exige 'fields'" in (steps[-1].error_message or "")

    def test_extract_attr_com_atributo_ausente_falha(
        self, demo_server: str, tmp_path: Path
    ) -> None:
        result, _, steps = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "atributo",
                    "action": "extract_attr",
                    "params": {"selector": "h1", "attr": "data-inexistente"},
                    "retries": 0,
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "failed"
        assert "ausente" in (steps[-1].error_message or "")

    def test_extract_all_com_limite(self, demo_server: str, tmp_path: Path) -> None:
        result, data, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "listar",
                    "action": "extract_all",
                    "params": {"selector": "article.product .name", "limit": 2},
                    "save_as": "dois",
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"
        assert len(data["data"]["dois"]) == 2

    def test_extract_all_com_atributo(self, demo_server: str, tmp_path: Path) -> None:
        result, data, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "links",
                    "action": "extract_all",
                    "params": {"selector": "a.detail", "attr": "href"},
                    "save_as": "links",
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"
        assert data["data"]["links"][0].startswith("/item/")

    def test_extract_all_sem_seletor_falha(self, demo_server: str, tmp_path: Path) -> None:
        result, _st, _ = _execute(
            [{"id": "listar", "action": "extract_all", "params": {}, "retries": 0}],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "failed"


class TestPaginationAction:
    def test_paginate_respeita_max_pages(self, demo_server: str, tmp_path: Path) -> None:
        result, data, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "paginar",
                    "action": "paginate",
                    "params": {"items": "article.product .name", "next": "#next", "max_pages": 1},
                    "save_as": "itens",
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"
        assert len(data["data"]["itens"]) == 5  # apenas a primeira página

    def test_paginate_para_quando_next_desaparece(self, demo_server: str, tmp_path: Path) -> None:
        result, data, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "paginar",
                    "action": "paginate",
                    "params": {"items": "article.product .name", "next": "#next"},
                    "save_as": "itens",
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"
        assert len(data["data"]["itens"]) == 15

    def test_paginate_sem_itens_falha(self, demo_server: str, tmp_path: Path) -> None:
        result, _, steps = _execute(
            [{"id": "paginar", "action": "paginate", "params": {}, "retries": 0}],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "failed"
        assert "exige 'items'" in (steps[-1].error_message or "")


class TestAssertActions:
    def test_assert_text_contains(self, demo_server: str, tmp_path: Path) -> None:
        result, _, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "conferir",
                    "action": "assert_text",
                    "params": {"selector": "#catalog h1", "contains": "Página 1"},
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"

    def test_assert_text_equals_falha_com_mensagem_util(
        self, demo_server: str, tmp_path: Path
    ) -> None:
        result, _, steps = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "conferir",
                    "action": "assert_text",
                    "params": {"selector": "#catalog h1", "equals": "Página 9 de 9"},
                    "retries": 0,
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "failed"
        message = steps[-1].error_message or ""
        assert "esperado" in message
        assert "obtido" in message

    def test_assert_text_regex(self, demo_server: str, tmp_path: Path) -> None:
        result, _, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/3"}},
                {
                    "id": "conferir",
                    "action": "assert_text",
                    "params": {"selector": "#catalog h1", "regex": "Página \\d de 3"},
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"

    def test_assert_text_sem_criterio_falha(self, demo_server: str, tmp_path: Path) -> None:
        result, _, steps = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "conferir",
                    "action": "assert_text",
                    "params": {"selector": "h1"},
                    "retries": 0,
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "failed"
        assert "informe" in (steps[-1].error_message or "")

    def test_assert_url(self, demo_server: str, tmp_path: Path) -> None:
        result, _, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/2"}},
                {"id": "conferir", "action": "assert_url", "params": {"contains": "/page/2"}},
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"

    def test_assert_url_falha(self, demo_server: str, tmp_path: Path) -> None:
        result, _, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/2"}},
                {
                    "id": "conferir",
                    "action": "assert_url",
                    "params": {"contains": "/dashboard"},
                    "retries": 0,
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "failed"


class TestDownloadAndHttp:
    def test_download_salva_arquivo_com_nome_e_extensao(
        self, demo_server: str, tmp_path: Path
    ) -> None:
        result, data, _ = _execute(
            [
                {
                    "id": "baixar",
                    "action": "download",
                    "params": {"url": "/download/sample.csv", "filename": "relatorio.csv"},
                    "save_as": "arquivo",
                }
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"
        info = data["data"]["arquivo"]
        assert info["name"] == "relatorio.csv"
        assert info["bytes"] > 0
        assert Path(info["path"]).read_text("utf-8").startswith("id,name,price")

    def test_download_sem_origem_falha(self, demo_server: str, tmp_path: Path) -> None:
        result, _, steps = _execute(
            [{"id": "baixar", "action": "download", "params": {}, "retries": 0}],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "failed"
        assert "exige" in (steps[-1].error_message or "")

    def test_http_get_json(self, demo_server: str, tmp_path: Path) -> None:
        result, data, _ = _execute(
            [
                {
                    "id": "consultar",
                    "action": "http_get",
                    "params": {"url": "/__health"},
                    "save_as": "saude",
                }
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"
        assert data["data"]["saude"]["status"] == "ok"

    def test_http_get_erro_http(self, demo_server: str, tmp_path: Path) -> None:
        result, _, steps = _execute(
            [
                {
                    "id": "consultar",
                    "action": "http_get",
                    "params": {"url": "/nao-existe"},
                    "retries": 0,
                }
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "failed"
        assert "HTTP 404" in (steps[-1].error_message or "")


class TestLoginAndProfile:
    def test_login_falha_com_credencial_errada(self, demo_server: str, tmp_path: Path) -> None:
        result, _, steps = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/login"}},
                {
                    "id": "entrar",
                    "action": "login",
                    "params": {
                        "user": "demo",
                        "password": "errada",
                        "failure_selector": "#error",
                    },
                    "retries": 0,
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "failed"
        assert "rejeitado" in (steps[-1].error_message or "")

    def test_login_com_url_propria(self, demo_server: str, tmp_path: Path) -> None:
        result, _, _ = _execute(
            [
                {
                    "id": "entrar",
                    "action": "login",
                    "params": {
                        "url": "/login",
                        "user": "demo",
                        "password": "demo",
                        "success_selector": "#secret",
                    },
                }
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"

    def test_reuse_profile_persiste_estado(self, demo_server: str, tmp_path: Path) -> None:
        """Com ``reuse_profile``, o cookie de sessão sobrevive entre execuções."""
        flow_path = write_flow(
            tmp_path / "flow.toml",
            "perfil",
            [
                {
                    "id": "entrar",
                    "action": "login",
                    "params": {
                        "url": "/login",
                        "user": "demo",
                        "password": "demo",
                        "success_selector": "#secret",
                    },
                }
            ],
        )
        config_path = tmp_path / "caravana.toml"
        config_path.write_text(
            f'[flow]\npath = "{flow_path.name}"\n\n[[session]]\nid = "perfil"\n'
            f'base_url = "{demo_server}"\nreuse_profile = true\n',
            "utf-8",
        )
        config = load_config(config_path)
        config.run_dir = tmp_path / "runs"
        first = Engine(config, run_dir=config.run_dir, bus=EventBus()).run()
        assert first.status == "completed"
        state_file = tmp_path / "runs" / "profiles" / "perfil" / "state.json"
        assert state_file.exists()

        # Segunda execução: já autenticada, o painel abre direto.
        flow_path = write_flow(
            tmp_path / "flow2.toml",
            "perfil",
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/dashboard"}},
                {
                    "id": "conferir",
                    "action": "assert_text",
                    "params": {"selector": "#secret", "contains": "autenticado"},
                },
            ],
        )
        config_path.write_text(
            f'[flow]\npath = "{flow_path.name}"\n\n[[session]]\nid = "perfil"\n'
            f'base_url = "{demo_server}"\nreuse_profile = true\n',
            "utf-8",
        )
        second_config = load_config(config_path)
        second_config.run_dir = tmp_path / "runs"
        second = Engine(second_config, run_dir=second_config.run_dir, bus=EventBus()).run()
        assert second.status == "completed", "o cookie deveria ter sido reaproveitado"


class TestRobotsAndTrace:
    def test_robots_bloqueia_caminho_proibido(self, demo_server: str, tmp_path: Path) -> None:
        """O site declara ``Disallow: /private``; o motor deve respeitar."""
        result, _, steps = _execute(
            [{"id": "proibido", "action": "goto", "params": {"url": "/private"}, "retries": 0}],
            tmp_path=tmp_path,
            base_url=demo_server,
            engine={"respect_robots": True},
        )
        assert result.status == "failed"
        assert "robots.txt" in (steps[-1].error_message or "")

    def test_robots_pode_ser_ignorado(self, demo_server: str, tmp_path: Path) -> None:
        result, _, _ = _execute(
            [{"id": "permitido", "action": "goto", "params": {"url": "/private"}}],
            tmp_path=tmp_path,
            base_url=demo_server,
            engine={"respect_robots": False},
        )
        assert result.status == "completed"

    def test_trace_gera_arquivo_zip(self, demo_server: str, tmp_path: Path) -> None:
        result, _, _ = _execute(
            [
                {
                    "id": "abrir",
                    "action": "goto",
                    "params": {"url": "/page/1"},
                    "artifacts": ["trace"],
                }
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"
        from caravana import Ledger

        with Ledger(tmp_path / "runs" / "caravana.db") as ledger:
            traces = ledger.artifacts_for(result.run_id, kind="trace")
        assert traces and Path(traces[0]["path"]).suffix == ".zip"

    def test_screenshots_always_captura_todo_passo(self, demo_server: str, tmp_path: Path) -> None:
        result, _, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {"id": "esperar", "action": "wait", "params": {"ms": 50}},
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
            engine={"screenshots": "always"},
        )
        from caravana import Ledger

        with Ledger(tmp_path / "runs" / "caravana.db") as ledger:
            shots = ledger.artifacts_for(result.run_id, kind="screenshot")
        assert len(shots) == 2

    def test_screenshot_por_seletor(self, demo_server: str, tmp_path: Path) -> None:
        result, data, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "capturar",
                    "action": "screenshot",
                    "params": {"name": "apenas-catalogo", "selector": "#catalog"},
                    "save_as": "imagem",
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"
        assert Path(data["data"]["imagem"]).name == "apenas-catalogo.png"

    def test_screenshots_never_nao_captura_sucesso(self, demo_server: str, tmp_path: Path) -> None:
        result, _, _ = _execute(
            [{"id": "abrir", "action": "goto", "params": {"url": "/page/1"}}],
            tmp_path=tmp_path,
            base_url=demo_server,
            engine={"screenshots": "never"},
        )
        from caravana import Ledger

        with Ledger(tmp_path / "runs" / "caravana.db") as ledger:
            shots = ledger.artifacts_for(result.run_id, kind="screenshot")
        assert shots == []


class TestConditionalSteps:
    def test_when_falso_ignora_passo(self, demo_server: str, tmp_path: Path) -> None:
        result, data, steps = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "condicional",
                    "action": "goto",
                    "params": {"url": "/page/3"},
                    "when": "${var.ativo} == nao",
                    "save_as": "nunca",
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
            variables={"ativo": "sim"},
        )
        assert result.status == "completed"
        assert "nunca" not in data["data"]
        skipped = [step for step in steps if step.status == "skipped"]
        assert skipped and "condição falsa" in (skipped[0].error_message or "")

    def test_when_comparando_dados_da_sessao(self, demo_server: str, tmp_path: Path) -> None:
        result, _dados, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "contar",
                    "action": "extract_all",
                    "params": {"selector": "article.product"},
                    "save_as": "itens",
                },
                {
                    "id": "contar_total",
                    "action": "extract",
                    "params": {"fields": {"total": {"selector": "article.product", "all": True}}},
                    "save_as": "resumo",
                },
                {
                    "id": "condicional",
                    "action": "goto",
                    "params": {"url": "/page/3"},
                    "when": "${session.data.tamanho} > 3",
                    "save_as": "foi",
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        # `tamanho` não existe: o motor precisa falhar de forma explícita, com o
        # nome da variável ausente na mensagem — e não seguir em silêncio.
        assert result.status == "failed"
        assert "session.data.tamanho" in (result.sessions["s"]["error"] or "")

    def test_when_com_contagem_explicita(self, demo_server: str, tmp_path: Path) -> None:
        """Uma variável escalar alimenta a condição — o caminho suportado."""
        result, data, _ = _execute(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "condicional",
                    "action": "goto",
                    "params": {"url": "/page/3"},
                    "when": "${var.minimo} > 3",
                    "save_as": "foi",
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
            variables={"minimo": "5"},
        )
        assert result.status == "completed"
        assert data["data"]["foi"]["url"].endswith("/page/3")

    def test_next_salta_para_passo_especifico(self, demo_server: str, tmp_path: Path) -> None:
        result, data, steps = _execute(
            [
                {
                    "id": "primeiro",
                    "action": "goto",
                    "params": {"url": "/page/1"},
                    "next": "terceiro",
                },
                {
                    "id": "segundo",
                    "action": "goto",
                    "params": {"url": "/page/2"},
                    "save_as": "dois",
                },
                {
                    "id": "terceiro",
                    "action": "goto",
                    "params": {"url": "/page/3"},
                    "save_as": "tres",
                },
            ],
            tmp_path=tmp_path,
            base_url=demo_server,
        )
        assert result.status == "completed"
        assert "dois" not in data["data"]
        assert "tres" in data["data"]
        assert any(step.status == "skipped" for step in steps)
