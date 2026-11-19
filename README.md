# Recolha da humidificação CSP2

Uma linha representa uma passagem/processo de um contentor nesta máquina.
O mesmo `operation_id` pode ter várias linhas, com diferentes inícios e
programas. Um novo início cria outra linha, mesmo que o programa se repita.
Mantendo-se operação e início, alterações de fim e resultados atualizam a
mesma linha. O fim não faz parte da chave, porque pode chegar mais tarde.
Uma alteração isolada do programa com o mesmo início não inventa uma passagem.

## Tabela

`humidificacao_csp2.operacoes`, na base definida por `DB_NAME` no `.env`:

| Coluna | Significado |
|---|---|
| record_id | Identificador automático da passagem |
| inicio | Início publicado pelo PLC |
| fim | Fim publicado pelo PLC, mesmo quando ainda anterior ao início |
| operation_id | Operação à qual pertence o contentor |
| programa | Programa associado à passagem |
| num_inj | Número de injetores |
| vel_tpt | Velocidade do tapete em percentagem |
| qtd_agua | Quantidade de água |
| hum_entrada | Humidade de entrada preenchida manualmente; NULL até preenchimento |
| hum_saida | Humidade de saída preenchida manualmente; NULL até preenchimento |
| created_at | Momento da primeira inserção na BD, atribuído pelo PostgreSQL |

Chave de deduplicação: `(operation_id, inicio)`. `created_at` mantém-se nos
UPDATEs; não é o timestamp do PLC nem o momento da leitura. Se a fila estiver
retida por uma falha de rede, só é atribuído quando a inserção for confirmada.
Não há id de máquina, estado, duração, timestamps de observação ou words em
hexadecimal na tabela. A duração pode ser calculada a partir de início/fim.
As humidades são reservadas para introdução manual através do futuro dashboard.
O coletor exclui estas colunas dos INSERTs e UPDATEs: os zeros do PLC nunca
substituem valores manuais. NULL significa não preenchido; zero é um valor
numérico distinto e só é guardado se for introduzido explicitamente.

Uma data final inválida/vazia é NULL. Zero é uma medição válida. Os instantes
do PLC são interpretados em `PLC_TIMEZONE` e guardados como TIMESTAMPTZ.
A água é arredondada a duas casas decimais antes da deduplicação e guardada
como NUMERIC(12,2); por exemplo, 0.80000007 fica 0.80 na BD. Configurar também
duas casas decimais no painel Grafana para manter esse formato na apresentação.

## Leitura e histórico

Consulta FINS/TCP ao PLC `10.0.0.60:9600`, a cada 15 segundos:

- Zona dinâmica: EM0_0..20, com os valores atuais.
- Histórico: EM0_1000..9999, até 360 registos de 25 words cada.

| Offset dentro do registo | Campo | Conversão |
|---|---|---|
| 0–2 | inicio | YY MM DD HH mm SS em BCD |
| 3–5 | fim | Mesmo formato |
| 6–15 | programa | Texto Latin-1, 20 bytes |
| 16 | operation_id | Inteiro sem sinal |
| 17 | num_inj | Inteiro sem sinal |
| 18 | vel_tpt | Inteiro sem sinal |
| 19–20 | qtd_agua | Float32, word baixa primeiro |
| 21–22 | hum_entrada do PLC | Não interpretada nem gravada pelo coletor |
| 23–24 | hum_saida do PLC | Não interpretada nem gravada pelo coletor |

A ordem dos timestamps do histórico acima foi confirmada por leitura real:
a ilustração do buffer no Excel tem as words dos timestamps invertidas.
Foram observados 100 registos preenchidos, mais recente em E0_1000,
e 260 posições vazias. O serviço percorre as 360 posições a cada consulta,
em blocos de 10 registos, sem depender de um ponteiro não documentado.
Compara também o primeiro registo antes/depois para detetar deslocações
durante a leitura; nesse caso, não envia a leitura parcial e volta a tentar.

Quando a passagem existe no histórico, usam-se os seus resultados. A zona
dinâmica pode já ter valores repostos a zero para a preparação seguinte. Uma
passagem presente só na zona dinâmica é gravada imediatamente, mesmo com fim
anterior ao início, e é atualizada quando chegarem outros valores ou quando
aparecer no histórico. O programa fica associado ao início exportado pelo PLC.

Na primeira execução recuperam-se todos os registos disponíveis no histórico,
por ordem de início. Não se usa a posição do buffer como identidade: os
registos podem mudar de posição. A fila SQLite e a comparação dos campos no
PostgreSQL impedem escritas repetidas quando nada muda, incluindo após reinícios.
O serviço só recupera o que ainda existir no buffer; não recupera registos que
tenham sido sobrescritos durante uma paragem longa.

## Horário

`SCHEDULE_TIMEZONE=Europe/Lisbon`, com mudança automática de hora:

- Segunda a sexta: 06:00 inclusive até 20:00 exclusive.
- Sábado: 08:00–08:30. Observando um início ou fim do próprio sábado, até ao
  momento atual, prolonga a recolha até às 20:00. A confirmação fica no volume.
- Sem confirmação, suspende às 08:30. Domingo: suspenso.

Um bloco histórico do dia anterior não confirma atividade de sábado. Se o PLC
estiver inacessível durante toda a janela de confirmação, suspende às 08:30.
`HOLIDAYS` existente no `.env` não é utilizado nesta versão.

Fora do horário, o processo fica em espera, fecha a ligação ao PLC e verifica
o relógio a cada 30 segundos. O contentor continua ligado. A fila pode terminar
de enviar alterações já recolhidas fora do horário.

## Futuro dashboard Grafana

O seletor de intervalo de datas pode filtrar `inicio`, usando a macro
`$__timeFilter(inicio)`. Uma variável de consulta pode listar `operation_id`
para escolher uma ou várias operações desse intervalo.

Para introduzir humidades, usar um painel com capacidade de edição, por exemplo
Business Table, ou um formulário Business Forms com dois campos e botão Guardar.
A escrita deve identificar a passagem por `record_id`: uma operação pode ter
várias linhas. Uma pequena API pode validar os valores e atualizar apenas
`hum_entrada` e `hum_saida`, usando parâmetros SQL. O datasource de consulta
do dashboard pode manter permissões só de leitura.

As humidades manuais pertencem à passagem selecionada na BD. Não alteram as
words do PLC nem as restantes passagens do mesmo `operation_id`.

Referências:

- [Variáveis PostgreSQL no Grafana](https://grafana.com/docs/grafana/latest/datasources/postgres/template-variables/)
- [Edição no Business Table](https://grafana.com/docs/plugins/volkovlabs-table-panel/latest/editable-data/)
- [Formulários Business Forms](https://grafana.com/docs/plugins/volkovlabs-form-panel/latest/)

## Arranque

O guia [DEPLOYMENT.md](DEPLOYMENT.md) descreve a publicação no GitHub, criação
do contentor parado no servidor, importação inicial e arranque da recolha.
`--import-json` importa o preview numa única transação e termina, sem consultar
o PLC nem iniciar a recolha. É repetível sem duplicar passagens e preserva as
humidades manuais existentes. `--check-db` mostra um resumo só de leitura.

O `schema.sql` é a única fonte do SQL de criação e está incluído na imagem.
O `.env` com as credenciais reais é passado ao contentor apenas em execução.
Opções de configuração adicionais estão em `.env.example`.

```sh
docker compose build
docker compose run --rm humidificacao --init-db
docker compose up -d
docker compose logs -f --tail=100 humidificacao
```

`--init-db` cria schema, tabela e índice e termina, sem iniciar a recolha.
Não migra uma tabela incompatível nem apaga a tabela da primeira versão,
`humidificacao.operacoes`. Esta versão usa `humidificacao_csp2.operacoes`.
Se já existir aí uma tabela com outro formato, é necessária uma migração.
Para atualizar uma tabela da versão anterior desta recolha (água DOUBLE PRECISION
e sem humidades), aplicar a alteração SQL na base de dados. Os scripts locais
em `migrations/` não são publicados neste repositório. `--init-db` apenas cria
a tabela se não existir; não executa migrações.
O utilizador de criação precisa de permissões DDL; o serviço precisa de
INSERT, UPDATE, SELECT e acesso à sequência de `record_id`.

O servidor precisa de acesso TCP ao PLC e ao PostgreSQL. Não há portas a
publicar. Executar apenas uma instância. O volume `hum_data` guarda a fila,
a deduplicação por passagem e a confirmação de sábado; não o apagar com
dados por enviar. Pendentes da primeira versão são convertidos para os novos
nomes sem serem descartados. O destino da nova versão é a tabela CSP2.
Um replay após commit mas antes da confirmação local não duplica a passagem.

## Validação local

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s testes -p test_service.py -v
python csp2_hum_machine.py --print-schema
python csp2_hum_machine.py --ignore-schedule
python csp2_hum_machine.py --write-db
```

Sem `--write-db`, só apresenta alterações, sem gravar operações nem fila.
`--ignore-schedule` permite um teste manual fora do horário. Parar com Ctrl+C.
