param(
    [Parameter(Mandatory = $true)][string]$Request,
    [Parameter(Mandatory = $true)][string]$OutputFolder
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$lines = Get-Content -LiteralPath $Request -Encoding UTF8 -Raw | ConvertFrom-Json
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $installed = $speaker.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -eq 'ko-KR' } | Select-Object -First 1
    if (-not $installed) { throw 'Install a Korean text-to-speech voice in Windows language settings.' }
    $speaker.SelectVoice($installed.VoiceInfo.Name)
    $speaker.Rate = 1
    for ($index = 0; $index -lt $lines.Count; $index++) {
        $track = Join-Path $OutputFolder ('voice-{0}.wav' -f $index)
        $speaker.SetOutputToWaveFile($track)
        $speaker.Speak([string]$lines[$index])
        $speaker.SetOutputToNull()
    }
} finally {
    $speaker.Dispose()
}
