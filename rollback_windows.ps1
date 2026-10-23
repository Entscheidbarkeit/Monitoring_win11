param([string]$InstallRoot='C:\ProgramData\LLMMonitor')
$ErrorActionPreference='Stop'
$tailscale='C:\Program Files\Tailscale\tailscale.exe'
$before=Get-Content -LiteralPath (Join-Path $InstallRoot 'tailscale-serve-before.json') -Raw -Encoding UTF8|ConvertFrom-Json
$target=$before.TCP.'11434'.TCPForward
if(-not $target){throw 'No previous API forward in backup; inspect backup before rollback'}
& $tailscale serve --bg --yes --tcp=11434 ('tcp://'+$target)
if($LASTEXITCODE -ne 0){throw 'Unable to restore API forward'}
if(-not $before.TCP.'18080'){
    & $tailscale serve --tcp=18080 off
    if($LASTEXITCODE -ne 0){throw 'Unable to remove monitor forward'}
}
Stop-ScheduledTask -TaskName 'LLM-Monitor'
Write-Output 'Original API forward restored. Monitoring task stopped; database preserved.'
