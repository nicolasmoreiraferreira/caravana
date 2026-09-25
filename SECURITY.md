# Política de segurança

## Versões suportadas

| Versão | Suporte |
| --- | --- |
| 0.1.x | Sim |

## Relatando uma vulnerabilidade

**Não abra uma issue pública para problemas de segurança.** Use o canal privado
do GitHub: aba **Security** → **Report a vulnerability**.

Inclua, se possível:

- versão da Caravana (`caravana version`) e do Python;
- o que um atacante consegue fazer, e em que condições;
- passos mínimos para reproduzir;
- sua avaliação de impacto.

Você recebe uma resposta em até 7 dias. Se a falha for confirmada, a correção
sai em uma versão de patch, e você é creditado no changelog (ou anônimo, se
preferir).

## Modelo de ameaça

Vale ser explícito sobre o que a Caravana **não** promete, porque a ferramenta
roda fluxos escritos por quem a usa e abre páginas de terceiros.

### O que está no escopo

**Vazamento de segredos.** Credenciais chegam por variável de ambiente ou
arquivo `.env`, são interpoladas no momento do uso e não devem aparecer no
ledger, no relatório, no log de eventos nem no manifesto. Há teste dedicado que
falha se um valor de segredo aparecer em qualquer saída.

**Injeção em templates.** `${...}` resolve apenas contra o cofre de segredos,
variáveis declaradas e dados coletados pela própria execução. Não há avaliação
de expressões arbitrárias, chamadas de função ou acesso a atributos de objetos
Python.

**Integridade das evidências.** Todo artefato é registrado com SHA-256 no
manifesto, e `caravana verify` detecta alteração posterior. Isso protege contra
adulteração acidental ou oportunista — não contra quem controla o sistema de
arquivos e pode reescrever o manifesto também.

**Manuseio de páginas hostis.** O conteúdo baixado é gravado em disco como dado.
HTML de falha é salvo como texto; o relatório HTML é gerado a partir de dados
internos, com escape, e não renderiza HTML da página alvo.

### O que não está no escopo

**Sites maliciosos atacando a máquina.** A Caravana abre um navegador real. Uma
página hostil pode explorar o próprio navegador ou o sistema operacional, da
mesma forma que exploraria se você a abrisse manualmente. Recomendação: rode
fluxos contra sites desconhecidos em contêiner ou máquina virtual, e mantenha o
Chromium atualizado.

**Isolamento entre sessões como fronteira de segurança.** Os contextos são
isolados para **correção** (cookies não vazam de uma conta para outra), não como
sandbox de segurança. Duas sessões rodam no mesmo processo e no mesmo usuário do
sistema.

**Fluxos escritos por terceiros.** Um arquivo de fluxo é código de configuração
executável: ele abre URLs, preenche formulários e baixa arquivos. Revise fluxos
antes de executá-los, como revisaria um script.

**Proteção contra uso indevido.** A Caravana respeita `robots.txt` por padrão,
mas o usuário pode desligar essa verificação. A ferramenta não tenta detectar
nem impedir uso abusivo — a responsabilidade é de quem a opera.

**Negação de serviço local.** Um fluxo com `count` alto consome memória e CPU
proporcionalmente. Não há limite embutido de sessões; use `workers` com critério.

## Boas práticas para quem escreve fluxos

- Nunca escreva credenciais no `flow.toml`; use `${secret.NOME}`.
- Adicione o arquivo de segredos ao `.gitignore`.
- Mantenha `respect_robots = true` ao apontar para sites de terceiros.
- Trate `.caravana/` como dado sensível: contém screenshots de páginas
  autenticadas, HTML de sessão e dados coletados. O `.gitignore` do projeto já o
  exclui; mantenha assim.
- Ao compartilhar um relatório, verifique `artifacts/` — as evidências podem
  conter dados de sessão.
