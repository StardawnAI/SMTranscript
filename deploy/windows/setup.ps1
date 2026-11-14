# Sets the SMTranscript worker up on a Windows server without Docker
# (first: Hetzner mp-streamfab-test, Windows Server 2022). Run as Administrator.
# Installs Python 3.12, ffmpeg and Tesseract (with German), puts the worker
# code of one commit into C:\smt-worker\app, installs its pinned packages and
# registers a scheduled task that starts it at boot and restarts it on failure.
# worker.env (the settings, see run-worker.py) is placed beside it separately.
param([Parameter(Mandatory = $true)][string]$Commit)
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$dir = "C:\smt-worker"
$tmp = Join-Path $env:TEMP "smt-setup"
New-Item -ItemType Directory -Force $dir, $tmp | Out-Null
function Get-File($url, $out) { Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $out }

if (-not (Test-Path "C:\Python312\python.exe")) {
  Get-File "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" "$tmp\python.exe"
  Start-Process "$tmp\python.exe" -Wait -ArgumentList "/quiet InstallAllUsers=1 TargetDir=C:\Python312 Include_test=0 Include_launcher=0 PrependPath=0"
}
if (-not (Test-Path "$dir\tools\ffmpeg\bin\ffmpeg.exe")) {
  Get-File "https://github.com/GyanD/codexffmpeg/releases/download/7.1.1/ffmpeg-7.1.1-essentials_build.zip" "$tmp\ffmpeg.zip"
  Expand-Archive -Force "$tmp\ffmpeg.zip" "$tmp\ffmpeg"
  New-Item -ItemType Directory -Force "$dir\tools" | Out-Null
  Move-Item -Force (Get-ChildItem "$tmp\ffmpeg" -Directory | Select-Object -First 1).FullName "$dir\tools\ffmpeg"
}
if (-not (Test-Path "C:\Program Files\Tesseract-OCR\tesseract.exe")) {
  Get-File "https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-5.4.0.20240606.exe" "$tmp\tesseract.exe"
  Start-Process "$tmp\tesseract.exe" -Wait -ArgumentList "/S"
}
if (-not (Test-Path "C:\Program Files\Tesseract-OCR\tessdata\deu.traineddata")) {
  Get-File "https://github.com/tesseract-ocr/tessdata_fast/raw/main/deu.traineddata" "C:\Program Files\Tesseract-OCR\tessdata\deu.traineddata"
}

# The worker code of the given commit.
Get-File "https://github.com/StardawnAI/SMTranscript/archive/$Commit.zip" "$tmp\smt.zip"
Remove-Item -Recurse -Force "$tmp\smt" -ErrorAction SilentlyContinue
Expand-Archive -Force "$tmp\smt.zip" "$tmp\smt"
$src = (Get-ChildItem "$tmp\smt" -Directory | Select-Object -First 1).FullName
Remove-Item -Recurse -Force "$dir\app" -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$dir\app" | Out-Null
Copy-Item -Recurse -Force "$src\worker\smt" "$dir\app\smt"
Copy-Item -Force "$src\worker\requirements.lock", "$src\deploy\windows\run-worker.py" "$dir\"
& C:\Python312\python.exe -m pip install --disable-pip-version-check --no-warn-script-location --no-deps -r "$dir\requirements.lock" | Out-Null
& C:\Python312\python.exe -m pip check
Set-Content -Path "$dir\commit.txt" -Value $Commit -Encoding ascii

icacls $dir /inheritance:r /grant "*S-1-5-32-544:(OI)(CI)F" /grant "*S-1-5-18:(OI)(CI)F" | Out-Null
$name = "SMTranscript worker"
$action = New-ScheduledTaskAction -Execute "C:\Python312\python.exe" -Argument "$dir\run-worker.py" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -User "SYSTEM" -RunLevel Highest -Force | Out-Null
"ready: python " + (& C:\Python312\python.exe --version) + ", ffmpeg " + (Test-Path "$dir\tools\ffmpeg\bin\ffmpeg.exe") + ", tesseract " + (Test-Path "C:\Program Files\Tesseract-OCR\tesseract.exe") + ", commit $Commit"
