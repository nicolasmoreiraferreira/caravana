# Caravana

[![CI](https://github.com/nicolasmoreiraferreira/caravana/actions/workflows/ci.yml/badge.svg)](https://github.com/nicolasmoreiraferreira/caravana/actions/workflows/ci.yml)
[![Licença: MIT](https://img.shields.io/badge/licen%C3%A7a-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy: strict](https://img.shields.io/badge/mypy-strict-blue.svg)](https://mypy-lang.org/)

**Orquestrador resiliente de sessões de navegador isoladas.**

Caravana executa fluxos declarativos de automação web em **N sessões isoladas em
paralelo**, com retomada após interrupção, evidências auditáveis e histórico
consultável em SQLite. Você descreve *o que* fazer; a Caravana cuida de
concorrência, tentativas, isolamento e rastro.

```bash
git clone https://github.com/nicolasmoreiraferreira/caravana
cd caravana
pip install -e ".[playwright]"
playwright install chromium
caravana run --config examples/catalogo/caravana.toml
```

> O nome vem da ideia central: em vez de um navegador sozinho, uma **caravana**
> de sessões que avança em paralelo, cada uma com seu próprio contexto, e que
> sabe voltar ao ponto onde parou.

---

## Índice

- [Por que existe](#por-que-existe)
- [Instalação](#instalação)
- [Início em 2 minutos](#início-em-2-minutos)
- [Como funciona](#como-funciona)
- [Escrevendo um fluxo](#escrevendo-um-fluxo)
- [Referência das ações](#referência-das-ações)
- [Sessões, concorrência e isolamento](#sessões-concorrência-e-isolamento)
- [Resiliência: retry, tolerância e desvio](#resiliência-retry-tolerância-e-desvio)
- [Segredos](#segredos)
- [Retomada](#retomada)
- [Saídas e relatórios](#saídas-e-relatórios)
- [Referência da CLI](#referência-da-cli)
- [Receitas](#receitas)
- [Limites e honestidade](#limites-e-honestidade)
- [Desenvolvimento](#desenvolvimento)
- [Licença](#licença)

---

## Por que existe

Automatizar um site de verdade raramente falha por causa de um seletor. Falha
porque:

- **a página demora**, e o passo seguinte roda contra a página antiga;
- **uma das contas está bloqueada**, e você descobre depois de 40 minutos;
- **o processo cai no meio**, e não há registro de onde parou;
- **a coleta precisa acontecer 12 vezes**, e rodar em série leva a tarde toda;
- **a evidência sumiu**, e ninguém consegue reproduzir o erro.

Frameworks de teste cobrem o caso "quero saber se o site funciona". A Caravana
cobre o caso **"quero que o trabalho aconteça, e quero o registro dele"**:

| Problema | O que a Caravana faz |
| --- | --- |
| Falha transitória | Tentativas por passo, com espera crescente e registro de cada tentativa |
| Uma sessão travada | Isolamento por contexto; o trabalho das outras continua |
| Interrupção (Ctrl+C, queda) | Retomada: passos concluídos não se repetem |
| Dúvida sobre o resultado | Ledger SQLite + relatório + manifesto com SHA-256 de cada artefato |
| Credenciais no código | Cofre de segredos por variável de ambiente ou arquivo `.env` |
| Site bloqueando crawlers | `robots.txt` interpretado conforme a RFC 9309 |
| Coleta em volume | N sessões concorrentes com limite de workers configurável |

---

## Instalação

Requer **Python 3.11 ou superior**.

```bash
git clone https://github.com/nicolasmoreiraferreira/caravana
cd caravana
pip install -e ".[playwright]"
playwright install chromium
caravana doctor
```

`caravana doctor` verifica Python, Playwright, Chromium, permissões de escrita,
variáveis de segredo e espaço em disco. Se algo estiver faltando, ele diz
exatamente o que instalar.

A distribuição no PyPI está prevista; até lá, a instalação é a partir do
repositório. O pacote tem dois níveis:

```bash
pip install -e .              # núcleo: fluxo, configuração, ledger, relatórios
pip install -e ".[playwright]"  # + execução real de navegador
```

Sem a dependência opcional, a Caravana ainda serve para `validate`, `plan`,
`show`, `report` e `verify` — só não abre navegador.

---

## Início em 2 minutos

Há um site de demonstração no repositório, com paginação, login, página
instável, download e `robots.txt`. Ele existe para que os exemplos rodem sem
tocar em nenhum site real.

```bash
git clone https://github.com/nicolasmoreiraferreira/caravana
cd caravana
pip install -e ".[playwright]" && playwright install chromium

# 1. site de demonstração em um terminal
python examples/demo-site/server.py --port 8787

# 2. execução em outro terminal
caravana run --config examples/catalogo/caravana.toml
```

A saída termina com o resumo e o caminho do relatório:

```
  execução  catalogo-20260925-221503-4f1c  completed
  sessões   3 concluídas · 0 degradadas · 0 falhas · 0 interrompidas
  passos    15 ok · 0 retentados · 0 falhas
  artefatos 6 arquivos · 118.4 KB
  duração   3.2s
  relatório .caravana/runs/catalogo-20260925-221503-4f1c/report.md
```

---

## Como funciona

```
   caravana.toml          flow.toml
        │                     │
        └────────┬────────────┘
                 ▼
         ┌───────────────┐
         │  validação    │  grafo, ações, saltos, save_as  (antes de abrir nada)
         └───────┬───────┘
                 ▼
    ┌────────────────────────────┐
    │  motor (workers fixos)     │
    │  ┌────────┐  ┌────────┐    │   cada sessão = contexto isolado
    │  │sessão 1│  │sessão 2│ …  │   cookies, storage e página próprios
    │  └───┬────┘  └───┬────┘    │
    └──────┼───────────┼─────────┘
           ▼           ▼
     ledger SQLite (thread-safe)  ──►  relatório · manifesto · badge
```

Três decisões que explicam o comportamento:

1. **Validação antes da execução.** Um `next` apontando para passo inexistente
   falha em milissegundos, não depois de abrir quatro navegadores.
2. **Uma thread por sessão, com contexto próprio.** Não há página compartilhada;
   o que uma sessão faz não vaza para as outras.
3. **Toda escrita passa pelo ledger.** Cada tentativa é registrada com início,
   duração, erro e artefatos. É por isso que a retomada e o relatório são
   confiáveis.

---

## Escrevendo um fluxo

Um fluxo é uma lista de passos. Cada passo tem um `id` estável (é ele que
aparece no histórico) e uma ação.

```toml
[flow]
name = "catalogo"
version = "1.0.0"
description = "Percorre o catálogo e extrai os produtos"

[[flow.steps]]
id = "abrir"
action = "goto"
params = { url = "/page/1" }
artifacts = ["screenshot"]

[[flow.steps]]
id = "coletar"
action = "paginate"
params = { items = "article.product .name", next = "#next", max_pages = 5 }
save_as = "produtos"

[[flow.steps]]
id = "detalhe"
action = "goto"
params = { url = "/item/1" }

[[flow.steps]]
id = "extrair"
action = "extract"
params = { fields = { nome = ".product-name", preco = ".price" } }
save_as = "detalhe"
```

E a configuração, que diz **onde** e **quantas vezes**:

```toml
[flow]
path = "flow.toml"

[engine]
workers = 4
max_attempts = 3
screenshots = "on_error"

[vars]
ambiente = "producao"

[[session]]
id = "loja"
count = 4
base_url = "http://127.0.0.1:8787"
```

`count = 4` cria `loja-1` … `loja-4`, cada uma com contexto próprio.

### Campos de um passo

| Campo | Tipo | Efeito |
| --- | --- | --- |
| `id` | texto | Identificador estável, único no fluxo |
| `action` | texto | Uma das ações da [referência](#referência-das-ações) |
| `params` | tabela | Argumentos da ação |
| `save_as` | texto | Guarda o resultado em `session.data[save_as]` |
| `next` | texto | Próximo passo (padrão: o seguinte) |
| `when` | texto | Condição para executar; falsa ⇒ passo pulado |
| `retries` | inteiro | Tentativas extras além da primeira |
| `retry_delay` | número | Espera entre tentativas, em segundos (cresce a cada falha) |
| `timeout_ms` | inteiro | Tempo máximo do passo |
| `optional` | booleano | Falha não derruba a sessão; ela termina **degradada** |
| `on_error` | texto | `fail` (padrão), `skip_to:<passo>` ou `ignore` |
| `artifacts` | lista | `screenshot`, `html`, `trace` |

### Variáveis e dados

Em qualquer `params`, `${...}` é interpolado:

| Expressão | Vem de |
| --- | --- |
| `${base}` | `base_url` da sessão |
| `${var.nome}` | bloco `[vars]` da configuração |
| `${secret.NOME}` | cofre de segredos (ambiente ou arquivo) |
| `${session.data.passo.campo}` | resultado de um passo anterior |
| `${session.id}` | identificador da sessão |

---

## Referência das ações

### Navegação

| Ação | Parâmetros | Observações |
| --- | --- | --- |
| `goto` | `url`, `wait_until` | Falha em HTTP ≥ 400 com a mensagem do status |
| `wait` | `ms` | Espera fixa, registrada no histórico |
| `wait_for` | `selector`, `state`, `timeout_ms` | `state`: `visible`, `attached`, `hidden`, `detached` |

### Interação

| Ação | Parâmetros | Observações |
| --- | --- | --- |
| `click` | `selector`, `wait_for` | Espera a navegação e o assentamento da página |
| `fill` | `selector`, `value` | Substitui o conteúdo do campo |
| `type` | `selector`, `text`, `delay_ms` | Digita caractere a caractere |
| `press` | `selector`, `key` | Ex.: `Enter`, `Escape`, `Tab` |
| `login` | `url`, `user`, `password`, `success_selector`, `failure_selector` | Credenciais fora do arquivo, via `${secret.*}` |

### Extração

| Ação | Parâmetros | Observações |
| --- | --- | --- |
| `extract` | `fields`, `selector`, `regex` | `fields` aceita texto ou `{selector, attr, all, regex}`; campo ausente vira `None` |
| `extract_all` | `selector`, `attr`, `limit` | Lista de textos ou atributos |
| `extract_attr` | `selector`, `attr`, `regex` | Falha se o atributo não existir; em campos de formulário lê a propriedade `value` |
| `paginate` | `items`, `next`, `max_pages` | Coleta e avança até `next` desaparecer ou atingir `max_pages` |

### Verificação

| Ação | Parâmetros | Observações |
| --- | --- | --- |
| `assert_text` | `selector`, `contains`/`equals`/`regex` | A mensagem mostra esperado e obtido |
| `assert_url` | `contains`/`equals`/`regex` | Compara a URL atual |

### Aquisição e registro

| Ação | Parâmetros | Observações |
| --- | --- | --- |
| `screenshot` | `name`, `full_page`, `selector` | PNG; `selector` captura só o elemento |
| `download` | `url` ou `selector`, `filename` | Salva em `artifacts/` e registra tamanho e SHA-256 |
| `http_get` | `url`, `headers` | Requisição direta, sem navegador; JSON vira estrutura |

Cada ação está documentada no código com o motivo das decisões — inclusive os
casos de borda, que são onde os fluxos reais quebram.

---

## Sessões, concorrência e isolamento

```toml
[engine]
workers = 4          # quantas sessões rodam ao mesmo tempo

[[session]]
id = "conta"
count = 12           # 12 sessões, executadas 4 por vez
base_url = "https://exemplo.test"
reuse_profile = true # cookies persistem entre execuções
```

- Cada sessão tem **contexto próprio**: cookies, `localStorage` e página
  isolados. Logar na `conta-1` não autentica a `conta-2`.
- `workers` limita o paralelismo; as sessões restantes entram na fila.
- `reuse_profile = true` grava o estado em `.caravana/profiles/<sessão>/`, útil
  para não refazer login a cada execução. Na primeira execução o perfil ainda
  não existe e a sessão começa limpa — o arquivo passa a existir ao final.
- Se uma sessão falha de forma definitiva, **as outras continuam**. O status
  final da execução reflete o pior caso.

---

## Resiliência: retry, tolerância e desvio

Três níveis distintos, na ordem em que você deve pensar neles:

**1. Retry — a falha é provavelmente transitória.**

```toml
[[flow.steps]]
id = "abrir"
action = "goto"
params = { url = "/page/1" }
retries = 3
retry_delay = 0.5   # 0.5s, 1.0s, 2.0s
```

**2. Tolerância — o passo é desejável, não essencial.**

```toml
[[flow.steps]]
id = "banner"
action = "extract"
params = { fields = { promo = ".banner" } }
optional = true
```

A sessão termina **degradada**: não é falha, mas o relatório deixa claro que
faltou algo. O fluxo segue.

**3. Desvio — um caminho alternativo declarado.**

```toml
[[flow.steps]]
id = "login_rapido"
action = "login"
params = { url = "/login", user = "${secret.USER}", password = "${secret.PASS}" }
on_error = "skip_to:coleta_publica"
```

Os passos saltados aparecem no histórico como `skipped`, com o motivo. Nada
desaparece em silêncio.

**Evidência automática.** Com `screenshots = "on_error"` (o padrão), toda falha
gera PNG da página e HTML do estado quebrado, antes de o navegador fechar. É o
que torna o erro reproduzível.

---

## Segredos

Credenciais nunca entram no arquivo de fluxo. Elas vêm de:

```bash
# variáveis de ambiente
export CARAVANA_SECRET_LOJA_USER=fulano
export CARAVANA_SECRET_LOJA_PASS=segredo

# ou de um arquivo .env
caravana run --config caravana.toml --secrets-file .env.local
```

E são usadas por referência:

```toml
params = { user = "${secret.LOJA_USER}", password = "${secret.LOJA_PASS}" }
```

O valor é interpolado no momento do uso e **não** é gravado no ledger, no
relatório nem no log de eventos. Há um teste dedicado que falha se um segredo
aparecer em qualquer saída — a garantia é verificada, não presumida.

---

## Retomada

Interrompa com `Ctrl+C` (ou deixe o processo cair) e continue de onde parou:

```bash
caravana run --config caravana.toml --resume-last
# ou, para uma execução específica:
caravana run --config caravana.toml --resume catalogo-20260925-221503-4f1c
```

O que acontece:

- passos concluídos **não** se repetem;
- o passo interrompido recomeça;
- a execução ganha uma nova *geração* no histórico, e o relatório mostra o
  estado atual — não a soma das tentativas;
- se o fluxo tiver mudado desde a execução original, a retomada é recusada com
  `ResumeError`: continuar com outro fluxo produziria um resultado incoerente.

---

## Saídas e relatórios

Cada execução cria um diretório próprio, autocontido:

```
.caravana/runs/catalogo-20260925-221503-4f1c/
├── caravana.db          histórico (SQLite) de todas as execuções
├── summary.json         resultado legível por máquina
├── manifest.json        inventário com SHA-256 de cada artefato
├── report.md            relatório em Markdown
├── report.html          relatório autocontido (sem CDN, abre offline)
├── badge.svg            selo de status
├── events.jsonl         linha do tempo de eventos (com --log)
├── data/<sessão>.json   o que cada sessão coletou
└── artifacts/           screenshots, HTML de falha, downloads, traces
```

**Verificação de integridade.** O manifesto registra o hash de cada artefato, e
`caravana verify` recalcula:

```bash
caravana verify --run-dir .caravana/runs
```

Se um arquivo foi alterado depois da execução, o comando aponta qual e sai com
código 1 — útil para anexar evidências a um processo ou auditoria.

---

## Referência da CLI

```
caravana run       executa um fluxo
caravana validate  valida configuração e fluxo, sem abrir navegador
caravana plan      mostra o que seria executado
caravana list      lista execuções registradas
caravana show      detalha uma execução (passos, dados, artefatos)
caravana report    gera/regenera relatórios
caravana verify    confere artefatos contra os hashes do manifesto
caravana doctor    diagnostica o ambiente
caravana version   versão instalada
```

Todas aceitam `--json` para uso em scripts. Exemplos:

```bash
# o que vai rodar, sem rodar
caravana plan --config caravana.toml

# validar antes do CI
caravana validate --config caravana.toml --mermaid   # diagrama do fluxo

# acompanhar uma execução de fora
caravana run --config caravana.toml --log eventos.jsonl

# inspecionar o que foi coletado
caravana show --run-dir .caravana/runs --json | jq '.stats'
```

### Códigos de saída

| Código | Significado |
| --- | --- |
| `0` | Concluído (inclusive com avisos/degradado) |
| `1` | Falhou, ou verificação de integridade detectou divergência |
| `2` | Erro de uso: configuração inválida, arquivo ausente, argumento malformado |
| `3` | Retomada pedida para execução inexistente |
| `130` | Interrompido pelo usuário (Ctrl+C) |

---

## Receitas

**Coletar muitas contas em paralelo, com login uma vez só**

```toml
[engine]
workers = 6

[[session]]
id = "conta"
count = 12
base_url = "https://exemplo.test"
reuse_profile = true
```

**Anexar evidência de cada passo**

```toml
[engine]
screenshots = "always"   # PNG de todo passo bem-sucedido + HTML de toda falha
```

**Deixar o CI verde só quando tudo deu certo**

```bash
caravana run --config caravana.toml --quiet --json | jq -e '.status == "completed"'
```

**Investigar por que um passo falhou**

```bash
caravana show --run-dir .caravana/runs            # o passo e a mensagem de erro
ls .caravana/runs/*/artifacts/falha-*.png         # a página no momento da falha
```

---

## Limites e honestidade

O que a Caravana **não** faz, dito de forma explícita:

- **Não resolve CAPTCHA** nem contorna proteção anti-bot. Se o site exige
  intervenção humana, o fluxo deve declarar isso — o motor não improvisa.
- **Não é um framework de teste.** Não tem asserções ricas, relatório JUnit ou
  integração com runner; para isso, Playwright Test, pytest-playwright e Robot
  Framework são escolhas melhores. A Caravana é sobre executar trabalho.
- **Não distribui entre máquinas.** O paralelismo é por threads, dentro de um
  processo. Para escala horizontal, rode várias instâncias com `run_dir`
  distintos.
- **`robots.txt` é obedecido quando `respect_robots = true`.** Desligar é uma
  decisão sua, e o padrão é respeitar.
- **A retomada exige fluxo idêntico.** Mudou o fluxo, muda o hash; a retomada
  recusa em vez de produzir resultado enganoso.

---

## Desenvolvimento

```bash
git clone https://github.com/nicolasmoreiraferreira/caravana
cd caravana
pip install -e ".[dev,playwright]" && playwright install chromium

python -m pytest tests/ -q                        # suíte completa
python -m pytest tests/ -q -m "not integration"   # sem navegador, rápido
python -m pytest tests/ --cov=caravana            # cobertura
python -m ruff check . && python -m ruff format --check .
python -m mypy
```

A suíte tem três camadas: unitária (fluxo, configuração, ledger, relatórios,
robots), de ações (cada ação contra o site de demonstração) e de integração
(concorrência, retomada, evidências, integridade). Nenhum teste toca a internet.

O site de demonstração é parte do projeto: qualquer comportamento que precise de
uma página específica — paginação, página instável, formulário, download — deve
ser reproduzido lá, para que o teste seja determinístico.

## Licença

MIT — veja [LICENSE](LICENSE).
