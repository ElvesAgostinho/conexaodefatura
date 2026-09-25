<#
.SYNOPSIS
  Regista e arranca a tarefa do Windows "Gateway Fiscal" e espera até o painel responder.
  Usado pelo instalador .exe e por instalar.ps1.

.PARAMETER Python
  pythonw.exe a usar (runtime incluído no .exe, ou .venv\Scripts\pythonw.exe).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Pasta,
    [Parameter(Mandatory = $true)][string]$Python,
    [int]$Porta = 8000,
    [int]$EsperaSegundos = 90
)
$ErrorActionPreference = "Stop"
$NomeTarefa = "Gateway Fiscal"

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$admin = ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

$existente = Get-ScheduledTask -TaskName $NomeTarefa -ErrorAction SilentlyContinue
if ($existente) {
    Stop-ScheduledTask -TaskName $NomeTarefa -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $NomeTarefa -Confirm:$false
}

$acao = New-ScheduledTaskAction -Execute $Python -Argument "manage.py run_gateway --host * --port $Porta" -WorkingDirectory $Pasta
$definicoes = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -StartWhenAvailable -MultipleInstances IgnoreNew
if ($admin) {
    $gatilho = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Write-Host "Arranque automático: com o Windows (conta SYSTEM)."
} else {
    $gatilho = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
    Write-Host "Arranque automático: quando $env:USERNAME inicia sessão (sem privilégios de administrador)."
}
Register-ScheduledTask -TaskName $NomeTarefa -Action $acao -Trigger $gatilho -Settings $definicoes `
    -Principal $principal -Description "Gateway Fiscal: painel, API e envio à AGT (porta $Porta)" | Out-Null
Start-ScheduledTask -TaskName $NomeTarefa

for ($i = 0; $i -lt $EsperaSegundos; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Porta/entrar/" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { Write-Host "Gateway a responder na porta $Porta."; exit 0 }
    } catch { }
    Start-Sleep -Seconds 1
}
Write-Host "O Gateway não respondeu na porta $Porta. Veja $Pasta\logs\gateway.log" -ForegroundColor Red
exit 1
