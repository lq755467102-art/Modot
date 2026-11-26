# Sample resident and committed memory for a Windows desktop process and its descendants.
# Each output line is a JSON object. UTC midpoint timestamps can be joined to UI events.
param(
    [Parameter(Mandatory)][int]$RootProcessId,
    [ValidateRange(1, 86400)][int]$Samples = 60,
    [ValidateRange(100, 60000)][int]$IntervalMilliseconds = 1000
)
$ErrorActionPreference = 'Stop'
$identity = (Get-Process -Id $RootProcessId).StartTime
for ($sample = 0; $sample -lt $Samples; $sample++) {
    $root = Get-Process -Id $RootProcessId -ErrorAction SilentlyContinue
    if (!$root -or $root.StartTime -ne $identity) { break }
    $start = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $processes = Get-CimInstance Win32_Process -Property ProcessId,ParentProcessId,CreationDate
    $children = $processes | Group-Object ParentProcessId -AsHashTable -AsString
    $family = [Collections.Generic.HashSet[int]]::new()
    $queue = [Collections.Generic.Queue[int]]::new()
    $queue.Enqueue($RootProcessId)
    while ($queue.Count) {
        $parentId = $queue.Dequeue()
        if (!$family.Add($parentId)) { continue }
        foreach ($child in $children[[string]$parentId]) {
            if ($child -and $child.ProcessId -ne $PID -and $child.CreationDate -ge $identity) {
                $queue.Enqueue([int]$child.ProcessId)
            }
        }
    }
    $members = @(Get-CimInstance Win32_PerfRawData_PerfProc_Process -Property IDProcess,WorkingSetPrivate,WorkingSet,PrivateBytes |
        Where-Object { $family.Contains([int]$_.IDProcess) })
    $finish = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    if ($members.Count) {
        [ordered]@{
            sampledAtUtcMs = [long](($start + $finish) / 2)
            snapshotDurationMs = $finish - $start
            processes = $members.Count
            privateWorkingSetBytes = [long](($members | Measure-Object WorkingSetPrivate -Sum).Sum)
            totalWorkingSetBytes = [long](($members | Measure-Object WorkingSet -Sum).Sum)
            privateCommitBytes = [long](($members | Measure-Object PrivateBytes -Sum).Sum)
        } | ConvertTo-Json -Compress
    }
    if ($sample + 1 -lt $Samples) {
        $remaining = $IntervalMilliseconds - ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() - $start)
        Start-Sleep -Milliseconds ([Math]::Max(15, $remaining))
    }
}
