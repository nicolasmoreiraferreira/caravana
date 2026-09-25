# Estado do projeto

Nota de trabalho, mantida durante a construção. Registra onde o projeto está,
o que já foi validado e o que ficou pendente. Não é documentação de uso — para
isso, veja o [README](../README.md).

## Situação

**Versão:** 0.1.0 — publicada em repositório público.
**Repositório:** https://github.com/nicolasmoreiraferreira/caravana
**Última verificação local:** 195 testes, `ruff` e `mypy` estrito limpos,
cobertura de 83%.

## O que foi entregue

O motor completo: fluxos declarativos em TOML com validação estática, N sessões
isoladas em paralelo, resiliência em três níveis (retry, tolerância, desvio),
retomada com gerações no ledger, cofre de segredos, ledger SQLite, relatórios em
Markdown/HTML, manifesto com SHA-256 e verificação de integridade, e uma CLI de
nove comandos.

Além do código, o projeto tem o que um repositório público precisa para ser
usável e mantível: README de referência, guia de contribuição, política de
segurança, changelog, três ADRs, templates de issue e PR, CI em três versões de
Python e três exemplos executáveis contra um site de demonstração local.

## Defeitos encontrados e corrigidos durante a construção

Todos foram descobertos pelos próprios testes, não por inspeção — o que é o
argumento para mantê-los.

| Defeito | Como apareceu | Correção |
| --- | --- | --- |
| `extract` ignorava a regex declarada por campo | Teste de extração com regex | Padrão por campo com queda para o do passo |
| `extract_attr` devolvia `None` em campo preenchido | Teste de `type` + leitura do valor | Queda para `input_value()` quando o atributo `value` está vazio |
| `reuse_profile` quebrava na primeira execução | Teste de perfil reutilizável | Passa `storage_state` só quando o arquivo existe |
| Contagem de falhas somava tentativas recuperadas | Relatório do exemplo de resiliência | Contadores passaram a ser por passo, não por tentativa |
| `/flaky` falhava uma vez por servidor, não por sessão | Exemplo de resiliência não exercitava retry | Estado por cliente, marcado por cookie |
| `press`/`click` não esperavam a página assentar | Teste de login intermitente | Espera de `domcontentloaded` após a ação |
| `robots.txt` liberava caminho proibido | Teste do parser com `Allow: /` + `Disallow` | Parser próprio conforme a RFC 9309 |
| Playwright importado no topo do módulo | Instalação mínima sem o extra | Import sob demanda, com mensagem acionável |
| `click` usado nos testes sem estar declarado | CI falhando nas três versões | Declarado no extra `dev` |
| Rich mutilava `[playwright]` como markup | Saída do `doctor` | Texto escapado, com teste sobre a saída renderizada |
| mypy reprovava ambiente sem Playwright | Job de lint do CI | `playwright.*` como import opcional |

## Pendências conhecidas

**Publicação no PyPI.** O README diz que a distribuição está prevista. Enquanto
não houver release publicada, a instalação é a partir do repositório. Publicar
exige conta no PyPI e um token — decisão do autor.

**Cobertura de 73% no CLI.** Os caminhos não cobertos são majoritariamente
interativos (painel ao vivo, abertura de navegador) e ramos de erro de sistema
de arquivos. Não são lacunas de correção, mas há espaço para testes de painel.

**Sessões compartilhadas no mesmo perfil.** `reuse_profile` é por sessão, e não
há como declarar que duas sessões compartilham um perfil de forma intencional.
Se isso for necessário, o modelo de configuração precisa de um campo explícito —
com a ressalva de que compartilhar cookies entre sessões contraria o isolamento
que é a premissa do projeto.

**Distribuição entre máquinas.** Fora do escopo por decisão registrada no
ADR 0001. Escala horizontal exige rodar instâncias separadas.
