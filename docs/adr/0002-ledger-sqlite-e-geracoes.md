# ADR 0002 — Ledger SQLite com gerações para retomada

- **Status:** aceito
- **Data:** 2026-09

## Contexto

Cada execução precisa responder, depois de terminar: o que rodou, o que falhou,
por que falhou, quanto tempo levou, o que foi coletado e onde está a evidência.
Além disso, uma execução interrompida deve poder continuar de onde parou, sem
repetir trabalho já concluído.

Os formatos possíveis eram: arquivos JSON por execução, um log de eventos
(`events.jsonl`) como fonte da verdade, ou um banco relacional.

## Decisão

Um banco **SQLite** por diretório de execuções (`<run_dir>/caravana.db`), com
tabelas `runs`, `steps` e `artifacts`. O log de eventos (`--log`) é derivado e
serve para acompanhamento ao vivo, não como fonte da verdade.

A retomada usa o conceito de **geração**: cada vez que uma execução é retomada,
o contador de geração da execução aumenta, e os passos são gravados com a
geração em que ocorreram. Consultas padrão leem a **geração ativa**.

## Consequências

**Ganhos**

- A retomada é uma consulta: os passos com `status = 'ok'` na geração ativa
  estão concluídos; o primeiro passo sem `ok` é onde recomeçar.
- O relatório de uma execução retomada mostra o estado **atual**, não a soma das
  tentativas — que confundiria mais do que informaria.
- As tentativas anteriores continuam no banco (geração anterior), então é
  possível reconstruir o histórico completo de uma execução problemática.
- Artefatos são consultados em todas as gerações: uma evidência capturada em
  uma tentativa que falhou continua sendo evidência, e não deve desaparecer
  porque a execução foi retomada depois.
- SQLite resolve escrita concorrente de várias threads com uma trava simples, e
  o banco é um arquivo único — copiável junto com o resto da execução.

**Custos**

- Toda leitura passa por SQL; não há atalho em memória para o estado. Em troca,
  o estado sobrevive à queda do processo.
- O esquema precisa de migração quando muda. A migração é feita com `ALTER TABLE`
  defensivo na abertura (`_ensure_schema`), o que mantém bancos antigos legíveis.
- Consultas que precisam de histórico completo precisam pedir explicitamente
  (`all_generations=True`), em vez de recebê-lo por engano.

**Alternativas descartadas**

- JSON por execução: simples, mas reescrever o arquivo inteiro a cada passo é
  caro e frágil a interrupção no meio da escrita.
- Log de eventos como fonte da verdade: exige reprocessar o arquivo inteiro para
  responder "onde parou", e um log truncado perde a informação de forma
  silenciosa.
