# Preparação do servidor 10.1.115.11

Executar os blocos na ordem indicada. Se um comando falhar, resolver antes de
continuar. A recolha contínua só começa no último passo, com `up -d`.
O diretório do servidor é `/home/dp/CSP2_HUM`. O serviço Compose chama-se
`humidificacao`; o nome do contentor é atribuído pelo Compose.

## 1. Publicar as alterações locais — Command Prompt do Windows

O repositório local já está na branch `main`, com o remoto `origin` configurado.

```bat
cd /d C:\proj_dev\CSP2_HUM
git status
git add .gitignore csp2_hum_machine.py testes/test_service.py README.md DEPLOYMENT.md
git commit -m "Preparar importacao inicial do buffer e deployment"
git push origin main
```

O `.gitignore` exclui `.env`, `migrations/`, `exemplos/`, `tmp*/` e `import/`.
O JSON e as credenciais serão copiados por SSH, fora do Git.

## 2. Verificar ferramentas e clonar — sessão SSH Linux

```sh
ssh dp@10.1.115.11
git --version
docker --version
docker compose version
docker info --format '{{.ServerVersion}}'
cd ~
git clone https://github.com/I-PD/Humidificacao_CSP2.git CSP2_HUM
cd ~/CSP2_HUM
git remote -v
git log -1 --oneline
mkdir -p import
chmod 755 import
```

`git clone` cria o diretório CSP2_HUM e associa-o ao GitHub. Se o repositório
for privado, será necessária autenticação GitHub no servidor. Se CSP2_HUM já
existir, parar e verificar o seu conteúdo antes de clonar. Se `docker info`
der erro de permissões, pedir acesso ao Docker; não avançar até estar resolvido.

## 3. Copiar configuração e preview — outra janela Command Prompt do Windows

```bat
cd /d C:\proj_dev\CSP2_HUM
scp .env dp@10.1.115.11:/home/dp/CSP2_HUM/.env
scp exemplos\buffer_observado.json dp@10.1.115.11:/home/dp/CSP2_HUM/import/buffer_observado.json
```

## 4. Construir e criar o contentor parado — sessão SSH Linux

```sh
cd ~/CSP2_HUM
chmod 600 .env
chmod 644 import/buffer_observado.json
docker compose config --quiet
docker compose build
docker compose create
docker compose ps -a
```

O estado do contentor deve ser `Created`. Não executar `start` nem `up` nesta
fase. A pasta de importação é montada apenas nos comandos da próxima fase.

## 5. Confirmar a tabela e importar o preview — sessão SSH Linux

```sh
docker compose run --rm --no-deps humidificacao --init-db
docker compose run --rm --no-deps humidificacao --check-db
docker compose run --rm --no-deps -v "$PWD/import:/import:ro,Z" humidificacao --import-json /import/buffer_observado.json
docker compose run --rm --no-deps humidificacao --check-db
```

Estes comandos usam contentores temporários que terminam no fim da tarefa;
não iniciam o coletor contínuo. `:Z` aplica o contexto SELinux à pasta import
dedicada ao projeto. A imagem usa um utilizador sem privilégios, pelo que a
pasta precisa de permissão de leitura/travessia e o JSON de leitura.

O importador valida o ficheiro inteiro antes de ligar à BD. Um erro de escrita
reverte todo o lote. `record_id` e `created_at` são atribuídos pelo PostgreSQL;
as humidades ficam NULL para as linhas novas e mantêm valores manuais em
linhas já existentes. Não há SELECT nem escrita ao PLC durante a importação.

Para o preview atual são esperadas 100 passagens, IDs de operação 248 a 347,
com início mais antigo em 2026-11-12T18:11:20+00:00 e mais recente em
2026-11-18T16:48:37+00:00. Numa tabela inicialmente vazia, o resumo final deve
mostrar `passagens: 100` e `operacoes_distintas: 100`. Se já existirem outras
passagens na BD, o total pode ser superior. Reimportar os mesmos dados não
cria duplicados; a mensagem mostra quantas linhas mudaram.

`--init-db` só cria uma tabela inexistente, não migra estruturas antigas.
A tabela atual `humidificacao_csp2.operacoes` já foi preparada na BD usada
durante o desenvolvimento. Um erro de schema ou de ligação deve ser resolvido
antes da importação. A configuração DB_HOST tem de ser acessível a partir do
contentor; `localhost` dentro do contentor é o próprio contentor.

## 6. Arrancar a recolha após confirmar a importação — sessão SSH Linux

```sh
docker compose up -d
docker compose ps
docker compose logs --tail=100 -f humidificacao
```

Ctrl+C sai da visualização dos logs e deixa o serviço ligado. Os registos já
importados não são duplicados; as novas passagens e alterações são recolhidas
segundo o horário configurado. A primeira consulta ainda pode enviar o buffer
para a fila local, mas o UPSERT não altera linhas cujos valores sejam iguais.

Para parar apenas este projeto:

```sh
docker compose stop
```

O volume `hum_data` mantém a fila após paragens e reinícios. Não apagar esse
volume com dados por enviar.
