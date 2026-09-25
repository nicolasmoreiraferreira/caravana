## O que muda

<!-- Uma frase sobre o efeito da mudança, não sobre os arquivos alterados. -->

## Por quê

<!-- O problema que isto resolve. Link para a issue, se houver. -->

## Como foi verificado

- [ ] `pytest tests/ -q` verde
- [ ] `ruff check .` e `ruff format --check .` sem avisos
- [ ] `mypy` limpo
- [ ] Teste novo que **falharia antes** desta mudança (quando aplicável)
- [ ] Documentação atualizada (README, CHANGELOG, docstrings)

## Checklist

- [ ] Se adiciona uma ação: registrada em `KNOWN_ACTIONS`, testada nos casos de
      borda e documentada na tabela do README
- [ ] Se toca o motor, o ledger ou a retomada: invariantes descritas abaixo
- [ ] Nenhum teste acessa a internet
- [ ] Nenhum segredo, dado real de sessão ou arquivo de `.caravana/` foi
      incluído no commit

## Invariantes (só para mudanças no núcleo)

<!--
Se a mudança toca execução, concorrência, retomada ou escrita no ledger,
descreva o que continua valendo. Ex.: "passos concluídos continuam sendo
reaproveitados na retomada" ou "uma sessão que falha não interrompe as outras".
-->
