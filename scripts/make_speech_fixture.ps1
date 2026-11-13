param(
    [Parameter(Mandatory = $true)][string]$Text,
    [Parameter(Mandatory = $true)][string]$OutWav,
    [Parameter(Mandatory = $true)][string]$OutJson,
    [string]$Voice = 'Microsoft Zira Desktop',
    [int]$Rate = 0
)
# Generates a speech fixture (SRD NFR-9: generated, never real recordings) with Windows'
# built-in speech synthesiser, and records where each word starts in the audio, as reported
# by the synthesiser itself. Those starts are the reference that Whisper's word timings are
# tested against (FR-UND-1).
#
#   powershell -File scripts\make_speech_fixture.ps1 -Text "..." -OutWav x.wav -OutJson x.words.json
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SelectVoice($Voice)
$synth.Rate = $Rate
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$synth.SetOutputToWaveFile([IO.Path]::GetFullPath($OutWav), $format)
$script:words = New-Object System.Collections.ArrayList
$synth.add_SpeakProgress({
        param($sender, $e)
        [void]$script:words.Add([ordered]@{ text = $e.Text; start = [math]::Round($e.AudioPosition.TotalSeconds, 3) })
    })
$synth.Speak($Text)
$synth.SetOutputToNull()
$synth.Dispose()
$doc = [ordered]@{ text = $Text; voice = $Voice; rate = $Rate; words = $script:words }
[IO.File]::WriteAllText([IO.Path]::GetFullPath($OutJson), ($doc | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
"wrote $OutWav and $OutJson with $($script:words.Count) words"
