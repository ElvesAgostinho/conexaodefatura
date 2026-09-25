<#
.SYNOPSIS
  Remove o arranque automático do Gateway Fiscal. Os dados ficam, a menos que use -ApagarDados.
#>
[CmdletBinding()]
param(
    [string]$Pasta = "C:\GatewayFiscal",
    [switch]$ApagarDados
)
$ErrorActionPreference = "Stop"
$NomeTarefa = "Gateway Fiscal"

$tarefa = Get-ScheduledTask -TaskName $NomeTarefa -ErrorAction SilentlyContinue
if ($tarefa) {
    Stop-ScheduledTask -TaskName $NomeTarefa -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $NomeTarefa -Confirm:$false
    Write-Host "Arranque automático removido."
} else { Write-Host "Não havia arranque automático registado." }

# Termina o processo do Gateway desta pasta, se ainda estiver a correr.
$alvo = [IO.Path]::GetFullPath($Pasta)
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*run_gateway*" -and $_.ExecutablePath -like "$alvo*" } |
    ForEach-Object {
        # O lançador do ambiente virtual e o Python verdadeiro terminam juntos: ignora os que já saíram.
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Processo $($_.ProcessId) terminado."
    }

Get-NetFirewallRule -DisplayName "Gateway Fiscal" -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue

if ($ApagarDados) {
    if (Test-Path $alvo) {
        Start-Sleep -Seconds 1
        Remove-Item -Recurse -Force -Path $alvo
        Write-Host "Pasta $alvo apagada."
    }
} else { Write-Host "Dados mantidos em $alvo (use -ApagarDados para os apagar)." }
