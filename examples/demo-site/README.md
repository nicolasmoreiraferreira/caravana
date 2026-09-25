# Site de demonstração da Caravana

Um fixture HTTP determinístico e offline para exercitar a orquestração de
sessões de navegador da [Caravana](https://github.com/nicolasmoreiraferreira/caravana).

Usa apenas a biblioteca padrão do Python 3.11+: sem dependências externas, sem
chamadas de rede, sem valores aleatórios e sem carimbos de tempo nas respostas.
É por isso que os testes que dependem dele não piscam.

## Como rodar

Da raiz do repositório:

```bash
python3 examples/demo-site/server.py
```

O endereço padrão é `127.0.0.1:8787`. Ambos os valores podem ser trocados:

```bash
python3 examples/demo-site/server.py --host 127.0.0.1 --port 8787
```

O processo imprime uma linha ao subir:

```text
demo site on http://127.0.0.1:8787
```

Para um servidor em processo (nos testes), importe `build_server`, que já faz o
*bind* imediatamente. Com a porta `0`, o sistema escolhe uma porta livre:

```python
from server import build_server

server = build_server(0)
# server.server_address[1] traz a porta atribuída
```

## Rotas

Todas as respostas HTML usam `text/html; charset=utf-8`. O servidor mantém um
contador de requisições por instância; o estado de `/flaky` é por cliente
(cookie), de modo que cada sessão nova vê exatamente uma falha.

| Método | Rota | Comportamento |
|---|---|---|
| `GET` | `/` | Índice intitulado **Caravana Demo Site**, com links para todos os fixtures. |
| `GET` | `/page/1`, `/page/2`, `/page/3` | Três páginas de catálogo com cinco produtos cada; paginação para a próxima nas páginas 1 e 2. |
| `GET` | `/item/<id>` | Detalhe do produto, para IDs de 1 a 15, com SKU e link de volta para a página do catálogo. |
| `GET` | `/flaky` | Devolve 503 com `temporarily unavailable` na **primeira visita de cada cliente** (marcada por cookie `flaky_seen`); depois responde 200 de forma estável. O estado é por cliente, para que cada sessão nova exercite o retry. |
| `GET` | `/slow?ms=<milissegundos>` | Aguarda o tempo pedido, com teto de 5000 ms; padrão de 1500 ms. |
| `GET` | `/login` | Formulário de login de demonstração. |
| `POST` | `/login` | Com `demo`/`demo`, define o cookie `caravana_session` e redireciona com 303 para `/dashboard`; outras credenciais recebem 401. |
| `GET` | `/dashboard` | Painel autenticado, válido apenas com o cookie de demonstração; sem ele, 403. |
| `GET` | `/download/sample.csv` | CSV de quatro linhas, com três linhas de dados, enviado como anexo. |
| `GET` | `/robots.txt` | Libera `/` e proíbe `/private`. |
| `GET` | `/private` | Página simples que existe para testar o respeito ao `robots.txt`. |
| `GET` | `/__health` | JSON com o status e o total de requisições desta instância. |
| `POST` | `/__reset` | Zera o contador de requisições; devolve confirmação em JSON. |
| `GET` | `/static/style.css` | Folha de estilo responsiva em tema escuro, sem dependências. |
| `GET` | qualquer caminho desconhecido | Página 404 com `#not-found`. |

## Como os testes usam

Os testes de integração chamam `build_server(0)`, iniciam
`server.serve_forever()` em uma thread daemon e leem a porta real em
`server.server_address[1]`. A partir daí, podem usar `urllib.request`, um
`http.cookiejar.CookieJar` ou um navegador contra
`http://127.0.0.1:<port>`.

O fixture cobre, de propósito:

- paginação que termina quando `#next` desaparece;
- navegação para o detalhe do produto, com IDs e SKUs determinísticos;
- retry, usando `/flaky` (uma falha por cliente, determinística);
- tratamento de timeout, com os atrasos limitados de `/slow`;
- envio de formulário, cookies, redirecionamento e controle de acesso;
- download e cabeçalhos de tipo e disposição de conteúdo;
- varredura que respeita `robots.txt`; e
- verificação de saúde e contagem de requisições por `/__health`.

Chame `server.shutdown()` e `server.server_close()` na limpeza. Nada é escrito
em disco.
