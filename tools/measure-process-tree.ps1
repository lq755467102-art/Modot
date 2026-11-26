param(
    [Parameter(Mandatory)][int[]]$RootProcessId,
    [ValidateRange(1, 1000)][int]$Samples = 5,
    [ValidateRange(1, 60)][int]$IntervalSeconds = 2
)
$ErrorActionPreference = 'Stop'
$roots = @{}
foreach ($rootId in $RootProcessId) {
    $roots[$rootId] = Get-Process -Id $rootId
}
$rows = for ($sample = 1; $sample -le $Samples; $sample++) {
    $processes = Get-CimInstance Win32_Process -Property ProcessId,ParentProcessId,Name,CreationDate
    $counters = @{}
    Get-CimInstance Win32_PerfRawData_PerfProc_Process -Property IDProcess,WorkingSetPrivate,WorkingSet,PrivateBytes |
        ForEach-Object { $counters[[int]$_.IDProcess] = $_ }
    $children = $processes | Group-Object -Property ParentProcessId -AsHashTable -AsString
    foreach ($rootId in $RootProcessId) {
        $root = $roots[$rootId]
        if ((Get-Process -Id $rootId).StartTime -ne $root.StartTime) { throw 'Application exited or PID changed' }
        $seen = [Collections.Generic.HashSet[int]]::new()
        $queue = [Collections.Generic.Queue[int]]::new()
        $queue.Enqueue($rootId)
        while ($queue.Count) {
            $parent = $queue.Dequeue()
            if (!$seen.Add($parent)) { continue }
            foreach ($child in $children[[string]$parent]) {
                if ($null -eq $child -or $child.ProcessId -eq $PID -or $child.ProcessId -in $RootProcessId) { continue }
                if ($child.CreationDate -lt $root.StartTime) { continue }
                $queue.Enqueue([int]$child.ProcessId)
            }
        }
        $live = @($seen | ForEach-Object { if ($counters.ContainsKey($_)) { $counters[$_] } })
        $shell = @($processes | Where-Object { $_.Name -eq ($root.ProcessName + '.exe') -and $seen.Contains([int]$_.ProcessId) } |
            ForEach-Object { if ($counters.ContainsKey([int]$_.ProcessId)) { $counters[[int]$_.ProcessId] } })
        [pscustomobject]@{
            sample = $sample
            rootProcessId = $rootId
            processes = $live.Count
            sameExecutableProcesses = $shell.Count
            sameExecutablePrivateWorkingSetBytes = [long](($shell | Measure-Object WorkingSetPrivate -Sum).Sum)
            privateWorkingSetBytes = [long](($live | Measure-Object WorkingSetPrivate -Sum).Sum)
            workingSetBytes = [long](($live | Measure-Object WorkingSet -Sum).Sum)
            privateBytes = [long](($live | Measure-Object PrivateBytes -Sum).Sum)
        }
    }
    if ($sample -lt $Samples) { Start-Sleep -Seconds $IntervalSeconds }
}
ConvertTo-Json -InputObject @($rows)
