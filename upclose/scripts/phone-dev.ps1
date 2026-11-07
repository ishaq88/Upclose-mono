# Opt-in local HTTPS. Never installs trust, opens a firewall, or starts a tunnel.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$LanAddress,
    [ValidateSet('Prepare', 'Start', 'Check', 'Smoke', 'Stop', 'Remove')][string]$Action = 'Prepare',
    [switch]$LoopbackOnly
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$phoneDirectory = Join-Path $repoRoot '.tools/phone-dev'
$parsedAddress = $null
if (-not [System.Net.IPAddress]::TryParse($LanAddress, [ref]$parsedAddress) -or
    $parsedAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
    $LanAddress -notmatch '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)') {
    throw 'Supply the computer Wi-Fi/Ethernet RFC1918 IPv4 address, for example 192.168.1.16.'
}
$dockerDirectory = Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin'
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
$dockerExecutable = if ($dockerCommand) { $dockerCommand.Source } else { Join-Path $dockerDirectory 'docker.exe' }
if (-not (Test-Path -LiteralPath $dockerExecutable)) { throw 'Docker Desktop CLI is required.' }
$oldPath = $env:PATH
$oldBind = $env:PHONE_BIND_IP
$oldTls = $env:PHONE_TLS_DIR
$oldListen = $env:PHONE_LISTEN_IP
$compose = @('compose', '--project-name', 'upclose-phone', '--env-file', (Join-Path $repoRoot '.env.example'),
    '-f', (Join-Path $repoRoot 'infra/compose.yaml'), '-f', (Join-Path $repoRoot 'infra/compose.phone.yaml'))
function Invoke-PhoneDocker([string[]]$Arguments) {
    & $dockerExecutable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Phone Docker command failed (exit $LASTEXITCODE)." }
}
function Invoke-PhoneOpenSsl([string[]]$Arguments) {
    & openssl @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Certificate command failed (exit $LASTEXITCODE)." }
}
try {
    $env:PATH = "$dockerDirectory;$oldPath"
    $env:PHONE_BIND_IP = $LanAddress
    $env:PHONE_TLS_DIR = $phoneDirectory.Replace('\', '/')
    $env:PHONE_LISTEN_IP = if ($LoopbackOnly) { '127.0.0.1' } else { $LanAddress }
    if ($Action -eq 'Prepare') {
        if (-not (Get-Command openssl -ErrorAction SilentlyContinue)) { throw 'OpenSSL is required on PATH to generate local certificates.' }
        New-Item -ItemType Directory -Force -Path $phoneDirectory | Out-Null
        $caKey = Join-Path $phoneDirectory 'ca.key'
        $caCert = Join-Path $phoneDirectory 'ca.crt'
        $serverKey = Join-Path $phoneDirectory 'server.key'
        $serverCert = Join-Path $phoneDirectory 'server.crt'
        $opensslConfig = Join-Path $phoneDirectory 'openssl.cnf'
        @('[req]', 'distinguished_name=dn', '[dn]') | Set-Content -LiteralPath $opensslConfig -Encoding ascii
        if (Test-Path -LiteralPath $serverCert) {
            Invoke-PhoneOpenSsl @('x509', '-in', $serverCert, '-noout', '-checkend', '86400')
        } else {
            Invoke-PhoneOpenSsl @('req', '-config', $opensslConfig, '-x509', '-newkey', 'rsa:3072', '-nodes', '-sha256', '-days', '30',
                '-keyout', $caKey, '-out', $caCert, '-subj', '/CN=UpClose Local Phone Test CA',
                '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
            $csr = Join-Path $phoneDirectory 'server.csr'
            $extensions = Join-Path $phoneDirectory 'server.ext'
            @("subjectAltName=IP:$LanAddress", 'basicConstraints=critical,CA:FALSE',
                'keyUsage=critical,digitalSignature,keyEncipherment', 'extendedKeyUsage=serverAuth') |
                Set-Content -LiteralPath $extensions -Encoding ascii
            Invoke-PhoneOpenSsl @('req', '-config', $opensslConfig, '-new', '-newkey', 'rsa:3072', '-nodes', '-sha256', '-keyout', $serverKey,
                '-out', $csr, '-subj', "/CN=$LanAddress")
            Invoke-PhoneOpenSsl @('x509', '-req', '-in', $csr, '-CA', $caCert, '-CAkey', $caKey, '-CAcreateserial',
                '-out', $serverCert, '-days', '7', '-sha256', '-extfile', $extensions)
        }
        Invoke-PhoneOpenSsl @('verify', '-CAfile', $caCert, '-verify_ip', $LanAddress, $serverCert)
        Invoke-PhoneDocker ($compose + @('config', '--quiet'))
        Write-Host "Prepared local certificates at $phoneDirectory (not installed or trusted)."
        Write-Host 'Install ONLY ca.crt on the test phone and enable root trust. Never transfer ca.key or server.key.'
        Write-Host "Next: ./scripts/phone-dev.ps1 -LanAddress $LanAddress -Action Start"
    } elseif ($Action -eq 'Start') {
        if (-not (Test-Path -LiteralPath (Join-Path $phoneDirectory 'server.crt'))) { throw 'Run Prepare first.' }
        Invoke-PhoneOpenSsl @('verify', '-CAfile', (Join-Path $phoneDirectory 'ca.crt'), '-verify_ip', $LanAddress, (Join-Path $phoneDirectory 'server.crt'))
        Invoke-PhoneOpenSsl @('x509', '-in', (Join-Path $phoneDirectory 'server.crt'), '-noout', '-checkend', '86400')
        if (-not (Get-NetIPAddress -AddressFamily IPv4 -IPAddress $LanAddress -ErrorAction SilentlyContinue)) {
            throw 'The supplied LAN address is not assigned to this computer.'
        }
        Invoke-PhoneDocker ($compose + @('config', '--quiet'))
        Invoke-PhoneDocker ($compose + @('up', '-d', '--wait', 'postgres', 'floci'))
        Invoke-PhoneDocker ($compose + @('run', '--rm', 'storage-init'))
        Invoke-PhoneDocker ($compose + @('build', 'api', 'worker', 'web'))
        Invoke-PhoneDocker ($compose + @('run', '--rm', '--no-deps', 'api', 'alembic', 'upgrade', 'head'))
        Invoke-PhoneDocker ($compose + @('up', '-d', '--wait', 'api', 'worker', 'web', 'phone-gateway'))
        Write-Host "Phone URL: https://${LanAddress}:8443 (trust the CA first)."
        Write-Host "App 8443 and multipart upload 9443 bind only $env:PHONE_LISTEN_IP. No firewall changes were made."
        if ($LoopbackOnly) { Write-Host 'Loopback verification mode: the phone cannot connect yet.' }
    } elseif ($Action -eq 'Check') {
        Invoke-PhoneDocker ($compose + @('config', '--quiet'))
        Invoke-PhoneDocker ($compose + @('exec', '-T', 'phone-gateway', 'nginx', '-t'))
        Invoke-PhoneDocker ($compose + @('ps'))
    } elseif ($Action -eq 'Smoke') {
        # Real TLS/HTTP via the gateway; generated bytes are aborted, never completed/ingested.
        # Public CA certificate only. Tokens and signed URLs stay inside this short-lived process.
        $caPem = Get-Content -LiteralPath (Join-Path $phoneDirectory 'ca.crt') -Raw
        $smokeCode = @'
import http.client,json,ssl,socket,sys,urllib.parse,uuid
host=sys.argv[1]
ctx=ssl.create_default_context(cadata=sys.argv[2])
def req(port,method,path,body=None,token=None,headers=None):
    conn=http.client.HTTPSConnection(host,port,context=ctx,timeout=20)
    conn._create_connection=lambda address,timeout,source_address=None:socket.create_connection(('phone-gateway',port),timeout,source_address)
    hs=dict(headers or {})
    if token:hs['Authorization']='Bearer '+token
    if isinstance(body,dict):body=json.dumps(body).encode();hs['Content-Type']='application/json'
    conn.request(method,path,body=body,headers=hs)
    res=conn.getresponse();data=res.read();out=(res.status,{k.lower():v for k,v in res.getheaders()},data);conn.close();return out
for path in ['/','/health','/ready']:
    assert req(8443,'GET',path)[0]==200,'App/health/readiness failed'
assert req(8443,'GET','/api/v1/operator/budgets')[0]==404,'Operator route exposed'
assert req(8443,'GET','/api/v1/webhooks/stripe')[0]==404,'Webhook route exposed'
for method in ['GET','HEAD','POST']:
    assert req(9443,method,'/upclose-phone/raw-denial-probe')[0]==403,'Emulator method exposed'
status,_,data=req(8443,'POST','/api/v1/sessions',{'purpose':'buyer','consent_version':'upclose-prototype-v1','consent':True})
assert status==201,'Session creation failed (phone fixture rate limit may need a new test day/project)'
session=json.loads(data);token=session['session_token'];sid=session['session_id']
try:
    status,_,data=req(8443,'POST',f'/api/v1/sessions/{sid}/parts',{'part_no':1,'content_type':'video/mp4','content_length':32},token,{'Idempotency-Key':str(uuid.uuid4())})
    assert status==201,'Upload creation failed'
    upload=json.loads(data)['upload_id']
    status,_,data=req(8443,'POST',f'/api/v1/uploads/{upload}/urls',{'part_numbers':[1]},token)
    assert status==200,'Piece signing failed'
    item=json.loads(data)['items'][0];url=urllib.parse.urlsplit(item['url'])
    assert url.scheme=='https' and url.hostname==host and url.port==9443,'Mixed-content or wrong upload origin'
    status,headers,_=req(9443,'OPTIONS',url.path+'?'+url.query,headers={'Origin':f'https://{host}:8443','Access-Control-Request-Method':'PUT','Access-Control-Request-Headers':'content-type'})
    assert status in (200,204) and headers.get('access-control-allow-origin')==f'https://{host}:8443','CORS preflight failed'
    status,headers,_=req(9443,'PUT',url.path+'?'+url.query,bytes(range(32)),headers={**item['headers'],'Origin':f'https://{host}:8443'})
    assert status==200 and 'etag' in headers,'TLS signed PUT failed'
    assert 'etag' in headers.get('access-control-expose-headers','').lower(),'ETag not exposed to browser'
    status,_,data=req(8443,'GET',f'/api/v1/uploads/{upload}',token=token)
    assert status==200,'Piece listing failed'
    pieces=json.loads(data)['pieces'];assert len(pieces)==1 and pieces[0]['size_bytes']==32,'Wrong accepted piece'
    assert req(8443,'DELETE',f'/api/v1/uploads/{upload}',token=token)[0]==204,'Upload abort failed'
finally:
    assert req(8443,'POST',f'/api/v1/sessions/{sid}/delete-media',{},token)[0]==202,'Smoke session deletion failed'
print('PASS: trusted TLS app/API; restricted operator/webhook/storage routes; bearer upload signing; HTTPS CORS+32-byte PUT/ETag/list; abort+delete202. Synthetic bytes, no inference.')
'@
        Invoke-PhoneDocker ($compose + @('exec', '-T', 'api', 'python', '-c', $smokeCode, $LanAddress, $caPem))
    } elseif ($Action -eq 'Stop') {
        Invoke-PhoneDocker ($compose + @('down', '--remove-orphans'))
        Write-Host 'Phone stack stopped; phone-project data and certificates retained.'
    } elseif ($Action -eq 'Remove') {
        Invoke-PhoneDocker ($compose + @('down', '--volumes', '--remove-orphans'))
        $resolvedTarget = [System.IO.Path]::GetFullPath($phoneDirectory)
        $expectedTarget = [System.IO.Path]::GetFullPath((Join-Path $repoRoot '.tools/phone-dev'))
        if ($resolvedTarget -ne $expectedTarget -or -not $resolvedTarget.StartsWith($repoRoot + [System.IO.Path]::DirectorySeparatorChar)) {
            throw 'Refusing certificate removal outside the designated workspace directory.'
        }
        if (Test-Path -LiteralPath $resolvedTarget) { Remove-Item -LiteralPath $resolvedTarget -Recurse -Force }
        Write-Host 'Disposable phone-project data and certificates removed. Remove the CA profile/root trust on the phone manually.'
    }
} finally {
    $env:PATH = $oldPath
    $env:PHONE_BIND_IP = $oldBind
    $env:PHONE_TLS_DIR = $oldTls
    $env:PHONE_LISTEN_IP = $oldListen
}
