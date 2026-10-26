param([Parameter(Mandatory=$true)][string[]]$Paths)
$ErrorActionPreference='Stop'
$taskRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskVersion=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')+'.'+[Guid]::NewGuid().ToString('N').Substring(0,8)
$taskResults=@()
foreach($taskRelative in $Paths){
    $taskSource=[IO.Path]::GetFullPath((Join-Path $taskRoot $taskRelative))
    $taskCheck=[IO.Path]::GetRelativePath($taskRoot,$taskSource)
    if([IO.Path]::IsPathRooted($taskCheck) -or ($taskCheck.Split([IO.Path]::DirectorySeparatorChar) -contains '..')){throw 'Backup target is outside project'}
    $taskItem=Get-Item -LiteralPath $taskSource -Force -ErrorAction Stop
    if($taskItem.PSIsContainer -or ($taskItem.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'Backup source must be a regular file'}
    $taskAncestor=$taskItem.Directory
    while($null -ne $taskAncestor){
        if($taskAncestor.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Linked source ancestor'}
        $taskAncestor=$taskAncestor.Parent
    }
    $taskHistory=Join-Path $taskItem.DirectoryName 'history'
    if(-not(Test-Path -LiteralPath $taskHistory)){[void][IO.Directory]::CreateDirectory($taskHistory)}
    $taskHistoryItem=Get-Item -LiteralPath $taskHistory -Force
    if(-not $taskHistoryItem.PSIsContainer -or ($taskHistoryItem.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'Unsafe history directory'}
    $taskVersionDir=Join-Path $taskHistory $taskVersion
    if(-not(Test-Path -LiteralPath $taskVersionDir)){[void][IO.Directory]::CreateDirectory($taskVersionDir)}
    if((Get-Item -LiteralPath $taskVersionDir -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Linked version directory'}
    $taskBackup=Join-Path $taskVersionDir $taskItem.Name
    if(Test-Path -LiteralPath $taskBackup){throw 'History collision'}
    $taskHash=(Get-FileHash -LiteralPath $taskSource -Algorithm SHA256).Hash
    $taskAcl=Get-Acl -LiteralPath $taskSource
    $taskRules=@($taskAcl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | ForEach-Object {
        '{0}|{1}|{2}|{3}|{4}' -f $_.IdentityReference.Value,$_.AccessControlType,$_.FileSystemRights,$_.InheritanceFlags,$_.PropagationFlags
    } | Sort-Object)
    Copy-Item -LiteralPath $taskSource -Destination $taskBackup -ErrorAction Stop
    $taskAcl.SetAccessRuleProtection($true,$true)
    Set-Acl -LiteralPath $taskBackup -AclObject $taskAcl -ErrorAction Stop
    $taskCopiedRules=@((Get-Acl -LiteralPath $taskBackup).GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | ForEach-Object {
        '{0}|{1}|{2}|{3}|{4}' -f $_.IdentityReference.Value,$_.AccessControlType,$_.FileSystemRights,$_.InheritanceFlags,$_.PropagationFlags
    } | Sort-Object)
    if(($taskRules -join "`n") -ne ($taskCopiedRules -join "`n")){throw 'Backup permissions mismatch'}
    if((Get-FileHash -LiteralPath $taskSource -Algorithm SHA256).Hash -ne $taskHash -or (Get-FileHash -LiteralPath $taskBackup -Algorithm SHA256).Hash -ne $taskHash){throw 'Concurrent source change or backup checksum mismatch'}
    $taskResults+=[pscustomobject]@{Source=$taskSource;Backup=$taskBackup;SHA256=$taskHash}
}
$taskResults | ConvertTo-Json -Compress
