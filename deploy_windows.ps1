param(
    [Parameter(Mandatory=$true)][string]$Archive,
    [string]$InstallRoot='C:\ProgramData\LLMMonitor',
    [string]$BasePython='C:\Users\Administrator\AppData\Local\Programs\Python\Python315\python.exe'
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$appDir=Join-Path $InstallRoot 'app'
New-Item -ItemType Directory -Force -Path $InstallRoot,$appDir,(Join-Path $InstallRoot 'data'),(Join-Path $InstallRoot 'logs')|Out-Null
$existing=Get-ScheduledTask -TaskName 'LLM-Monitor' -ErrorAction SilentlyContinue
if($existing){Stop-ScheduledTask -TaskName 'LLM-Monitor'; Start-Sleep -Seconds 2}
Expand-Archive -LiteralPath $Archive -DestinationPath $appDir -Force
$venv=Join-Path $InstallRoot 'venv'
$python=Join-Path $venv 'Scripts\python.exe'
if(-not(Test-Path -LiteralPath $python)){
    & $BasePython -m venv $venv
    if($LASTEXITCODE -ne 0){throw 'Unable to create project Python environment'}
}
& $python -m pip install --only-binary=:all: -r (Join-Path $appDir 'requirements.txt') --disable-pip-version-check
if($LASTEXITCODE -ne 0){throw 'Dependency installation failed'}
Push-Location -LiteralPath $appDir
try{
    & $python -m unittest discover -s tests -v
    if($LASTEXITCODE -ne 0){throw 'Remote tests failed'}
}finally{Pop-Location}
$account=[Security.Principal.WindowsIdentity]::GetCurrent().Name
# Grant only this app directory to the ordinary account used by the task.
& icacls.exe $InstallRoot /grant ($account+':(OI)(CI)M') | Out-Null
if($LASTEXITCODE -ne 0){throw 'Unable to grant application directory access'}
$action=New-ScheduledTaskAction -Execute $python -Argument ('"'+(Join-Path $appDir 'service_runner.py')+'"') -WorkingDirectory $appDir
$principal=New-ScheduledTaskPrincipal -UserId $account -LogonType S4U -RunLevel Limited
$trigger=New-ScheduledTaskTrigger -AtStartup
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'LLM-Monitor' -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Description 'Loopback LLM monitoring gateway and system collector, running with limited privileges.' -Force|Out-Null
Start-ScheduledTask -TaskName 'LLM-Monitor'
$ready=$false
for($i=0;$i -lt 20;$i++){
    try{$r=Invoke-RestMethod 'http://127.0.0.1:18080/healthz' -TimeoutSec 3;if($r.service -eq 'llm-monitor'){$ready=$true;break}}catch{}
    Start-Sleep -Seconds 1
}
if(-not $ready){throw 'Monitor failed to start; see logs\monitor.log'}
$tailscale='C:\Program Files\Tailscale\tailscale.exe'
$backup=Join-Path $InstallRoot 'tailscale-serve-before.json'
if(-not(Test-Path -LiteralPath $backup)){
    $before=& $tailscale serve status --json
    if($LASTEXITCODE -ne 0){throw 'Unable to read Tailscale configuration'}
    [IO.File]::WriteAllText($backup,($before -join "`n"),(New-Object Text.UTF8Encoding $false))
}
& $tailscale serve --bg --yes --tcp=18080 tcp://127.0.0.1:18080
if($LASTEXITCODE -ne 0){throw 'Unable to publish monitor port'}
& $tailscale serve --bg --yes --tcp=11434 tcp://127.0.0.1:18080
if($LASTEXITCODE -ne 0){throw 'Unable to route existing API through gateway'}
$freeze=& $python -m pip freeze
[IO.File]::WriteAllText((Join-Path $InstallRoot 'requirements.lock.txt'),($freeze -join "`n"),(New-Object Text.UTF8Encoding $false))
@{service='LLM-Monitor';account=$account;run_level='Limited';root=$InstallRoot;dashboard='http://100.122.59.107:18080';api='http://100.122.59.107:11434';installed_utc=(Get-Date).ToUniversalTime().ToString('o')}|ConvertTo-Json
