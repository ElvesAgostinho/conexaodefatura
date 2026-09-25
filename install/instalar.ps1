<#
.SYNOPSIS
  Instala (ou atualiza) o Gateway Fiscal neste computador.

.DESCRIPTION
  1. Verifica o Python (3.11 ou mais recente).
  2. Copia o Gateway para a pasta de instalação (por omissão C:\GatewayFiscal).
  3. Cria o ambiente Python e instala as dependências (sem internet, se existir install\wheels).
  4. Gera o .env com chaves secretas novas (numa atualização, o .env existente é mantido).
  5. Prepara a base de dados do Gateway e cria a empresa e o administrador.
  6. Regista o arranque automático (tarefa do Windows) e abre o painel na
     configuração automática, onde o cliente autoriza a deteção da base de dados.

.EXAMPLE
  .\instalar.ps1
  .\instalar.ps1 -Pasta D:\GatewayFiscal -Porta 8080 -Empresa "Hotel X, Lda" -Nif 5000000000
#>
[CmdletBinding()]
param(
    [string]$Pasta = "C:\GatewayFiscal",
    [int]$Porta = 8000,
    [string]$Empresa,
    [string]$Nif,
    [string]$Utilizador = "admin",
    [SecureString]$Senha,
    [switch]$SemTarefa,
    [switch]$AbrirFirewall,
    [switch]$NaoAbrirNavegador,
    [string]$Python
)

$ErrorActionPreference = "Stop"
$Origem = Split-Path -Parent $PSScriptRoot
$NomeTarefa = "Gateway Fiscal"

function Passo($texto) { Write-Host ""; Write-Host "==> $texto" -ForegroundColor Cyan }
function Falhar($texto) { Write-Host ""; Write-Host "ERRO: $texto" -ForegroundColor Red; exit 1 }
function EAdministrador {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Executar($exe, [string[]]$argumentos, $mensagem) {
    & $exe @argumentos
    if ($LASTEXITCODE -ne 0) { Falhar "$mensagem (código $LASTEXITCODE)" }
}

Write-Host "Gateway Fiscal - instalação" -ForegroundColor White
if ($Porta -lt 1 -or $Porta -gt 65535) { Falhar "Porta inválida: $Porta" }

# --------------------------------------------------------------- 1. Python
Passo "A verificar o Python"
$candidatos = @()
if ($Python) { $candidatos += ,@($Python) }
$candidatos += ,@("py", "-3")
$candidatos += ,@("python")
$pyExe = $null; $pyArgs = @()
foreach ($c in $candidatos) {
    try {
        $versao = & $c[0] @($c[1..($c.Length)] | Where-Object { $_ }) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $versao) {
            $partes = $versao.Trim().Split(".")
            if ([int]$partes[0] -eq 3 -and [int]$partes[1] -ge 11) {
                $pyExe = $c[0]; $pyArgs = @($c[1..($c.Length)] | Where-Object { $_ }); break
            }
        }
    } catch { }
}
if (-not $pyExe) {
    Falhar "Não foi encontrado o Python 3.11 ou mais recente. Instale-o de https://www.python.org/downloads/ (marque 'Add python.exe to PATH') e volte a correr este instalador."
}
Write-Host "Python $versao encontrado ($pyExe)."

# ------------------------------------------------------------ 2. ficheiros
Passo "A copiar o Gateway para $Pasta"
$atualizacao = Test-Path (Join-Path $Pasta ".env")
New-Item -ItemType Directory -Force -Path $Pasta | Out-Null
$exclusoesPastas = @(".venv", ".git", "__pycache__", "staticfiles", "logs", "dados", "reports", "wheels", "node_modules")
$exclusoesFicheiros = @(".env", "*.sqlite3", "*.sqlite3-wal", "*.sqlite3-shm", "*.pyc", "*.log")
$robo = @($Origem, $Pasta, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/XD") + $exclusoesPastas + @("/XF") + $exclusoesFicheiros
& robocopy @robo | Out-Null
if ($LASTEXITCODE -ge 8) { Falhar "A cópia dos ficheiros falhou (robocopy $LASTEXITCODE)." }
if ($atualizacao) { Write-Host "Instalação existente encontrada: a atualizar (dados e .env mantidos)." }

# ------------------------------------------------------- 3. ambiente Python
Passo "A preparar o ambiente Python (pode demorar alguns minutos)"
$venvPython = Join-Path $Pasta ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Executar $pyExe ($pyArgs + @("-m", "venv", (Join-Path $Pasta ".venv"))) "Não foi possível criar o ambiente Python"
}
$wheels = Join-Path $PSScriptRoot "wheels"
$pipArgs = @("-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", (Join-Path $Pasta "requirements.txt"))
if ((Test-Path $wheels) -and (Get-ChildItem $wheels -Filter *.whl -ErrorAction SilentlyContinue)) {
    Write-Host "A instalar sem internet (pacotes em install\wheels)."
    $pipArgs += @("--no-index", "--find-links", $wheels)
}
Executar $venvPython $pipArgs "Não foi possível instalar as dependências"

# ------------------------------------------------------------------ 4. .env
Passo "A gerar a configuração"
$dados = Join-Path $Pasta "dados"
Executar $venvPython @((Join-Path $Pasta "install\gerar_env.py"), "--saida", (Join-Path $Pasta ".env"), "--dados", $dados, "--porta", "$Porta") "Não foi possível gerar o .env"

# ----------------------------------------------------- 5. base de dados, admin
Passo "A preparar a base de dados do Gateway"
Push-Location $Pasta
try {
    Executar $venvPython @("manage.py", "migrate", "--noinput", "-v", "0") "Falha ao preparar a base de dados"
    Executar $venvPython @("manage.py", "collectstatic", "--noinput", "-v", "0") "Falha ao preparar os ficheiros do painel"

    if (-not $atualizacao) {
        Passo "Empresa e administrador"
        if (-not $Empresa) { $Empresa = Read-Host "Nome da empresa" }
        if (-not $Nif) { $Nif = Read-Host "NIF da empresa" }
        if (-not $Senha) { $Senha = Read-Host "Senha do administrador '$Utilizador' (mínimo 10 caracteres)" -AsSecureString }
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Senha)
        try {
            $env:GATEWAY_ADMIN_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
            Executar $venvPython @("manage.py", "gateway_bootstrap", "--empresa", $Empresa, "--nif", $Nif, "--utilizador", $Utilizador) "Falha ao criar a empresa e o administrador"
        } finally {
            Remove-Item Env:GATEWAY_ADMIN_PASSWORD -ErrorAction SilentlyContinue
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    }
} finally { Pop-Location }

# ------------------------------------------------------ 6. arranque automático
$pythonw = Join-Path $Pasta ".venv\Scripts\pythonw.exe"
if (-not $SemTarefa) {
    Passo "A registar o arranque automático e a aguardar o Gateway"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Pasta "install\tarefa.ps1") -Pasta $Pasta -Python $pythonw -Porta $Porta
    if ($LASTEXITCODE -ne 0) { Falhar "O Gateway não arrancou. Veja $Pasta\logs\gateway.log" }
}

if ($AbrirFirewall) {
    if (EAdministrador) {
        Passo "A permitir a porta $Porta na firewall (rede privada)"
        Get-NetFirewallRule -DisplayName "Gateway Fiscal" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        New-NetFirewallRule -DisplayName "Gateway Fiscal" -Direction Inbound -Protocol TCP -LocalPort $Porta `
            -Action Allow -Profile Private,Domain | Out-Null
    } else { Write-Host "Aviso: para abrir a firewall execute o instalador como administrador." -ForegroundColor Yellow }
}

# ---------------------------------------------------------------- pronto
$url = "http://localhost:$Porta/configurar/"
Write-Host ""
Write-Host "Gateway Fiscal instalado em $Pasta" -ForegroundColor Green
Write-Host "Painel: $url"
if (-not $SemTarefa -and -not $NaoAbrirNavegador) { Start-Process $url }
