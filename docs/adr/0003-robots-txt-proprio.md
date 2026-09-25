# ADR 0003 — Parser de `robots.txt` próprio em vez do da biblioteca padrão

- **Status:** aceito
- **Data:** 2026-09

## Contexto

A Caravana oferece `respect_robots = true` como padrão: quem aponta a ferramenta
para um site promete obedecer ao que o dono declarou. Essa promessa precisa ser
cumprida de verdade.

A implementação inicial usava `urllib.robotparser` da biblioteca padrão. Durante
os testes, um caso revelou o problema: para o arquivo

```
User-agent: *
Allow: /
Disallow: /private
```

o `robotparser` respondia que `/private` **pode** ser acessado.

A causa é que o `robotparser` avalia `Allow` e `Disallow` em passagens
separadas: ele primeiro pergunta se algum `Allow` casa; se casar, libera —
ignorando um `Disallow` mais específico. A RFC 9309 diz o contrário: entre as
regras que casam, vence a de **caminho mais longo**, e em empate vence `Allow`.

O padrão `Allow: /` seguido de exceções é comum em sites reais (é como se
declara "tudo liberado, menos esta área"). Ou seja: a biblioteca padrão erra
justamente no caso mais frequente, e erra na direção de acessar o que foi
proibido.

## Decisão

Implementar um parser próprio (`src/caravana/robots.py`), seguindo a RFC 9309:

- grupos começam em `User-agent` e terminam no próximo;
- vale o agente mais específico; `*` é reserva;
- entre as regras aplicáveis, vence a de caminho mais longo;
- em empate de comprimento, `Allow` prevalece;
- curingas `*` e ancoragem `$` são interpretados;
- `Crawl-delay` é lido e exposto, mas não aplicado automaticamente (o motor
  respeita o paralelismo configurado pelo usuário; aplicar um atraso escondido
  mudaria o tempo de execução sem o usuário pedir);
- falha de rede ao buscar o arquivo **não** bloqueia: sem arquivo, não há regra
  declarada. Já uma regra lida é obedecida.

## Consequências

**Ganhos**

- A promessa é cumprida no caso que mais importa — o de exceções sob um `Allow`
  amplo.
- O comportamento é testável de forma direta, sem abrir navegador: 20 testes em
  `tests/test_robots.py` cobrem a matriz de regras, incluindo os casos em que a
  biblioteca padrão diverge.
- Menos dependência implícita de comportamento não especificado.

**Custos**

- Código a manter (~90 linhas) e a responsabilidade de acompanhar a RFC.
- Casos exóticos que o `robotparser` talvez trate e o parser próprio não trate
  ficam por nossa conta — mitigado pela suíte de testes, que é explícita sobre o
  que é suportado.
- `Crawl-delay` lido mas não aplicado é uma decisão que precisa estar
  documentada (está), para não ser lida como esquecimento.

**Alternativas descartadas**

- Manter o `robotparser` e documentar a limitação: inaceitável — a limitação
  leva a acessar exatamente o que o site pediu para não acessar.
- Bloquear quando o arquivo não puder ser lido: transformaria uma falha de rede
  transitória em bloqueio total, punindo o usuário por um problema que não é
  dele. A escolha de liberar é a mesma do padrão da web.
