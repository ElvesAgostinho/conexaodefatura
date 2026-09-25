# Instalar o Gateway Fiscal no cliente

## O que é preciso

- Windows 10/11 ou Windows Server 2016+ (64 bits).
- Para ligar a SQL Server: o **ODBC Driver 18 for SQL Server** da Microsoft (gratuito). O
  instalador avisa se faltar.
- Cerca de 5 minutos. **Não é preciso instalar Python**: vem incluído no instalador.

## Instalar (recomendado: instalador .exe)

1. Copie `GatewayFiscal-Setup-<versão>.exe` para o computador do cliente (por exemplo, o
   servidor onde está o SQL Server).
2. Duplo clique. Responda: pasta (por omissão `C:\GatewayFiscal`), nome da empresa, NIF,
   utilizador e senha do administrador, porta do painel. Opcional: permitir o acesso a
   partir de outros computadores da rede (firewall).
3. No fim, o painel abre na **Configuração automática**:
   1. o cliente **autoriza** a procura de bases de dados (neste computador e, se quiser, na rede local);
   2. escolhe o servidor e **autoriza** a entrada (conta do Windows ou um administrador do
      SQL Server — usado só naquele momento, nunca guardado);
   3. escolhe a base de dados (as que parecem ter faturas aparecem primeiro);
   4. **aprova** a criação do utilizador só de leitura (o Gateway mostra os comandos exatos e
      verifica depois que o utilizador não consegue escrever);
   5. confirma o mapeamento sugerido com **Pré-visualizar** e guarda.

A partir daí o Gateway lê as faturas novas a cada 5 minutos e trata da fila de envio. Se a
internet falhar, as faturas ficam à espera e são enviadas quando a ligação voltar.

### Instalação sem perguntas (técnicos, instalação em massa)

```bat
set GATEWAY_ADMIN_PASSWORD=Senha-Forte-Do-Admin
GatewayFiscal-Setup-1.0.0.exe /VERYSILENT /EMPRESA="Hotel X, Lda" /NIF=5000000000 /PORTA=8000
```

A senha vai na variável de ambiente e **não** na linha de comando (o Inno Setup regista a
linha de comando no registo da instalação). Outras opções: `/DIR=C:\GatewayFiscal`,
`/UTILIZADOR=admin`, `/LOG=instalacao.log`, `/CURRENTUSER` (sem administrador: arranca
quando o utilizador inicia sessão).

**Atualizar:** corra o `.exe` da versão nova. Empresa, dados e `.env` mantêm-se.
**Desinstalar:** Painel de Controlo › Programas, ou `unins000.exe` na pasta. Os dados
(`dados\`, `.env`, `logs\`) ficam na pasta.

### Construir o instalador (equipa do Gateway)

`install\construir_instalador.ps1` descarrega o Python oficial da mesma versão, instala as
dependências dentro dele, testa-o e compila `dist\GatewayFiscal-Setup-<versão>.exe` com o
Inno Setup. A versão está em `config/version.py`.

## Alternativa: instalar com PowerShell (Python já instalado)

Duplo clique em `install\Instalar Gateway Fiscal.cmd` (precisa de Python 3.11+).

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
