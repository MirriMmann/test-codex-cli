# PowerShell entry point: Ctrl+C does not trigger CMD's batch confirmation.
Set-Location -LiteralPath $PSScriptRoot
$bootstrapPath = Join-Path $PSScriptRoot 'bootstrap.py'
$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) {
    foreach ($version in @('3.15', '3.14', '3.13', '3.12', '3.11', '3.10')) {
        & $launcher.Source "-$version" -c 'import sys; sys.exit(0)' 2>$null
        if ($LASTEXITCODE -eq 0) {
            & $launcher.Source "-$version" $bootstrapPath @args
            exit $LASTEXITCODE
        }
    }
}
& python $bootstrapPath @args
exit $LASTEXITCODE
