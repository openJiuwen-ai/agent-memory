$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

function Test-RedisService {
    $client = $null
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $connect = $client.ConnectAsync("127.0.0.1", 6379)
        if (-not $connect.Wait(2000) -or -not $client.Connected) { return $false }
        $stream = $client.GetStream()
        $stream.ReadTimeout = 2000
        $payload = [System.Text.Encoding]::ASCII.GetBytes("*1`r`n`$4`r`nPING`r`n")
        $stream.Write($payload, 0, $payload.Length)
        $buffer = New-Object byte[] 64
        $count = $stream.Read($buffer, 0, $buffer.Length)
        $reply = [System.Text.Encoding]::ASCII.GetString($buffer, 0, $count)
        return $reply.StartsWith("+PONG")
    } catch {
        return $false
    } finally {
        if ($null -ne $client) { $client.Dispose() }
    }
}

function Test-HttpService([string]$Uri) {
    try {
        $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

Set-Location $RepoRoot
& (Join-Path $PSScriptRoot "setup.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$Compose = "evaluation\environment\docker-compose.yml"
& docker compose -f $Compose down --volumes --remove-orphans
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$Services = @()
if (Test-RedisService) {
    Write-Host "Using existing Redis at 127.0.0.1:6379."
} else {
    $Services += "redis"
}
if (Test-HttpService "http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=2s") {
    Write-Host "Using existing Elasticsearch at 127.0.0.1:9200."
} else {
    $Services += "elasticsearch"
}
if (Test-HttpService "http://127.0.0.1:9091/healthz") {
    Write-Host "Using existing Milvus at 127.0.0.1:19530."
} else {
    $Services += "milvus"
}
if ($Services.Count -gt 0) {
    & docker compose -f $Compose up -d --wait @Services
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$env:REDIS_URL = "redis://127.0.0.1:6379/0"
$env:ES_HOSTS = "http://127.0.0.1:9200"
$env:MILVUS_URI = "http://127.0.0.1:19530"
$env:MEM2_MILVUS_URI = "http://127.0.0.1:19530"
& $Python -m evaluation --config evaluation\config.yml
exit $LASTEXITCODE
