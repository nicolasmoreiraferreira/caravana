"""Barramento de eventos entre as threads de sessão e a interface.

O motor roda uma sessão por thread; o painel de progresso roda na thread
principal. O barramento é a única ponte entre eles: as sessões publicam eventos
pequenos e imutáveis, a interface consome. Nada de compartilhar objetos de
navegador entre threads — cada thread cria, usa e destrói os seus.

O mesmo barramento alimenta o modo ``--json`` (linha a linha, para integração
com outros processos) e o log em arquivo, sem duplicar lógica de formatação.
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .util import utc_now

__all__ = ["Counter", "Event", "EventBus", "EventSink", "JsonlSink", "NullSink"]


@dataclass(slots=True)
class Event:
    """Evento publicado durante a execução.

    ``kind`` assume um dos valores: ``run_started``, ``session_started``,
    ``step_ok``, ``step_failed``, ``step_skipped``, ``step_retry``,
    ``session_finished``, ``run_finished``, ``log``, ``artifact``.
    """

    kind: str
    session: str = ""
    step: str = ""
    message: str = ""
    attempt: int = 0
    duration_ms: float = 0.0
    level: str = "info"
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: utc_now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Serializa o evento para JSON Lines."""
        payload = asdict(self)
        payload.pop("timestamp", None)
        return payload


class EventSink(Protocol):
    """Destino de eventos (painel, arquivo, stdout em JSON)."""

    def handle(self, event: Event) -> None:
        """Consome um evento."""
        ...

    def close(self) -> None:
        """Libera recursos."""
        ...


class NullSink:
    """Descarta eventos. Usado em testes e em execuções silenciosas."""

    def handle(self, event: Event) -> None:  # noqa: ARG002 - interface
        return None

    def close(self) -> None:
        return None


class JsonlSink:
    """Grava eventos como JSON Lines, uma linha por evento."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def handle(self, event: Event) -> None:
        line = json.dumps({"ts": event.timestamp, **event.to_dict()}, ensure_ascii=False)
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            self._handle.close()


class EventBus:
    """Fila thread-safe de eventos, com contadores agregados em tempo real."""

    def __init__(self, sinks: list[EventSink] | None = None) -> None:
        self._queue: queue.Queue[Event | None] = queue.Queue()
        self._sinks = list(sinks or [])
        self._lock = threading.Lock()
        self._closed = False

    def publish(self, event: Event) -> None:
        """Publica um evento para todos os destinos."""
        with self._lock:
            if self._closed:
                return
        for sink in self._sinks:
            sink.handle(event)
        self._queue.put(event)

    def log(self, message: str, level: str = "info", **data: Any) -> None:
        """Atalho para eventos de log."""
        self.publish(Event(kind="log", message=message, level=level, data=data))

    def drain(self, timeout: float = 0.0) -> list[Event]:
        """Retira os eventos disponíveis, esperando até ``timeout`` pelo primeiro."""
        events: list[Event] = []
        first = True
        while True:
            wait = timeout if first else 0.0
            try:
                item = self._queue.get(timeout=wait)
            except queue.Empty:
                break
            first = False
            if item is None:
                break
            events.append(item)
        return events

    def close(self) -> None:
        """Encerra o barramento e os destinos."""
        with self._lock:
            self._closed = True
        for sink in self._sinks:
            sink.close()
        self._queue.put(None)

    def add_sink(self, sink: EventSink) -> None:
        """Anexa um destino extra (usado para abrir o log só depois do run id)."""
        self._sinks.append(sink)


class Counter:
    """Contador atômico, usado para medir concorrência real durante os testes."""

    def __init__(self) -> None:
        self._value = 0
        self._peak = 0
        self._lock = threading.Lock()

    def enter(self) -> int:
        """Incrementa e devolve o valor atual."""
        with self._lock:
            self._value += 1
            self._peak = max(self._peak, self._value)
            return self._value

    def exit(self) -> int:
        """Decrementa e devolve o valor atual."""
        with self._lock:
            self._value -= 1
            return self._value

    @property
    def peak(self) -> int:
        """Maior valor simultâneo observado."""
        with self._lock:
            return self._peak
