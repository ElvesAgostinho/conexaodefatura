<#
.SYNOPSIS
  Para o Gateway desta pasta (antes de uma atualização, para os ficheiros não estarem em uso).
#>
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Pasta)
$ErrorActionPreference = "SilentlyContinue"
Stop-ScheduledTask -TaskName "Gateway Fiscal" -ErrorAction SilentlyContinue
$alvo = [IO.Path]::GetFullPath($Pasta)
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*run_gateway*" -and $_.ExecutablePath -like "$alvo*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
exit 0
