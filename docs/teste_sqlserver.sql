-- Base de TESTE do Gateway Fiscal (dados fictícios). Não representa o HOST nem outro sistema real.
SET NOCOUNT ON;
IF DB_ID('GatewayTeste') IS NULL CREATE DATABASE GatewayTeste;
GO
USE GatewayTeste;
GO
IF OBJECT_ID('dbo.DocumentoLinhas') IS NOT NULL DROP TABLE dbo.DocumentoLinhas;
IF OBJECT_ID('dbo.Documentos') IS NOT NULL DROP TABLE dbo.Documentos;
GO
CREATE TABLE dbo.Documentos (
    Id              int IDENTITY(1,1) PRIMARY KEY,
    TipoDoc         varchar(5)     NOT NULL,
    Serie           varchar(20)    NOT NULL,
    NumDoc          varchar(40)    NOT NULL,
    DataDoc         date           NOT NULL,
    ClienteNome     nvarchar(150)  NOT NULL,
    ClienteNIF      varchar(20)    NULL,
    Moeda           char(3)        NOT NULL DEFAULT 'AOA',
    TotalLiquido    decimal(15,2)  NOT NULL,
    TotalIVA        decimal(15,2)  NOT NULL,
    TotalDocumento  decimal(15,2)  NOT NULL,
    [Obs Interna]   nvarchar(200)  NULL,
    DataCriacao     datetime2(0)   NOT NULL DEFAULT SYSDATETIME(),
    CONSTRAINT UQ_Documentos UNIQUE (TipoDoc, Serie, NumDoc)
);
CREATE TABLE dbo.DocumentoLinhas (
    Id              int IDENTITY(1,1) PRIMARY KEY,
    DocumentoId     int            NOT NULL REFERENCES dbo.Documentos(Id),
    NumLinha        int            NOT NULL,
    CodArtigo       varchar(30)    NULL,
    Descricao       nvarchar(300)  NOT NULL,
    Quantidade      decimal(15,4)  NOT NULL,
    PrecoUnitario   decimal(15,4)  NOT NULL,
    Desconto        decimal(15,2)  NOT NULL DEFAULT 0,
    TaxaIVA         decimal(7,4)   NOT NULL,
    ValorIVA        decimal(15,2)  NOT NULL,
    TotalLinha      decimal(15,2)  NOT NULL,
    CodIsencao      varchar(10)    NULL
);
GO
DECLARE @d int;
-- 1: fatura simples
INSERT dbo.Documentos (TipoDoc, Serie, NumDoc, DataDoc, ClienteNome, ClienteNIF, TotalLiquido, TotalIVA, TotalDocumento)
VALUES ('FT', 'FT2026', 'FT FT2026/1', DATEADD(day, -9, CAST(GETDATE() AS date)), N'Empresa Alfa, Lda', '5000000101', 30000.00, 4200.00, 34200.00);
SET @d = SCOPE_IDENTITY();
INSERT dbo.DocumentoLinhas (DocumentoId, NumLinha, CodArtigo, Descricao, Quantidade, PrecoUnitario, TaxaIVA, ValorIVA, TotalLinha)
VALUES (@d, 1, 'PC01', N'Computador portátil', 2, 15000, 14, 4200.00, 30000.00);
-- 2: fatura com desconto e duas linhas
INSERT dbo.Documentos (TipoDoc, Serie, NumDoc, DataDoc, ClienteNome, ClienteNIF, TotalLiquido, TotalIVA, TotalDocumento)
VALUES ('FT', 'FT2026', 'FT FT2026/2', DATEADD(day, -7, CAST(GETDATE() AS date)), N'Beta Serviços, SA', '5000000102', 12500.00, 1750.00, 14250.00);
SET @d = SCOPE_IDENTITY();
INSERT dbo.DocumentoLinhas (DocumentoId, NumLinha, CodArtigo, Descricao, Quantidade, PrecoUnitario, Desconto, TaxaIVA, ValorIVA, TotalLinha)
VALUES (@d, 1, 'RATO', N'Rato sem fios', 5, 1500, 500.00, 14, 980.00, 7000.00),
       (@d, 2, 'TEC',  N'Teclado', 2, 2750, 0, 14, 770.00, 5500.00);
-- 3: fatura-recibo com linha isenta
INSERT dbo.Documentos (TipoDoc, Serie, NumDoc, DataDoc, ClienteNome, ClienteNIF, TotalLiquido, TotalIVA, TotalDocumento)
VALUES ('FR', 'FR2026', 'FR FR2026/1', DATEADD(day, -5, CAST(GETDATE() AS date)), N'Consumidor final', NULL, 8000.00, 700.00, 8700.00);
SET @d = SCOPE_IDENTITY();
INSERT dbo.DocumentoLinhas (DocumentoId, NumLinha, CodArtigo, Descricao, Quantidade, PrecoUnitario, TaxaIVA, ValorIVA, TotalLinha, CodIsencao)
VALUES (@d, 1, 'SERV', N'Serviço de instalação', 1, 5000, 14, 700.00, 5000.00, NULL),
       (@d, 2, 'LIVRO', N'Manual (isento)', 1, 3000, 0, 0, 3000.00, 'TESTE');
-- 4: fatura com TOTAL ERRADO (para ver a validação)
INSERT dbo.Documentos (TipoDoc, Serie, NumDoc, DataDoc, ClienteNome, ClienteNIF, TotalLiquido, TotalIVA, TotalDocumento, [Obs Interna])
VALUES ('FT', 'FT2026', 'FT FT2026/3', DATEADD(day, -3, CAST(GETDATE() AS date)), N'Gama Tours', '5000000104', 10000.00, 1400.00, 99999.00, N'total errado de propósito');
SET @d = SCOPE_IDENTITY();
INSERT dbo.DocumentoLinhas (DocumentoId, NumLinha, CodArtigo, Descricao, Quantidade, PrecoUnitario, TaxaIVA, ValorIVA, TotalLinha)
VALUES (@d, 1, 'VIAG', N'Pacote de viagem', 1, 10000, 14, 1400.00, 10000.00);
-- 5: nota de crédito
INSERT dbo.Documentos (TipoDoc, Serie, NumDoc, DataDoc, ClienteNome, ClienteNIF, TotalLiquido, TotalIVA, TotalDocumento)
VALUES ('NC', 'NC2026', 'NC NC2026/1', DATEADD(day, -2, CAST(GETDATE() AS date)), N'Empresa Alfa, Lda', '5000000101', 15000.00, 2100.00, 17100.00);
SET @d = SCOPE_IDENTITY();
INSERT dbo.DocumentoLinhas (DocumentoId, NumLinha, CodArtigo, Descricao, Quantidade, PrecoUnitario, TaxaIVA, ValorIVA, TotalLinha)
VALUES (@d, 1, 'PC01', N'Devolução de computador', 1, 15000, 14, 2100.00, 15000.00);
-- 6: fatura com nome e descrição acentuados
INSERT dbo.Documentos (TipoDoc, Serie, NumDoc, DataDoc, ClienteNome, ClienteNIF, TotalLiquido, TotalIVA, TotalDocumento)
VALUES ('FT', 'FT2026', 'FT FT2026/4', DATEADD(day, -1, CAST(GETDATE() AS date)), N'Hóspede João Gonçalves', '5000000106', 45000.00, 6300.00, 51300.00);
SET @d = SCOPE_IDENTITY();
INSERT dbo.DocumentoLinhas (DocumentoId, NumLinha, CodArtigo, Descricao, Quantidade, PrecoUnitario, TaxaIVA, ValorIVA, TotalLinha)
VALUES (@d, 1, 'ALOJ', N'Alojamento — 3 noites', 3, 15000, 14, 6300.00, 45000.00);
GO
IF USER_ID('gateway_leitura') IS NULL CREATE USER [gateway_leitura] FOR LOGIN [gateway_leitura];
ALTER ROLE db_datareader ADD MEMBER [gateway_leitura];
GO
SELECT COUNT(*) AS documentos FROM dbo.Documentos;
SELECT COUNT(*) AS linhas FROM dbo.DocumentoLinhas;
