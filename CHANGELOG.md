# Changelog

Todas as mudanças relevantes deste projeto são documentadas aqui.

O formato segue [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e o
projeto adere ao [Versionamento Semântico](https://semver.org/lang/pt-BR/).

## [Não publicado]

## [0.1.0] — 2026-09-25

Primeira versão pública.

### Adicionado

**Fluxos declarativos**

- Modelo de fluxo em TOML com passos identificados, `next`, `when`, `save_as`,
  `retries`, `retry_delay`, `timeout_ms`, `optional`, `on_error` e `artifacts`.
- Validação estática completa antes da execução: identificadores duplicados,
  ações desconhecidas, saltos para passos inexistentes e `save_as` inconsistente.
- Interpolação de `${base}`, `${var.*}`, `${secret.*}` e `${session.data.*}`.
- Exportação do fluxo em Mermaid para revisão em pull request.

**Motor**

- N sessões isoladas em paralelo, com limite de `workers`.
- Contexto de navegador independente por sessão: cookies, `localStorage` e
  página não são compartilhados.
- Retomada após interrupção, com gerações no histórico: passos concluídos não se
  repetem, e a retomada é recusada se o fluxo tiver mudado.
- Políticas de falha em três níveis: retry, tolerância (`optional`) e desvio
  (`on_error = "skip_to:<passo>"`).
- `reuse_profile` para persistir cookies entre execuções.
- `robots.txt` interpretado conforme a RFC 9309.

**Ações**

- Navegação: `goto`, `wait`, `wait_for`.
- Interação: `click`, `fill`, `type`, `press`, `login`.
- Extração: `extract`, `extract_all`, `extract_attr`, `paginate`.
- Verificação: `assert_text`, `assert_url`.
- Aquisição e registro: `screenshot`, `download`, `http_get`.

**Observabilidade**

- Ledger SQLite com execuções, tentativas, duração, erros e artefatos.
- Relatório em Markdown e HTML autocontido, além de selo SVG.
- Manifesto com SHA-256 de cada artefato e comando `verify` para conferência.
- Linha do tempo de eventos em JSON Lines (`--log`).
- Captura automática de screenshot e HTML no momento da falha.

**Interface**

- CLI com `run`, `validate`, `plan`, `list`, `show`, `report`, `verify`, `doctor`
  e `version`, todas com `--json`.
- Painel de progresso ao vivo e códigos de saída documentados.

**Segurança**

- Cofre de segredos por variável de ambiente (`CARAVANA_SECRET_*`) ou arquivo
  `.env`, com teste que falha se um segredo aparecer em qualquer saída.

**Qualidade**

- Suíte com testes unitários, por ação e de integração, contra um site de
  demonstração local — nenhum teste acessa a internet.
- `ruff` (regras e formatação) e `mypy` em modo estrito.
- CI em Python 3.11, 3.12 e 3.13, com execução de exemplo ponta a ponta.

[Não publicado]: https://github.com/nicolasmoreiraferreira/caravana/compare/main...HEAD
[0.1.0]: https://github.com/nicolasmoreiraferreira/caravana/releases/tag/v0.1.0
