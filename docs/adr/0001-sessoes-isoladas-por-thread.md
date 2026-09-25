# ADR 0001 — Sessões isoladas, uma thread por sessão

- **Status:** aceito
- **Data:** 2026-09

## Contexto

A Caravana precisa executar o mesmo fluxo em várias identidades ao mesmo tempo
(três contas de uma loja, doze lojas diferentes). As opções de paralelismo eram:

1. **Vários processos**, um por sessão, coordenados por um arquivo ou fila.
2. **`asyncio` com a API assíncrona do Playwright**, tudo em um processo.
3. **Threads**, cada uma com seu próprio Playwright síncrono e contexto de
   navegador.

O caso de uso é automação de carga moderada (unidades a dezenas de sessões),
executada por uma pessoa na própria máquina ou em um contêiner pequeno. Não é
um serviço distribuído, e não se pretende que seja.

## Decisão

Uma **thread por sessão**, cada uma com seu próprio objeto `sync_playwright`,
navegador e contexto. Um pool com `workers` fixos limita quantas rodam ao mesmo
tempo.

O ledger SQLite é o único estado compartilhado, protegido por trava de escrita e
`check_same_thread=False`.

## Consequências

**Ganhos**

- Cada sessão é literalmente independente: um `Ctrl+C` no meio, uma página
  travada ou um contexto corrompido não afeta as outras.
- O código fica legível: a API síncrona é a que documentação e exemplos do
  Playwright usam, e um passo é uma sequência linear de chamadas.
- A retomada é natural: o estado vive no ledger, não na memória do processo.

**Custos**

- Cada thread carrega o custo de um Playwright síncrono — não é gratuito, e por
  isso `workers` tem padrão conservador em vez de "o máximo possível".
- Não há como paralelizar *dentro* de uma sessão (duas páginas da mesma conta ao
  mesmo tempo). Quando isso for necessário, a solução é abrir outra sessão.
- Escala horizontal exige rodar várias instâncias com `run_dir` distintos; o
  projeto não faz coordenação entre processos.

**Alternativas descartadas**

- `asyncio`: obrigaria toda a superfície pública a ser assíncrona e tornaria o
  código de cada ação mais difícil de seguir, sem ganho no caso de uso real.
- Multiprocesso: melhor isolamento, mas custo de serialização do estado e
  complexidade de coordenação desproporcionais ao problema.
