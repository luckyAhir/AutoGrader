# Regenerate every sample report on native Windows (MinGW-w64 gcc).
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\run_samples.ps1 [-Cc C:\mingw64\bin\gcc.exe]
param([string]$Cc = "gcc")
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

python -m autograder grade submissions/add_two_integers --rules tests/fixtures/add_two_integers/rules.json --cc $Cc --out reports-windows/add_two_integers --quiet
python -m autograder grade submissions/sort_integers --rules tests/fixtures/sort_integers/rules.json --cc $Cc --out reports-windows/sort_integers --quiet
Get-Content reports-windows/add_two_integers/summary.md, reports-windows/sort_integers/summary.md
python -m autograder verify submissions/add_two_integers/add.c --rules tests/fixtures/add_two_integers/rules.json --cc $Cc --runs 3
