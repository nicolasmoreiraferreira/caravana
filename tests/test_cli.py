"""Testes da interface de linha de comando.

A CLI é a superfície que mais erra em silêncio: códigos de saída errados
quebram pipelines de CI sem ninguém perceber. Por isso cada comando é exercitado
tanto no caminho feliz quanto no caminho de erro, verificando o **código de
saída** e não apenas o texto impresso.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from caravana.cli import app

from ._helpers import write_flow

runner = CliRunner()


def invoke(*args: str) -> Result:
    """Invoca a CLI e guarda a saída combinada em ``result.text``.

    O Click 8.2+ separa stdout e stderr. Os erros do Caravana saem em stderr de
    propósito (para não poluir a saída de dados em ``--json``), então os testes
    olham o texto combinado — que é o que um usuário vê no terminal.
    """
    result = runner.invoke(app, list(args))
    result.text = (result.stdout or "") + (result.stderr or "")  # type: ignore[attr-defined]
    return result


def output(result: Result) -> str:
    """Texto combinado produzido pelo comando."""
    return str(getattr(result, "text", result.stdout))


def _project(tmp_path: Path, *, base_url: str = "http://127.0.0.1:9", count: int = 1) -> Path:
    """Cria um projeto mínimo (fluxo + configuração) em ``tmp_path``."""
    flow_path = write_flow(
        tmp_path / "flow.toml",
        "cli",
        [
            {"id": "abrir", "action": "goto", "params": {"url": "/page/1"}},
            {
                "id": "coletar",
                "action": "extract_all",
                "params": {"selector": "article.product .name"},
                "save_as": "nomes",
            },
        ],
    )
    config_path = tmp_path / "caravana.toml"
    config_path.write_text(
        f"""
[flow]
path = "{flow_path.name}"

[engine]
workers = 2
max_attempts = 2

[[session]]
id = "s"
count = {count}
base_url = "{base_url}"
""",
        "utf-8",
    )
    return config_path


class TestVersionValidatePlan:
    def test_version(self) -> None:
        result = invoke("version")
        assert result.exit_code == 0
        assert "caravana" in output(result)

    def test_version_json(self) -> None:
        result = invoke("version", "--json")
        assert result.exit_code == 0
        assert json.loads(result.stdout)["name"] == "caravana"

    def test_validate_ok(self, tmp_path: Path) -> None:
        config_path = _project(tmp_path)
        result = invoke("validate", "-c", str(config_path))
        assert result.exit_code == 0
        assert "consistentes" in output(result)

    def test_validate_json(self, tmp_path: Path) -> None:
        config_path = _project(tmp_path, count=2)
        result = invoke("validate", "-c", str(config_path), "--json")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["sessões"] == 2

    def test_validate_mermaid(self, tmp_path: Path) -> None:
        config_path = _project(tmp_path)
        result = invoke("validate", "-c", str(config_path), "--mermaid")
        assert result.exit_code == 0
        assert output(result).startswith("flowchart TD")

    def test_validate_fluxo_invalido(self, tmp_path: Path) -> None:
        flow_path = write_flow(
            tmp_path / "flow.toml",
            "quebrado",
            [{"id": "a", "action": "goto", "next": "fantasma"}],
        )
        config_path = tmp_path / "caravana.toml"
        config_path.write_text(
            f'[flow]\npath = "{flow_path.name}"\n\n[[session]]\nid = "s"\n', "utf-8"
        )
        result = invoke("validate", "-c", str(config_path))
        assert result.exit_code == 1
        assert "fantasma" in output(result)

    def test_validate_json_com_erro(self, tmp_path: Path) -> None:
        config_path = tmp_path / "vazio.toml"
        config_path.write_text("[[session]]\nid = 's'\n", "utf-8")
        result = invoke("validate", "-c", str(config_path), "--json")
        assert result.exit_code == 1
        assert json.loads(result.stdout)["ok"] is False

    def test_plan_lista_sessoes(self, tmp_path: Path) -> None:
        config_path = _project(tmp_path, count=3)
        result = invoke("plan", "-c", str(config_path))
        assert result.exit_code == 0
        assert "s-1" in output(result)
        assert "s-3" in output(result)

    def test_plan_json(self, tmp_path: Path) -> None:
        config_path = _project(tmp_path, count=2)
        result = invoke("plan", "-c", str(config_path), "--json")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["flow"] == "cli"
        assert len(payload["tasks"]) == 2

    def test_plan_limit(self, tmp_path: Path) -> None:
        config_path = _project(tmp_path, count=4)
        result = invoke("plan", "-c", str(config_path), "--limit", "2", "--json")
        assert len(json.loads(result.stdout)["tasks"]) == 2


class TestRunCommand:
    def test_dry_run_nao_executa(
        self, tmp_path: Path, demo_server: str, requires_browser: None
    ) -> None:
        config_path = _project(tmp_path, base_url=demo_server)
        result = invoke("run", "-c", str(config_path), "--dry-run", "--json")
        assert result.exit_code == 0
        assert "tasks" in result.stdout

    def test_run_com_sucesso(
        self, tmp_path: Path, demo_server: str, requires_browser: None
    ) -> None:
        config_path = _project(tmp_path, base_url=demo_server)
        run_dir = tmp_path / "runs"
        result = invoke(
            "run", "-c", str(config_path), "--run-dir", str(run_dir), "--quiet", "--json"
        )
        assert result.exit_code == 0, output(result)
        events = [json.loads(line) for line in result.stdout.strip().splitlines()]
        assert any(event["kind"] == "run_finished" for event in events)
        summary_file = next(run_dir.glob("*/summary.json"))
        summary = json.loads(summary_file.read_text("utf-8"))
        assert summary["status"] == "completed"
        assert summary["stats"]["completed"] == 1

    def test_run_falha_retorna_codigo_1(
        self, tmp_path: Path, demo_server: str, requires_browser: None
    ) -> None:
        flow_path = write_flow(
            tmp_path / "flow.toml",
            "quebrado",
            [
                {
                    "id": "quebrado",
                    "action": "click",
                    "params": {"selector": "#nao-existe"},
                    "retries": 0,
                }
            ],
        )
        config_path = tmp_path / "caravana.toml"
        config_path.write_text(
            f'[flow]\npath = "{flow_path.name}"\n\n[[session]]\nid = "s"\n'
            f'base_url = "{demo_server}"\n',
            "utf-8",
        )
        result = invoke(
            "run", "-c", str(config_path), "--run-dir", str(tmp_path / "runs"), "--quiet"
        )
        assert result.exit_code == 1
        assert "failed" in output(result)

    def test_var_sobrescreve_variavel(
        self, tmp_path: Path, demo_server: str, requires_browser: None
    ) -> None:
        flow_path = write_flow(
            tmp_path / "flow.toml",
            "variavel",
            [
                {"id": "abrir", "action": "goto", "params": {"url": "/page/${var.pagina}"}},
                {
                    "id": "extrair",
                    "action": "extract",
                    "params": {"fields": {"titulo": "#catalog h1"}},
                    "save_as": "titulo",
                },
            ],
        )
        config_path = tmp_path / "caravana.toml"
        config_path.write_text(
            f'[flow]\npath = "{flow_path.name}"\n\n[vars]\npagina = "1"\n'
            f'\n[[session]]\nid = "s"\nbase_url = "{demo_server}"\n',
            "utf-8",
        )
        run_dir = tmp_path / "runs"
        result = invoke(
            "run",
            "-c",
            str(config_path),
            "--run-dir",
            str(run_dir),
            "--quiet",
            "--var",
            "pagina=3",
        )
        assert result.exit_code == 0, output(result)
        data = json.loads(next(run_dir.glob("*/data/s.json")).read_text("utf-8"))
        assert data["data"]["titulo"]["titulo"] == "Página 3 de 3"

    def test_var_malformada(self, tmp_path: Path) -> None:
        config_path = _project(tmp_path)
        result = invoke("run", "-c", str(config_path), "--var", "semigual")
        assert result.exit_code == 2
        assert "nome=valor" in output(result)

    def test_secrets_file_ausente(self, tmp_path: Path) -> None:
        config_path = _project(tmp_path)
        result = invoke("run", "-c", str(config_path), "--secrets-file", str(tmp_path / "nada.env"))
        assert result.exit_code == 2
        assert "não encontrado" in output(result)

    def test_log_jsonl_e_gerado(
        self, tmp_path: Path, demo_server: str, requires_browser: None
    ) -> None:
        config_path = _project(tmp_path, base_url=demo_server)
        log_path = tmp_path / "eventos.jsonl"
        result = invoke(
            "run",
            "-c",
            str(config_path),
            "--run-dir",
            str(tmp_path / "runs"),
            "--quiet",
            "--log",
            str(log_path),
        )
        assert result.exit_code == 0, output(result)
        eventos = [json.loads(line) for line in log_path.read_text("utf-8").splitlines()]
        assert any(event["kind"] == "step_ok" for event in eventos)


class TestHistoryCommands:
    @pytest.fixture
    def executed(
        self, tmp_path: Path, demo_server: str, requires_browser: None
    ) -> tuple[Path, Path, str]:
        """Executa um fluxo e devolve (config, run_dir, run_id)."""
        config_path = _project(tmp_path, base_url=demo_server)
        run_dir = tmp_path / "runs"
        result = invoke(
            "run", "-c", str(config_path), "--run-dir", str(run_dir), "--quiet", "--json"
        )
        assert result.exit_code == 0, output(result)
        events = [json.loads(line) for line in result.stdout.strip().splitlines()]
        run_id = next(event["data"]["run_id"] for event in events if event["kind"] == "run_started")
        return config_path, run_dir, run_id

    def test_list_sem_historico(self, tmp_path: Path) -> None:
        result = invoke("list", "--run-dir", str(tmp_path / "runs"))
        assert result.exit_code == 0
        assert "nenhuma execução" in output(result)

    def test_list_json(self, executed: tuple[Path, Path, str]) -> None:
        _, run_dir, run_id = executed
        result = invoke("list", "--run-dir", str(run_dir), "--json")
        assert result.exit_code == 0
        runs = json.loads(result.stdout)
        assert runs[0]["id"] == run_id

    def test_show_texto(self, executed: tuple[Path, Path, str]) -> None:
        _, run_dir, run_id = executed
        result = invoke("show", run_id, "--run-dir", str(run_dir))
        assert result.exit_code == 0
        text = output(result)
        assert run_id in text
        assert "abrir" in text

    def test_show_json(self, executed: tuple[Path, Path, str]) -> None:
        _, run_dir, run_id = executed
        result = invoke("show", run_id, "--run-dir", str(run_dir), "--json")
        payload = json.loads(result.stdout)
        assert payload["run"]["id"] == run_id
        assert payload["stats"]["completed"] == 1

    def test_show_manifest(self, executed: tuple[Path, Path, str]) -> None:
        _, run_dir, run_id = executed
        result = invoke("show", run_id, "--run-dir", str(run_dir), "--manifest")
        assert result.exit_code == 0
        manifest = json.loads(result.stdout)
        assert manifest["schema"] == 1

    def test_show_ultima_execucao_sem_id(self, executed: tuple[Path, Path, str]) -> None:
        _, run_dir, _ = executed
        result = invoke("show", "--run-dir", str(run_dir))
        assert result.exit_code == 0

    def test_show_execucao_inexistente(self, executed: tuple[Path, Path, str]) -> None:
        _, run_dir, _ = executed
        result = invoke("show", "nao-existe", "--run-dir", str(run_dir))
        assert result.exit_code == 1

    def test_show_sem_historico(self, tmp_path: Path) -> None:
        result = invoke("show", "--run-dir", str(tmp_path / "nada"))
        assert result.exit_code == 2

    def test_report_regera_arquivos(self, executed: tuple[Path, Path, str], tmp_path: Path) -> None:
        config_path, run_dir, run_id = executed
        report_path = next(run_dir.glob("*/report.md"))
        report_path.unlink()
        result = invoke("report", run_id, "--run-dir", str(run_dir), "-c", str(config_path))
        assert result.exit_code == 0, output(result)
        assert report_path.exists()
        assert "Relatório" in report_path.read_text("utf-8")

    def test_report_formato_texto(self, executed: tuple[Path, Path, str]) -> None:
        config_path, run_dir, run_id = executed
        result = invoke(
            "report", run_id, "--run-dir", str(run_dir), "-c", str(config_path), "-f", "text"
        )
        assert result.exit_code == 0
        assert "execução" in output(result)

    def test_verify_artefatos_integros(self, executed: tuple[Path, Path, str]) -> None:
        _, run_dir, run_id = executed
        result = invoke("verify", run_id, "--run-dir", str(run_dir))
        assert result.exit_code == 0
        assert "conferem" in output(result)

    def test_verify_detecta_adulteracao(self, executed: tuple[Path, Path, str]) -> None:
        _, run_dir, run_id = executed
        manifest_path = next(run_dir.glob("*/manifest.json"))
        manifest = json.loads(manifest_path.read_text("utf-8"))
        if not manifest["artifacts"]:  # nenhuma evidência: nada a adulterar
            pytest.skip("execução sem artefatos")
        Path(manifest["artifacts"][0]["path"]).write_bytes(b"adulterado")
        result = invoke("verify", run_id, "--run-dir", str(run_dir))
        assert result.exit_code == 1
        assert "divergente" in output(result)


class TestDoctor:
    def test_doctor_sem_config(self, tmp_path: Path) -> None:
        result = invoke("doctor", "--json")
        assert result.exit_code in {0, 1}
        payload = json.loads(result.stdout)
        assert any(check["name"] == "python" for check in payload["checks"])

    def test_doctor_com_config(self, tmp_path: Path) -> None:
        config_path = _project(tmp_path, count=2)
        result = invoke("doctor", "-c", str(config_path), "--json")
        payload = json.loads(result.stdout)
        config_check = next(check for check in payload["checks"] if check["name"] == "configuração")
        assert config_check["ok"] is True
        assert "2 sessão" in config_check["detail"]


class TestResumeCommand:
    def test_resume_last_sem_historico(self, tmp_path: Path) -> None:
        config_path = _project(tmp_path)
        result = invoke(
            "run", "-c", str(config_path), "--resume-last", "--run-dir", str(tmp_path / "runs")
        )
        assert result.exit_code == 1
        assert "interrompida" in output(result)

    def test_resume_execucao_inexistente(self, tmp_path: Path) -> None:
        config_path = _project(tmp_path)
        result = invoke(
            "run",
            "-c",
            str(config_path),
            "--resume",
            "nao-existe",
            "--run-dir",
            str(tmp_path / "runs"),
        )
        assert result.exit_code == 3
        assert "não encontrada" in output(result)


class TestJsonSink:
    def test_eventos_em_json_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        from caravana.cli import _JsonSink
        from caravana.events import Event

        sink = _JsonSink()
        sink.handle(Event(kind="run_started", message="ok", session="s"))
        sink.close()
        captured = capsys.readouterr().out.strip()
        payload: dict[str, Any] = json.loads(captured)
        assert payload["kind"] == "run_started"
        assert payload["session"] == "s"
