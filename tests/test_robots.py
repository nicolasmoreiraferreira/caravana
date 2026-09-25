"""Testes do parser de ``robots.txt`` (RFC 9309)."""

from __future__ import annotations

import pytest

from caravana.robots import parse_robots

AGENT = "Caravana/0.1 (+https://github.com/nicolasmoreiraferreira/caravana)"


class TestParseRobots:
    def test_arquivo_vazio_permite_tudo(self) -> None:
        rules = parse_robots("")
        assert rules.can_fetch(AGENT, "http://x.test/qualquer") is True

    def test_allow_e_disallow_juntos(self) -> None:
        """O caso que a biblioteca padrão erra: ``Allow: /`` não anula um ``Disallow`` específico."""
        rules = parse_robots("User-agent: *\nAllow: /\nDisallow: /private\n")
        assert rules.can_fetch(AGENT, "http://x.test/page/1") is True
        assert rules.can_fetch(AGENT, "http://x.test/private") is False
        assert rules.can_fetch(AGENT, "http://x.test/private/relatorio") is False

    def test_disallow_de_raiz(self) -> None:
        rules = parse_robots("User-agent: *\nDisallow: /\n")
        assert rules.can_fetch(AGENT, "http://x.test/qualquer") is False

    def test_disallow_vazio_libera_tudo(self) -> None:
        rules = parse_robots("User-agent: *\nDisallow:\n")
        assert rules.can_fetch(AGENT, "http://x.test/qualquer") is True

    def test_regra_mais_longa_vence(self) -> None:
        rules = parse_robots("User-agent: *\nDisallow: /docs\nAllow: /docs/publico\n")
        assert rules.can_fetch(AGENT, "http://x.test/docs/interno") is False
        assert rules.can_fetch(AGENT, "http://x.test/docs/publico/guia") is True

    def test_allow_vence_empate(self) -> None:
        rules = parse_robots("User-agent: *\nDisallow: /a\nAllow: /a\n")
        assert rules.can_fetch(AGENT, "http://x.test/a") is True

    def test_curinga_no_meio(self) -> None:
        rules = parse_robots("User-agent: *\nDisallow: /*.pdf$\n")
        assert rules.can_fetch(AGENT, "http://x.test/relatorio.pdf") is False
        assert rules.can_fetch(AGENT, "http://x.test/relatorio.pdf.html") is True

    def test_agente_especifico_tem_prioridade(self) -> None:
        rules = parse_robots(
            "User-agent: *\nDisallow: /\n\nUser-agent: caravana\nDisallow: /bloqueado\n"
        )
        assert rules.can_fetch(AGENT, "http://x.test/livre") is True
        assert rules.can_fetch(AGENT, "http://x.test/bloqueado") is False
        assert rules.can_fetch("Outro/2.0", "http://x.test/livre") is False

    def test_grupos_multiplos_do_mesmo_agente(self) -> None:
        rules = parse_robots(
            "User-agent: a\nUser-agent: b\nDisallow: /x\n\nUser-agent: a\nDisallow: /y\n"
        )
        assert rules.can_fetch("a/1", "http://x.test/x") is False
        assert rules.can_fetch("a/1", "http://x.test/y") is False
        assert rules.can_fetch("b/1", "http://x.test/x") is False
        assert rules.can_fetch("b/1", "http://x.test/y") is True

    def test_comentarios_e_linhas_invalidas(self) -> None:
        rules = parse_robots(
            "# comentário\n\nsem dois pontos\nUser-agent: * # agente\nDisallow: /segredo # nota\n"
        )
        assert rules.can_fetch(AGENT, "http://x.test/segredo") is False

    def test_crawl_delay_e_sitemap(self) -> None:
        rules = parse_robots(
            "User-agent: *\nCrawl-delay: 2.5\nDisallow: /x\nSitemap: http://x.test/sitemap.xml\n"
        )
        assert rules.crawl_delay == 2.5
        assert rules.sitemaps == ["http://x.test/sitemap.xml"]

    def test_query_string_considerada(self) -> None:
        rules = parse_robots("User-agent: *\nDisallow: /*?sessao=\n")
        assert rules.can_fetch(AGENT, "http://x.test/pagina?sessao=1") is False
        assert rules.can_fetch(AGENT, "http://x.test/pagina") is True

    def test_agente_sem_versao_e_reconhecido(self) -> None:
        rules = parse_robots("User-agent: caravana\nDisallow: /x\n")
        assert rules.can_fetch("caravana", "http://x.test/x") is False
        assert rules.can_fetch("Caravana/0.1", "http://x.test/x") is False

    @pytest.mark.parametrize(
        ("pattern", "path", "allowed"),
        [
            ("/a", "/a", False),
            ("/a", "/ab", False),
            ("/a$", "/ab", True),
            ("/a$", "/a", False),
            ("/*/privado", "/x/privado", False),
            ("/publico*", "/publico/qualquer", False),
            ("/publico/", "/publico", True),
        ],
    )
    def test_padroes(self, pattern: str, path: str, allowed: bool) -> None:
        rules = parse_robots(f"User-agent: *\nDisallow: {pattern}\n")
        assert rules.can_fetch(AGENT, f"http://x.test{path}") is allowed
