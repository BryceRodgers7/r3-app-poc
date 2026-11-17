param([int]$AppPid, [int]$VlcPid, [int]$Seconds = 30, [string]$Label = "sample")
# Read-only sampler: process + per-thread CPU, system CPU, GPU engines. Touches nothing in the app.
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices; using System.Text;
public static class Thr {
  [DllImport("kernel32.dll")] public static extern IntPtr OpenThread(uint a, bool i, uint id);
  [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
  [DllImport("kernel32.dll")] public static extern int GetThreadDescription(IntPtr h, out IntPtr d);
  [DllImport("kernel32.dll")] public static extern bool GetThreadTimes(IntPtr h, out long c, out long e, out long k, out long u);
  public static string Desc(uint tid) {
    IntPtr h = OpenThread(0x0800 /*QUERY_LIMITED*/, false, tid); if (h == IntPtr.Zero) return "";
    try { IntPtr p; if (GetThreadDescription(h, out p) != 0 || p == IntPtr.Zero) return ""; return Marshal.PtrToStringUni(p); } finally { CloseHandle(h); }
  }
  public static double CpuSec(uint tid) {
    IntPtr h = OpenThread(0x0800, false, tid); if (h == IntPtr.Zero) return -1;
    try { long c,e,k,u; if (!GetThreadTimes(h, out c, out e, out k, out u)) return -1; return (k+u)/1e7; } finally { CloseHandle(h); }
  }
}
"@
function Snap($pid_) {
  $h = @{}
  try { (Get-Process -Id $pid_ -ErrorAction Stop).Threads | % { $h[[uint32]$_.Id] = [Thr]::CpuSec([uint32]$_.Id) } } catch {}
  $h
}
$procApp = Get-Process -Id $AppPid; $procVlc = Get-Process -Id $VlcPid
$a0 = Snap $AppPid; $app0 = $procApp.TotalProcessorTime.TotalSeconds; $vlc0 = $procVlc.TotalProcessorTime.TotalSeconds
$names = @{}; foreach ($t in $a0.Keys) { $names[$t] = [Thr]::Desc($t) }
$logStart = (Get-Content C:\SportsReplay\logs\app.log | Measure-Object -Line).Lines
$t0 = Get-Date
$sys = Get-Counter '\Processor(_Total)\% Processor Time' -SampleInterval 1 -MaxSamples $Seconds
$gpu = $null
try { $gpu = Get-Counter '\GPU Engine(*)\Utilization Percentage' -SampleInterval 1 -MaxSamples 3 } catch {}
$dt = ((Get-Date) - $t0).TotalSeconds
$a1 = Snap $AppPid; $procApp.Refresh(); $procVlc.Refresh()
$appCpu = ($procApp.TotalProcessorTime.TotalSeconds - $app0) / $dt
$vlcCpu = ($procVlc.TotalProcessorTime.TotalSeconds - $vlc0) / $dt
$sysAvg = ($sys.CounterSamples | Measure-Object CookedValue -Average).Average
"=== $Label  ($([math]::Round($dt)) s) ==="
"system CPU avg {0:N1}%   app {1:N2} cores ({2:N1}% of 8)   vlc {3:N2} cores ({4:N1}% of 8)   app WS {5:N0} MB" -f $sysAvg,$appCpu,($appCpu/8*100),$vlcCpu,($vlcCpu/8*100),($procApp.WorkingSet64/1MB)
"--- app threads, CPU cores used (top 18) ---"
$rows = foreach ($t in $a1.Keys) { if ($a0.ContainsKey($t)) { $d = ($a1[$t]-$a0[$t])/$dt; if ($d -gt 0.005) { $n = $names[$t]; if (-not $n) { $n = [Thr]::Desc($t) }; [pscustomobject]@{ Tid=$t; Cores=[math]::Round($d,3); Name=$n } } } }
$rows | Sort-Object Cores -Descending | Select-Object -First 18 | ft -auto
"thread cpu sum {0:N2} cores" -f (($rows | Measure-Object Cores -Sum).Sum)
if ($gpu) {
  "--- GPU engines (avg of 3 s), only app/vlc/total by engine type ---"
  $gpu.CounterSamples | ? { $_.CookedValue -gt 0.5 } | % {
    if ($_.InstanceName -match 'pid_(\d+).*engtype_(\w+)') { [pscustomobject]@{ Pid=$Matches[1]; Engine=$Matches[2]; Util=$_.CookedValue } }
  } | Group-Object Pid,Engine | % { [pscustomobject]@{ Pid=$_.Group[0].Pid; Engine=$_.Group[0].Engine; Util=[math]::Round(($_.Group | Measure-Object Util -Sum).Sum/3,1) } } |
    Sort-Object Util -Descending | Select-Object -First 12 | ft -auto
}
"--- app.log telemetry during window (last values) ---"
Get-Content C:\SportsReplay\logs\app.log | Select-Object -Skip $logStart | ? { $_ -match 'telemetry feed_id|replay tick|Replay clock|health_event' } | Select-Object -Last 12 | % { $_.Substring(11,[Math]::Min(200,$_.Length-11)) }
