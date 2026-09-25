"""Utilitários pequenos e sem estado compartilhado.

Concentram formatação de tempo, hashing de arquivos, escrita atômica e
mascaramento de segredos, para que o restante do código não repita esses
detalhes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "FileLock",
    "atomic_write_text",
    "chunked",
    "format_duration",
    "human_bytes",
    "new_run_id",
    "redact",
    "safe_filename",
    "sha256_file",
    "sha256_text",
    "slugify",
    "utc_now",
]

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MASK = "***"


def utc_now() -> datetime:
    """Retorna o instante atual em UTC (com timezone)."""
    return datetime.now(UTC)


def new_run_id(prefix: str = "run") -> str:
    """Gera um identificador de execução ordenável no tempo.

    Formato ``<prefixo>-YYYYMMDD-HHMMSS-xxxx``: a ordenação lexicográfica
    coincide com a ordem cronológica, o que simplifica ``caravana list``.
    """
    stamp = utc_now().strftime("%Y%m%d-%H%M%S")
    suffix = hashlib.sha1(os.urandom(8)).hexdigest()[:4]
    return f"{slugify(prefix)}-{stamp}-{suffix}"


def slugify(value: str) -> str:
    """Converte texto livre em um identificador seguro para nomes de arquivo."""
    cleaned = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return cleaned or "item"


def safe_filename(value: str, fallback: str = "arquivo.bin") -> str:
    """Normaliza um nome de arquivo preservando a extensão.

    ``slugify`` sozinho transformaria ``catalogo.csv`` em ``catalogo-csv``;
    aqui o sufixo é mantido, porque o tipo do arquivo faz parte do resultado
    entregue ao usuário.
    """
    name = Path(value).name.strip()
    if not name:
        return fallback
    suffix = Path(name).suffix.lower()
    stem = name[: -len(suffix)] if suffix else name
    cleaned = slugify(stem)
    return f"{cleaned}{suffix}" if suffix else cleaned


def format_duration(seconds: float) -> str:
    """Formata uma duração em segundos de forma legível (``1m 04s``, ``812ms``)."""
    if seconds < 1:
        return f"{round(seconds * 1000):.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def human_bytes(size: int) -> str:
    """Formata um tamanho em bytes com unidade binária."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def sha256_text(text: str) -> str:
    """Hash SHA-256 de texto UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """Hash SHA-256 de um arquivo, lido em blocos para não carregar tudo na memória."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Grava texto de forma atômica: escreve em arquivo temporário e renomeia.

    Evita relatórios truncados quando dois processos leem o mesmo diretório de
    execução enquanto o relatório é reescrito.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding=encoding, dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def atomic_write_json(path: Path, payload: Any) -> None:
    """Grava JSON indentado de forma atômica."""
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")


def redact(text: str, secrets: Iterable[str]) -> str:
    """Substitui valores secretos por máscara.

    Aplicado a toda mensagem que vai para o histórico, o log ou o relatório:
    um segredo nunca é persistido em claro, nem mesmo dentro de uma mensagem de
    erro produzida pela biblioteca de automação.
    """
    result = text
    for secret in secrets:
        if secret and len(secret) >= 3:
            result = result.replace(secret, _MASK)
    return result


def chunked(items: Iterable[Any], size: int) -> Iterator[list[Any]]:
    """Divide um iterável em listas de tamanho fixo."""
    if size < 1:
        raise ValueError("size precisa ser >= 1")
    bucket: list[Any] = []
    for item in items:
        bucket.append(item)
        if len(bucket) == size:
            yield bucket
            bucket = []
    if bucket:
        yield bucket


class FileLock:
    """Trava de arquivo simples, usada para serializar escritas no histórico.

    Implementada com ``O_EXCL`` para funcionar igualmente bem em Linux, macOS e
    Windows, sem dependências externas.
    """

    def __init__(self, path: Path, timeout: float = 10.0, poll: float = 0.05) -> None:
        self.path = path
        self.timeout = timeout
        self.poll = poll

    def __enter__(self) -> FileLock:
        deadline = time.monotonic() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"não foi possível obter a trava {self.path}") from None
                time.sleep(self.poll)
            else:
                os.close(fd)
                return self

    def __exit__(self, *exc: object) -> None:
        self.path.unlink(missing_ok=True)
