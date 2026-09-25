"""Sessão isolada de navegador e o catálogo de ações.

Cada sessão é dona exclusiva do seu ``BrowserContext``: cookies, storage,
cache, viewport e locale próprios. Isso significa que duas sessões podem estar
logadas com usuários diferentes ao mesmo tempo, sem interferência — o problema
que normalmente aparece quando um único navegador é reaproveitado.

Regras de threading (as mesmas que sustentam o BOTSNAY):

* um ``BrowserContext`` nunca é compartilhado entre threads;
* cada thread cria o seu próprio objeto Playwright e o destrói ao terminar;
* resultados são transportados como dados simples (dict/list/str), nunca como
  objetos de página, para o ledger e para os relatórios.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    sync_playwright,
)

from .config import Config, SessionConfig
from .errors import ActionError, SessionError, StepFailed
from .events import Event, EventBus
from .ledger import Ledger
from .robots import RobotsRules, parse_robots
from .templating import SecretStore, TemplateContext, render, render_value
from .util import redact, safe_filename, sha256_file, slugify, utc_now

__all__ = ["ActionResult", "SessionRunner", "evaluate_when"]

_COMPARISON_RE = re.compile(
    r"^\s*(?P<left>[^=!<>]+?)\s*(?P<op>==|!=|>=|<=|>|<)\s*(?P<right>.+?)\s*$"
)
_FALSEY = {"", "0", "false", "no", "não", "nao", "off"}
_SESSION_STATUS_ABORTED = "aborted"


@dataclass(slots=True)
class ActionResult:
    """Resultado de uma ação: valor salvo em ``data`` e artefatos produzidos."""

    value: Any = None
    artifacts: list[Path] = field(default_factory=list)
    message: str = ""


def evaluate_when(expression: str, context: TemplateContext) -> bool:
    """Avalia uma condição simples declarada em ``when``.

    Suporta comparações (``==``, ``!=``, ``>``, ``<``, ``>=``, ``<=``) e a forma
    direta, em que um valor vazio, ``0``, ``false`` ou ``off`` é falso. Os dois
    lados são resolvidos com o mesmo motor de templates dos demais parâmetros,
    então ``${session.data.total} > 10`` funciona sem sintaxe extra.
    """
    text = expression.strip()
    match = _COMPARISON_RE.match(text)
    if not match:
        resolved = render(text, context).strip().lower()
        return resolved not in _FALSEY

    left = render(match.group("left").strip(), context).strip()
    right = render(match.group("right").strip(), context).strip()
    operator = match.group("op")

    def as_number(value: str) -> float | None:
        try:
            return float(value.replace(",", "."))
        except ValueError:
            return None

    left_number, right_number = as_number(left), as_number(right)
    if operator == "==":
        if left_number is not None and right_number is not None:
            return left_number == right_number
        return left == right
    if operator == "!=":
        if left_number is not None and right_number is not None:
            return left_number != right_number
        return left != right
    if left_number is None or right_number is None:
        hint = ""
        if left.strip().startswith(("[", "{")) or right.strip().startswith(("[", "{")):
            hint = (
                "; o valor comparado é uma lista ou objeto — compare um campo "
                "escalar (por exemplo, o texto de um elemento) ou conte os itens "
                "em um passo próprio"
            )
        raise ActionError(
            f"comparador '{operator}' exige números em when='{expression}'"
            f" (lado esquerdo: {left[:40]!r}, lado direito: {right[:40]!r}){hint}"
        )
    if operator == ">":
        return left_number > right_number
    if operator == "<":
        return left_number < right_number
    if operator == ">=":
        return left_number >= right_number
    return left_number <= right_number


class SessionRunner:
    """Executa o fluxo em uma sessão isolada, dentro da thread atual."""

    def __init__(
        self,
        *,
        config: Config,
        session_config: SessionConfig,
        session_id: str,
        index: int,
        run_id: str,
        run_dir: Path,
        ledger: Ledger,
        bus: EventBus,
        secrets: SecretStore | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.session_config = session_config
        self.session_id = session_id
        self.index = index
        self.run_id = run_id
        self.run_dir = run_dir
        self.ledger = ledger
        self.bus = bus
        self.secret_store = secrets or SecretStore()
        self.stop_event = stop_event or threading.Event()

        self.data: dict[str, Any] = {}
        self.pages_visited = 0
        self.base_url = self._resolve_base_url()
        self.artifacts_dir = run_dir / "artifacts" / session_id
        self.data_dir = run_dir / "data"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._robots: dict[str, RobotsRules] = {}
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._tracing = False
        self.status = "ok"
        self.error: str | None = None
        self.tolerated_failures: list[str] = []

    @property
    def secrets(self) -> list[str]:
        """Valores sensíveis conhecidos, para mascaramento em textos de saída."""
        return self.secret_store.values()

    def _resolve_base_url(self) -> str:
        """Resolve ``${var.…}``/``${env.…}`` na ``base_url`` declarada na sessão."""
        raw = self.session_config.base_url
        if not raw:
            return ""
        context = TemplateContext(
            variables=self.config.variables,
            secrets=self.secret_store,
            run={"id": self.run_id, "dir": str(self.run_dir)},
            session={"id": self.session_id, "index": self.index},
        )
        return render(raw, context).rstrip("/")

    # -- contexto de templates --------------------------------------------
    def template_context(self, step_id: str = "") -> TemplateContext:
        """Escopos disponíveis: variáveis, segredos, dados da execução e da sessão."""
        return TemplateContext(
            variables=self.config.variables,
            secrets=self.secret_store,
            run={
                "id": self.run_id,
                "dir": str(self.run_dir),
                "started_at": utc_now().isoformat(),
            },
            session={
                "id": self.session_id,
                "index": self.index,
                "base_url": self.base_url,
                "data": self.data,
                "page": self.pages_visited,
                "step": step_id,
            },
        )

    # -- ciclo de vida ----------------------------------------------------
    def __enter__(self) -> SessionRunner:
        started = time.perf_counter()
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self._context = self._browser.new_context(**self._context_options())
            self._context.set_default_timeout(self.config.timeout_ms)
            self._context.set_default_navigation_timeout(self.config.timeout_ms)
            self._page = self._context.new_page()
            self._page.set_default_timeout(self.config.timeout_ms)
        except Exception as exc:  # pragma: no cover - depende do ambiente
            self._teardown()
            raise SessionError(
                f"sessão '{self.session_id}': falha ao iniciar navegador: {exc}"
            ) from exc

        self._publish(
            "session_started",
            message=f"sessão pronta em {time.perf_counter() - started:.2f}s",
        )
        return self

    def __exit__(self, *exc: object) -> None:
        self._teardown()

    def _context_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "viewport": {
                "width": self.session_config.viewport[0],
                "height": self.session_config.viewport[1],
            },
            "locale": self.session_config.locale,
            "timezone_id": self.session_config.timezone_id,
            "user_agent": self.session_config.user_agent or self.config.user_agent,
            "extra_http_headers": dict(self.session_config.headers),
            "accept_downloads": True,
        }
        if self.session_config.reuse_profile:
            state_file = _profile_path(self.run_dir, self.session_config.id)
            state_file.parent.mkdir(parents=True, exist_ok=True)
            # Na primeira execução o arquivo ainda não existe; passar um caminho
            # inexistente para o Playwright levanta erro. A ausência é o estado
            # normal de um perfil novo, não uma falha.
            if state_file.exists():
                options["storage_state"] = str(state_file)
        return options

    def _teardown(self) -> None:
        """Encerra contexto, navegador e Playwright, nesta ordem.

        Cada etapa é protegida individualmente: se o contexto já morreu, o
        navegador ainda precisa ser fechado — caso contrário o processo de
        teste deixaria um Chromium órfão.
        """
        for closer in (self._close_context, self._close_browser, self._stop_playwright):
            with contextlib.suppress(Exception):  # pragma: no cover - já encerrado
                closer()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    def _close_context(self) -> None:
        if self._context is not None:
            self._context.close()

    def _close_browser(self) -> None:
        if self._browser is not None:
            self._browser.close()

    def _stop_playwright(self) -> None:
        if self._playwright is not None:
            self._playwright.stop()

    def save_profile(self) -> None:
        """Persiste cookies e storage quando a sessão declara ``reuse_profile``."""
        if not self.session_config.reuse_profile or self._context is None:
            return
        state_file = _profile_path(self.run_dir, self.session_config.id)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(state_file))

    @property
    def page(self) -> Page:
        """Página ativa da sessão."""
        if self._page is None:  # pragma: no cover - uso incorreto
            raise SessionError("sessão não iniciada: use o gerenciador de contexto")
        return self._page

    # -- execução ---------------------------------------------------------
    def run(self, resume_from: str | None = None) -> str:
        """Percorre os passos do fluxo a partir do ponto de retomada.

        Args:
            resume_from: id do último passo concluído. Os passos até ele são
                considerados prontos e registrados como retomados.

        Returns:
            O estado final da sessão: ``ok``, ``degraded``, ``failed`` ou ``aborted``.
        """
        flow = self.config.flow
        order = flow.order()
        mapping = flow.step_map()
        if resume_from and resume_from in order:
            cut = order.index(resume_from)
            done, pending = order[: cut + 1], order[cut + 1 :]
            for step_id in done:
                self.ledger.record_step(
                    self.run_id,
                    session_id=self.session_id,
                    step_id=step_id,
                    step_name=mapping[step_id].name,
                    action=mapping[step_id].action,
                    attempt=1,
                    status="skipped",
                    started_at=utc_now().isoformat(),
                    duration_ms=0.0,
                    error_message="retomado de execução anterior",
                )
            self._publish(
                "step_skipped",
                message=f"{len(done)} passo(s) retomados de execução anterior",
                data={"resumed": done},
            )
        else:
            pending = order

        cursor = 0
        while cursor < len(pending):
            if self.stop_event.is_set():
                self.status = _SESSION_STATUS_ABORTED
                self.error = "execução interrompida pelo usuário"
                # A interrupção é registrada no histórico: sem esta linha a
                # sessão apareceria como concluída, e o relatório mentiria
                # sobre o que realmente aconteceu.
                interrupted_step = mapping[pending[cursor]]
                self.ledger.record_step(
                    self.run_id,
                    session_id=self.session_id,
                    step_id=interrupted_step.id,
                    step_name=interrupted_step.name,
                    action=interrupted_step.action,
                    attempt=0,
                    status="aborted",
                    started_at=utc_now().isoformat(),
                    duration_ms=0.0,
                    error_message="execução interrompida pelo usuário antes do passo",
                )
                self._publish(
                    "log",
                    level="warning",
                    message=f"sessão interrompida antes de '{interrupted_step.id}'",
                )
                break
            step = mapping[pending[cursor]]
            outcome = self._run_step(step)
            if outcome is None:
                self.status = "failed"
                break
            target, advance = outcome
            if target is not None and target in pending:
                target_index = pending.index(target)
                if target_index > cursor + 1:
                    # Um desvio deixa passos para trás. Registrá-los como
                    # ignorados é o que permite auditar *por que* um passo não
                    # aparece no resultado, em vez de simplesmente não existir.
                    self._record_skipped_range(pending[cursor + 1 : target_index], mapping)
                cursor = target_index
                continue
            cursor += advance
        return self.status

    def _record_skipped_range(self, step_ids: list[str], mapping: dict[str, Any]) -> None:
        """Marca como ignorados os passos contornados por um desvio de fluxo."""
        for step_id in step_ids:
            step = mapping[step_id]
            self.ledger.record_step(
                self.run_id,
                session_id=self.session_id,
                step_id=step.id,
                step_name=step.name,
                action=step.action,
                attempt=1,
                status="skipped",
                started_at=utc_now().isoformat(),
                duration_ms=0.0,
                error_message="pulado por desvio de fluxo",
            )
            self._publish("step_skipped", step=step.id, message="pulado por desvio de fluxo")

    def _run_step(self, step: Any) -> tuple[str | None, int] | None:
        """Executa um passo com tentativas.

        Returns:
            ``None`` quando a sessão deve parar (falha definitiva), ou
            ``(destino, avanço)`` com o próximo passo explícito ou o avanço padrão.
        """
        context = self.template_context(step.id)
        if step.when:
            try:
                should_run = evaluate_when(step.when, context)
            except Exception as exc:
                # A causa precisa aparecer: "falha ao avaliar 'when'" sozinho não
                # diz se o problema é variável ausente, sintaxe ou comparação
                # entre tipos incompatíveis.
                self._record_failure(step, 1, exc, f"falha ao avaliar when='{step.when}': {exc}")
                return None
            if not should_run:
                self.ledger.record_step(
                    self.run_id,
                    session_id=self.session_id,
                    step_id=step.id,
                    step_name=step.name,
                    action=step.action,
                    attempt=1,
                    status="skipped",
                    started_at=utc_now().isoformat(),
                    duration_ms=0.0,
                    error_message=f"condição falsa: {step.when}",
                )
                self._publish("step_skipped", step=step.id, message=f"condição falsa: {step.when}")
                return (None, 1)

        attempts = max(1, min(step.retries + 1, self.config.max_attempts))
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            started_at = utc_now().isoformat()
            start = time.perf_counter()
            trace_wanted = "trace" in step.artifacts
            if trace_wanted:
                self._start_trace(step.id)
            try:
                result = self._dispatch(step, context)
            except Exception as exc:
                duration_ms = (time.perf_counter() - start) * 1000
                last_error = exc
                artifacts = self._capture_failure(step)
                if trace_wanted:
                    artifacts += self._stop_trace(step.id)
                message = redact(str(exc), self.secrets)[:2000]
                self.ledger.record_step(
                    self.run_id,
                    session_id=self.session_id,
                    step_id=step.id,
                    step_name=step.name,
                    action=step.action,
                    attempt=attempt,
                    status="failed",
                    started_at=started_at,
                    duration_ms=duration_ms,
                    error_type=type(exc).__name__,
                    error_message=message,
                    artifacts=[str(path) for path in artifacts],
                )
                self._register_artifacts(step.id, artifacts)
                if attempt < attempts and not self.stop_event.is_set():
                    delay = step.retry_delay * attempt
                    self._publish(
                        "step_retry",
                        step=step.id,
                        attempt=attempt,
                        level="warning",
                        message=f"tentativa {attempt}/{attempts} falhou ({type(exc).__name__}); "
                        f"nova tentativa em {delay:.1f}s",
                    )
                    time.sleep(delay)
                    continue
                break

            duration_ms = (time.perf_counter() - start) * 1000
            if trace_wanted:
                result.artifacts += self._stop_trace(step.id)
            artifacts = list(result.artifacts) + self._capture_success(step)
            if step.save_as and result.value is not None:
                self.data[step.save_as] = result.value
            self.ledger.record_step(
                self.run_id,
                session_id=self.session_id,
                step_id=step.id,
                step_name=step.name,
                action=step.action,
                attempt=attempt,
                status="ok",
                started_at=started_at,
                duration_ms=duration_ms,
                data=_truncate_for_ledger(result.value, self.secrets),
                artifacts=[str(path) for path in artifacts],
            )
            self._register_artifacts(step.id, artifacts)
            self._publish(
                "step_ok",
                step=step.id,
                attempt=attempt,
                duration_ms=duration_ms,
                message=result.message,
                data={"saved": step.save_as} if step.save_as else {},
            )
            if step.next:
                return (step.next, 1)
            return (None, 1)

        # Esgotou as tentativas: a falha é definitiva *para este passo*. Ainda
        # pode ser tolerada — passo opcional, `on_error = "continue"` ou desvio
        # controlado. Só então a sessão é marcada como degradada, e não como
        # falhada: a diferença aparece no relatório e no código de saída.
        error = StepFailed(step.id, attempts, last_error or ActionError("falha desconhecida"))
        message = redact(str(error), self.secrets)[:2000]
        tolerated = bool(
            step.optional
            or step.on_error in {"continue"}
            or (step.on_error and step.on_error.startswith("skip_to:"))
        )
        self._record_failure(step, attempts, error, message, tolerated=tolerated)
        if step.optional or step.on_error == "continue":
            self._publish(
                "step_failed",
                step=step.id,
                level="warning",
                message=f"passo tolerado após {attempts} tentativa(s): {message}",
            )
            return (None, 1)
        if step.on_error and step.on_error.startswith("skip_to:"):
            target = step.on_error.split(":", 1)[1]
            self._publish(
                "step_failed",
                step=step.id,
                level="warning",
                message=f"seguindo para '{target}' após falha: {message}",
            )
            return (target, 1)
        return None

    def _record_failure(
        self,
        step: Any,
        attempts: int,
        exc: BaseException,
        message: str,
        *,
        tolerated: bool = False,
    ) -> None:
        """Registra a falha definitiva de um passo.

        ``tolerated`` distingue "o passo falhou e a sessão continua" de "o passo
        falhou e a sessão parou aqui". No primeiro caso o estado da sessão vira
        ``degraded``: o resultado existe, mas chegou com uma lacuna declarada.
        """
        if tolerated:
            if self.status == "ok":
                self.status = "degraded"
            self.tolerated_failures.append(step.id)
        else:
            self.status = "failed"
            self.error = message[:500]
        self.ledger.record_step(
            self.run_id,
            session_id=self.session_id,
            step_id=step.id,
            step_name=step.name,
            action=step.action,
            attempt=attempts,
            status="failed",
            started_at=utc_now().isoformat(),
            duration_ms=0.0,
            error_type=type(exc).__name__,
            error_message=message[:2000],
        )
        self._publish(
            "step_failed", step=step.id, level="warning" if tolerated else "error", message=message
        )

    def _publish(self, kind: str, **kwargs: Any) -> None:
        kwargs.setdefault("session", self.session_id)
        self.bus.publish(Event(kind=kind, **kwargs))

    # -- despacho de ações -------------------------------------------------
    def _dispatch(self, step: Any, context: TemplateContext) -> ActionResult:
        handler = getattr(self, f"_action_{step.action}", None)
        if handler is None:  # pragma: no cover - validação estática cobre isso
            raise ActionError(f"ação não implementada: {step.action}")
        params = render_value(step.params, context)
        timeout_ms = step.timeout_ms or self.config.timeout_ms
        self.page.set_default_timeout(timeout_ms)
        return handler(params)  # type: ignore[no-any-return]

    def _url(self, value: str) -> str:
        """Resolve URL relativa contra a ``base_url`` da sessão."""
        if not value:
            raise ActionError("URL vazia")
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme in {"http", "https"}:
            return value
        if parsed.scheme in {"file", "data", "about"}:
            return value
        base = self.base_url or self.config.base_url
        if not base:
            raise ActionError(
                f"URL relativa '{value}' sem base_url definida na sessão '{self.session_id}'"
            )
        return urllib.parse.urljoin(base + "/", value.lstrip("/"))

    def _check_robots(self, url: str) -> None:
        if not self.config.respect_robots:
            return
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return
        origin = f"{parsed.scheme}://{parsed.netloc}"
        rules = self._robots.get(origin)
        if rules is None:
            # Falha de rede ao buscar o robots.txt não bloqueia a execução: sem
            # arquivo, não há regra declarada. Já uma regra lida é obedecida.
            try:
                request = urllib.request.Request(
                    urllib.parse.urljoin(origin, "/robots.txt"),
                    headers={"User-Agent": self.config.user_agent},
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    text = response.read(1_000_000).decode("utf-8", "replace")
                rules = parse_robots(text)
            except Exception:
                rules = RobotsRules()
            self._robots[origin] = rules
        if not rules.can_fetch(self.config.user_agent, url):
            raise ActionError(f"robots.txt de {origin} proíbe o acesso a {parsed.path}")

    # -- ações -------------------------------------------------------------
    def _action_goto(self, params: dict[str, Any]) -> ActionResult:
        url = self._url(str(params.get("url", "")))
        self._check_robots(url)
        wait_until = str(params.get("wait_until", "domcontentloaded"))
        response = self.page.goto(url, wait_until=wait_until)  # type: ignore[arg-type]
        self.pages_visited += 1
        status = response.status if response else 0
        if status >= 400:
            raise ActionError(f"HTTP {status} em {url}")
        title = self.page.title()
        return ActionResult(
            value={"url": url, "status": status, "title": title},
            message=f"abriu {url} ({status})",
        )

    def _action_click(self, params: dict[str, Any]) -> ActionResult:
        selector = str(params.get("selector", ""))
        if not selector:
            raise ActionError("click exige 'selector'")
        # O Playwright espera a navegação disparada pelo clique, mas não o fim do
        # carregamento: sem o assentamento abaixo, o passo seguinte pode ler a
        # página antiga e devolver dados vazios sem erro nenhum.
        self.page.click(selector)
        self._settle()
        wait_for = params.get("wait_for")
        if wait_for:
            self.page.wait_for_selector(str(wait_for))
        return ActionResult(message=f"clicou em {selector}")

    def _action_fill(self, params: dict[str, Any]) -> ActionResult:
        selector = str(params.get("selector", ""))
        if not selector:
            raise ActionError("fill exige 'selector'")
        self.page.fill(selector, str(params.get("value", "")))
        return ActionResult(message=f"preencheu {selector}")

    def _action_type(self, params: dict[str, Any]) -> ActionResult:
        selector = str(params.get("selector", ""))
        if not selector:
            raise ActionError("type exige 'selector'")
        delay = float(params.get("delay_ms", 25))
        self.page.click(selector)
        self.page.type(selector, str(params.get("text", "")), delay=delay)
        return ActionResult(message=f"digitou em {selector}")

    def _action_press(self, params: dict[str, Any]) -> ActionResult:
        key = str(params.get("key", ""))
        if not key:
            raise ActionError("press exige 'key'")
        selector = params.get("selector")
        if selector:
            self.page.press(str(selector), key)
        else:
            self.page.keyboard.press(key)
        self._settle()
        return ActionResult(message=f"pressionou {key}")

    def _action_wait_for(self, params: dict[str, Any]) -> ActionResult:
        selector = str(params.get("selector", ""))
        if not selector:
            raise ActionError("wait_for exige 'selector'")
        state = str(params.get("state", "visible"))
        self.page.wait_for_selector(selector, state=state)  # type: ignore[arg-type]
        return ActionResult(message=f"elemento visível: {selector}")

    def _action_wait(self, params: dict[str, Any]) -> ActionResult:
        milliseconds = float(params.get("ms", 1000))
        time.sleep(min(milliseconds, 60_000) / 1000)
        return ActionResult(message=f"aguardou {milliseconds:.0f}ms")

    def _action_extract(self, params: dict[str, Any]) -> ActionResult:
        """Extrai um registro estruturado, com seletores por campo.

        ``fields`` aceita tanto ``nome = "seletor"`` (texto) quanto
        ``nome = { selector = "...", attr = "href" }``. Um campo ausente na
        página vira ``None`` em vez de derrubar o passo: páginas reais têm
        campos opcionais.
        """
        fields = params.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ActionError("extract exige 'fields' com ao menos um campo")
        # A regex pode ser declarada no passo (vale para todos os campos) ou em
        # cada campo, para recortar só o trecho útil daquele texto.
        step_regex = params.get("regex")
        step_pattern = re.compile(str(step_regex)) if step_regex else None
        root_selector = params.get("selector")
        root = self.page.locator(str(root_selector)) if root_selector else None
        if root is not None and root.count() == 0:
            raise ActionError(f"extract: seletor raiz não encontrado: {root_selector}")

        record: dict[str, Any] = {}
        for name, spec in fields.items():
            if isinstance(spec, dict):
                selector = str(spec.get("selector", ""))
                attr = spec.get("attr")
                all_values = bool(spec.get("all", False))
                field_regex = spec.get("regex")
                pattern = re.compile(str(field_regex)) if field_regex else step_pattern
            else:
                selector, attr, all_values = str(spec), None, False
                pattern = step_pattern
            # O escopo é sempre um Locator: a página vira ``locator("html")``,
            # o que mantém a leitura uniforme e tipada.
            scope: Locator = root if root is not None else self.page.locator("html")
            locator = scope.locator(selector) if selector else scope
            count = locator.count()
            if all_values:
                values = [_clean(_read(locator.nth(i), attr, pattern)) for i in range(count)]
                record[name] = [value for value in values if value is not None]
            elif count == 0:
                record[name] = None
            else:
                record[name] = _clean(_read(locator.first, attr, pattern))
        return ActionResult(value=record, message=f"extraiu {len(record)} campo(s)")

    def _action_extract_attr(self, params: dict[str, Any]) -> ActionResult:
        selector = str(params.get("selector", ""))
        attr = str(params.get("attr", "href"))
        if not selector:
            raise ActionError("extract_attr exige 'selector'")
        regex = params.get("regex")
        pattern = re.compile(str(regex)) if regex else None
        locator = self.page.locator(selector)
        if locator.count() == 0:
            raise ActionError(f"extract_attr: seletor não encontrado: {selector}")
        value = _clean(_read(locator.first, attr, pattern))
        if value is None:
            raise ActionError(f"atributo '{attr}' ausente em {selector}")
        return ActionResult(value=value, message=f"atributo {attr} de {selector}")

    def _action_extract_all(self, params: dict[str, Any]) -> ActionResult:
        selector = str(params.get("selector", ""))
        if not selector:
            raise ActionError("extract_all exige 'selector'")
        attr = params.get("attr")
        limit = int(params.get("limit", 1000))
        locator = self.page.locator(selector)
        count = min(locator.count(), limit)
        values = [_clean(_read(locator.nth(index), attr, None)) for index in range(count)]
        cleaned = [value for value in values if value is not None]
        return ActionResult(value=cleaned, message=f"{len(cleaned)} item(ns) em {selector}")

    def _action_paginate(self, params: dict[str, Any]) -> ActionResult:
        """Percorre páginas numeradas até o fim, acumulando itens.

        Para quando: o seletor ``next`` desaparece, o limite de páginas da
        sessão é atingido ou o botão não muda a página. O valor salvo é a lista
        acumulada — é o passo que transforma uma listagem paginada em dados.
        """
        items_selector = str(params.get("items", ""))
        next_selector = str(params.get("next", "#next"))
        if not items_selector:
            raise ActionError("paginate exige 'items'")
        attr = params.get("attr")
        max_pages = min(
            int(params.get("max_pages", self.session_config.max_pages)),
            self.session_config.max_pages,
        )
        collected: list[Any] = []
        pages = 0
        while pages < max_pages:
            locator = self.page.locator(items_selector)
            count = locator.count()
            for index in range(count):
                value = _clean(_read(locator.nth(index), attr, None))
                if value is not None:
                    collected.append(value)
            pages += 1
            self.pages_visited += 1
            next_button = self.page.locator(next_selector)
            if next_button.count() == 0:
                break
            before = self.page.url
            try:
                next_button.first.click()
                self.page.wait_for_load_state("domcontentloaded")
            except Exception as exc:
                self._publish(
                    "log",
                    level="warning",
                    message=f"paginação interrompida: {exc}",
                )
                break
            if self.page.url == before and locator.count() == count:
                break
        return ActionResult(
            value=collected,
            message=f"{len(collected)} item(ns) em {pages} página(s)",
        )

    def _action_download(self, params: dict[str, Any]) -> ActionResult:
        filename = str(params.get("filename") or "")
        target_dir = self.artifacts_dir / "downloads"
        target_dir.mkdir(parents=True, exist_ok=True)
        if params.get("url") and not params.get("selector"):
            # URL direta: baixa pelo contexto de requisições da sessão, que
            # compartilha cookies e cabeçalhos e não depende de navegação.
            url = self._url(str(params["url"]))
            self._check_robots(url)
            if self._context is None:  # pragma: no cover - sessão não iniciada
                raise SessionError("sessão não iniciada para download")
            response = self._context.request.get(url)
            if not response.ok:
                raise ActionError(f"HTTP {response.status} ao baixar {url}")
            body = response.body()
            name = filename or _filename_from(url, response.headers.get("content-disposition"))
            path = target_dir / safe_filename(name)
            path.write_bytes(body)
            return ActionResult(
                value={"path": str(path), "bytes": len(body), "name": path.name},
                artifacts=[path],
                message=f"baixou {path.name} ({len(body)} bytes)",
            )
        with self.page.expect_download() as download_info:
            if params.get("selector"):
                self.page.click(str(params["selector"]))
            else:
                raise ActionError("download exige 'selector' ou 'url'")
        download = download_info.value
        suggested = filename or download.suggested_filename or "arquivo.bin"
        path = target_dir / safe_filename(suggested)
        download.save_as(str(path))
        return ActionResult(
            value={"path": str(path), "bytes": path.stat().st_size, "name": path.name},
            artifacts=[path],
            message=f"baixou {path.name}",
        )

    def _action_http_get(self, params: dict[str, Any]) -> ActionResult:
        """Faz uma chamada HTTP reaproveitando cookies e headers da sessão."""
        url = self._url(str(params.get("url", "")))
        self._check_robots(url)
        response = self._context.request.get(url) if self._context else None
        if response is None:  # pragma: no cover - sessão não iniciada
            raise SessionError("sessão não iniciada para http_get")
        if not response.ok:
            raise ActionError(f"HTTP {response.status} em {url}")
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            return ActionResult(value=response.json(), message=f"JSON de {url}")
        text = response.text()
        return ActionResult(
            value={"status": response.status, "text": text[:2000], "length": len(text)},
            message=f"texto de {url} ({len(text)} caracteres)",
        )

    def _action_login(self, params: dict[str, Any]) -> ActionResult:
        """Autentica na página atual ou na URL informada.

        Sucesso é verificado por ``success_selector`` (opcional) e por
        ``failure_selector`` (opcional). As credenciais chegam por template, com
        ``${secret.NOME}``, e nunca são gravadas no histórico.
        """
        url = params.get("url")
        if url:
            self.page.goto(self._url(str(url)), wait_until="domcontentloaded")
            self.pages_visited += 1
        user_selector = str(params.get("user_selector", "#user"))
        password_selector = str(params.get("password_selector", "#password"))
        submit_selector = str(params.get("submit_selector", "button[type=submit]"))
        self.page.fill(user_selector, str(params.get("user", "")))
        self.page.fill(password_selector, str(params.get("password", "")))
        self.page.click(submit_selector)
        self.page.wait_for_load_state("domcontentloaded")

        failure_selector = params.get("failure_selector")
        if failure_selector and self.page.locator(str(failure_selector)).count() > 0:
            raise ActionError("login rejeitado pela aplicação")
        success_selector = params.get("success_selector")
        if success_selector:
            self.page.wait_for_selector(str(success_selector))
        cookies = len(self._context.cookies()) if self._context else 0
        return ActionResult(
            value=True,
            message=f"login concluído ({cookies} cookie(s) na sessão)",
        )

    def _action_screenshot(self, params: dict[str, Any]) -> ActionResult:
        name = slugify(str(params.get("name", f"shot-{self.pages_visited}")))
        path = self.artifacts_dir / f"{name}.png"
        if params.get("selector"):
            self.page.locator(str(params["selector"])).first.screenshot(path=str(path))
        else:
            # `quality` só é aceito pelo Playwright para JPEG; passar em PNG
            # levanta erro e derrubaria um passo que deveria apenas registrar
            # evidência.
            options: dict[str, Any] = {
                "path": str(path),
                "full_page": bool(params.get("full_page", True)),
            }
            if path.suffix.lower() in {".jpg", ".jpeg"}:
                options["type"] = "jpeg"
                options["quality"] = self.config.screenshot_quality
            self.page.screenshot(**options)
        return ActionResult(value=str(path), artifacts=[path], message=f"capturou {path.name}")

    def _action_assert_text(self, params: dict[str, Any]) -> ActionResult:
        selector = str(params.get("selector", "body"))
        locator = self.page.locator(selector).first
        if locator.count() == 0:
            raise ActionError(f"assert_text: seletor não encontrado: {selector}")
        text = locator.inner_text()
        return self._assert_value(text, params, f"texto de {selector}")

    def _action_assert_url(self, params: dict[str, Any]) -> ActionResult:
        return self._assert_value(self.page.url, params, "URL atual")

    def _assert_value(self, value: str, params: dict[str, Any], label: str) -> ActionResult:
        expected = params.get("equals")
        contains = params.get("contains")
        regex = params.get("regex")
        if expected is not None and value.strip() != str(expected).strip():
            raise ActionError(f"{label}: esperado '{expected}', obtido '{value.strip()[:200]}'")
        if contains is not None and str(contains) not in value:
            raise ActionError(f"{label}: não contém '{contains}' (obtido '{value.strip()[:200]}')")
        if regex is not None and not re.search(str(regex), value):
            raise ActionError(f"{label}: não casa com /{regex}/ (obtido '{value.strip()[:200]}')")
        if expected is None and contains is None and regex is None:
            raise ActionError(f"{label}: informe 'equals', 'contains' ou 'regex'")
        return ActionResult(value=True, message=f"{label} conferido")

    # -- artefatos ---------------------------------------------------------
    def _settle(self, timeout_ms: float = 5000) -> None:
        """Espera a página assentar depois de uma ação que pode navegar.

        ``click`` e ``press`` retornam assim que a navegação *começa*; a página
        seguinte pode ainda não existir. Esperar ``domcontentloaded`` fecha essa
        janela. Não é erro quando a ação não navega: nesse caso o estado já está
        satisfeito e a chamada volta imediatamente — por isso a falha é ignorada
        aqui, e não propagada.
        """
        with contextlib.suppress(Exception):  # pragma: no cover - já assentada
            self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

    def _capture_failure(self, step: Any) -> list[Path]:
        """Captura screenshot e HTML no momento da falha.

        É o que torna um erro reproduzível: sem a página no estado em que
        quebrou, a única informação disponível seria a mensagem de exceção.

        Política: em ``on_error`` e ``always`` a captura acontece sempre que um
        passo falha; em ``never`` só quando o próprio passo pede
        ``artifacts = ["screenshot"]`` ou ``["html"]``.
        """
        paths: list[Path] = []
        wants = set(step.artifacts)
        mode = self.config.screenshots
        wants_screenshot = "screenshot" in wants or mode in {"on_error", "always"}
        wants_html = "html" in wants or mode in {"on_error", "always"}
        if wants_screenshot:
            try:
                path = self.artifacts_dir / f"falha-{slugify(step.id)}.png"
                self.page.screenshot(path=str(path), full_page=True, type="png")
                paths.append(path)
            except Exception:  # pragma: no cover - página já fechada
                pass
        if wants_html:
            try:
                path = self.artifacts_dir / f"falha-{slugify(step.id)}.html"
                path.write_text(self.page.content(), encoding="utf-8")
                paths.append(path)
            except Exception:  # pragma: no cover - página já fechada
                pass
        return paths

    def _capture_success(self, step: Any) -> list[Path]:
        """Captura evidência de um passo bem-sucedido, quando pedido.

        ``screenshots = "always"`` captura todo passo; caso contrário, apenas os
        passos que declaram ``artifacts = ["screenshot"]``.
        """
        if "screenshot" in step.artifacts or self.config.screenshots == "always":
            try:
                path = self.artifacts_dir / f"{slugify(step.id)}.png"
                self.page.screenshot(path=str(path), full_page=False, type="png")
                return [path]
            except Exception:  # pragma: no cover - página já fechada
                return []
        return []

    def _start_trace(self, step_id: str) -> None:
        if self._context is None:  # pragma: no cover
            return
        try:
            self._context.tracing.start(screenshots=True, snapshots=True, sources=False)
            self._tracing = True
        except Exception:  # pragma: no cover - trace indisponível
            self._tracing = False
        self._trace_step = step_id

    def _stop_trace(self, step_id: str) -> list[Path]:
        if self._context is None or not self._tracing:  # pragma: no cover
            return []
        path = self.artifacts_dir / f"trace-{slugify(step_id)}.zip"
        try:
            self._context.tracing.stop(path=str(path))
        except Exception:  # pragma: no cover
            return []
        finally:
            self._tracing = False
        return [path] if path.exists() else []

    def _register_artifacts(self, step_id: str, paths: list[Path]) -> None:
        for path in paths:
            try:
                size = path.stat().st_size
                digest = sha256_file(path)
            except OSError:  # pragma: no cover - artefato removido
                size, digest = 0, None
            kind = _artifact_kind(path)
            self.ledger.record_artifact(
                self.run_id,
                session_id=self.session_id,
                step_id=step_id,
                kind=kind,
                path=str(path),
                size=size,
                digest=digest,
            )
            self._publish(
                "artifact",
                step=step_id,
                message=f"{kind}: {path.name}",
                data={"path": str(path), "bytes": size},
            )

    def write_summary(self) -> Path:
        """Grava o resumo da sessão em ``data/<sessão>.json``.

        O arquivo é o contrato de saída do motor: quem consome o resultado não
        precisa ler SQLite nem interpretar logs.
        """
        payload = {
            "run_id": self.run_id,
            "session": self.session_id,
            "status": self.status,
            "error": self.error,
            "tolerated_failures": list(self.tolerated_failures),
            "pages_visited": self.pages_visited,
            "steps": len(self.config.flow.steps),
            "data": self.data,
            "finished_at": utc_now().isoformat(),
        }
        path = self.data_dir / f"{self.session_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), "utf-8")
        return path


def _profile_path(run_dir: Path, session_id: str) -> Path:
    """Caminho do estado persistido de um perfil reutilizável.

    Fica em ``<run_dir>/../profiles/<sessão>/state.json``: fora de uma execução
    específica, porque o perfil atravessa execuções.
    """
    return run_dir.parent / "profiles" / session_id / "state.json"


def _read(locator: Any, attr: Any, pattern: re.Pattern[str] | None) -> Any:
    """Lê texto ou atributo de um elemento, aplicando regex opcional.

    Em campos de formulário, o conteúdo digitado vive na *propriedade*
    ``value``, não no atributo HTML: um ``<input>`` preenchido pelo usuário
    continua com o atributo ``value`` vazio. Por isso há a queda para
    ``input_value()`` — sem ela, ``extract_attr(value)`` devolveria ``None``
    justamente nos campos que o fluxo acabou de preencher.
    """
    if attr:
        name = str(attr)
        value: Any = locator.get_attribute(name)
        if value is None and name in {"value", "valor"}:
            try:
                value = locator.input_value()
            except Exception:
                value = None
    else:
        try:
            value = locator.inner_text()
        except Exception:  # pragma: no cover - elemento sem texto
            value = locator.text_content()
    if value is not None and pattern is not None:
        match = pattern.search(str(value))
        value = match.group(1) if match and match.groups() else (match.group(0) if match else None)
    return value


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def _artifact_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg"}:
        return "screenshot"
    if suffix == ".html":
        return "html"
    if suffix == ".zip":
        return "trace"
    return "download"


def _filename_from(url: str, content_disposition: str | None) -> str:
    """Descobre o nome do arquivo a partir do cabeçalho ou da URL."""
    if content_disposition:
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^\";]+)"?', content_disposition)
        if match:
            return match.group(1)
    name = Path(urllib.parse.urlparse(url).path).name
    return name or "arquivo.bin"


def _truncate_for_ledger(value: Any, secrets: list[str]) -> Any:
    """Limita o tamanho do dado gravado no histórico e mascara segredos."""
    if value is None:
        return None
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > 20_000:
        text = text[:20_000] + "...(truncado)"
    return json.loads(redact(text, secrets))
