"""Parser de ``robots.txt`` conforme a RFC 9309.

Por que não usar ``urllib.robotparser`` da biblioteca padrão: ele trata
``Allow`` e ``Disallow`` de forma assimétrica e, em arquivos que declaram
``Allow: /`` junto de ``Disallow: /algo``, libera o caminho proibido — o
oposto do que a especificação determina. Como respeitar ``robots.txt`` é uma
promessa feita ao dono do site, um erro silencioso nessa direção é grave.

Regras implementadas:

* grupos começam em ``User-agent`` e terminam no próximo ``User-agent``;
* vale o agente mais específico (nome do produto) ou ``*`` como reserva;
* entre as regras aplicáveis, vence a de caminho mais longo;
* em empate de comprimento, ``Allow`` prevalece sobre ``Disallow``;
* ``Crawl-delay`` é lido e exposto, mas não aplicado automaticamente.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

__all__ = ["RobotsRules", "parse_robots"]

_COMMENT_RE = re.compile(r"#.*$")


@dataclass(slots=True)
class RobotsRules:
    """Regras de acesso de um ``robots.txt`` já interpretadas."""

    groups: dict[str, list[tuple[bool, str]]] = field(default_factory=dict)
    crawl_delay: float | None = None
    sitemaps: list[str] = field(default_factory=list)

    def _rules_for(self, user_agent: str) -> list[tuple[bool, str]] | None:
        """Seleciona o grupo aplicável ao agente informado."""
        agent = (user_agent or "").split("/")[0].strip().lower()
        if not agent:
            agent = "*"
        best: list[tuple[bool, str]] | None = None
        best_length = -1
        for name, rules in self.groups.items():
            if name == "*":
                continue
            if name.lower() in agent and len(name) > best_length:
                best, best_length = rules, len(name)
        if best is not None:
            return best
        return self.groups.get("*")

    def can_fetch(self, user_agent: str, url: str) -> bool:
        """Diz se o agente pode acessar a URL.

        Sem regras aplicáveis, o acesso é permitido — o padrão da web é abrir,
        e a ausência de ``robots.txt`` não significa proibição.
        """
        rules = self._rules_for(user_agent)
        if not rules:
            return True
        path = urlparse(url).path or "/"
        if urlparse(url).query:
            path = f"{path}?{urlparse(url).query}"
        best: tuple[bool, int] | None = None
        for allow, pattern in rules:
            if not pattern or not _matches(pattern, path):
                continue
            length = len(pattern)
            if best is None or length > best[1] or (length == best[1] and allow and not best[0]):
                best = (allow, length)
        return True if best is None else best[0]


def _matches(pattern: str, path: str) -> bool:
    """Casa um padrão de ``robots.txt`` (com ``*`` e ``$``) contra um caminho."""
    if pattern == "/":
        return True
    regex = ""
    for char in pattern:
        if char == "*":
            regex += ".*"
        elif char == "$":
            regex += "$"
        else:
            regex += re.escape(char)
    if not regex.endswith("$"):
        regex += ".*"
    return re.match(regex, path) is not None


def parse_robots(text: str) -> RobotsRules:
    """Interpreta o conteúdo de um ``robots.txt``."""
    rules = RobotsRules()
    current_agents: list[str] = []
    pending: list[tuple[bool, str]] = []
    delay: float | None = None

    def flush() -> None:
        nonlocal current_agents, pending, delay
        for agent in current_agents:
            rules.groups.setdefault(agent, []).extend(pending)
            if delay is not None and rules.crawl_delay is None:
                rules.crawl_delay = delay
        current_agents, pending, delay = [], [], None

    for raw in text.splitlines():
        line = _COMMENT_RE.sub("", raw).strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if pending or delay is not None:
                flush()
            current_agents.append(value)
        elif key in {"allow", "disallow"}:
            if not current_agents:
                continue
            if key == "disallow" and value == "":
                continue  # "Disallow:" vazio libera tudo e não é uma regra
            pending.append((key == "allow", value))
        elif key == "crawl-delay":
            try:
                delay = float(value)
            except ValueError:
                delay = None
        elif key == "sitemap":
            rules.sitemaps.append(value)
    flush()
    return rules
