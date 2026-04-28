# Re-upload Cloud Run secrets WITHOUT trailing CRLF.
#
# Why: PowerShell's `echo "value" | gcloud secrets create` adds CRLF on Windows.
# The OpenAI/Anthropic/Groq SDKs build "Bearer <key>" headers from the raw
# secret value; httpx rejects any header with \r\n (LocalProtocolError),
# which surfaces in logs as APIConnectionError.
#
# Run from backend/ after rotating the leaked Groq key:
#   .\fix_secrets.ps1
#
# You'll be prompted for each key. The script writes each value to a temp
# file with NO trailing newline, then uploads it as a new secret version.

$ErrorActionPreference = "Stop"

function Set-SecretClean {
    param(
        [string]$Name,
        [string]$Prompt
    )

    Write-Host ""
    Write-Host $Prompt -ForegroundColor Cyan
    $secure = Read-Host -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $value = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

    if ([string]::IsNullOrWhiteSpace($value)) {
        Write-Host "  (skipped — empty)" -ForegroundColor Yellow
        return
    }

    # Trim any whitespace / CRLF the user may have pasted in.
    $value = $value.Trim()

    # Write to a temp file with NO BOM and NO trailing newline.
    $tmp = New-TemporaryFile
    try {
        [System.IO.File]::WriteAllText($tmp.FullName, $value, [System.Text.UTF8Encoding]::new($false))

        # Verify file size matches value length (sanity check — no hidden bytes).
        $fileBytes = (Get-Item $tmp.FullName).Length
        if ($fileBytes -ne $value.Length) {
            Write-Host "  WARNING: file size ($fileBytes) != value length ($($value.Length))" -ForegroundColor Yellow
        }

        & gcloud secrets versions add $Name --data-file=$tmp.FullName | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  OK — new version of '$Name' uploaded ($($value.Length) chars, no trailing newline)" -ForegroundColor Green
        } else {
            Write-Host "  FAILED to upload '$Name'" -ForegroundColor Red
        }
    } finally {
        Remove-Item $tmp.FullName -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Re-uploading Cloud Run secrets WITHOUT trailing CRLF..." -ForegroundColor Cyan
Write-Host "Press Enter to skip any key you don't want to change." -ForegroundColor DarkGray

Set-SecretClean -Name "erp-groq-key"      -Prompt "New GROQ API key (gsk_...)"
Set-SecretClean -Name "erp-anthropic-key" -Prompt "Anthropic API key (sk-ant-...)"
Set-SecretClean -Name "erp-gemini-key"    -Prompt "Gemini API key (AIza...)"

Write-Host ""
Write-Host "Triggering a new Cloud Run revision so it picks up the new secret versions..." -ForegroundColor Cyan
gcloud run services update erp-thaki --region europe-west4 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Done. Test from your phone now." -ForegroundColor Green
} else {
    Write-Host "Revision update failed. Try: gcloud run services update erp-thaki --region europe-west4" -ForegroundColor Red
}
