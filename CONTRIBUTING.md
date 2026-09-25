# Contribuindo com a Caravana

Obrigado pelo interesse. Este documento diz como o projeto é organizado, quais
são os padrões de código e o que se espera de uma contribuição.

## Princípios

Antes de propor uma mudança, vale entender o que guia as decisões do projeto —
mudanças que contrariam estes princípios tendem a ser recusadas, mesmo quando
bem implementadas.

1. **Falhar cedo, falhar claro.** Um erro de digitação no fluxo deve aparecer na
   validação, em milissegundos, e não no meio de uma execução de 40 minutos.
   Mensagens de erro dizem o que foi tentado, o que se esperava e o que veio.
2. **Nada desaparece em silêncio.** Um passo pulado, tolerado ou retomado
   aparece no histórico com o motivo. Relatório que omite trabalho é pior do
   que relatório que reporta falha.
3. **Evidência é o produto.** Screenshot e HTML de falha, hash de artefato,
   ledger consultável. Sem isso, automação é fé, não engenharia.
4. **O navegador é caro.** Não abra um contexto para descobrir que o seletor
   está errado. Valide antes; reutilize o que já existe.
5. **Segredo não vaza.** Credencial interpolada no uso não entra no ledger, no
   relatório nem no log — e há teste que verifica isso.

## Ambiente

```bash
git clone https://github.com/nicolasmoreiraferreira/caravana
cd caravana
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,playwright]"
python -m playwright install chromium
```

## Antes de abrir o pull request

```bash
python -m pytest tests/ -q                       # tudo verde
python -m ruff check .                           # sem avisos
python -m ruff format --check .                  # formatado
python -m mypy                                   # tipagem limpa
```

O CI roda exatamente estes comandos em Python 3.11, 3.12 e 3.13, mais uma
execução de exemplo ponta a ponta. PR com lint ou tipagem vermelha não é
revisado.

## Testes

A suíte tem três camadas, e cada uma tem um papel:

| Camada | Arquivo | O que cobre | Precisa de navegador |
| --- | --- | --- | --- |
| Unitária | `test_flow_config.py`, `test_ledger_report.py`, `test_robots.py` | Fluxo, configuração, templates, ledger, relatórios, robots | Não |
| Ações | `test_actions.py` | Cada ação contra o site de demonstração | Sim |
| Integração | `test_integration.py` | Concorrência, retomada, evidências, integridade | Sim |
| CLI | `test_cli.py` | Comandos, códigos de saída, `--json` | Parcialmente |

Marcadores disponíveis:

```bash
pytest -m "not integration"   # rápido, sem navegador
pytest -m integration         # tudo que abre Chromium
pytest -m slow                # casos com espera real
```

### Regras para testes

- **Nenhum teste toca a internet.** Todo comportamento que precise de uma página
  específica deve ser adicionado ao site de demonstração
  (`examples/demo-site/server.py`). Se o seu caso não pode ser reproduzido lá,
  provavelmente o caso não é determinístico o suficiente para ser testado.
- **Teste o comportamento, não a implementação.** `assert result.status ==
  "completed"` e a leitura de `data/<sessão>.json` valem mais do que espiar
  atributos internos.
- **Nomeie em português, descrevendo o cenário.** `test_passo_tolerado_marca_sessao_degradada`
  diz o que se espera; `test_optional` não diz nada.
- **Espere o suficiente para não piscar.** Testes com tempo real precisam de
  margem. Se um teste falha uma vez em vinte, ele é um bug de teste, e vale
  corrigir a margem em vez de repetir a execução.

## Estilo de código

- Python 3.11+, tipagem em todas as funções públicas (o `mypy` está no modo
  estrito).
- Linhas de até 100 colunas, formatadas pelo `ruff format`.
- **Comentários explicam o porquê, não o quê.** Este é o padrão mais importante
  do projeto: um comentário que repete o código é ruído; um comentário que
  explica por que a decisão foi tomada — e o que aconteceria se ela fosse
  diferente — é documentação. Exemplo:

  ```python
  # `quality` só é aceito pelo Playwright para JPEG; passar em PNG levanta erro
  # e derrubaria um passo que deveria apenas registrar evidência.
  ```

- Docstrings descrevem o contrato e os casos de borda relevantes.
- Erros próprios herdam de `CaravanaError` (veja `src/caravana/errors.py`);
  mensagens em português, com o valor que causou o problema.

## Adicionando uma ação

1. Implemente `_action_<nome>` em `src/caravana/session.py`, seguindo o padrão
   das existentes: validar parâmetros com `ActionError`, devolver
   `ActionResult` com `value`, `artifacts` e `message`.
2. Registre o nome em `KNOWN_ACTIONS` (`src/caravana/flow.py`), para que a
   validação estática o reconheça.
3. Adicione a ação à tabela do README e um exemplo no fluxo de exemplo, se ela
   merecer demonstração.
4. Escreva testes em `tests/test_actions.py` cobrindo o caminho feliz **e** os
   casos de borda: parâmetro ausente, elemento inexistente, valor vazio.
5. Se a ação precisar de uma página específica, adicione a rota ao site de
   demonstração.

## Adicionando uma capacidade de infraestrutura

Mudanças no motor, no ledger ou na retomada têm risco maior. Nesses casos:

- explique no PR qual invariante a mudança preserva ou altera;
- adicione um teste que falharia **antes** da mudança — isso prova que o teste
  mede algo;
- considere registrar a decisão em `docs/adr/` (veja abaixo).

## Decisões de arquitetura

Decisões estruturais ficam em `docs/adr/`, no formato
[ADR](https://adr.github.io/): contexto, decisão, consequências. Um ADR não é
documentação de uso — é o registro de por que o projeto é como é, para que a
pergunta "por que não fizeram diferente?" tenha resposta sem arqueologia.

## Relatando problemas

Um relatório útil tem:

- versão (`caravana version`), sistema e versão do Python;
- o fluxo e a configuração mínimos que reproduzem o problema;
- o que você esperava e o que aconteceu;
- a saída de `caravana show --run-dir <dir> --json`, se aplicável.

Se for uma falha de passo, anexe o screenshot e o HTML gerados em
`artifacts/falha-*` — eles existem justamente para isso.

## Licença das contribuições

Ao enviar um pull request, você concorda em licenciar sua contribuição sob a
licença MIT, os mesmos termos do projeto.
