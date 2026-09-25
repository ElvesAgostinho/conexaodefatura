# Gateway Fiscal: origens → Django → AGT

Ponte de integração que recebe faturas já emitidas pelos sistemas de faturação das
empresas (HOST Hotel Systems e outros) e as comunica à AGT. Não é um ERP nem um sistema
de faturação: nunca escreve nas bases de dados de origem.

```
 BD do HOST / outro sistema ──(leitura, mapeamento configurável)──┐
                                                                    ├─► importação ─► validação ─► fila ─► AGT
 ERP / POS ──(API REST com chave da empresa)───────────────────────┘      (sem duplicados)
```

## Estado

| Fase | Estado |
|---|---|
| 1–5. Projeto, BD do Gateway (vários SGBD), configuração segura, diagnóstico e teste de ligação | ✅ |
| 6. Mapeamento real do HOST | ⏳ **precisa de acesso à BD real do HOST**. O motor genérico já está pronto: falta só preencher o mapeamento da origem HOST ([docs/origens.md](docs/origens.md)) |
| Multiempresa e várias origens por empresa (BD de qualquer SGBD ou API) | ✅ |
| Importação, formato canónico e validação | ✅ [docs/formato_canonico.md](docs/formato_canonico.md) |
| API REST para outros sistemas | ✅ [docs/api.md](docs/api.md) |
| Configuração AGT por empresa e fila de envio com novas tentativas | ✅ |
| Comunicação real com a AGT | ⏳ **precisa da especificação técnica oficial**. Até lá só o modo *Simulação* envia; os ambientes Testes/Produção falham de forma explícita |
| Painel web | ✅ |

## Instalação

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # preencher
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py createsuperuser
.venv\Scripts\python manage.py test
.venv\Scripts\python manage.py runserver     # painel em http://127.0.0.1:8000/
```

Primeiros passos: no `/admin/` criar a empresa e dar acesso aos utilizadores (papéis:
Administrador, Operador, Consulta). No painel, configurar as origens, a AGT e as chaves de API.

## Painel

- **Início**: contagem por estado, últimos documentos, estado das origens, avisos
  (modo simulação, configuração AGT incompleta, envios interrompidos).
- **Faturas**: pesquisa e filtros; detalhe com linhas, erros de validação, resposta AGT e
  histórico. Operador: enviar, reenviar rejeitadas/erros, revalidar.
- **Origens**: criar e editar (administrador), testar a ligação e sincronizar (operador).
- **Configuração AGT**: ambiente, endereço, software certificado, envio automático,
  tentativas. Os segredos ficam no `.env`; o painel só mostra se estão definidos.
- **Chaves de API** e **Auditoria**: só administradores.

Cada utilizador só vê as empresas a que tem acesso. Todas as ações ficam na auditoria.

## Bases de dados de origem (somente leitura)

Motores suportados em `HOST_DB_ENGINE`: `mssql`, `postgresql`, `mysql`, `mariadb`,
`oracle`, `sqlite`. Para outro SGBD (Firebird, DB2, Informix, ...), instalar o dialeto
SQLAlchemy e usar `HOST_DB_URL`.

Proteções contra escrita no HOST, em camadas:

1. **Utilizador dedicado só com SELECT** (a única garantia real; pedir ao DBA).
   Exemplo em SQL Server:
   ```sql
   CREATE LOGIN gateway_leitura WITH PASSWORD = '...';
   USE [BD_DO_HOST];
   CREATE USER gateway_leitura FOR LOGIN gateway_leitura;
   ALTER ROLE db_datareader ADD MEMBER gateway_leitura;
   ```
2. Sessão em modo leitura no SGBD, quando existe (PostgreSQL, MySQL/MariaDB, SQLite;
   `ApplicationIntent=ReadOnly` em SQL Server).
3. Guarda de SQL (`host_connector/sql_guard.py`): só `SELECT`/`WITH`, uma instrução, sem
   `INSERT/UPDATE/DELETE/MERGE/DROP/ALTER/EXEC/SELECT INTO/...`.
4. Nunca é feito commit: todas as ligações terminam em rollback.

`host_check` avisa se o utilizador configurado tiver permissões de escrita.

## Comandos

```powershell
# Fase 5: testa a ligação, mostra as proteções ativas e verifica permissões
.venv\Scripts\python manage.py host_check

# Fase 4: estrutura completa (schemas, tabelas, vistas, colunas, tipos, PK, FK,
# índices, estimativa de linhas) em reports\host_schema_*.json e *.md
.venv\Scripts\python manage.py host_inspect
.venv\Scripts\python manage.py host_inspect --schema dbo --no-row-counts

# Qualquer comando acima aceita --connection NOME (variáveis HOST_NOME_DB_*)
# ou --company NIF [--source CODIGO] (usa a origem do tipo base de dados da empresa)
.venv\Scripts\python manage.py host_check --company 5000000000 --source HOST

# Chave de API (mostrada uma única vez; só o hash fica guardado). Também no painel.
.venv\Scripts\python manage.py create_api_key --company 5000000000 --name "ERP" --source ERP

# Tarefas periódicas (Agendador de Tarefas do Windows ou cron), ex.: a cada 5 minutos
.venv\Scripts\python manage.py sync_sources         # lê documentos novos das origens de BD
.venv\Scripts\python manage.py process_agt_queue    # envia a fila à AGT

# Apoio à Fase 6: ver algumas linhas reais de uma tabela para confirmar o mapeamento
.venv\Scripts\python manage.py host_sample dbo.NomeDaTabela --limit 3 --order-by Coluna --desc
```

O relatório inclui uma secção de **candidatos** (cabeçalho, linhas, cliente, artigos,
impostos, pagamentos, séries, anulações), obtida por palavras-chave nos nomes. É apenas
uma sugestão: o mapeamento definitivo tem de ser confirmado com dados reais.

Os relatórios têm a estrutura interna do cliente: ficam em `reports/`, que não entra no Git.

## Estrutura

```
config/            settings, .env, BD do Gateway, mascaramento de segredos
host_connector/    ligação só de leitura a qualquer SGBD, guarda de SQL, diagnóstico
companies/         empresas, acessos por papel, chaves de API
sources/           origens (BD ou API), mapeamento configurável, sincronização
invoices/          formato canónico, importação sem duplicados, validação
agt/               configuração AGT por empresa, cliente (simulação), fila de envio
api/               API REST v1 para sistemas externos
dashboard/         painel web
audit/             logs de integração e eventos de auditoria (mascarados)
docs/              formato canónico, origens, API
```
