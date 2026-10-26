param([int]$Port=8080)
$ErrorActionPreference='Stop'
$taskRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $taskRoot
$taskPython=Join-Path $taskRoot '.venv\Scripts\python.exe'
if(-not(Test-Path -LiteralPath $taskPython)){throw 'Create the Python 3.12 .venv first; see README_ResolveDesk.txt'}
if(-not(Test-Path -LiteralPath (Join-Path $taskRoot 'data\auth.json'))){throw 'Run .venv\Scripts\python.exe -X utf8 tools\setup_local.py --download-model first'}
& $taskPython -X utf8 -B -m uvicorn resolvedesk.app:app --host 127.0.0.1 --port $Port --workers 1 --no-access-log
if($LASTEXITCODE -ne 0){throw "ResolveDesk stopped with exit code $LASTEXITCODE"}
