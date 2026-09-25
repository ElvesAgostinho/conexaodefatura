<#
.SYNOPSIS
  Prepara a instalação SEM INTERNET: descarrega todos os pacotes Python para install\wheels.

.DESCRIPTION
  Corra num computador com internet e com a MESMA versão do Python que o cliente vai usar
  (os pacotes compilados dependem da versão). Depois copie a pasta completa do Gateway
  (incluindo install\wheels) para o servidor do cliente e corra o instalador lá.
#>
[CmdletBinding()]
param([string]$Python = "py")
$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent $PSScriptRoot
$destino = Join-Path $PSScriptRoot "wheels"
New-Item -ItemType Directory -Force -Path $destino | Out-Null
& $Python -m pip download --disable-pip-version-check -q -r (Join-Path $raiz "requirements.txt") -d $destino
if ($LASTEXITCODE -ne 0) { Write-Host "ERRO: não foi possível descarregar os pacotes." -ForegroundColor Red; exit 1 }
$versao = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])"
Set-Content -Path (Join-Path $destino "LEIA-ME.txt") -Value "Pacotes para Python $versao (Windows 64 bits). O cliente tem de usar esta versão do Python." -Encoding UTF8
Write-Host "Pacotes guardados em $destino (Python $versao): $((Get-ChildItem $destino -Filter *.whl).Count) ficheiros."
