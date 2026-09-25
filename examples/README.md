# Exemplos

Três fluxos, cada um demonstrando uma ideia diferente do motor. Todos rodam
contra o site de demonstração local — nenhum toca a internet.

```bash
# terminal 1
python examples/demo-site/server.py --port 8787
```

## `catalogo` — coleta concorrente

Três sessões em paralelo percorrem o catálogo paginado, confirmam o conteúdo,
extraem campos estruturados, baixam um arquivo e guardam screenshot do primeiro
passo.

```bash
caravana run --config examples/catalogo/caravana.toml
```

O que observar: `paginate` segue o seletor "próxima" até ele desaparecer;
`save_as` grava o resultado em `data/<sessão>.json`; `artifacts` produz
evidência; e `workers = 3` faz as três sessões avançarem juntas.

## `login` — credenciais fora do arquivo

Duas contas autenticam ao mesmo tempo, com usuário e senha vindos do cofre de
segredos. Nenhum valor aparece no fluxo, no histórico ou no relatório.

```bash
export CARAVANA_SECRET_DEMO_USER=demo
export CARAVANA_SECRET_DEMO_PASSWORD=demo
caravana run --config examples/login/caravana.toml
```

O que observar: `${secret.*}` é resolvido no momento do uso, e o motor mascara o
valor em toda mensagem que persiste. Há um teste dedicado que falha se um
segredo vazar para qualquer saída.

## `resiliencia` — o que acontece quando algo falha

Quatro sessões enfrentam situações diferentes de propósito: uma página instável
que exige retry, um passo opcional que falha sem derrubar a sessão, um desvio
declarado para um caminho alternativo e uma falha definitiva.

```bash
caravana run --config examples/resiliencia/caravana.toml
```

O que observar no relatório:

- a sessão que sofreu retry mostra **as duas tentativas** no histórico;
- a sessão com passo opcional termina **degradada**, e não como sucesso pleno —
  o relatório diz o que faltou;
- os passos saltados pelo desvio aparecem como `skipped`, com o motivo;
- a sessão que falhou de verdade gera screenshot e HTML da página quebrada,
  antes de o navegador fechar.

É o exemplo mais útil para entender a diferença entre *retry*, *tolerância* e
*desvio*.

## Depois de rodar

```bash
caravana list --run-dir examples/catalogo/.caravana/runs
caravana show --run-dir examples/catalogo/.caravana/runs --json | head -40
caravana verify --run-dir examples/catalogo/.caravana/runs
```

`verify` recalcula o SHA-256 de cada artefato contra o manifesto. Alterar um
arquivo e rodar de novo faz o comando apontar qual divergiu — é a mesma
verificação que se usa para anexar evidências a uma auditoria.

Os diretórios `.caravana/` são ignorados pelo Git: contêm dados de sessão,
screenshots e o que foi coletado.
