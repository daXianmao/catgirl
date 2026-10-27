# 铃洛 · 家里笔记本看门狗
# 由任务计划程序每 3 分钟调用一次，做三件事：
#   1. 反向隧道（ssh -R）没在跑 -> 拉起来
#   2. QQ 进程根本不在 -> 拉起 NapCat
#   3. 进程在但 3001 不监听（假死 / 掉登录）-> 连续两次确认后重启，30 分钟冷却免得打断扫码
$ErrorActionPreference = "SilentlyContinue"

$BinDir   = "C:\Lingdang"      # 脚本和日志都放这，路径不要带空格和中文
$NapDir   = "C:\NapCat"
$Port     = 3001
$PidFile  = Join-Path $BinDir "napcat_launcher.pid"
$MissFile = Join-Path $BinDir "napcat_miss.count"
$LastFile = Join-Path $BinDir "napcat_last_restart.txt"
$LogFile  = Join-Path $BinDir "watchdog.log"

function Write-Log([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "MM-dd HH:mm:ss"), $msg
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    # 只留最近 500 行，免得日志越滚越大把盘吃满
    $all = @(Get-Content $LogFile -Encoding UTF8)
    if ($all.Count -gt 500) {
        $all[-500..-1] | Set-Content -Path $LogFile -Encoding UTF8
    }
}

function Test-Port([string]$target, [int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($target, $port)
        return ($task.Wait(1500) -and $client.Connected)
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Test-LauncherAlive {
    if (-not (Test-Path $PidFile)) { return $false }
    $raw = (Get-Content $PidFile -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($raw)) { return $false }
    $procId = 0
    if (-not [int]::TryParse($raw, [ref]$procId)) { return $false }
    return [bool](Get-Process -Id $procId -ErrorAction SilentlyContinue)
}

function Start-Launcher {
    $bat = Join-Path $NapDir "napcat_start.bat"
    if (-not (Test-Path $bat)) {
        Write-Log "找不到 $bat，检查 NapCat 装在哪"
        return
    }
    $proc = Start-Process -FilePath $bat -WindowStyle Minimized -PassThru
    Set-Content -Path $PidFile -Value $proc.Id -Encoding ASCII
    Write-Log ("拉起 NapCat，启动器 pid=" + $proc.Id)
}

# ---------------------------------------------------------------- 1. 反向隧道
# 这台机器上除了本隧道没别的东西用 ssh，按进程名判断足够了
if (-not (Get-Process ssh -ErrorAction SilentlyContinue)) {
    $bat = Join-Path $BinDir "tunnel_start.bat"
    if (Test-Path $bat) {
        Write-Log "隧道没在跑，拉起 tunnel_start.bat"
        Start-Process -FilePath $bat -WindowStyle Minimized
    } else {
        Write-Log "找不到 $bat"
    }
}

# ---------------------------------------------------------------- 2. 一切正常
if (Test-Port "127.0.0.1" $Port) {
    if (Test-Path $MissFile) { Set-Content -Path $MissFile -Value 0 -Encoding ASCII }
    exit 0
}

# ---------------------------------------------------------------- 3. 3001 不通
$qq = Get-Process QQ -ErrorAction SilentlyContinue
if (-not $qq -and -not (Test-LauncherAlive)) {
    Write-Log "QQ 进程不在，直接拉起"
    Start-Launcher
    exit 0
}

# 进程在但不监听：可能刚启动还没起服务，也可能掉登录了 —— 连续两次才动手
$miss = 0
if (Test-Path $MissFile) {
    [void][int]::TryParse((Get-Content $MissFile -Raw).Trim(), [ref]$miss)
}
$miss = $miss + 1
Set-Content -Path $MissFile -Value $miss -Encoding ASCII
if ($miss -lt 2) {
    Write-Log ("3001 不通，第 {0} 次，先观察" -f $miss)
    exit 0
}

# 冷却：30 分钟内已经重启过就不再动，避免把等你扫码的窗口杀掉
$last = [datetime]::MinValue
if (Test-Path $LastFile) {
    [void][datetime]::TryParse((Get-Content $LastFile -Raw).Trim(), [ref]$last)
}
if (((Get-Date) - $last).TotalMinutes -lt 30) {
    Write-Log ("3001 仍不通，但 {0} 刚重启过，冷却中" -f $last)
    exit 0
}

Write-Log "判定 NapCat 假死/掉登录，重启 QQ"
Stop-Process -Name QQ -Force
Stop-Process -Name node -Force
Start-Sleep -Seconds 8
Start-Launcher
Set-Content -Path $LastFile -Value (Get-Date -Format "s") -Encoding ASCII
Set-Content -Path $MissFile -Value 0 -Encoding ASCII
