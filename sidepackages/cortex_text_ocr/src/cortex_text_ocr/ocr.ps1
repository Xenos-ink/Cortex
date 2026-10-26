# cortex_text_ocr transport: Windows.Media.Ocr over WinRT projections (Windows
# PowerShell 5.1, which projects WinRT natively; PowerShell 7+ does not).
#
# Modes:
#   -Image <path>  -> full OCR of the PNG at <path>
#   (no -Image)    -> engine-creation probe only (used by available())
#
# Output: ONE line of compressed JSON on stdout:
#   {"ok":true,"mode":"probe|ocr","lines":[{"text","x","y","width","height","words"}]}
#   {"ok":false,"reason":"..."}
# The working invocation shape (EXP-020.1's naive single-projection probe is
# superseded): load BOTH projections (OcrEngine + Language), create via
# TryCreateFromUserProfileLanguages (explicit Language("en-US") fallback), and
# await every WinRT IAsyncOperation through the System.WindowsRuntimeSystemExtensions
# AsTask reflection helper.

param([string]$Image = "")

# Encoding fix (AVR-010 live bug): force UTF-8 output WITHOUT BOM so the Python
# side's utf-8 decode of stdout is exact for non-ASCII (a real terminal's Arabic
# text etc.); the default OEM console codepage mangles unmappable characters.
# try/catch: setting OutputEncoding can fail when no console handle exists.
try {
  [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch { }

# JSON-safety (AVR-010 live bug): Windows PowerShell 5.1's ConvertTo-Json can emit
# RAW C0 control characters inside string values (real terminal/progress text
# carries them), which is invalid JSON and broke the Python parse. Choice of
# record: each control char in a string VALUE is replaced by its literal
# backslash-u escape TEXT (e.g. the 6 characters \u001b) — ConvertTo-Json then
# escapes the backslash, so the value stays valid JSON and remains a readable
# representation of the character. U+0009/U+000A/U+000D are left for
# ConvertTo-Json's own \t/\n/\r escaping.
function ConvertTo-JsonSafeText([string]$Value) {
  if ([string]::IsNullOrEmpty($Value)) { return $Value }
  $sb = New-Object System.Text.StringBuilder
  foreach ($ch in $Value.ToCharArray()) {
    $code = [int]$ch
    if (($code -le 0x08) -or ($code -eq 0x0B) -or ($code -eq 0x0C) -or (($code -ge 0x0E) -and ($code -le 0x1F))) {
      [void]$sb.Append('\u{0:x4}' -f $code)
    } else {
      [void]$sb.Append($ch)
    }
  }
  return $sb.ToString()
}

$ErrorActionPreference = 'Stop'
$out = [ordered]@{ ok = $false; mode = 'probe'; reason = $null; lines = @() }
try {
  [void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
  [void][Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
  if ($Image -ne '') {
    [void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
    [void][Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
  }

  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if (-not $engine) {
    foreach ($tag in @('en-US', 'en-GB', 'en')) {
      try {
        $lang = [Windows.Globalization.Language]::new($tag)
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
        if ($engine) { break }
      } catch { }
    }
  }
  if (-not $engine) {
    $out.reason = 'no OcrEngine: TryCreateFromUserProfileLanguages and the explicit Language fallbacks all failed (broken/incomplete language-pack configuration)'
    $out | ConvertTo-Json -Depth 5 -Compress
    exit 0
  }

  if ($Image -eq '') {
    $out.ok = $true
    $out | ConvertTo-Json -Depth 5 -Compress
    exit 0
  }

  $out.mode = 'ocr'
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  if (-not $asTaskGeneric) { throw 'AsTask(IAsyncOperation`1) reflection helper not found' }
  function Await-WinRT([object]$Operation, [type]$ResultType) {
    $m = $script:asTaskGeneric.MakeGenericMethod($ResultType)
    $t = $m.Invoke($null, @($Operation))
    $t.Wait(-1) | Out-Null
    return $t.Result
  }

  $file = Await-WinRT ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Image)) ([Windows.Storage.StorageFile])
  $stream = Await-WinRT ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await-WinRT ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

  $result = Await-WinRT ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $lines = @()
  foreach ($line in $result.Lines) {
    $x = [double]::MaxValue; $y = [double]::MaxValue; $r = [double]::MinValue; $b = [double]::MinValue
    foreach ($w in $line.Words) {
      $rc = $w.BoundingRect
      if ($rc.X -lt $x) { $x = $rc.X }
      if ($rc.Y -lt $y) { $y = $rc.Y }
      if (($rc.X + $rc.Width) -gt $r) { $r = $rc.X + $rc.Width }
      if (($rc.Y + $rc.Height) -gt $b) { $b = $rc.Y + $rc.Height }
    }
    $lines += [ordered]@{
      text = ConvertTo-JsonSafeText ([string]$line.Text)
      x = $x; y = $y; width = ($r - $x); height = ($b - $y)
      words = @($line.Words).Count
    }
  }
  $out.ok = $true
  $out.lines = $lines
} catch {
  $out.ok = $false
  $out.reason = ConvertTo-JsonSafeText (
    $_.Exception.GetType().Name + ': ' + $_.Exception.Message +
    $(if ($_.Exception.InnerException) { ' | inner: ' + $_.Exception.InnerException.Message } else { '' })
  )
}
$out | ConvertTo-Json -Depth 5 -Compress
