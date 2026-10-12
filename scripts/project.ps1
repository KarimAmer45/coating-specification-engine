param([string]$Action='build',[string]$Python='python')
$ErrorActionPreference='Stop'
$projectRoot=Split-Path $PSScriptRoot -Parent
if(Test-Path "$projectRoot\.env") {
 # This project's .env uses PG* variables. A DATABASE_URL left in the session by another project's
 # wrapper would take precedence and point every action at that project's database.
 [Environment]::SetEnvironmentVariable('DATABASE_URL',$null,'Process')
 foreach($line in Get-Content -LiteralPath "$projectRoot\.env") {
  if($line -match '^([A-Z_]+)=(.*)$') {[Environment]::SetEnvironmentVariable($Matches[1],$Matches[2],'Process')}
 }
}
if(Test-Path "$projectRoot\.local\pydeps") {$env:PYTHONPATH="$projectRoot\.local\pydeps;$env:PYTHONPATH"}
$commands=@{migrate=@('scripts/migrate.py');build=@('scripts/build.py');offline=@('scripts/build.py','--offline');seed=@('ingest/p1_simulate_plant.py');test=@('-m','pytest','-q','tests');'record-tests'=@('scripts/record_tests.py');download=@('scripts/download_data.py');refresh=@('scripts/download_data.py','--refresh-norms');policy=@('scripts/load_policy.py');catalog=@('ingest/p4_partec_norms.py','--initial-effective-date','2026-01-01');report=@('ingest/p4_release_report.py');export=@('scripts/export_results.py')}
if(-not $commands.ContainsKey($Action)){throw "Unknown action: $Action"}
Push-Location $projectRoot
try {& $Python @($commands[$Action]);if($LASTEXITCODE -ne 0){throw "Failed: $Action"}} finally {Pop-Location}
