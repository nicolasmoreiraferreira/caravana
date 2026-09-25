"""Testes de fluxo, configuração, templates e utilitários."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from caravana import (
    ConfigError,
    FlowValidationError,
    SecretStore,
    TemplateContext,
    TemplateError,
    format_duration,
    human_bytes,
    load_config,
    load_flow,
    parse_flow,
    redact,
    render,
    safe_filename,
    sha256_text,
    slugify,
)
from caravana.flow import KNOWN_ACTIONS

from ._helpers import write_flow


class TestFlowParsing:
    def test_parse_flow_minimo(self) -> None:
        flow = parse_flow(
            {
                "flow": {
                    "name": "demo",
                    "steps": [{"id": "a", "action": "goto", "params": {"url": "/x"}}],
                }
            }
        )
        assert flow.name == "demo"
        assert [step.id for step in flow.steps] == ["a"]
        assert flow.entry().action == "goto"

    def test_start_define_entrada(self) -> None:
        flow = parse_flow(
            {
                "flow": {
                    "name": "demo",
                    "start": "b",
                    "steps": [
                        {"id": "a", "action": "goto"},
                        {"id": "b", "action": "goto"},
                    ],
                }
            }
        )
        assert flow.entry().id == "b"
        assert flow.order() == ["b", "a"]

    def test_id_duplicado_e_rejeitado(self) -> None:
        with pytest.raises(FlowValidationError) as excinfo:
            parse_flow(
                {
                    "flow": {
                        "name": "demo",
                        "steps": [
                            {"id": "a", "action": "goto"},
                            {"id": "a", "action": "goto"},
                        ],
                    }
                }
            )
        assert "duplicado" in str(excinfo.value)

    def test_acao_desconhecida_lista_alternativas(self) -> None:
        with pytest.raises(FlowValidationError) as excinfo:
            parse_flow({"flow": {"name": "d", "steps": [{"id": "a", "action": "teleportar"}]}})
        message = str(excinfo.value)
        assert "teleportar" in message
        assert "goto" in message

    def test_next_inexistente_e_rejeitado(self) -> None:
        with pytest.raises(FlowValidationError) as excinfo:
            parse_flow(
                {
                    "flow": {
                        "name": "d",
                        "steps": [{"id": "a", "action": "goto", "next": "fantasma"}],
                    }
                }
            )
        assert "fantasma" in str(excinfo.value)

    def test_on_error_skip_to_invalido(self) -> None:
        with pytest.raises(FlowValidationError) as excinfo:
            parse_flow(
                {
                    "flow": {
                        "name": "d",
                        "steps": [
                            {"id": "a", "action": "goto", "on_error": "skip_to:nada"},
                        ],
                    }
                }
            )
        assert "nada" in str(excinfo.value)

    def test_artefato_desconhecido(self) -> None:
        with pytest.raises(FlowValidationError) as excinfo:
            parse_flow(
                {
                    "flow": {
                        "name": "d",
                        "steps": [{"id": "a", "action": "goto", "artifacts": ["hologram"]}],
                    }
                }
            )
        assert "hologram" in str(excinfo.value)

    def test_id_invalido(self) -> None:
        with pytest.raises(FlowValidationError):
            parse_flow(
                {"flow": {"name": "d", "steps": [{"id": "Ação Inválida", "action": "goto"}]}}
            )

    def test_todas_as_acoes_conhecidas_sao_aceitas(self) -> None:
        steps = [
            {"id": f"p{index}", "action": action} for index, action in enumerate(KNOWN_ACTIONS)
        ]
        flow = parse_flow({"flow": {"name": "d", "steps": steps}})
        assert len(flow.steps) == len(KNOWN_ACTIONS)

    def test_mermaid_gera_fluxograma(self) -> None:
        flow = parse_flow(
            {
                "flow": {
                    "name": "d",
                    "steps": [
                        {"id": "a", "action": "goto"},
                        {"id": "b", "action": "extract", "next": "c"},
                        {"id": "c", "action": "download", "on_error": "skip_to:a"},
                    ],
                }
            }
        )
        mermaid = flow.mermaid()
        assert mermaid.startswith("flowchart TD")
        assert "a --> b" in mermaid
        assert "b -.->|sucesso| c" in mermaid
        assert "c -.->|erro| a" in mermaid

    def test_flow_to_dict_e_estavel(self) -> None:
        payload = {"flow": {"name": "d", "steps": [{"id": "a", "action": "goto"}]}}
        assert parse_flow(payload).to_dict() == parse_flow(payload).to_dict()


class TestConfig:
    def test_load_config_completo(self, tmp_path: Path) -> None:
        flow_path = write_flow(
            tmp_path / "flow.toml",
            "coleta",
            [{"id": "a", "action": "goto", "params": {"url": "/"}}],
        )
        config_path = tmp_path / "caravana.toml"
        config_path.write_text(
            f"""
[flow]
path = "{flow_path.name}"

[engine]
workers = 2
max_attempts = 4
timeout_ms = 5000
screenshots = "always"
respect_robots = false

[vars]
base = "http://exemplo.test"

[[session]]
id = "loja"
count = 3
base_url = "http://exemplo.test"
""",
            "utf-8",
        )
        config = load_config(config_path)
        assert config.flow.name == "coleta"
        assert config.workers == 2
        assert config.max_attempts == 4
        assert config.timeout_ms == 5000
        assert config.screenshots == "always"
        assert config.respect_robots is False
        assert config.base_url == "http://exemplo.test"
        assert config.variables["base"] == "http://exemplo.test"
        assert config.sessions[0].label(0) == "loja-1"
        assert config.sessions[0].label(2) == "loja-3"

    def test_label_sem_count(self, tmp_path: Path) -> None:
        flow_path = write_flow(tmp_path / "f.toml", "d", [{"id": "a", "action": "goto"}])
        config_path = tmp_path / "c.toml"
        config_path.write_text(
            f'[flow]\npath = "{flow_path.name}"\n\n[[session]]\nid = "unica"\n', "utf-8"
        )
        config = load_config(config_path)
        assert config.sessions[0].label(0) == "unica"

    def test_flow_path_ausente(self, tmp_path: Path) -> None:
        path = tmp_path / "c.toml"
        path.write_text('[[session]]\nid = "s"\n', "utf-8")
        with pytest.raises(ConfigError, match=r"\[flow\]\.path"):
            load_config(path)

    def test_sem_sessao(self, tmp_path: Path) -> None:
        flow_path = write_flow(tmp_path / "f.toml", "d", [{"id": "a", "action": "goto"}])
        path = tmp_path / "c.toml"
        path.write_text(f'[flow]\npath = "{flow_path.name}"\n', "utf-8")
        with pytest.raises(ConfigError, match="session"):
            load_config(path)

    def test_count_invalido(self, tmp_path: Path) -> None:
        flow_path = write_flow(tmp_path / "f.toml", "d", [{"id": "a", "action": "goto"}])
        path = tmp_path / "c.toml"
        path.write_text(
            f'[flow]\npath = "{flow_path.name}"\n\n[[session]]\nid = "s"\ncount = 0\n', "utf-8"
        )
        with pytest.raises(FlowValidationError, match="count"):
            load_config(path)

    def test_screenshots_invalido(self, tmp_path: Path) -> None:
        flow_path = write_flow(tmp_path / "f.toml", "d", [{"id": "a", "action": "goto"}])
        path = tmp_path / "c.toml"
        path.write_text(
            f'[flow]\npath = "{flow_path.name}"\n\n[engine]\nscreenshots = "talvez"\n'
            '\n[[session]]\nid = "s"\n',
            "utf-8",
        )
        with pytest.raises(ConfigError, match="screenshots"):
            load_config(path)

    def test_base_url_com_template_e_aceita(self, tmp_path: Path) -> None:
        flow_path = write_flow(tmp_path / "f.toml", "d", [{"id": "a", "action": "goto"}])
        path = tmp_path / "c.toml"
        path.write_text(
            f'[flow]\npath = "{flow_path.name}"\n\n[vars]\nbase = "http://x.test"\n'
            '\n[[session]]\nid = "s"\nbase_url = "${var.base}"\n',
            "utf-8",
        )
        config = load_config(path)
        assert config.sessions[0].base_url == "${var.base}"

    def test_base_url_invalida_sem_template(self, tmp_path: Path) -> None:
        flow_path = write_flow(tmp_path / "f.toml", "d", [{"id": "a", "action": "goto"}])
        path = tmp_path / "c.toml"
        path.write_text(
            f'[flow]\npath = "{flow_path.name}"\n\n[[session]]\nid = "s"\nbase_url = "x.test"\n',
            "utf-8",
        )
        with pytest.raises(FlowValidationError, match="base_url"):
            load_config(path)

    def test_viewport_invalido(self, tmp_path: Path) -> None:
        flow_path = write_flow(tmp_path / "f.toml", "d", [{"id": "a", "action": "goto"}])
        path = tmp_path / "c.toml"
        path.write_text(
            f'[flow]\npath = "{flow_path.name}"\n\n[[session]]\nid = "s"\nviewport = [100, 100]\n',
            "utf-8",
        )
        with pytest.raises(FlowValidationError, match="viewport"):
            load_config(path)

    def test_flow_inexistente(self, tmp_path: Path) -> None:
        path = tmp_path / "c.toml"
        path.write_text('[flow]\npath = "nao-existe.toml"\n\n[[session]]\nid = "s"\n', "utf-8")
        with pytest.raises(ConfigError, match="não encontrado"):
            load_config(path)

    def test_load_flow_de_arquivo(self, tmp_path: Path) -> None:
        path = write_flow(tmp_path / "f.toml", "arquivo", [{"id": "a", "action": "goto"}])
        flow = load_flow(path)
        assert flow.name == "arquivo"
        assert flow.source == path

    def test_toml_invalido(self, tmp_path: Path) -> None:
        path = tmp_path / "quebrado.toml"
        path.write_text("[flow\nname = ", "utf-8")
        with pytest.raises(ConfigError, match="TOML inválido"):
            load_flow(path)

    def test_describe_resume_a_configuracao(self, tmp_path: Path) -> None:
        flow_path = write_flow(tmp_path / "f.toml", "d", [{"id": "a", "action": "goto"}])
        path = tmp_path / "c.toml"
        path.write_text(
            f'[flow]\npath = "{flow_path.name}"\n\n[[session]]\nid = "s"\ncount = 2\n', "utf-8"
        )
        described = load_config(path).describe()
        assert described["sessões"] == 2
        assert described["passos"] == 1


class TestTemplating:
    def test_render_variavel(self) -> None:
        context = TemplateContext(variables={"base": "http://x.test"})
        assert render("${var.base}/produtos", context) == "http://x.test/produtos"

    def test_render_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CARAVANA_TESTE_ENV", "valor")
        assert render("${env.CARAVANA_TESTE_ENV}", context=TemplateContext()) == "valor"

    def test_render_run_e_session(self) -> None:
        context = TemplateContext(
            run={"id": "run-1"}, session={"id": "loja-1", "data": {"total": 3}}
        )
        assert render("${run.id}/${session.id}", context) == "run-1/loja-1"
        assert render("${session.data.total}", context) == "3"

    def test_render_segredo(self) -> None:
        store = SecretStore()
        store._data["TOKEN"] = "segredo"
        context = TemplateContext(secrets=store)
        assert render("Bearer ${secret.TOKEN}", context) == "Bearer segredo"

    def test_segredo_ausente_nao_expoe_valor(self) -> None:
        context = TemplateContext(secrets=SecretStore())
        with pytest.raises(TemplateError, match="CARAVANA_SECRET_SENHA"):
            render("${secret.SENHA}", context)

    def test_escopo_desconhecido(self) -> None:
        with pytest.raises(TemplateError, match="escopo desconhecido"):
            render("${planeta.nome}", TemplateContext())

    def test_variavel_ausente(self) -> None:
        with pytest.raises(TemplateError, match=r"\$\{var.falta\}"):
            render("${var.falta}", TemplateContext(variables={}))

    def test_escopo_sem_nome(self) -> None:
        with pytest.raises(TemplateError, match="nome depois do escopo"):
            render("${var}", TemplateContext())

    def test_store_le_ambiente(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # O ambiente do processo de teste pode ter outros CARAVANA_SECRET_*;
        # a asserção usa o mapa injetado para ser determinística.
        monkeypatch.setenv("CARAVANA_SECRET_ALFA", "um")
        monkeypatch.setenv("CARAVANA_SECRET_BETA", "dois")
        store = SecretStore()
        store._data.clear()
        store.load_from_environ(
            {"CARAVANA_SECRET_ALFA": "um", "CARAVANA_SECRET_BETA": "dois", "OUTRA": "x"}
        )
        assert store.names() == ["ALFA", "BETA"]
        assert sorted(store.values()) == ["dois", "um"]

    def test_store_le_arquivo(self, tmp_path: Path) -> None:
        path = tmp_path / "secrets.env"
        path.write_text("# comentário\nUSUARIO=demo\nSENHA='abc123'\n", "utf-8")
        store = SecretStore()
        store.load_from_file(path)
        assert store.get("USUARIO") == "demo"
        assert store.get("SENHA") == "abc123"

    def test_store_arquivo_ausente(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="não encontrado"):
            SecretStore().load_from_file(tmp_path / "nada.env")

    def test_store_linha_invalida(self, tmp_path: Path) -> None:
        path = tmp_path / "secrets.env"
        path.write_text("SEM_IGUAL\n", "utf-8")
        with pytest.raises(ConfigError, match="NOME=valor"):
            SecretStore().load_from_file(path)

    def test_redact_mascara_valores(self) -> None:
        assert redact("token abc123 aqui", ["abc123"]) == "token *** aqui"
        assert redact("curto", ["ab"]) == "curto"


class TestUtil:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0.2, "200ms"), (1.5, "1.5s"), (65, "1m 05s"), (3700, "1h 01m")],
    )
    def test_format_duration(self, seconds: float, expected: str) -> None:
        assert format_duration(seconds) == expected

    @pytest.mark.parametrize(
        ("size", "expected"),
        [(512, "512 B"), (2048, "2.0 KB"), (5 * 1024 * 1024, "5.0 MB")],
    )
    def test_human_bytes(self, size: int, expected: str) -> None:
        assert human_bytes(size) == expected

    def test_slugify(self) -> None:
        assert slugify("Catálogo de Produtos!") == "cat-logo-de-produtos"
        assert slugify("***") == "item"

    def test_safe_filename_preserva_extensao(self) -> None:
        assert safe_filename("Catalogo Final.CSV") == "catalogo-final.csv"
        assert safe_filename("") == "arquivo.bin"

    def test_sha256_text_estavel(self) -> None:
        assert sha256_text("a") == sha256_text("a")
        assert sha256_text("a") != sha256_text("b")


class TestEventBus:
    def test_eventos_sao_entregues_ao_sink(self) -> None:
        from caravana import Event, EventBus

        recebidos: list[str] = []

        class Sink:
            def handle(self, event: Event) -> None:
                recebidos.append(event.kind)

            def close(self) -> None:
                recebidos.append("fechado")

        bus = EventBus([Sink()])
        bus.publish(Event(kind="run_started"))
        bus.log("mensagem")
        bus.close()
        assert recebidos == ["run_started", "log", "fechado"]

    def test_drain_retira_eventos(self) -> None:
        from caravana import Event, EventBus

        bus = EventBus()
        bus.publish(Event(kind="a"))
        bus.publish(Event(kind="b"))
        assert [event.kind for event in bus.drain()] == ["a", "b"]
        assert bus.drain() == []

    def test_jsonl_sink_grava_linhas(self, tmp_path: Path) -> None:
        from caravana import Event, EventBus
        from caravana.events import JsonlSink

        path = tmp_path / "eventos.jsonl"
        sink = JsonlSink(path)
        bus = EventBus([sink])
        bus.publish(Event(kind="run_started", message="início"))
        bus.close()
        linhas = path.read_text("utf-8").strip().splitlines()
        assert len(linhas) == 1
        payload = json.loads(linhas[0])
        assert payload["kind"] == "run_started"
        assert payload["message"] == "início"

    def test_counter_mede_pico(self) -> None:
        from caravana import Counter

        counter = Counter()
        assert counter.enter() == 1
        assert counter.enter() == 2
        counter.exit()
        assert counter.peak == 2
        assert counter.exit() == 0


class TestEvaluateWhen:
    def test_condicao_booleana(self) -> None:
        from caravana import TemplateContext, evaluate_when

        context = TemplateContext(variables={"ligado": "true"})
        assert evaluate_when("${var.ligado}", context) is True

    def test_valor_falso(self) -> None:
        from caravana import TemplateContext, evaluate_when

        context = TemplateContext(variables={"ligado": "false"})
        assert evaluate_when("${var.ligado}", context) is False

    def test_comparacao_numerica(self) -> None:
        from caravana import TemplateContext, evaluate_when

        context = TemplateContext(session={"data": {"total": 12}})
        assert evaluate_when("${session.data.total} > 10", context) is True
        assert evaluate_when("${session.data.total} <= 10", context) is False

    def test_comparacao_textual(self) -> None:
        from caravana import TemplateContext, evaluate_when

        context = TemplateContext(variables={"tipo": "premium"})
        assert evaluate_when("${var.tipo} == premium", context) is True
        assert evaluate_when("${var.tipo} != premium", context) is False

    def test_comparador_numerico_com_texto(self) -> None:
        from caravana import ActionError, TemplateContext, evaluate_when

        with pytest.raises(ActionError, match="exige números"):
            evaluate_when("abc > 1", TemplateContext())
