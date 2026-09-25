# API do Gateway Fiscal (v1)

Para sistemas de faturação que enviam os seus documentos ao Gateway. O Gateway valida,
evita duplicados e trata da comunicação com a AGT.

## Autenticação

Cada sistema recebe uma chave de API, criada no painel (*Chaves de API*) e ligada a uma
origem. Enviar em todos os pedidos:

```
Authorization: Api-Key gw_xxxxxxxx_yyyyyyyyyyyyyyyy
```

(ou `X-API-Key: gw_...`). Só HTTPS em produção. Uma chave só vê os documentos da sua
origem. Chave em falta, inválida ou revogada: `401`. Demasiados pedidos: `429`
(omissão: 120 por minuto por chave).

## Endpoints

| Método | Caminho | Uso |
|---|---|---|
| GET | `/api/v1/ping/` | Verifica a chave; devolve a empresa e a origem |
| POST | `/api/v1/documents/` | Envia um documento |
| GET | `/api/v1/documents/?status=&page=` | Lista os documentos da origem (100 por página) |
| GET | `/api/v1/documents/<source_document_id>/` | Estado de um documento |

## Enviar um documento

`POST /api/v1/documents/`, `Content-Type: application/json`, no
[formato canónico](formato_canonico.md):

```json
{
  "source_document_id": "1001",
  "document_type": "FT",
  "series": "A2026",
  "document_number": "FT A2026/1",
  "document_date": "2026-01-15",
  "customer_name": "Cliente, Lda",
  "customer_nif": "5000000009",
  "currency": "AOA",
  "subtotal": "150.00",
  "tax_amount": "14.00",
  "total": "164.00",
  "lines": [
    {"line_number": 1, "product_code": "ALOJ", "description": "Alojamento", "quantity": "2",
     "unit_price": "50.00", "discount": "0", "tax_rate": "14", "tax_amount": "14.00", "total": "100.00"},
    {"line_number": 2, "description": "Serviço isento", "quantity": "1", "unit_price": "50",
     "tax_rate": "0", "tax_amount": "0", "tax_exemption_code": "<código de isenção>", "total": "50.00"}
  ]
}
```

- Valores como texto ou número; no máximo 2 casas decimais (4 em quantidade e preço).
  Recomenda-se texto, para evitar arredondamentos de vírgula flutuante.
- `line.total` = quantidade × preço − desconto (sem imposto). `total` = `subtotal` + `tax_amount`.
- Taxa 0% exige `tax_exemption_code`.
- Opcional: `document_hash` (assinatura/hash da fatura feita pelo vosso programa) e `hash_control`
  (versão da chave). O Gateway transporta-os sem alteração.
- Campos não previstos são recusados. O cliente não escolhe a origem nem o estado.

### Respostas

| Código | Significado |
|---|---|
| `201` | Criado (`"result": "created"`) |
| `200` | Já existia igual (`"duplicate"`, idempotente: pode repetir o envio em segurança) ou foi corrigido antes de ser comunicado (`"updated"`) |
| `400` | Estrutura inválida: `{"errors": [...]}`; nada foi gravado |
| `409` | Conflito: o documento já foi comunicado à AGT e os dados mudaram, ou o número fiscal já existe noutra origem |
| `413` | Pedido demasiado grande |

Um documento com a estrutura correta mas com erros de negócio (ex.: totais que não
batem) é gravado com `"status": "ERROR"` e `validation_errors`. Corrija-o e envie de
novo com o mesmo `source_document_id`.

### Estado

`status`: `PENDING`, `IMPORTED`, `VALIDATED`, `QUEUED`, `SENDING`, `SENT`, `CONFIRMED`
(validado pela AGT), `REJECTED`, `ERROR`. O bloco `agt` traz `request_id`, `message`,
`simulated` (`true` em modo simulação: nada foi enviado à AGT), `sent_at` e `confirmed_at`.
