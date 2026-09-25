# Origens de documentos

Uma empresa pode receber documentos de vários sistemas ao mesmo tempo (HOST Hotel
Systems, outro ERP, POS, ...). Cada sistema é uma **origem**, configurada no painel em
*Origens*. O código da origem (ex.: `HOST`, `ERP-LOJA1`) fica gravado em cada documento
e não pode mudar depois de haver documentos importados.

Todas as origens passam pelo mesmo importador: formato canónico
([formato_canonico.md](formato_canonico.md)), sem duplicados, validação, fila AGT.

## Tipo API: o sistema envia

1. Em *Ligações*, escolher **Ligar por API**.
2. Na página da ligação, criar uma chave (é mostrada uma única vez) e copiar o endereço e os exemplos.
3. O sistema envia os documentos para `POST /api/v1/documents/`. Ver [api.md](api.md).

## Tipo base de dados: o Gateway lê

O Gateway liga-se **só em leitura** à BD do sistema, de qualquer SGBD (SQL Server,
PostgreSQL, MySQL/MariaDB, Oracle, SQLite, ou outro com dialeto SQLAlchemy via URL).
Tudo se faz no painel, em *Ligações*:

1. **Ligar base de dados**: escolher o sistema (SQL Server, ...), servidor, porta, base
   de dados, utilizador e senha. A senha fica **cifrada** na base do Gateway e nunca é
   mostrada. Usar *Testar ligação* antes de guardar. Alternativa: guardar as credenciais
   no `.env` (`HOST_DB_*` ou `HOST_<NOME>_DB_*`) e escolher "Variáveis do ficheiro .env".
2. **Explorar estrutura**: ver tabelas, colunas e algumas linhas reais.
3. **Assistente de mapeamento**: escolher a tabela dos documentos e a das linhas e, em
   listas com as colunas reais, indicar onde está cada campo. As consultas SQL são
   geradas automaticamente (nomes validados contra a estrutura real e citados pelo
   próprio SGBD). *Pré-visualizar* mostra os 3 primeiros documentos sem importar nada.
4. **Sincronizar agora**, e agendar `python manage.py sync_sources` (Agendador de
   Tarefas do Windows ou cron).

Nada está pré-configurado para nenhum sistema. O mapeamento do HOST é feito na Fase 6,
a partir da estrutura real. Para casos especiais há o editor de mapeamento em JSON.

### Mapeamento

```json
{
  "documents_query": "SELECT ... FROM <tabela de cabeçalhos> WHERE <coluna> > :cursor ORDER BY <coluna>",
  "lines_query": "SELECT ... FROM <tabela de linhas> WHERE <coluna do documento> = :document_id",
  "cursor_column": "<coluna que cresce a cada documento novo>",
  "cursor_type": "int",
  "initial_cursor": "0",
  "batch_size": 200,
  "fields": {
    "source_document_id": "<coluna>", "document_type": "<coluna>", "series": "<coluna>",
    "document_number": "<coluna>", "document_date": "<coluna>", "customer_name": "<coluna>",
    "customer_nif": "<coluna>", "subtotal": "<coluna>", "tax_amount": "<coluna>", "total": "<coluna>"
  },
  "line_fields": {
    "line_number": "<coluna>", "product_code": "<coluna>", "description": "<coluna>",
    "quantity": "<coluna>", "unit_price": "<coluna>", "discount": "<coluna>",
    "tax_rate": "<coluna>", "tax_amount": "<coluna>", "tax_exemption_code": "<coluna>", "total": "<coluna>"
  },
  "defaults": { "currency": "AOA" },
  "line_defaults": {}
}
```

- `fields` / `line_fields`: campo canónico → nome da coluna no resultado da consulta.
  Maiúsculas e minúsculas não contam (o Oracle devolve nomes em maiúsculas).
- `defaults` / `line_defaults`: valor fixo quando o sistema não tem essa coluna.
- `source_document_id` tem de vir de uma coluna: é o valor passado em `:document_id`.
- `cursor_type`: `int`, `str` ou `datetime` (ISO). O cursor só avança depois de o
  documento ficar gravado.
- As consultas são verificadas: só `SELECT`/`WITH`, uma instrução, sem escrita, e têm de
  usar `:cursor` / `:document_id` (parâmetros, nunca concatenação).

### Comportamento em caso de problemas

| Situação | O que acontece |
|---|---|
| Documento com erros de negócio (totais, isenção, ...) | Importado com estado *Erro*; o cursor avança. Corrigido na origem, é atualizado na próxima leitura, se ainda não tiver sido comunicado. |
| Documento ilegível (coluna em falta, sem linhas, data inválida) | A sincronização **para** nesse documento e o cursor não o ultrapassa. Nenhum documento é saltado em silêncio. |
| Documento já comunicado à AGT e alterado na origem | Não é alterado; fica registado como conflito. |
| Mesmo número fiscal vindo de outra origem | Recusado como conflito. |
| Duas sincronizações ao mesmo tempo | A segunda é recusada (bloqueio de 15 minutos, libertado no fim). |
