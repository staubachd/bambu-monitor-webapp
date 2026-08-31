# Run every test, each against a fresh copy of the database, and report
# pass/fail explicitly. Silence is not a pass.
#
# The tests run against a COPY of the app in a scratch folder, so a test that
# writes cannot touch the real telemetry.db. The tests themselves used to live
# in that scratch folder too, under %TEMP% - which is where Windows eventually
# deletes things it has not touched in a while, and about twenty of them went
# that way. They live beside the project now; only the scratch copy is
# disposable.
$src = Split-Path -Parent $PSScriptRoot
$b   = Join-Path $env:TEMP "bambu-tests"
New-Item -ItemType Directory -Force $b | Out-Null
# The tests are COPIED into the scratch folder so they can import the app copy
# beside them, which means "the source, relative to me" would resolve to the
# scratch folder's parent. This is the authority instead.
$env:BAMBU_SRC = $src
Copy-Item "$PSScriptRoot\t_*.py" $b -Force
Copy-Item "$PSScriptRoot\t_*.js" $b -Force
Copy-Item "$src\*.py" $b -Force
Copy-Item "$src\dashboard.html" $b -Force
Copy-Item "$src\setup.html" $b -Force
# The app is configured by instance/db.json now, not by a config file, and every
# setting rides along inside the telemetry.db each test starts from. A stray
# printer.config.json here would make the wizard tests think this is an upgrade.
Remove-Item "$b\printer.config.json" -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$b\instance" | Out-Null
# no BOM: Out-File -Encoding utf8 writes one in Windows PowerShell 5.1
[IO.File]::WriteAllText("$b\instance\db.json",
    '{ "backend": "sqlite", "sqlite_path": "telemetry.db" }')
New-Item -ItemType Directory -Force "$b\samples" | Out-Null
Copy-Item "$src\samples\*" "$b\samples" -Force
New-Item -ItemType Directory -Force "$b\tools" | Out-Null
Copy-Item "$src\tools\*.py" "$b\tools" -Force

$fail = 0
foreach ($f in Get-ChildItem "$b\t_*.py" | Sort-Object Name) {
    Copy-Item "$src\telemetry.db" $b -Force        # every test starts clean
    $out = & py $f.FullName 2>&1
    $ok  = ($LASTEXITCODE -eq 0) -and ($out -join "`n") -match '(?m)^ok\s*$'
    if (-not $ok) { $fail++ }
    "{0,-18} {1}" -f $f.Name, $(if ($ok) { "pass" } else { "FAIL" })
    if (-not $ok) { $out | Select-Object -Last 4 | ForEach-Object { "      $_" } }
}
foreach ($f in Get-ChildItem "$b\t_*.js" | Sort-Object Name) {
    $out = & node $f.FullName 2>&1
    $ok  = $LASTEXITCODE -eq 0
    if (-not $ok) { $fail++ }
    "{0,-18} {1}" -f $f.Name, $(if ($ok) { "pass" } else { "FAIL" })
    if (-not $ok) { $out | Select-Object -Last 4 | ForEach-Object { "      $_" } }
}
foreach ($m in "filament_catalog.py", "bambu_state.py") {
    Push-Location $src
    $out = & py $m 2>&1
    Pop-Location
    $ok = $LASTEXITCODE -eq 0
    if (-not $ok) { $fail++ }
    "{0,-18} {1}" -f $m, $(if ($ok) { "pass" } else { "FAIL" })
}
""
if ($fail) { "$fail FAILED" } else { "all green" }
