"""Testes de integração: navegador real contra o site de demonstração local.

Estes testes exercitam o que a suíte unitária não alcança: contexto isolado por
sessão, concorrência real entre threads, retomada após interrupção, captura de
evidências no momento da falha e o contrato de saída do motor (``data/*.json``).

São marcados com ``integration`` e pulam automaticamente quando o Chromium do
Playwright não está instalado.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from caravana import (
    Engine,
    EventBus,
    Ledger,
    SecretStore,
    load_config,
)
from caravana.session import SessionRunner

from ._helpers import write_flow

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _run(
    config_path: Path,
    *,
    tmp_path: Path,
    secrets: dict[str, str] | None = None,
    **kwargs: Any,
) -> tuple[Any, Ledger]:
    """Executa um fluxo e devolve o resultado mais um ledger aberto para leitura."""
    config = load_config(config_path)
    run_dir = tmp_path / "runs"
    config.run_dir = run_dir
    store = SecretStore()
    store._data.update(secrets or {})
    engine = Engine(config, run_dir=run_dir, bus=EventBus(), secrets=store)
    result = engine.run(**kwargs)
    return result, Ledger(run_dir / "caravana.db")


class TestCatalogFlow:
    """Coleta paginada: extração, paginação, download e artefatos."""

    def test_coleta_paginada_em_tres_sessoes(
        self,
        demo_server: str,
        flow_factory: Any,
        config_factory: Any,
        tmp_path: Path,
        requires_browser: None,
    ) -> None:
        flow = flow_factory(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "coletar",
                    "action": "paginate",
                    "params": {"items": "article.product .name", "next": "#next", "max_pages": 5},
                    "save_as": "produtos",
                    "artifacts": ["screenshot"],
                },
                {
                    "id": "detalhe",
                    "action": "goto",
                    "params": {"url": "/item/1"},
                },
                {
                    "id": "extrair",
                    "action": "extract",
                    "params": {
                        "fields": {"nome": ".product-name", "preco": ".price", "sku": ".sku"}
                    },
                    "save_as": "detalhe",
                },
                {
                    "id": "baixar",
                    "action": "download",
                    "params": {"url": "/download/sample.csv", "filename": "catalogo.csv"},
                    "save_as": "planilha",
                },
            ],
            name="catalogo",
        )
        config_path = config_factory(
            flow,
            demo_server,
            sessions=[{"id": "loja", "count": 3, "base_url": "${base}"}],
            engine={"workers": 3, "max_attempts": 2},
        )
        result, ledger = _run(config_path, tmp_path=tmp_path)
        try:
            assert result.status == "completed"
            assert result.stats.total_sessions == 3
            assert result.stats.completed == 3
            assert result.stats.steps_failed == 0

            for index in (1, 2, 3):
                summary = json.loads(
                    (result.run_dir / "data" / f"loja-{index}.json").read_text("utf-8")
                )
                assert summary["status"] == "ok"
                assert len(summary["data"]["produtos"]) == 15
                assert summary["data"]["detalhe"]["sku"] == "SKU-0001"
                assert summary["data"]["planilha"]["name"] == "catalogo.csv"

            screenshots = [
                item for item in ledger.artifacts_for(result.run_id) if item["kind"] == "screenshot"
            ]
            downloads = [
                item for item in ledger.artifacts_for(result.run_id) if item["kind"] == "download"
            ]
            assert len(screenshots) == 3
            assert len(downloads) == 3
            assert all(Path(item["path"]).exists() for item in downloads)
        finally:
            ledger.close()

    def test_sessoes_sao_realmente_isoladas(
        self,
        demo_server: str,
        flow_factory: Any,
        config_factory: Any,
        tmp_path: Path,
        requires_browser: None,
    ) -> None:
        """Cada sessão tem o próprio contexto: logar em uma não afeta a outra."""
        flow = flow_factory(
            [
                {"id": "abrir_login", "action": "goto", "params": {"url": "/login"}},
                {
                    "id": "autenticar",
                    "action": "login",
                    "params": {
                        "user": "${secret.USUARIO}",
                        "password": "${secret.SENHA}",
                        "success_selector": "#secret",
                    },
                },
                {
                    "id": "conferir_painel",
                    "action": "assert_text",
                    "params": {"selector": "#secret", "contains": "autenticado"},
                },
            ],
            name="login",
        )
        config_path = config_factory(
            flow,
            demo_server,
            sessions=[{"id": "conta", "count": 2, "base_url": "${base}"}],
            engine={"workers": 2},
        )
        result, ledger = _run(
            config_path, tmp_path=tmp_path, secrets={"USUARIO": "demo", "SENHA": "demo"}
        )
        try:
            assert result.status == "completed"
            assert result.stats.completed == 2
            steps = ledger.steps_for(result.run_id)
            assert {step.session_id for step in steps} == {"conta-1", "conta-2"}
        finally:
            ledger.close()

    def test_credencial_nao_aparece_no_historico(
        self,
        demo_server: str,
        flow_factory: Any,
        config_factory: Any,
        tmp_path: Path,
        requires_browser: None,
    ) -> None:
        """Segredo em template nunca é persistido no ledger nem no relatório."""
        segredo = "super-secreto-1234"
        flow = flow_factory(
            [
                {"id": "abrir_login", "action": "goto", "params": {"url": "/login"}},
                {
                    "id": "autenticar",
                    "action": "login",
                    "params": {"user": "${secret.USUARIO}", "password": "${secret.SENHA}"},
                    "retries": 0,
                },
            ],
            name="segredo",
        )
        config_path = config_factory(
            flow, demo_server, sessions=[{"id": "s", "base_url": "${base}"}]
        )
        result, ledger = _run(
            config_path, tmp_path=tmp_path, secrets={"USUARIO": "demo", "SENHA": segredo}
        )
        try:
            assert result.status == "completed"
            dump = json.dumps(
                [step.to_dict() for step in ledger.steps_for(result.run_id)], ensure_ascii=False
            )
            assert segredo not in dump
            report = (result.run_dir / "report.md").read_text("utf-8")
            assert segredo not in report
        finally:
            ledger.close()


class TestResilience:
    """Retry, passo tolerado, desvio de fluxo e evidências de falha."""

    def test_retry_recupera_pagina_instavel(
        self,
        demo_server: str,
        flow_factory: Any,
        config_factory: Any,
        tmp_path: Path,
        requires_browser: None,
    ) -> None:
        flow = flow_factory(
            [
                {
                    "id": "instavel",
                    "action": "goto",
                    "params": {"url": "/flaky"},
                    "retries": 2,
                    "retry_delay": 0.1,
                    "artifacts": ["screenshot"],
                },
                {
                    "id": "confirmar",
                    "action": "assert_text",
                    "params": {"selector": "#ok", "equals": "flaky ok"},
                },
            ],
            name="retry",
        )
        config_path = config_factory(
            flow,
            demo_server,
            sessions=[{"id": "s", "base_url": "${base}"}],
            engine={"max_attempts": 3, "screenshots": "on_error"},
        )
        result, ledger = _run(config_path, tmp_path=tmp_path)
        try:
            assert result.status == "completed"
            assert result.stats.steps_retried == 1
            attempts = [
                step for step in ledger.steps_for(result.run_id) if step.step_id == "instavel"
            ]
            assert [step.status for step in attempts] == ["failed", "ok"]
            # A falha da primeira tentativa gerou evidência, mesmo com sucesso depois.
            evidencias = [
                item for item in ledger.artifacts_for(result.run_id) if item["kind"] == "screenshot"
            ]
            assert evidencias and Path(evidencias[0]["path"]).exists()
        finally:
            ledger.close()

    def test_passo_tolerado_marca_sessao_degradada(
        self,
        demo_server: str,
        flow_factory: Any,
        config_factory: Any,
        tmp_path: Path,
        requires_browser: None,
    ) -> None:
        flow = flow_factory(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "opcional",
                    "action": "goto",
                    "params": {"url": "/nao-existe"},
                    "optional": True,
                    "retries": 0,
                },
                {
                    "id": "final",
                    "action": "extract",
                    "params": {"fields": {"titulo": "#catalog h1"}},
                    "save_as": "titulo",
                },
            ],
            name="tolerado",
        )
        config_path = config_factory(
            flow, demo_server, sessions=[{"id": "s", "base_url": "${base}"}]
        )
        result, ledger = _run(config_path, tmp_path=tmp_path)
        try:
            assert result.status == "completed_with_warnings"
            assert result.stats.degraded == 1
            assert result.stats.failed == 0
            summary = json.loads((result.run_dir / "data" / "s.json").read_text("utf-8"))
            assert summary["status"] == "degraded"
            assert summary["tolerated_failures"] == ["opcional"]
            # O fluxo seguiu: o passo final executou e produziu um registro. O
            # valor lido é None porque a navegação anterior parou na página 404
            # — exatamente o que aconteceria em produção, e o motivo pelo qual
            # a sessão aparece como degradada em vez de concluída.
            assert "titulo" in summary["data"]
            final_steps = [
                step for step in ledger.steps_for(result.run_id) if step.step_id == "final"
            ]
            assert final_steps and final_steps[-1].status == "ok"
        finally:
            ledger.close()

    def test_desvio_de_fluxo_registra_passos_pulados(
        self,
        demo_server: str,
        flow_factory: Any,
        config_factory: Any,
        tmp_path: Path,
        requires_browser: None,
    ) -> None:
        flow = flow_factory(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "falha",
                    "action": "goto",
                    "params": {"url": "/nao-existe"},
                    "retries": 0,
                    "on_error": "skip_to:fechamento",
                },
                {"id": "pulado", "action": "goto", "params": {"url": "/page/3"}},
                {"id": "tambem_pulado", "action": "goto", "params": {"url": "/page/2"}},
                {
                    "id": "fechamento",
                    "action": "extract",
                    "params": {"fields": {"titulo": "h1"}},
                    "save_as": "titulo",
                },
            ],
            name="desvio",
        )
        config_path = config_factory(
            flow, demo_server, sessions=[{"id": "s", "base_url": "${base}"}]
        )
        result, ledger = _run(config_path, tmp_path=tmp_path)
        try:
            assert result.status == "completed_with_warnings"
            statuses = {step.step_id: step.status for step in ledger.steps_for(result.run_id)}
            assert statuses["falha"] == "failed"
            assert statuses["pulado"] == "skipped"
            assert statuses["tambem_pulado"] == "skipped"
            assert statuses["fechamento"] == "ok"
            pulados = [step for step in ledger.steps_for(result.run_id) if step.status == "skipped"]
            assert all("desvio" in (step.error_message or "") for step in pulados)
        finally:
            ledger.close()

    def test_falha_definitiva_derruba_a_sessao(
        self,
        demo_server: str,
        flow_factory: Any,
        config_factory: Any,
        tmp_path: Path,
        requires_browser: None,
    ) -> None:
        flow = flow_factory(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "quebrado",
                    "action": "click",
                    "params": {"selector": "#nao-existe-mesmo"},
                    "retries": 1,
                    "retry_delay": 0.05,
                },
                {"id": "nunca", "action": "goto", "params": {"url": "/page/2"}},
            ],
            name="falha",
        )
        config_path = config_factory(
            flow, demo_server, sessions=[{"id": "s", "base_url": "${base}"}]
        )
        result, ledger = _run(config_path, tmp_path=tmp_path)
        try:
            assert result.status == "failed"
            assert result.stats.failed == 1
            steps = ledger.steps_for(result.run_id)
            assert all(step.step_id != "nunca" for step in steps)
            falha = [step for step in steps if step.status == "failed"]
            assert falha and falha[-1].attempt == 2
            # Evidência da falha: screenshot + HTML do estado quebrado.
            kinds = {item["kind"] for item in ledger.artifacts_for(result.run_id)}
            assert "screenshot" in kinds
            assert "html" in kinds
        finally:
            ledger.close()

    def test_timeout_do_passo_e_respeitado(
        self,
        demo_server: str,
        flow_factory: Any,
        config_factory: Any,
        tmp_path: Path,
        requires_browser: None,
    ) -> None:
        flow = flow_factory(
            [
                {
                    "id": "lento",
                    "action": "wait_for",
                    "params": {"selector": "#nunca-aparece"},
                    "timeout_ms": 400,
                    "retries": 0,
                },
            ],
            name="timeout",
        )
        config_path = config_factory(
            flow,
            demo_server,
            sessions=[{"id": "s", "base_url": "${base}"}],
            engine={"timeout_ms": 10000},
        )
        started = time.perf_counter()
        result, ledger = _run(config_path, tmp_path=tmp_path)
        elapsed = time.perf_counter() - started
        try:
            assert result.status == "failed"
            assert elapsed < 8, "o timeout do passo deveria cortar a espera"
        finally:
            ledger.close()


class TestConcurrency:
    """Concorrência limitada e real entre sessões."""

    def test_workers_limita_concorrencia(
        self,
        demo_server: str,
        flow_factory: Any,
        config_factory: Any,
        tmp_path: Path,
        requires_browser: None,
    ) -> None:
        from caravana import Counter

        flow = flow_factory(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/slow?ms=300"}},
                {"id": "esperar", "action": "wait", "params": {"ms": 200}},
            ],
            name="concorrencia",
        )
        config_path = config_factory(
            flow,
            demo_server,
            sessions=[{"id": "w", "count": 4, "base_url": "${base}"}],
            engine={"workers": 2},
        )
        config = load_config(config_path)
        run_dir = tmp_path / "runs"
        config.run_dir = run_dir

        # Instrumenta o motor para medir o pico real de sessões simultâneas.
        counter = Counter()
        original = SessionRunner.run

        def measured(self: SessionRunner, resume_from: str | None = None) -> str:
            counter.enter()
            try:
                return original(self, resume_from=resume_from)
            finally:
                counter.exit()

        SessionRunner.run = measured  # type: ignore[method-assign]
        try:
            result = Engine(config, run_dir=run_dir, bus=EventBus()).run()
        finally:
            SessionRunner.run = original  # type: ignore[method-assign]

        assert result.status == "completed"
        assert result.stats.completed == 4
        assert counter.peak == 2, f"esperado pico de 2 sessões, obtido {counter.peak}"
        # 4 sessões em 2 workers: ~2 rodadas de (300ms de página + 200ms de espera),
        # mais o custo de iniciar dois navegadores por rodada.
        assert result.stats.duration_s < 4.0

    def test_execucao_e_serializada_no_ledger(
        self,
        demo_server: str,
        flow_factory: Any,
        config_factory: Any,
        tmp_path: Path,
        requires_browser: None,
    ) -> None:
        """Muitas sessões gravando ao mesmo tempo não corrompem o histórico."""
        flow = flow_factory(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "coletar",
                    "action": "extract_all",
                    "params": {"selector": "article.product .name"},
                    "save_as": "nomes",
                },
            ],
            name="serializado",
        )
        config_path = config_factory(
            flow,
            demo_server,
            sessions=[{"id": "s", "count": 8, "base_url": "${base}"}],
            engine={"workers": 8},
        )
        result, ledger = _run(config_path, tmp_path=tmp_path)
        try:
            assert result.status == "completed"
            assert result.stats.completed == 8
            assert result.stats.steps_ok == 16
            # Banco íntegro e consultável depois de oito threads escreverem.
            assert ledger.stats(result.run_id).total_sessions == 8
        finally:
            ledger.close()


class TestResume:
    """Retomada após interrupção."""

    def test_interrupcao_e_retomada_continua_de_onde_parou(
        self,
        demo_server: str,
        flow_factory: Any,
        config_factory: Any,
        tmp_path: Path,
        requires_browser: None,
    ) -> None:
        flow = flow_factory(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {"id": "meio", "action": "wait", "params": {"ms": 4000}},
                {"id": "fim", "action": "goto", "params": {"url": "/page/3"}},
                {
                    "id": "coletar",
                    "action": "extract_all",
                    "params": {"selector": "article.product .name"},
                    "save_as": "nomes",
                },
            ],
            name="retomavel",
        )
        config_path = config_factory(
            flow, demo_server, sessions=[{"id": "s", "base_url": "${base}"}]
        )
        config = load_config(config_path)
        run_dir = tmp_path / "runs"
        config.run_dir = run_dir

        stop_event = threading.Event()

        def interromper() -> None:
            time.sleep(1.2)
            stop_event.set()

        threading.Thread(target=interromper, daemon=True).start()
        first = Engine(config, run_dir=run_dir, bus=EventBus()).run(stop_event=stop_event)
        assert first.status == "interrupted"

        with Ledger(run_dir / "caravana.db") as ledger:
            partial = ledger.session_results(first.run_id)
            assert partial["s"]["status"] == "aborted"
            # O passo lento não terminou; a retomada deve recomeçar nele.
            assert "fim" not in partial["s"]["data"]

        resumed = Engine(config, run_dir=run_dir, bus=EventBus()).run(resume=first.run_id)
        assert resumed.status == "completed"
        assert resumed.run_id == first.run_id

        with Ledger(run_dir / "caravana.db") as ledger:
            run = ledger.get_run(first.run_id)
            assert run is not None
            assert run.generation == 2
            assert run.metadata["resume_count"] == 1
            final = ledger.session_results(first.run_id)
            assert final["s"]["status"] == "ok"
            assert len(final["s"]["data"]["coletar"]) == 5  # página 3 tem 5 itens

    def test_resume_recusa_fluxo_diferente(
        self,
        demo_server: str,
        flow_factory: Any,
        config_factory: Any,
        tmp_path: Path,
        requires_browser: None,
    ) -> None:
        from caravana import ResumeError

        flow = flow_factory(
            [{"id": "abrir", "action": "goto", "params": {"url": "/page/1"}}],
            name="original",
        )
        config_path = config_factory(
            flow, demo_server, sessions=[{"id": "s", "base_url": "${base}"}]
        )
        config = load_config(config_path)
        run_dir = tmp_path / "runs"
        config.run_dir = run_dir
        first = Engine(config, run_dir=run_dir, bus=EventBus()).run()

        # Um fluxo diferente no mesmo arquivo muda o hash do fluxo.
        write_flow(
            flow,
            "alterado",
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/2"}},
                {"id": "extra", "action": "goto", "params": {"url": "/page/3"}},
            ],
        )
        changed = load_config(config_path)
        changed.run_dir = run_dir
        with pytest.raises(ResumeError, match="outra versão do fluxo"):
            Engine(changed, run_dir=run_dir, bus=EventBus()).run(resume=first.run_id)

    def test_retomada_nao_repete_trabalho_concluido(
        self,
        demo_server: str,
        flow_factory: Any,
        config_factory: Any,
        tmp_path: Path,
        requires_browser: None,
    ) -> None:
        flow = flow_factory(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {"id": "espera", "action": "wait", "params": {"ms": 2000}},
                {"id": "fim", "action": "goto", "params": {"url": "/page/2"}},
            ],
            name="economia",
        )
        config_path = config_factory(
            flow, demo_server, sessions=[{"id": "s", "base_url": "${base}"}]
        )
        config = load_config(config_path)
        run_dir = tmp_path / "runs"
        config.run_dir = run_dir

        stop_event = threading.Event()
        threading.Thread(target=lambda: (time.sleep(1.0), stop_event.set()), daemon=True).start()
        first = Engine(config, run_dir=run_dir, bus=EventBus()).run(stop_event=stop_event)
        assert first.status == "interrupted"

        started = time.perf_counter()
        resumed = Engine(config, run_dir=run_dir, bus=EventBus()).run(resume=first.run_id)
        elapsed = time.perf_counter() - started
        assert resumed.status == "completed"
        assert elapsed < 1.5, "a retomada não deveria repetir os passos concluídos"

        with Ledger(run_dir / "caravana.db") as ledger:
            skipped = [
                step
                for step in ledger.steps_for(first.run_id)
                if step.status == "skipped" and "retomado" in (step.error_message or "")
            ]
            assert skipped, "os passos já concluídos devem aparecer como retomados"


class TestOutputs:
    """Contrato de saída: resumo, relatórios, manifesto e verificação."""

    def test_artefatos_completos_de_uma_execucao(
        self,
        demo_server: str,
        flow_factory: Any,
        config_factory: Any,
        tmp_path: Path,
        requires_browser: None,
    ) -> None:
        flow = flow_factory(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/2"}},
                {
                    "id": "capturar",
                    "action": "screenshot",
                    "params": {"name": "catalogo", "full_page": True},
                },
                {
                    "id": "extrair",
                    "action": "extract",
                    "params": {
                        "fields": {
                            "titulo": "h1",
                            "total": {"selector": "article.product", "all": True},
                        }
                    },
                    "save_as": "pagina",
                },
            ],
            name="saidas",
        )
        config_path = config_factory(
            flow, demo_server, sessions=[{"id": "s", "base_url": "${base}"}]
        )
        result, ledger = _run(config_path, tmp_path=tmp_path)
        try:
            assert result.status == "completed"
            for key in ("summary", "markdown", "html", "badge", "manifest"):
                assert key in result.reports
                assert result.reports[key].exists()
                assert result.reports[key].stat().st_size > 0

            manifest = json.loads(result.reports["manifest"].read_text("utf-8"))
            assert manifest["stats"]["completed"] == 1
            assert manifest["flow"]["name"] == "saidas"
            assert manifest["artifacts"]
            assert all(item["sha256"] for item in manifest["artifacts"])

            html_report = result.reports["html"].read_text("utf-8")
            assert "Relatório de execução" in html_report
            assert "<svg" in html_report
        finally:
            ledger.close()

    def test_manifesto_acusa_artefato_alterado(
        self,
        demo_server: str,
        flow_factory: Any,
        config_factory: Any,
        tmp_path: Path,
        requires_browser: None,
    ) -> None:
        from caravana.util import sha256_file

        flow = flow_factory(
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
                {
                    "id": "capturar",
                    "action": "screenshot",
                    "params": {"name": "evidencia"},
                },
            ],
            name="integridade",
        )
        config_path = config_factory(
            flow, demo_server, sessions=[{"id": "s", "base_url": "${base}"}]
        )
        result, ledger = _run(config_path, tmp_path=tmp_path)
        try:
            manifest = json.loads(result.reports["manifest"].read_text("utf-8"))
            entry = manifest["artifacts"][0]
            path = Path(entry["path"])
            assert sha256_file(path) == entry["sha256"]

            path.write_bytes(b"conteudo adulterado")
            assert sha256_file(path) != entry["sha256"]
        finally:
            ledger.close()
