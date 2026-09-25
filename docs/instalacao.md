# Instalar o Gateway Fiscal no cliente

## O que é preciso

- Windows 10/11 ou Windows Server 2016+.
- **Python 3.11 ou mais recente** (https://www.python.org/downloads/ — marcar
  "Add python.exe to PATH"). Para o SQL Server, o **ODBC Driver 18 for SQL Server**.
- Cerca de 10 minutos.

## Instalar (3 passos)

1. Copie a pasta do Gateway para o computador do cliente (por exemplo, para o servidor
   onde está o SQL Server).
2. Abra a pasta `install` e faça **duplo clique em `Instalar Gateway Fiscal.cmd`**. O
   Windows pede autorização de administrador (para o Gateway arrancar com o Windows).
3. Responda a três perguntas: nome da empresa, NIF e senha do administrador.

No fim, o painel abre sozinho na **Configuração automática**:

1. O cliente **autoriza** a procura de bases de dados (neste computador e, se quiser, na
   rede local).
2. Escolhe o servidor encontrado e **autoriza** a entrada (conta do Windows ou um
   administrador do SQL Server — usado só naquele momento, nunca guardado).
3. Escolhe a base de dados (as que parecem ter faturas aparecem primeiro).
4. **Aprova** a criação de um utilizador só de leitura — o Gateway mostra os comandos
   exatos antes de os executar e verifica depois que o utilizador não consegue escrever.
5. Confirma o mapeamento sugerido com **Pré-visualizar** e guarda.

A partir daí o Gateway lê as faturas novas a cada 5 minutos e trata da fila de envio.
Todas as autorizações ficam registadas em *Auditoria*.

## O que o instalador faz

| Passo | Detalhe |
|---|---|
| Pasta | `C:\GatewayFiscal` (mude com `-Pasta`) |
| Ambiente Python | `.venv` na pasta de instalação, com as dependências |
| Configuração | `.env` com chaves secretas novas (**faça cópia de segurança**: sem a `GATEWAY_ENCRYPTION_KEY` as senhas guardadas não podem ser lidas) |
| Dados | `dados\gateway.sqlite3` |
| Registo | `logs\gateway.log` (rotativo, sem senhas) |
| Arranque | Tarefa do Windows "Gateway Fiscal": com administrador arranca com o Windows (conta SYSTEM); sem administrador, quando o utilizador inicia sessão. Reinicia sozinha se parar. |
| Painel | `http://localhost:8000/` (mude com `-Porta`) |

Opções: `-Porta 8080`, `-Pasta D:\Gateway`, `-AbrirFirewall` (permite o acesso a partir de
outros computadores da rede privada), `-Empresa`, `-Nif`, `-Senha` (instalação sem perguntas).

## Atualizar

Copie a versão nova e corra o instalador outra vez. O `.env`, os dados e a empresa são
mantidos; só o programa é atualizado e a tarefa reiniciada.

## Sem internet no cliente

Num computador com internet e a **mesma versão do Python** do cliente, corra
`install\preparar_offline.ps1`. Copie a pasta completa (com `install\wheels`) para o
cliente: o instalador usa esses pacotes em vez da internet.

## Desinstalar

`install\desinstalar.ps1` remove o arranque automático e mantém os dados.
`install\desinstalar.ps1 -ApagarDados` apaga também a pasta.

## Conta do Windows e SQL Server

Com a tarefa a correr como **SYSTEM**, a opção "Conta do Windows" da configuração
automática entra no SQL Server como `NT AUTHORITY\SYSTEM` (o ecrã mostra a conta exata).
Se essa conta não for administradora do SQL Server, use um utilizador e senha de
administrador — são usados só durante a configuração.

## Com HTTPS

Numa rede interna o painel funciona em `http://servidor:8000`. Para aceder de fora da
empresa, coloque-o atrás de um proxy HTTPS (IIS, nginx) e mude no `.env`:
`DJANGO_HTTPS=True` e `DJANGO_BEHIND_PROXY=True`.
