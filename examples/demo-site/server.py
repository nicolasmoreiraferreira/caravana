"""Deterministic, dependency-free HTTP demo site for Caravana integration tests."""

from __future__ import annotations

import argparse
import html
import json
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import parse_qs, unquote, urlsplit

DEFAULT_PORT = 8787


@dataclass(frozen=True)
class Product:
    """A deterministic catalog product."""

    product_id: int
    name: str
    price: str
    brand: str
    description: str


PRODUCTS: tuple[Product, ...] = (
    Product(
        1,
        "Mochila Urbana Compacta",
        "R$ 129,90",
        "Serra",
        "Mochila leve com compartimento acolchoado para o dia a dia.",
    ),
    Product(
        2,
        "Garrafa Térmica Inox",
        "R$ 89,50",
        "Brisa",
        "Garrafa de aço inoxidável que mantém bebidas na temperatura ideal.",
    ),
    Product(
        3,
        "Fone Bluetooth Essential",
        "R$ 219,00",
        "Aurora",
        "Fone sem fio confortável com bateria para longas jornadas.",
    ),
    Product(
        4,
        "Caderno Pontilhado A5",
        "R$ 42,90",
        "Papel Vivo",
        "Caderno A5 de capa resistente para notas, planos e desenhos.",
    ),
    Product(
        5,
        "Luminária de Mesa LED",
        "R$ 159,90",
        "Lume",
        "Luminária LED ajustável para uma mesa mais confortável e produtiva.",
    ),
    Product(
        6,
        "Camiseta Básica Algodão",
        "R$ 69,90",
        "Nativo",
        "Camiseta macia de algodão com modelagem clássica e versátil.",
    ),
    Product(
        7,
        "Teclado Mecânico Compacto",
        "R$ 349,90",
        "Nexo",
        "Teclado compacto com teclas mecânicas e conexão USB confiável.",
    ),
    Product(
        8,
        "Caneca Cerâmica Artesanal",
        "R$ 54,90",
        "Barro & Mar",
        "Caneca artesanal esmaltada, feita para acompanhar pausas tranquilas.",
    ),
    Product(
        9,
        "Cabo USB-C Reforçado",
        "R$ 39,90",
        "Conecta",
        "Cabo reforçado para carregamento e transferência de dados no cotidiano.",
    ),
    Product(
        10,
        "Organizador de Mesa",
        "R$ 74,90",
        "Ordem",
        "Organizador modular para deixar pequenos objetos sempre à mão.",
    ),
    Product(
        11,
        "Tênis Casual Caminho",
        "R$ 279,90",
        "Passo",
        "Tênis casual com sola flexível para rotinas urbanas movimentadas.",
    ),
    Product(
        12,
        "Almofada de Linho",
        "R$ 99,90",
        "Casa Clara",
        "Almofada de linho com toque natural para renovar o ambiente.",
    ),
    Product(
        13,
        "Kit de Jardinagem",
        "R$ 119,90",
        "Verdejar",
        "Kit essencial com ferramentas compactas para cuidar de plantas.",
    ),
    Product(
        14,
        "Carteira Minimalista",
        "R$ 84,90",
        "Traço",
        "Carteira fina com divisórias práticas e acabamento durável.",
    ),
    Product(
        15,
        "Caixa de Som Portátil",
        "R$ 249,90",
        "Sonsul",
        "Caixa portátil para ouvir música com clareza em qualquer lugar.",
    ),
)


class DemoHTTPServer(ThreadingHTTPServer):
    """Threading server carrying mutable state for one demo-site instance."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int]) -> None:
        super().__init__(server_address, DemoRequestHandler)
        self.state_lock = threading.Lock()
        self.request_count = 0


class DemoRequestHandler(BaseHTTPRequestHandler):
    """Serve deterministic HTML and fixture endpoints."""

    server: DemoHTTPServer
    protocol_version = "HTTP/1.1"
    _style_href: ClassVar[str] = "/static/style.css"

    def log_message(self, format: str, *args: object) -> None:
        """Suppress the default access log so test output stays quiet."""

    def do_GET(self) -> None:
        """Handle all public GET routes."""
        self._record_request()
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)

        if path == "/":
            self._send_html(self._index_page())
        elif path.startswith("/page/"):
            self._serve_catalog_page(path)
        elif path.startswith("/item/"):
            self._serve_item(path)
        elif path == "/flaky":
            self._serve_flaky()
        elif path == "/slow":
            self._serve_slow(parsed.query)
        elif path == "/login":
            self._send_html(self._login_page())
        elif path == "/dashboard":
            self._serve_dashboard()
        elif path == "/download/sample.csv":
            self._serve_csv()
        elif path == "/robots.txt":
            self._send_text("User-agent: *\nAllow: /\nDisallow: /private\n")
        elif path == "/private":
            self._send_html(self._page("Área privada", '<div id="private">conteúdo privado</div>'))
        elif path == "/__health":
            self._serve_health()
        elif path == "/static/style.css":
            self._serve_css()
        else:
            self._not_found()

    def do_POST(self) -> None:
        """Handle login and test-state reset POST routes."""
        self._record_request()
        path = unquote(urlsplit(self.path).path)

        if path == "/login":
            self._serve_login_post()
        elif path == "/__reset":
            self._serve_reset()
        else:
            self._not_found()

    def _record_request(self) -> None:
        """Increment this server's request counter atomically."""
        with self.server.state_lock:
            self.server.request_count += 1

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """Send a complete response with deterministic headers and length."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(
        self,
        body: str,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """Send UTF-8 HTML."""
        self._send_bytes(body.encode("utf-8"), "text/html; charset=utf-8", status, extra_headers)

    def _send_text(
        self,
        body: str,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """Send UTF-8 plain text."""
        self._send_bytes(body.encode("utf-8"), "text/plain; charset=utf-8", status, extra_headers)

    def _page(self, title: str, content: str, nav: bool = True) -> str:
        """Wrap page content in the shared document layout."""
        navigation = ""
        if nav:
            navigation = """
      <nav class="nav" aria-label="Navegação principal">
        <a href="/">Início</a>
        <a href="/page/1">Catálogo</a>
        <a href="/login">Login</a>
      </nav>"""
        return f"""<!doctype html>
<html lang="pt-BR">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(title)}</title>
    <link rel="stylesheet" href="{self._style_href}">
  </head>
  <body>
    <main class="container">
      {navigation}
      {content}
    </main>
  </body>
</html>
"""

    def _index_page(self) -> str:
        """Render the landing page and its complete fixture navigation."""
        content = """
      <section class="hero">
        <p class="eyebrow">Sessões resilientes para a web</p>
        <h1>Caravana Demo Site</h1>
        <p>Um site local, determinístico e offline para testar automação de navegador.</p>
      </section>
      <section class="panel">
        <h2>Rotas de teste</h2>
        <nav class="route-list" aria-label="Rotas do fixture">
          <a href="/page/1">Página 1</a>
          <a href="/page/2">Página 2</a>
          <a href="/page/3">Página 3</a>
          <a href="/login">Login</a>
          <a href="/flaky">Flaky</a>
          <a href="/slow">Slow</a>
          <a href="/download/sample.csv">Download CSV</a>
        </nav>
      </section>"""
        return self._page("Caravana Demo Site", content)

    def _serve_catalog_page(self, path: str) -> None:
        """Render one of the three five-item catalog pages."""
        page_text = path.removeprefix("/page/")
        if not page_text.isdigit():
            self._not_found()
            return
        page_number = int(page_text)
        if page_number not in (1, 2, 3):
            self._not_found()
            return

        start = (page_number - 1) * 5
        cards = "\n".join(self._product_card(product) for product in PRODUCTS[start : start + 5])
        next_link = ""
        if page_number < 3:
            next_link = (
                f'\n        <a id="next" class="button" href="/page/{page_number + 1}">Próxima</a>'
            )
        content = f"""
      <section id="catalog" data-page="{page_number}">
        <p class="eyebrow">Catálogo</p>
        <h1>Página {page_number} de 3</h1>
        <div class="product-grid">
{cards}
        </div>{next_link}
      </section>"""
        self._send_html(self._page(f"Página {page_number} | Caravana Demo Site", content))

    @staticmethod
    def _product_card(product: Product) -> str:
        """Render one catalog card."""
        return f"""          <article class="product">
            <h3 class="name">{html.escape(product.name)}</h3>
            <span class="price">{html.escape(product.price)}</span>
            <span class="brand">{html.escape(product.brand)}</span>
            <a class="detail" href="/item/{product.product_id}">Ver detalhes</a>
          </article>"""

    def _serve_item(self, path: str) -> None:
        """Render a product detail page for IDs one through fifteen."""
        item_text = path.removeprefix("/item/")
        if not item_text.isdigit():
            self._not_found()
            return
        product_id = int(item_text)
        if not 1 <= product_id <= len(PRODUCTS):
            self._not_found()
            return
        product = PRODUCTS[product_id - 1]
        page_number = (product_id - 1) // 5 + 1
        content = f"""
      <article class="detail-page">
        <p class="eyebrow">Produto {product.product_id:02d}</p>
        <h1 class="product-name">{html.escape(product.name)}</h1>
        <span class="price">{html.escape(product.price)}</span>
        <span class="brand">{html.escape(product.brand)}</span>
        <div class="description">{html.escape(product.description)}</div>
        <span class="sku">SKU-{product.product_id:04d}</span>
        <a id="back" class="button" href="/page/{page_number}">Voltar ao catálogo</a>
      </article>"""
        self._send_html(self._page(f"{product.name} | Caravana Demo Site", content))

    def _serve_flaky(self) -> None:
        """Falha na primeira visita de cada cliente, depois responde com sucesso.

        O estado é por cliente, marcado por um cookie, e não por servidor. Um
        contador global tornaria o resultado dependente da ordem de chegada das
        sessões: quem executasse depois encontraria a falha já consumida por
        outra sessão, e o retry nunca seria exercitado. Com o cookie, cada
        sessão nova vê exatamente uma falha — que é o que o exemplo promete.
        """
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        if cookie.get("flaky_seen") is None:
            self._send_html(
                self._page(
                    "Temporariamente indisponível",
                    '<div id="error">temporarily unavailable</div>',
                ),
                HTTPStatus.SERVICE_UNAVAILABLE,
                extra_headers={"Set-Cookie": "flaky_seen=1; Path=/; HttpOnly"},
            )
            return
        self._send_html(self._page("Flaky", '<h1>Flaky</h1><div id="ok">flaky ok</div>'))

    def _serve_slow(self, query: str) -> None:
        """Sleep for the requested bounded number of milliseconds."""
        values = parse_qs(query).get("ms", ["1500"])
        try:
            milliseconds = int(values[0])
        except ValueError:
            milliseconds = 1500
        time.sleep(max(0, min(milliseconds, 5000)) / 1000)
        self._send_html(self._page("Slow", '<div id="slow-ok">slow ok</div>'))

    def _login_page(self) -> str:
        """Render the login form."""
        content = """
      <section class="panel narrow">
        <p class="eyebrow">Área de acesso</p>
        <h1>Entrar</h1>
        <form id="login" method="post" action="/login">
          <label for="user">Usuário</label>
          <input id="user" name="user">
          <label for="password">Senha</label>
          <input id="password" name="password" type="password">
          <button id="submit" type="submit">Entrar</button>
        </form>
      </section>"""
        return self._page("Login | Caravana Demo Site", content)

    def _serve_login_post(self) -> None:
        """Authenticate the fixed demo credentials or return an error."""
        form = self._read_form()
        user = form.get("user", [""])[0]
        password = form.get("password", [""])[0]
        if user == "demo" and password == "demo":
            headers = {
                "Set-Cookie": "caravana_session=demo-token; Path=/; HttpOnly",
                "Location": "/dashboard",
            }
            self._send_html("", HTTPStatus.SEE_OTHER, headers)
            return
        self._send_html(
            self._page("Login | Caravana Demo Site", '<div id="error">credenciais inválidas</div>'),
            HTTPStatus.UNAUTHORIZED,
        )

    def _read_form(self) -> dict[str, list[str]]:
        """Read and parse a URL-encoded request body."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        body = self.rfile.read(max(0, length)).decode("utf-8", errors="replace")
        return parse_qs(body, keep_blank_values=True)

    def _serve_dashboard(self) -> None:
        """Render the authenticated dashboard when the fixed cookie is present."""
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        session = cookie.get("caravana_session")
        if session is None or session.value != "demo-token":
            self._send_html(
                self._page("Acesso negado", '<div id="denied">acesso negado</div>'),
                HTTPStatus.FORBIDDEN,
            )
            return
        content = '<h1>Painel</h1><div id="secret">conteúdo autenticado</div>'
        self._send_html(self._page("Painel | Caravana Demo Site", content))

    def _serve_csv(self) -> None:
        """Send the deterministic sample CSV attachment."""
        body = (
            "id,name,price\n"
            "1,Mochila Urbana Compacta,129.90\n"
            "2,Garrafa Térmica Inox,89.50\n"
            "3,Fone Bluetooth Essential,219.00\n"
        )
        self._send_text(
            body,
            extra_headers={
                "Content-Disposition": 'attachment; filename="sample.csv"',
            },
        )

    def _serve_health(self) -> None:
        """Return the current request count as JSON."""
        with self.server.state_lock:
            count = self.server.request_count
        body = json.dumps({"status": "ok", "requests": count}, ensure_ascii=False)
        self._send_bytes(body.encode("utf-8"), "application/json; charset=utf-8")

    def _serve_reset(self) -> None:
        """Zera o contador de requisições desta instância.

        O estado de ``/flaky`` é por cliente (cookie), então não há o que zerar
        aqui: um cliente que já viu a falha continuaria vendo sucesso. Para
        reiniciar a experiência, use um contexto de navegador novo.
        """
        with self.server.state_lock:
            self.server.request_count = 0
        body = json.dumps({"ok": True, "flaky": "estado por cliente (cookie)"})
        self._send_bytes(body.encode("utf-8"), "application/json; charset=utf-8")

    def _serve_css(self) -> None:
        """Send the self-contained dark-theme stylesheet."""
        css = """:root {
  color-scheme: dark;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
    "Segoe UI", sans-serif;
  background: #0b1120;
  color: #e2e8f0;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  min-width: 320px;
  background: radial-gradient(circle at top, #172554 0, #0b1120 42rem);
}

.container { width: min(1120px, calc(100% - 2rem)); margin: 0 auto; padding: 1.5rem 0 4rem; }

.nav { display: flex; flex-wrap: wrap; gap: 1rem; margin-bottom: 4rem; }
.nav a, a { color: #67e8f9; text-decoration: none; }
.nav a:hover, a:hover { color: #a5f3fc; text-decoration: underline; }

.hero { max-width: 720px; padding: 2rem 0 3rem; }
.hero h1, h1 {
  margin: 0 0 1rem; color: #f8fafc; font-size: clamp(2rem, 5vw, 3.7rem);
  letter-spacing: -0.04em;
}
.hero p:not(.eyebrow) { color: #a5b4fc; font-size: 1.15rem; line-height: 1.7; }
.eyebrow {
  margin: 0 0 .75rem; color: #22d3ee; font-size: .78rem; font-weight: 700;
  letter-spacing: .14em; text-transform: uppercase;
}

.panel, .product, .detail-page {
  border: 1px solid #334155;
  border-radius: 1rem;
  background: rgba(15, 23, 42, .82);
  box-shadow: 0 1rem 3rem rgba(2, 6, 23, .2);
}
.panel { padding: clamp(1.25rem, 4vw, 2.25rem); }
.panel h2 { margin-top: 0; color: #f8fafc; }
.route-list { display: flex; flex-wrap: wrap; gap: .75rem; }
.route-list a, .button {
  display: inline-block; padding: .65rem .9rem; border: 1px solid #155e75;
  border-radius: .6rem; background: #164e63; color: #cffafe;
}

#catalog h1 { font-size: clamp(2rem, 4vw, 3rem); }
.product-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 1rem; margin: 1.75rem 0;
}
.product { display: flex; min-height: 190px; flex-direction: column; gap: .7rem; padding: 1.25rem; }
.product .name { margin: 0; color: #f8fafc; font-size: 1.1rem; }
.price { color: #67e8f9; font-size: 1.15rem; font-weight: 700; }
.brand { color: #94a3b8; font-size: .9rem; }
.detail { margin-top: auto; }
.detail-page { max-width: 720px; padding: clamp(1.5rem, 5vw, 3rem); }
.detail-page .product-name { font-size: clamp(2rem, 5vw, 3.5rem); }
.description { margin: 1.5rem 0; color: #cbd5e1; font-size: 1.05rem; line-height: 1.7; }
.sku {
  display: block; margin-bottom: 1.5rem; color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

form { display: grid; gap: .6rem; max-width: 380px; }
label { color: #cbd5e1; font-size: .9rem; }
input {
  margin-bottom: .65rem; padding: .75rem; border: 1px solid #475569;
  border-radius: .5rem; background: #0f172a; color: #f8fafc; font: inherit;
}
button {
  padding: .75rem 1rem; border: 0; border-radius: .5rem; background: #22d3ee;
  color: #083344; cursor: pointer; font: inherit; font-weight: 700;
}
button:hover { background: #67e8f9; }

@media (max-width: 560px) {
  .container { width: min(100% - 1.25rem, 1120px); padding-top: 1rem; }
  .nav { margin-bottom: 2rem; }
}
"""
        self._send_bytes(css.encode("utf-8"), "text/css; charset=utf-8")

    def _not_found(self) -> None:
        """Send the fixture's deterministic 404 page."""
        self._send_html(
            self._page(
                "Não encontrado | Caravana Demo Site",
                '<div id="not-found">página não encontrada</div>',
            ),
            HTTPStatus.NOT_FOUND,
        )


def build_server(port: int = 0, host: str = "127.0.0.1") -> "ThreadingHTTPServer":  # noqa: UP037
    """Create and immediately bind a threaded demo server."""
    return DemoHTTPServer((host, port))


def serve_forever(port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> None:
    """Bind the demo server, print its address, and serve until interrupted."""
    server = build_server(port, host)
    actual_port = server.server_address[1]
    print(f"demo site on http://{host}:{actual_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    """Run the demo site command-line interface."""
    parser = argparse.ArgumentParser(description="Run the Caravana offline demo site.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port (default: 8787)")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    args = parser.parse_args(argv)
    serve_forever(args.port, args.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
