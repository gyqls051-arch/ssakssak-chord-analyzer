"""Verified Windows updates. No trust configuration or unverified fallback is accepted."""
from __future__ import annotations
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, unquote
import urllib.request

OWNER = "gyqls051-arch"
MAX_BYTES = 512 * 1024 * 1024
HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
class UpdateError(RuntimeError):
    pass

@dataclass(frozen=True)
class Config:
    repo: str
    asset: str
    mode: str
    product: str
    target_name: str = ""
    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.repo) or self.mode not in ("portable", "inno"):
            raise ValueError("Invalid built-in update configuration")
        if "/" in self.asset or "\\" in self.asset or not self.asset.endswith(".exe"):
            raise ValueError("Invalid built-in asset name")

@dataclass(frozen=True)
class Prepared:
    config: Config
    version: str
    target: Path
    payload: Path
    stage: Path
    old_hash: str
    new_hash: str
    signer: str

def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9][0-9]{0,5})(\.(0|[1-9][0-9]{0,5})){1,3}", value):
        raise UpdateError("올바르지 않은 업데이트 버전입니다.")
    parts = tuple(map(int, value.split(".")))
    return parts + (0,) * (4 - len(parts))

def validate_url(url, hosts):
    try:
        p = urlsplit(url)
        if (p.scheme != "https" or p.hostname not in hosts or p.username or p.password
                or p.port not in (None, 443) or p.fragment
                or any(c.isspace() or ord(c) < 32 for c in unquote(p.path))
                or any(c.isspace() or ord(c) < 32 for c in url)):
            raise ValueError()
    except (ValueError, TypeError):
        raise UpdateError("공식 HTTPS 업데이트 주소만 허용합니다.") from None
    return url

class Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts, self.count = hosts, 0
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > 5:
            raise UpdateError("업데이트 이동 횟수가 너무 많습니다.")
        validate_url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def open_remote(url):
    hosts = {"api.github.com"} if urlsplit(url).hostname == "api.github.com" else HOSTS
    validate_url(url, hosts)
    request = urllib.request.Request(url, headers={"User-Agent": "SsakSsak-Verified-Update", "Accept-Encoding": "identity"})
    return urllib.request.build_opener(Redirects(hosts)).open(request, timeout=5)

def system_powershell():
    if os.name != "nt":
        raise UpdateError("앱 내부 설치는 Windows 설치본에서 지원합니다.")
    buf = ctypes.create_unicode_buffer(32768)
    count = ctypes.windll.kernel32.GetSystemDirectoryW(buf, len(buf))
    if count <= 0 or count >= len(buf):
        raise UpdateError("Windows 시스템 경로를 확인하지 못했습니다.")
    path = Path(buf.value) / "WindowsPowerShell/v1.0/powershell.exe"
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise UpdateError("Windows 서명 검사기를 찾지 못했습니다.")
    return str(path)

INSPECT = r"""
$ErrorActionPreference='Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -Force
$p=$env:SSAK_INSPECT_PATH
$s=Microsoft.PowerShell.Security\Get-AuthenticodeSignature -LiteralPath $p
$v=[Diagnostics.FileVersionInfo]::GetVersionInfo($p)
$c=$s.SignerCertificate
$thumb='';$eku=$false
if($c){$thumb=$c.Thumbprint;$eku=@($c.EnhancedKeyUsageList | Where-Object {$_.ObjectId.Value -eq '1.3.6.1.5.5.7.3.3'}).Count -gt 0}
[ordered]@{status=[string]$s.Status;thumbprint=$thumb;product=([string]$v.ProductName).Trim();version=([string]$v.ProductVersion).Trim();timestamped=($null -ne $s.TimeStamperCertificate);codeSigning=$eku} | ConvertTo-Json -Compress
"""
def ps_args(script):
    return [system_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive",
            "-EncodedCommand", base64.b64encode(script.encode("utf-16le")).decode("ascii")]

def inspect_signature(path):
    env = os.environ.copy()
    env["SSAK_INSPECT_PATH"] = str(path)
    result = subprocess.run(ps_args(INSPECT), env=env, capture_output=True,
                            timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or len(result.stdout) > 16384:
        raise UpdateError("파일 서명을 확인하지 못했습니다.")
    try:
        return json.loads(result.stdout.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise UpdateError("서명 검사 결과가 올바르지 않습니다.") from None

def validate_signature(info, product, version, signer=None):
    if (not isinstance(info, dict) or info.get("status") != "Valid"
            or not info.get("timestamped") or not info.get("codeSigning")
            or str(info.get("product", "")).strip() != product
            or not re.fullmatch(r"[0-9A-Fa-f]{40}", str(info.get("thumbprint", "")))
            or (signer and info["thumbprint"].upper() != signer.upper())
            or version_tuple(str(info.get("version", "")).strip()) != version_tuple(version)):
        raise UpdateError("제품·버전·서명자를 확인하지 못해 업데이트를 중단했습니다.")
    return info["thumbprint"].upper()

def select_asset(config, version, release):
    version_tuple(version)
    if (not isinstance(release, dict) or release.get("draft") or release.get("prerelease")
            or release.get("tag_name") != "v" + version):
        raise UpdateError("요청한 공식 안정 릴리스가 아닙니다.")
    name = config.asset.format(version=version)
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("설치 자산 정보가 없습니다.")
    matches = [a for a in assets if isinstance(a, dict) and a.get("name") == name]
    if len(matches) != 1:
        raise UpdateError("이 앱의 업데이트 파일을 한 개로 식별하지 못했습니다.")
    asset = matches[0]
    expected = f"https://github.com/{OWNER}/{config.repo}/releases/download/v{version}/{name}"
    if (asset.get("browser_download_url") != expected
            or not isinstance(asset.get("size"), int) or not 2 <= asset["size"] <= MAX_BYTES
            or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", str(asset.get("digest", "")))):
        raise UpdateError("업데이트 주소·크기·SHA256 정보가 올바르지 않습니다.")
    validate_url(expected, {"github.com"})
    return asset

def plain_path(path):
    for p in [path, *path.parents]:
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            raise UpdateError("링크 또는 재분석 경로에서는 업데이트하지 않습니다.")
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise UpdateError("독립된 실행 파일만 업데이트할 수 있습니다.")

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def read_limited(response, limit, sink=None):
    start, size, chunks = time.monotonic(), 0, []
    while True:
        if time.monotonic() - start > 120:
            raise UpdateError("업데이트 다운로드 제한 시간을 넘었습니다.")
        block = response.read(min(65536, limit + 1 - size))
        if not block:
            break
        size += len(block)
        if size > limit:
            raise UpdateError("업데이트 응답 크기가 제한을 넘었습니다.")
        if sink:
            sink.write(block)
        else:
            chunks.append(block)
    return size if sink else b"".join(chunks)

def prepare(config, current, version):
    if version_tuple(version) <= version_tuple(current):
        raise UpdateError("현재보다 새로운 버전만 설치할 수 있습니다.")
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise UpdateError("서명된 Windows 실행 파일에서 업데이트해 주세요.")
    target = Path(sys.executable).absolute()
    plain_path(target)
    if config.mode == "inno" and (not config.target_name or target.name != config.target_name):
        raise UpdateError("설치형 앱 실행 파일의 이름을 확인하지 못했습니다.")
    signer = validate_signature(inspect_signature(target), config.product, current)
    old_hash = digest(target)
    url = f"https://api.github.com/repos/{OWNER}/{config.repo}/releases/tags/v{version}"
    with open_remote(url) as response:
        validate_url(response.geturl(), {"api.github.com"})
        data = json.loads(read_limited(response, 512 * 1024))
    asset = select_asset(config, version, data)
    # Same volume is required for atomic portable replacement. No arbitrary path from remote data.
    stage = Path(tempfile.mkdtemp(prefix=".ssak-update-", dir=target.parent))
    payload = stage / "payload.exe"
    try:
        with open_remote(asset["browser_download_url"]) as response, payload.open("xb") as out:
            validate_url(response.geturl(), HOSTS)
            size = read_limited(response, asset["size"], out)
            out.flush()
            os.fsync(out.fileno())
        expected_hash = asset["digest"][7:].lower()
        with payload.open("rb") as f:
            if f.read(2) != b"MZ":
                raise UpdateError("올바른 Windows 실행 파일이 아닙니다.")
        if size != asset["size"] or digest(payload) != expected_hash:
            raise UpdateError("업데이트 파일이 변조되었거나 불완전합니다.")
        validate_signature(inspect_signature(payload), config.product, version, signer)
        if digest(target) != old_hash:
            raise UpdateError("업데이트 준비 중 현재 실행 파일이 바뀌었습니다.")
        return Prepared(config, version, target, payload, stage, old_hash, expected_hash, signer)
    except Exception:
        if payload.exists() and not payload.is_symlink():
            payload.unlink()
        try: stage.rmdir()
        except OSError: pass
        raise

def discard(prepared):
    # Never recursively remove a directory or delete a backup/new replacement.
    if prepared.stage.is_symlink() or prepared.stage.parent != prepared.target.parent:
        return
    try:
        if prepared.payload.is_file() and not prepared.payload.is_symlink():
            prepared.payload.unlink()
        prepared.stage.rmdir()
    except OSError:
        pass

HANDOFF = r"""
$ErrorActionPreference='Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -Force
function Hash-File($path) {
  $stream=[IO.File]::OpenRead($path);$sha=[Security.Cryptography.SHA256]::Create()
  try {return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}
  finally {$stream.Dispose();$sha.Dispose()}
}
function Plain-Path($path) {
  $item=Get-Item -LiteralPath $path -Force
  while($null -ne $item){
    if(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Reparse path rejected'}
    if($item -is [IO.FileInfo]){$item=$item.Directory}else{$item=$item.Parent}
  }
}
function Check-File($path,$hash,$thumb,$product,$version) {
  Plain-Path $path
  if((Hash-File $path) -ne $hash){throw 'File changed'}
  $s=Microsoft.PowerShell.Security\Get-AuthenticodeSignature -LiteralPath $path
  $v=[Diagnostics.FileVersionInfo]::GetVersionInfo($path)
  if($s.Status -ne 'Valid' -or $null -eq $s.TimeStamperCertificate -or $s.SignerCertificate.Thumbprint -ne $thumb){throw 'Signer mismatch'}
  if(([string]$v.ProductName).Trim() -ne $product -or ([string]$v.ProductVersion).Trim() -ne $version){throw 'Product/version mismatch'}
}
function Version-Key($value) {
  $parts=@(([string]$value).Split('.') | ForEach-Object {[int]$_})
  while($parts.Count -lt 4){$parts+=0}
  return ($parts -join '.')
}
function Start-App($target) {
  $start=New-Object Diagnostics.ProcessStartInfo
  $start.FileName=$target;$start.WorkingDirectory=[IO.Path]::GetDirectoryName($target)
  $start.UseShellExecute=$false
  return [Diagnostics.Process]::Start($start)
}
function Replace-Portable($payload,$target,$backup) {
  [IO.File]::Replace($payload,$target,$backup,$false)
  try { $null=Start-App $target }
  catch {
    [IO.File]::Replace($backup,$target,$payload,$false)
    throw
  }
}
# JOB_ENTRY
$j=$null;$parent=$null
try {
  $j=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:SSAK_UPDATE_JOB)) | ConvertFrom-Json
  Plain-Path $j.target
  Plain-Path $j.payload
  $parent=[Diagnostics.Process]::GetProcessById([int]$j.parent)
  if($parent.StartTime.ToUniversalTime().Ticks.ToString() -ne $j.parentStarted){throw 'Parent process changed'}
  Check-File $j.payload $j.newHash $j.signer $j.product $j.productVersion
  if((Hash-File $j.target) -ne $j.oldHash){throw 'Current executable changed'}
  $ready=[IO.File]::Open($j.ready,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
  try {$bytes=[Text.Encoding]::UTF8.GetBytes($j.nonce);$ready.Write($bytes,0,$bytes.Length)} finally {$ready.Dispose()}
  if(-not $parent.WaitForExit(120000)){throw 'Application did not exit'}
  Check-File $j.payload $j.newHash $j.signer $j.product $j.productVersion
  Plain-Path $j.target
  if((Hash-File $j.target) -ne $j.oldHash){throw 'Current executable changed'}
  if($j.mode -eq 'portable'){
    Replace-Portable $j.payload $j.target $j.backup
  } elseif($j.mode -eq 'inno') {
    $dir=[IO.Path]::GetDirectoryName($j.target)
    $arguments=@('/VERYSILENT','/SUPPRESSMSGBOXES','/SP-','/NORESTART','/NOCLOSEAPPLICATIONS','/NORESTARTAPPLICATIONS',('/DIR="'+$dir+'"'))
    $setup=Start-Process -FilePath $j.payload -ArgumentList $arguments -PassThru -WindowStyle Hidden
    $setup.WaitForExit()
    if($setup.ExitCode -ne 0){throw 'Installer did not complete'}
    $signature=Microsoft.PowerShell.Security\Get-AuthenticodeSignature -LiteralPath $j.target
    $installed=[Diagnostics.FileVersionInfo]::GetVersionInfo($j.target)
    if($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Thumbprint -ne $j.signer -or ([string]$installed.ProductName).Trim() -ne $j.product -or (Version-Key $installed.ProductVersion) -ne (Version-Key $j.productVersion)){throw 'Installed executable not trusted'}
    $null=Start-App $j.target
  } else {throw 'Unknown update mode'}
  [IO.File]::WriteAllText($j.result,(@{ok=$true;target=$j.target;product=$j.product} | ConvertTo-Json -Compress))
  if($j.mode -eq 'inno' -and (Hash-File $j.payload) -eq $j.newHash){[IO.File]::Delete($j.payload)}
} catch {
  if($null -ne $j){
    [IO.File]::WriteAllText($j.result,(@{ok=$false;target=$j.target;product=$j.product;error='Update failed; previous backup retained'} | ConvertTo-Json -Compress))
    try {if($null -ne $parent -and $parent.HasExited -and (Hash-File $j.target) -eq $j.oldHash){$null=Start-App $j.target}} catch {}
  }
  exit 1
}
"""
def launch_handoff(prepared):
    import secrets
    plain_path(prepared.target)
    plain_path(prepared.payload)
    if digest(prepared.target) != prepared.old_hash or digest(prepared.payload) != prepared.new_hash:
        raise UpdateError("설치 직전 파일이 바뀌어 중단했습니다.")
    signed = inspect_signature(prepared.payload)
    validate_signature(signed, prepared.config.product, prepared.version, prepared.signer)
    env = os.environ.copy()
    started = subprocess.run(ps_args(f"([Diagnostics.Process]::GetProcessById({os.getpid()})).StartTime.ToUniversalTime().Ticks.ToString()"),
                             capture_output=True, timeout=5, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    stamp = started.stdout.decode("ascii").strip()
    if started.returncode or not stamp.isdigit():
        raise UpdateError("현재 프로세스의 종료 대상을 확인하지 못했습니다.")
    ready = prepared.stage / "ready"
    nonce = secrets.token_hex(24)
    job = dict(target=str(prepared.target), payload=str(prepared.payload),
               oldHash=prepared.old_hash, newHash=prepared.new_hash, signer=prepared.signer,
               product=prepared.config.product, productVersion=signed["version"],
               mode=prepared.config.mode, parent=os.getpid(), parentStarted=stamp,
               ready=str(ready), nonce=nonce, backup=str(prepared.stage / "previous.exe"),
               result=str(prepared.stage / "result.json"))
    env["SSAK_UPDATE_JOB"] = base64.b64encode(json.dumps(job).encode("utf-8")).decode("ascii")
    process = subprocess.Popen(ps_args(HANDOFF), env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    limit = time.monotonic() + 10
    while time.monotonic() < limit:
        if ready.exists() and ready.read_text(encoding="utf-8") == nonce:
            return
        if process.poll() is not None:
            break
        time.sleep(.05)
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=5)
    raise UpdateError("업데이트 인계에 실패했습니다. 현재 앱은 계속 사용할 수 있습니다.")

class Client:
    """One in-process operation; GUI receives results through its existing signal/queue."""
    def __init__(self, config, current, deliver):
        self.config,self.current,self.deliver=config,current,deliver
        self.state="idle"
        self.prepared=None
        self.worker=None
        self.lock=threading.Lock()
    def _run(self, operation, success):
        try:
            operation()
            self.deliver({"kind":success})
        except Exception as error:
            with self.lock:
                prepared,self.prepared=self.prepared,None
                self.state="idle"
            if prepared: discard(prepared)
            self.deliver({"kind":"install_error","message":str(error)[:240]})
    def begin(self, version):
        with self.lock:
            if self.state!="idle":return False
            self.state="preparing"
        def operation():
            prepared=prepare(self.config,self.current,version)
            with self.lock:self.prepared=prepared;self.state="prepared"
        self.worker=threading.Thread(target=self._run,args=(operation,"install_ready"),daemon=True)
        self.worker.start()
        return True
    def install(self):
        with self.lock:
            if self.state!="prepared":return False
            self.state="installing"
        def operation():
            launch_handoff(self.prepared)
            with self.lock:self.state="handed_off"
        self.worker=threading.Thread(target=self._run,args=(operation,"install_started"),daemon=True)
        self.worker.start()
        return True
    def recover(self):
        if not getattr(sys, "frozen", False):
            return
        target = Path(sys.executable).absolute()
        for marker in list(target.parent.glob(".ssak-update-*/result.json"))[:32]:
            try:
                plain_path(marker)
                if marker.stat().st_size > 1024:
                    continue
                data = json.loads(marker.read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict):
                    continue
                if data.get("target") != str(target) or data.get("product") != self.config.product:
                    continue
                seen = marker.with_suffix(".seen")
                if seen.exists():
                    continue
                marker.rename(seen)
                if data.get("ok") is False:
                    self.deliver({"kind":"install_error", "message":"이전 업데이트를 완료하지 못했습니다. 기존 실행 파일 또는 백업이 보존되어 있습니다."})
            except (OSError, ValueError, UpdateError):
                continue
    def cancel(self):
        with self.lock:
            if self.state!="prepared":return False
            prepared,self.prepared=self.prepared,None
            self.state="idle"
        discard(prepared)
        return True
