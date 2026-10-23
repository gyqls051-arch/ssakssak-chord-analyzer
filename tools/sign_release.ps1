param(
 [Parameter(Mandatory=$true)][string[]]$Artifact,
 [Parameter(Mandatory=$true)][string]$Product,
 [Parameter(Mandatory=$true)][string]$Version,
 [switch]$VerifyOnly
)
$ErrorActionPreference='Stop'
$repoRoot=[IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$thumb=($env:SSAK_SIGN_THUMBPRINT -replace '\s','').ToUpperInvariant()
if($thumb -notmatch '^[0-9A-F]{40}$'){throw 'Configure the approved code-signing certificate thumbprint first'}
$files=@()
foreach($item in $Artifact){
 $full=[IO.Path]::GetFullPath((Join-Path $repoRoot $item))
 if(-not $full.StartsWith($repoRoot+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Artifact outside project'}
 if(-not (Test-Path -LiteralPath $full -PathType Leaf)){throw 'Build artifact missing'}
 if((Get-Item -LiteralPath $full).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Linked artifact rejected'}
 $files+=$full
}
function Normalize-Version($value){
 $value=([string]$value).Trim()
 if($value -notmatch '^\d+(\.\d+){1,3}$'){throw 'Invalid version resource'}
 $parts=@($value.Split('.') | ForEach-Object {[int]$_})
 while($parts.Count -lt 4){$parts+=0}
 return ($parts -join '.')
}
$imported=$false;$pfx=$null;$importedThumb=$null
try {
 if(-not $VerifyOnly){
  $uri=$null
  if(-not [Uri]::TryCreate($env:SSAK_SIGN_TIMESTAMP,[UriKind]::Absolute,[ref]$uri) -or $uri.Scheme -ne 'https'){throw 'Configure approved HTTPS timestamp service'}
  if(-not (Test-Path -LiteralPath ('Cert:\CurrentUser\My\'+$thumb))){
   if(-not $env:SSAK_SIGN_PFX -or -not $env:SSAK_SIGN_PASSWORD){throw 'Signing certificate is unavailable'}
   $tempRoot=[IO.Path]::GetFullPath([IO.Path]::GetTempPath())
   $pfx=Join-Path $tempRoot ([Guid]::NewGuid().ToString()+'.pfx')
   if(-not [IO.Path]::GetFullPath($pfx).StartsWith($tempRoot,[StringComparison]::OrdinalIgnoreCase)){throw 'Invalid temporary signing path'}
   [IO.File]::WriteAllBytes($pfx,[Convert]::FromBase64String($env:SSAK_SIGN_PFX))
   $password=ConvertTo-SecureString $env:SSAK_SIGN_PASSWORD -AsPlainText -Force
   $public=Get-PfxData -FilePath $pfx -Password $password
   if(@($public.EndEntityCertificates).Count -ne 1 -or $public.EndEntityCertificates[0].Thumbprint -ne $thumb){throw 'PFX identity does not match approved certificate'}
   $cert=Import-PfxCertificate -FilePath $pfx -CertStoreLocation Cert:\CurrentUser\My -Password $password
   $importedThumb=$cert.Thumbprint
   $imported=$true
   if($importedThumb -ne $thumb){throw 'Imported signing identity does not match approved thumbprint'}
  }
  $kits=Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Windows Kits\10\bin'
  $tool=Get-ChildItem -LiteralPath $kits -Directory | Sort-Object Name -Descending |
   ForEach-Object {Join-Path $_.FullName 'x64\signtool.exe'} | Where-Object {Test-Path -LiteralPath $_} | Select-Object -First 1
  if(-not $tool){throw 'Windows SDK SignTool is missing'}
  foreach($file in $files){
   & $tool sign /sha1 $thumb /fd SHA256 /td SHA256 /tr $uri.AbsoluteUri $file
   if($LASTEXITCODE -ne 0){throw 'Authenticode signing failed'}
  }
 }
 foreach($file in $files){
  $signature=Get-AuthenticodeSignature -LiteralPath $file
  $meta=[Diagnostics.FileVersionInfo]::GetVersionInfo($file)
  if($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Thumbprint -ne $thumb -or $null -eq $signature.TimeStamperCertificate){throw 'Artifact signature validation failed'}
  if(([string]$meta.ProductName).Trim() -ne $Product -or (Normalize-Version $meta.ProductVersion) -ne (Normalize-Version $Version)){throw 'Artifact product/version mismatch'}
 }
} finally {
 if($pfx -and (Test-Path -LiteralPath $pfx)){Remove-Item -LiteralPath $pfx -Force}
 if($imported -and $importedThumb){Remove-Item -LiteralPath ('Cert:\CurrentUser\My\'+$importedThumb) -Force}
}
