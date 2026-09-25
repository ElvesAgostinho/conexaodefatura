# Formato canónico de fatura

O Gateway converte cada documento de origem (HOST, ou outro sistema no futuro) para um
modelo interno único (`invoices.Invoice` + `invoices.InvoiceItem`). O resto do Gateway
(validação, fila, envio à AGT, painel) só conhece este formato, nunca a estrutura do HOST.

O mapeamento HOST → formato canónico é feito na Fase 6, **com base na estrutura real** do
HOST. Nada aqui assume nomes de tabelas ou colunas do HOST.

## Identificação e duplicados

| Regra | Restrição na BD |
|---|---|
| A mesma origem nunca é importada duas vezes | única: `company` + `source_system` + `source_document_id` |
| O mesmo documento fiscal nunca existe duas vezes | única: `company` + `document_type` + `series` + `document_number` |

Empresas diferentes podem ter os mesmos números. `source_data` guarda os dados recebidos
da origem para auditoria.

## Valores

```
InvoiceItem.total      = quantity * unit_price - discount   (sem imposto)
InvoiceItem.tax_amount = imposto da linha
Invoice.subtotal       = soma de InvoiceItem.total
Invoice.tax_amount     = soma de InvoiceItem.tax_amount
Invoice.total          = subtotal + tax_amount
```

- Valores monetários: 15 dígitos, 2 casas decimais (até 9 999 999 999 999,99).
- Quantidade e preço unitário: 15 dígitos, 4 casas decimais.
- Taxa de imposto em percentagem (ex.: `14.0000`). Linhas isentas têm `tax_exemption_code`.
- O limite de 15 dígitos garante valores exatos em todos os SGBD suportados (o SQLite
  guarda decimais como vírgula flutuante de 64 bits).
- Moeda por omissão: `AOA`.

## Estados

`IMPORTED → VALIDATED → QUEUED → SENDING → SENT/CONFIRMED`, com `REJECTED` (recusada pela
AGT) e `ERROR` (falha técnica). Agrupamentos do painel em `invoices.models.STATUS_GROUPS`:
pendentes, enviadas, rejeitadas, erros. Cada estado pertence a exatamente um grupo.
