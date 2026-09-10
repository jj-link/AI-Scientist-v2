# start_run.ps1 - relaunch the AI-Scientist pipeline (double-click or run in any terminal)
# Stop it by closing the terminal window or pressing Ctrl+C.
# Watch progress live:  Get-Content .\run_live.log -Wait -Tail 30

$repo = $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# Load the Hugging Face token from .env (git-ignored)
$env:HF_TOKEN = (Get-Content "$repo\.env" | Where-Object { $_ -match '^HF_TOKEN=' }) -replace '^HF_TOKEN=', ''

# Fresh PATH so pdflatex/chktex (MiKTeX) and conda tools resolve
$env:PATH = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')

& "C:\Users\josep\miniconda3\envs\ai_scientist\python.exe" "$repo\launch_scientist_bfts.py" `
    --load_ideas "ai_scientist/ideas/i_cant_believe_its_not_better.json" `
    --load_code --idea_idx 0 2>&1 | Tee-Object -FilePath "$repo\run_live.log" -Append

exit $LASTEXITCODE
