"""Testes do histórico (ledger), do manifesto e dos relatórios."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from caravana import Ledger, build_badge_svg, build_report_html, build_report_markdown
from caravana.report import build_manifest, build_report_text
from caravana.util import sha256_text


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    """Histórico vazio em diretório temporário."""
    with Ledger(tmp_path / "caravana.db") as instance:
        yield instance


def _seed_run(ledger: Ledger, run_id: str = "run-teste") -> str:
    """Cria uma execução com duas sessões: uma ok, outra com falha definitiva."""
    ledger.start_run(
        run_id,
        flow_name="coleta",
        flow_version="1.0.0",
        flow_hash=sha256_text("fluxo"),
        config_path="/tmp",
        run_dir="/tmp/run",
        sessions=2,
        workers=2,
    )
    for attempt, status in ((1, "failed"), (2, "ok")):
        ledger.record_step(
            run_id,
            session_id="s-1",
            step_id="abrir",
            step_name="Abrir",
            action="goto",
            attempt=attempt,
            status=status,
            started_at=f"2026-01-01T00:00:0{attempt}+00:00",
            duration_ms=100.0 * attempt,
            error_message="HTTP 503" if status == "failed" else None,
        )
    ledger.record_step(
        run_id,
        session_id="s-1",
        step_id="coletar",
        step_name="Coletar",
        action="extract",
        attempt=1,
        status="ok",
        started_at="2026-01-01T00:00:03+00:00",
        duration_ms=50.0,
        data={"total": 15},
    )
    ledger.record_step(
        run_id,
        session_id="s-2",
        step_id="abrir",
        step_name="Abrir",
        action="goto",
        attempt=1,
        status="ok",
        started_at="2026-01-01T00:00:01+00:00",
        duration_ms=80.0,
    )
    ledger.record_step(
        run_id,
        session_id="s-2",
        step_id="coletar",
        step_name="Coletar",
        action="extract",
        attempt=3,
        status="failed",
        started_at="2026-01-01T00:00:05+00:00",
        duration_ms=120.0,
        error_type="ActionError",
        error_message="seletor ausente: .produto",
    )
    ledger.finish_run(run_id, "failed")
    return run_id


class TestLedgerRuns:
    def test_start_e_finish(self, ledger: Ledger) -> None:
        record = ledger.start_run(
            "r1",
            flow_name="f",
            flow_version="1",
            flow_hash="abc",
            config_path="/tmp",
            run_dir="/tmp/r1",
            sessions=3,
            workers=2,
            metadata={"engine": "0.1.0"},
        )
        assert record.status == "running"
        ledger.finish_run("r1", "completed")
        finished = ledger.get_run("r1")
        assert finished is not None
        assert finished.status == "completed"
        assert finished.duration_s is not None
        assert finished.to_dict()["sessions"] == 3

    def test_get_run_aceita_prefixo_unico(self, ledger: Ledger) -> None:
        ledger.start_run(
            "catalogo-20260101-abc",
            flow_name="f",
            flow_version="1",
            flow_hash="h",
            config_path="/tmp",
            run_dir="/tmp/a",
            sessions=1,
            workers=1,
        )
        assert ledger.get_run("catalogo-2026") is not None
        assert ledger.get_run("inexistente") is None

    def test_prefixo_ambiguo_retorna_none(self, ledger: Ledger) -> None:
        for suffix in ("aaa", "bbb"):
            ledger.start_run(
                f"coleta-{suffix}",
                flow_name="f",
                flow_version="1",
                flow_hash="h",
                config_path="/tmp",
                run_dir="/tmp/x",
                sessions=1,
                workers=1,
            )
        assert ledger.get_run("coleta-") is None

    def test_list_runs_ordena_e_filtra(self, ledger: Ledger) -> None:
        for index in range(3):
            ledger.start_run(
                f"r{index}",
                flow_name="f",
                flow_version="1",
                flow_hash="h",
                config_path="/tmp",
                run_dir="/tmp/x",
                sessions=1,
                workers=1,
            )
        ledger.finish_run("r2", "failed")
        assert [run.id for run in ledger.list_runs()][:3] == ["r2", "r1", "r0"]
        assert [run.id for run in ledger.list_runs(status="failed")] == ["r2"]
        assert ledger.latest_run() is not None

    def test_find_resumable_respeita_hash(self, ledger: Ledger) -> None:
        ledger.start_run(
            "r1",
            flow_name="f",
            flow_version="1",
            flow_hash="hash-a",
            config_path="/tmp",
            run_dir="/tmp/x",
            sessions=1,
            workers=1,
        )
        ledger.finish_run("r1", "interrupted")
        assert ledger.find_resumable("hash-a") is not None
        assert ledger.find_resumable("hash-b") is None
        assert ledger.find_resumable() is not None

    def test_metadata_resume_execucao_anterior(self, ledger: Ledger) -> None:
        ledger.start_run(
            "r1",
            flow_name="f",
            flow_version="1",
            flow_hash="h",
            config_path="/tmp",
            run_dir="/tmp/x",
            sessions=1,
            workers=1,
        )
        ledger.finish_run("r1", "interrupted")
        record = ledger.start_run(
            "r1",
            flow_name="f",
            flow_version="1",
            flow_hash="h",
            config_path="/tmp",
            run_dir="/tmp/x",
            sessions=1,
            workers=1,
            resumed_from="r0",
        )
        assert record.metadata["resumed_from"] == "r0"


class TestLedgerSteps:
    def test_record_e_leitura(self, ledger: Ledger) -> None:
        _seed_run(ledger)
        steps = ledger.steps_for("run-teste", "s-1")
        assert [step.step_id for step in steps] == ["abrir", "abrir", "coletar"]
        assert steps[-1].data == {"total": 15}
        assert steps[-1].to_dict()["session"] == "s-1"

    def test_last_attempt(self, ledger: Ledger) -> None:
        _seed_run(ledger)
        last = ledger.last_attempt("run-teste", "s-1", "abrir")
        assert last is not None
        assert last.attempt == 2
        assert last.status == "ok"

    def test_checkpoint_lista_concluidos(self, ledger: Ledger) -> None:
        _seed_run(ledger)
        checkpoint = ledger.checkpoint("run-teste")
        assert "abrir" in checkpoint["s-1"]["done"]
        assert "coletar" in checkpoint["s-1"]["done"]
        assert checkpoint["s-2"]["failed"]["coletar"] == "seletor ausente: .produto"

    def test_resume_plan_por_sessao(self, ledger: Ledger) -> None:
        _seed_run(ledger)
        assert ledger.resume_plan("run-teste") == {"s-1": "coletar", "s-2": "abrir"}

    def test_stats_contam_por_passo_e_nao_por_tentativa(self, ledger: Ledger) -> None:
        """Uma tentativa falha e recuperada não é falha definitiva.

        Contar tentativas faria uma execução que se recuperou de um erro
        transitório aparecer com "falha" no relatório — exatamente o oposto do
        que aconteceu.
        """
        _seed_run(ledger)
        stats = ledger.stats("run-teste")
        assert stats.total_sessions == 2
        assert stats.completed == 1
        assert stats.failed == 1
        # `abrir` de s-1 falhou na tentativa 1 e passou na 2: conta como ok.
        assert stats.steps_ok == 3
        # Só `coletar` de s-2 terminou falho de verdade.
        assert stats.steps_failed == 1
        assert stats.steps_retried == 2  # tentativa 2 de s-1 e tentativa 3 de s-2
        assert stats.success_rate == pytest.approx(0.5)
        # 4 passos distintos (2 em s-1, 2 em s-2), somando ok + falhos.
        assert stats.to_dict()["steps_total"] == 4

    def test_stats_sessao_degradada(self, ledger: Ledger) -> None:
        ledger.start_run(
            "r-degradado",
            flow_name="f",
            flow_version="1",
            flow_hash="h",
            config_path="/tmp",
            run_dir="/tmp/x",
            sessions=1,
            workers=1,
        )
        ledger.record_step(
            "r-degradado",
            session_id="s",
            step_id="opcional",
            step_name=None,
            action="goto",
            attempt=1,
            status="failed",
            started_at="2026-01-01T00:00:01+00:00",
            duration_ms=10.0,
            error_message="404",
        )
        ledger.record_step(
            "r-degradado",
            session_id="s",
            step_id="fechamento",
            step_name=None,
            action="extract",
            attempt=1,
            status="ok",
            started_at="2026-01-01T00:00:02+00:00",
            duration_ms=10.0,
        )
        stats = ledger.stats("r-degradado")
        assert stats.degraded == 1
        assert stats.failed == 0
        assert stats.success_rate == pytest.approx(1.0)

    def test_session_results_traz_dados_e_estado(self, ledger: Ledger) -> None:
        _seed_run(ledger)
        results = ledger.session_results("run-teste")
        assert results["s-1"]["status"] == "ok"
        assert results["s-1"]["data"]["coletar"] == {"total": 15}
        assert results["s-2"]["status"] == "failed"
        assert "seletor ausente" in results["s-2"]["error"]


class TestArtifacts:
    def test_registrar_e_listar(self, ledger: Ledger, tmp_path: Path) -> None:
        artifact = tmp_path / "falha.png"
        artifact.write_bytes(b"imagem")
        ledger.start_run(
            "r",
            flow_name="f",
            flow_version="1",
            flow_hash="h",
            config_path="/tmp",
            run_dir=str(tmp_path),
            sessions=1,
            workers=1,
        )
        ledger.record_artifact(
            "r",
            session_id="s",
            step_id="abrir",
            kind="screenshot",
            path=str(artifact),
            size=6,
            digest=sha256_text("imagem"),
        )
        artifacts = ledger.artifacts_for("r")
        assert len(artifacts) == 1
        assert artifacts[0]["kind"] == "screenshot"
        assert ledger.artifacts_for("r", kind="download") == []
        assert ledger.stats("r").artifacts == 1
        assert ledger.stats("r").bytes_written == 6


class TestReports:
    def test_report_markdown_traz_falhas_e_artefatos(self, ledger: Ledger) -> None:
        _seed_run(ledger)
        markdown = build_report_markdown(ledger, "run-teste")
        assert "run-teste" in markdown
        assert "seletor ausente" in markdown
        assert "| `s-1` | sucesso |" in markdown
        assert "## Falhas" in markdown
        assert "Hash do fluxo" in markdown

    def test_report_markdown_execucao_ausente(self, ledger: Ledger) -> None:
        assert "não está no histórico" in build_report_markdown(ledger, "nada")

    def test_report_html_e_autocontido(self, ledger: Ledger) -> None:
        _seed_run(ledger)
        html_report = build_report_html(ledger, "run-teste")
        assert html_report.startswith("<!doctype html>")
        assert "<style>" in html_report
        assert "<svg" in html_report
        assert "http://" not in html_report.split("<style>")[0]  # nenhum recurso externo
        assert "run-teste" in html_report

    def test_report_text_resume(self, ledger: Ledger) -> None:
        _seed_run(ledger)
        text = build_report_text(ledger, "run-teste")
        assert "sessões     1 ok" in text
        assert "✓ s-1" in text
        assert "✗ s-2" in text

    def test_badge_cores_por_taxa(self, ledger: Ledger) -> None:
        _seed_run(ledger)
        stats = ledger.stats("run-teste")
        svg = build_badge_svg(stats)
        assert svg.startswith("<svg")
        assert "1/2 ok" in svg
        assert "#fbbf24" in svg  # metade concluída => amarelo

    def test_badge_sem_dados(self, ledger: Ledger) -> None:
        svg = build_badge_svg(ledger.stats("inexistente"))
        assert "sem dados" in svg

    def test_manifest_guarda_hashes(self, ledger: Ledger, tmp_path: Path) -> None:
        from caravana import load_config

        from ._helpers import write_flow

        flow_path = write_flow(
            tmp_path / "flow.toml", "demo", [{"id": "a", "action": "goto", "params": {"url": "/"}}]
        )
        config_path = tmp_path / "caravana.toml"
        config_path.write_text(
            f'[flow]\npath = "{flow_path.name}"\n\n[[session]]\nid = "s"\n', "utf-8"
        )
        config = load_config(config_path)
        _seed_run(ledger, "r-manifesto")
        artifact = tmp_path / "evidencia.png"
        artifact.write_bytes(b"conteudo")
        ledger.record_artifact(
            "r-manifesto",
            session_id="s",
            step_id="abrir",
            kind="screenshot",
            path=str(artifact),
            size=8,
            digest=sha256_text("conteudo"),
        )
        run = ledger.get_run("r-manifesto")
        assert run is not None
        manifest = build_manifest(
            ledger=ledger,
            run=run,
            config=config,
            stats=ledger.stats("r-manifesto"),
            engine_version="0.1.0",
        )
        assert manifest["schema"] == 1
        assert manifest["flow"]["steps"] == ["a"]
        assert manifest["artifacts"][0]["exists"] is True
        assert manifest["artifacts"][0]["sha256"] == sha256_text("conteudo")
        assert json.dumps(manifest)  # serializável

    def test_manifest_recalcula_hash_quando_ausente(self, ledger: Ledger, tmp_path: Path) -> None:
        from caravana import load_config

        from ._helpers import write_flow

        flow_path = write_flow(tmp_path / "flow.toml", "demo", [{"id": "a", "action": "goto"}])
        config_path = tmp_path / "caravana.toml"
        config_path.write_text(
            f'[flow]\npath = "{flow_path.name}"\n\n[[session]]\nid = "s"\n', "utf-8"
        )
        config = load_config(config_path)
        ledger.start_run(
            "r-hash",
            flow_name="f",
            flow_version="1",
            flow_hash="h",
            config_path="/tmp",
            run_dir=str(tmp_path),
            sessions=1,
            workers=1,
        )
        artifact = tmp_path / "arquivo.bin"
        artifact.write_bytes(b"abc")
        ledger.record_artifact(
            "r-hash",
            session_id="s",
            step_id="a",
            kind="download",
            path=str(artifact),
            size=3,
            digest=None,
        )
        run = ledger.get_run("r-hash")
        assert run is not None
        manifest = build_manifest(
            ledger=ledger, run=run, config=config, stats=ledger.stats("r-hash"), engine_version="0"
        )
        assert manifest["artifacts"][0]["sha256"] == sha256_text("abc")
