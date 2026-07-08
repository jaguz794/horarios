$ErrorActionPreference = "Stop"

$baseDir = Split-Path -Parent $PSScriptRoot
$docsDir = Join-Path $baseDir "docs"

$files = @(
    "Esquema_Trabajo_Portal_Horarios.docx",
    "Manual_Operativo_Portal_Horarios.docx"
)

$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0

try {
    foreach ($file in $files) {
        $docxPath = Join-Path $docsDir $file
        if (-not (Test-Path $docxPath)) {
            throw "No existe el archivo $docxPath"
        }

        $pdfPath = [System.IO.Path]::ChangeExtension($docxPath, ".pdf")
        $document = $word.Documents.Open($docxPath, $false, $true)
        try {
            $document.ExportAsFixedFormat($pdfPath, 17)
            Write-Output "PDF_OK $pdfPath"
        }
        finally {
            $document.Close()
        }
    }
}
finally {
    $word.Quit()
}
