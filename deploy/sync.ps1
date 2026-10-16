$ErrorActionPreference = 'Stop'
# Successful CI and Git pushes are not release approval.
# Install only through the separately approved server transition.
Write-Output 'Automatic News deployment is disabled in this candidate. Use the approved manual release workflow and a SHA-bound private release permit at the server console.'
