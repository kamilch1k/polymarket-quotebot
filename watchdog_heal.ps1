# Self-heal for the quotebot paper run: relaunch if the dashboard stops serving.
# Registration (once):
#   schtasks /Create /TN QuotebotWatchdog /SC MINUTE /MO 10 /F /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\cc\quotebot\watchdog_heal.ps1"
# Kills by COMMAND LINE match so it can never touch the copybot's process.
$ErrorActionPreference = "SilentlyContinue"
try { $r = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8778/ -TimeoutSec 10 } catch { $r = $null }
if ($r -and $r.StatusCode -eq 200) { exit 0 }

Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -match 'quotebot\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep 2
Start-Process -FilePath "C:\Users\rewwe\AppData\Local\Programs\Python\Python312\pythonw.exe" `
    -ArgumentList "`"$PSScriptRoot\quotebot.py`" --headless" -WorkingDirectory $PSScriptRoot
Add-Content "$PSScriptRoot\watchdog_heal.log" "$(Get-Date -Format s) dashboard was down - relaunched"
