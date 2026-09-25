<#
.SYNOPSIS
  Constrói dist\GatewayFiscal-Setup-<versão>.exe (Python incluído: o cliente não instala nada antes).

.DESCRIPTION
  1. Descarrega o Python "embeddable" oficial (python.org) da MESMA versão do Python usado
     aqui (os pacotes compilados dependem da versão) e guarda-o em build\cache.
  2. Instala as dependências dentro desse Python (build\runtime\Lib\site-packages).
  3. Copia o programa para build\app (sem ficheiros de desenvolvimento, dados ou segredos).
  4. Testa o Python incluído (importa as dependências e corre o "check" do Django).
  5. Compila o instalador com o Inno Setup (ISCC.exe).
#>
[CmdletBinding()]
param(
    [string]$Python,
    [string]$Iscc
)
$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $raiz ".venv\Scripts\python.exe" }
if (-not $Iscc) {
    $Iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
              "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc -or -not (Test-Path $Iscc)) { throw "Inno Setup (ISCC.exe) não encontrado." }

function Passo($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Verificar($mensagem) { if ($LASTEXITCODE -ne 0) { throw "$mensagem (código $LASTEXITCODE)" } }

$build = Join-Path $raiz "build"; $cache = Join-Path $build "cache"
$app = Join-Path $build "app"; $runtime = Join-Path $app "runtime"  # mesma estrutura da instalação
New-Item -ItemType Directory -Force -Path $cache | Out-Null

Push-Location $raiz
try { $versao = (& $Python -c "from config.version import VERSION; print(VERSION)").Trim() } finally { Pop-Location }
$pyver = (& $Python -c "import platform; print(platform.python_version())").Trim()
$curto = (& $Python -c "import sys; print('%d%d' % sys.version_info[:2])").Trim()
Write-Host "Gateway Fiscal $versao | Python $pyver"

Passo "Python embeddable $pyver"
$zip = Join-Path $cache "python-$pyver-embed-amd64.zip"
if (-not (Test-Path $zip)) {
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$pyver/python-$pyver-embed-amd64.zip" -OutFile $zip -UseBasicParsing
}
Write-Host "SHA256: $((Get-FileHash $zip -Algorithm SHA256).Hash)"
if (Test-Path $app) { Remove-Item -Recurse -Force $app }

Passo "Programa"
$robo = @($raiz, $app, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
          "/XD", ".venv", ".git", "build", "dist", "wheels", "__pycache__", "staticfiles", "logs", "dados", "reports", ".claude",
          "/XF", ".env", "*.sqlite3", "*.sqlite3-wal", "*.sqlite3-shm", "*.pyc", "*.log")
& robocopy @robo | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Falha ao copiar o programa (robocopy $LASTEXITCODE)" }

Passo "Python incluído"
Expand-Archive -Path $zip -DestinationPath $runtime
# O ficheiro ._pth define o sys.path do Python incluído: pacotes e a pasta do programa.
Set-Content -Path (Join-Path $runtime "python$curto._pth") -Encoding ASCII -Value @(
    "python$curto.zip", ".", "Lib\site-packages", "..", "import site")

Passo "Dependências"
# --no-compile: sem caches .pyc no pacote (são os caminhos mais longos; o Windows limita a 260).
& $Python -m pip install --disable-pip-version-check -q --no-compile --only-binary=:all: --target (Join-Path $runtime "Lib\site-packages") -r (Join-Path $raiz "requirements.txt")
Verificar "Falha ao instalar as dependências"

Passo "Teste do Python incluído"
$pyInc = Join-Path $runtime "python.exe"
& $pyInc -c "import django, sqlalchemy, waitress, whitenoise, cryptography, pyodbc, psycopg, pymysql, oracledb, rest_framework, mssql, tzdata, sqlite3; print('dependências OK')"
Verificar "O Python incluído não carrega as dependências"
$env:DJANGO_SECRET_KEY = "construcao-" + [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
Push-Location $app
try { & $pyInc manage.py check; Verificar "manage.py check falhou com o Python incluído" } finally {
    Pop-Location; Remove-Item Env:DJANGO_SECRET_KEY -ErrorAction SilentlyContinue }

Passo "Instalador"
Get-ChildItem $app -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
$maxRel = (Get-ChildItem $app -Recurse -File | ForEach-Object { $_.FullName.Length - $app.Length } | Measure-Object -Maximum).Maximum
Write-Host "Caminho relativo mais longo: $maxRel caracteres (a pasta de instalação pode ter até $(250 - $maxRel))."
& $Iscc "/DAppVersion=$versao" "/DMaxRelPath=$maxRel" "/Qp" (Join-Path $PSScriptRoot "gateway.iss")
Verificar "O Inno Setup falhou"
$exe = Join-Path $raiz "dist\GatewayFiscal-Setup-$versao.exe"
$info = Get-Item $exe
Write-Host ""
Write-Host "Instalador: $exe" -ForegroundColor Green
Write-Host ("Tamanho: {0:N1} MB | SHA256: {1}" -f ($info.Length / 1MB), (Get-FileHash $exe -Algorithm SHA256).Hash)
