$ErrorActionPreference = 'Stop'
try {
  # Case-insensitive -match follows the thread culture, under which (tr-TR,
  # az) 'I' does not match 'i', while PowerShell resolves command, variable,
  # member and drive names without regard to culture.
  [System.Threading.Thread]::CurrentThread.CurrentCulture = [System.Globalization.CultureInfo]::InvariantCulture
  $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
  if ($null -eq $request.source -or $request.source -isnot [string]) { throw 'bad request' }
  $tokens = $null; $errors = $null
  $tParser = [System.Management.Automation.Language.Parser]
  function L([string]$n) { $tParser.Assembly.GetType("System.Management.Automation.Language.$n") }
  $ast = $tParser::ParseInput($request.source, [ref]$tokens, [ref]$errors)
  if ($errors -and $errors.Count) { throw 'parse error' }
  if ($request.source.Length -gt 262144) { throw 'input too large' }
  $tStr = L StringConstantExpressionAst; $tParam = L CommandParameterAst; $tSb = L ScriptBlockExpressionAst
  $tVar = L VariableExpressionAst; $tPipe = L PipelineAst; $tType = L TypeExpressionAst
  $tArr = L ArrayLiteralAst; $tMem = L MemberExpressionAst; $tInvMem = L InvokeMemberExpressionAst
  $tCmdExpr = L CommandExpressionAst; $tConst = L ConstantExpressionAst; $tTok = L TokenKind
  $tWild = [System.Management.Automation.WildcardPattern]; $ord = [StringComparison]::Ordinal
  function Find([object]$r, [type]$t, [bool]$d = $true) { ,@($r.FindAll({ param($n) $n -is $t }, $d)) }
  function PN($p) { $p.ParameterName.ToLowerInvariant() }
  function Pre([string]$p, [string[]]$xs, [int]$m = 1) {
    if ($p.Length -lt $m) { return $false }
    foreach ($x in $xs) { if ($x.StartsWith($p, $ord)) { return $true } }
    return $false
  }
  function Literal([object]$node) { $node -is $tStr -or $node -is $tConst -or $node -is $tSb }
  function Text([object]$node) { if (Literal $node) { [string]$node.Value } else { $null } }
  # Parameter tables mirror Get-Command metadata for Windows PowerShell 5.1
  # and PowerShell 7 separately: the inspecting host need not be the host
  # that runs the command, and a token can bind differently on each (5.1
  # Start-Process has no WhatIf, so -wi is a prefix of WindowStyle there).
  # Kinds: switch (no operand), value (one operand), plus command-specific
  # operand kinds.  A token that is unknown, ambiguous, or of a different
  # kind on either version throws.
  $common51 = @{}
  foreach ($k in 'verbose','vb','debug','db') { $common51[$k] = 'switch' }
  foreach ($k in 'erroraction','ea','warningaction','wa','informationaction','infa',
      'errorvariable','ev','warningvariable','wv','informationvariable','iv','outvariable','ov',
      'outbuffer','ob','pipelinevariable','pv') { $common51[$k] = 'value' }
  $common7 = $common51.Clone()
  foreach ($k in 'progressaction','proga') { $common7[$k] = 'value' }
  function Spec([hashtable] $common, [string] $groups) {
    # $groups: 'kind:name,name;kind:name'
    $t = $common.Clone()
    foreach ($group in $groups.Split(';')) {
      $pair = $group.Split(':')
      foreach ($n in $pair[1].Split(',')) { $t[$n] = $pair[0] }
    }
    return $t
  }
  function Specs([string] $both, [string] $only51, [string] $only7) {
    return ,@((Spec $common51 ($both + $only51)), (Spec $common7 ($both + $only7)))
  }
  $should = ';switch:whatif,wi,confirm,cf'
  $tx = ';switch:usetransaction,usetx'
  $startBoth = 'file:filepath,pspath;args:argumentlist,args;switch:loaduserprofile,lup,nonewwindow,nnw,passthru,wait,usenewenvironment;' +
      'value:credential,runas,workingdirectory,redirectstandarderror,rse,redirectstandardinput,rsi,redirectstandardoutput,rso,verb,windowstyle'
  $startParams = Specs $startBoth '' (';file:path;value:environment' + $should)
  # Item writers: 'path' fills the positional Path slot, 'filter' narrows it,
  # 'dest' is a same-provider destination, 'data' is the item content.
  $pathParams = 'path:path,literalpath,pspath;filter:filter,include,exclude;value:credential'
  $contentSpec = Specs ($pathParams + ';data:value;value:encoding,stream;switch:passthru,force,nonewline' + $should) $tx ';path:lp;switch:asbytestream'
  $writerParams = @{
    'set-item' = (Specs ($pathParams + ';data:value;switch:force,passthru' + $should) $tx ';path:lp')
    'new-item' = (Specs ('path:path;filter:name;data:value,target;value:itemtype,type,credential;switch:force' + $should) $tx '')
    'set-content' = $contentSpec; 'add-content' = $contentSpec
    'copy-item' = (Specs ($pathParams + ';dest:destination;value:fromsession,tosession;switch:container,force,recurse,passthru' + $should) $tx ';path:lp')
    'move-item' = (Specs ($pathParams + ';dest:destination;switch:force,passthru' + $should) $tx ';path:lp')
    'rename-item' = (Specs ('path:path,literalpath,pspath;dest:newname;value:credential;switch:force,passthru' + $should) $tx ';path:lp')
  }
  # Positional slots in binding order; named binding removes a slot.
  $writerSlots = @{ 'set-item' = @('path','data'); 'new-item' = @('path'); 'set-content' = @('path','data'); 'add-content' = @('path','data');
    'copy-item' = @('path','dest'); 'move-item' = @('path','dest'); 'rename-item' = @('path','dest') }
  function AliasMap([string]$s) {
    $m = @{}
    foreach ($g in $s.Split(';')) { $k, $vs = $g.Split(':'); $m[$k] = $k; foreach ($v in $vs.Split(',')) { if ($v) { $m[$v] = $k } } }
    $m
  }
  $writerNames = AliasMap 'set-item:si;new-item:ni,mkdir,md;set-content:sc;add-content:ac;copy-item:copy,cp,cpi;move-item:move,mv,mi;rename-item:ren,rni'
  $addMemberParams = Specs ('type:membertype,type;value:name,value,secondvalue,inputobject,typename;' +
      'note:notepropertyname,notepropertyvalue,notepropertymembers;switch:force,passthru') '' ''
  $newObjectParams = Specs 'type:typename;com:comobject;value:argumentlist,args,property;switch:strict' '' ''
  $vf = 'name:name;filter:include,exclude'; $vt = ';data:value;value:description,option,visibility,scope;switch:force,passthru' + $should
  $variableParams = @{
    'get-variable' = (Specs ($vf + ';value:scope;switch:valueonly') '' '')
    'set-variable' = (Specs ($vf + $vt) '' ';switch:append')
    'new-variable' = (Specs ('name:name' + $vt) '' '')
    'clear-variable' = (Specs ($vf + ';value:scope;switch:force,passthru' + $should) '' '')
    'remove-variable' = (Specs ($vf + ';value:scope;switch:force' + $should) '' '')
  }
  $variableSlots = @{ 'get-variable'=@('name');'set-variable'=@('name','data');'new-variable'=@('name','data');'clear-variable'=@('name');'remove-variable'=@('name') }
  $variableNames = AliasMap 'get-variable:gv;set-variable:set,sv;new-variable:nv;clear-variable:clv;remove-variable:rv'
  $teeParams = Specs 'var:variable;value:filepath,literalpath,pspath,inputobject;switch:append' '' ';value:encoding'
  $compareParams = Specs 'prop:property;value:referenceobject,differenceobject,syncwindow,culture;switch:casesensitive,excludedifferent,includeequal,passthru' '' ''
  $icmParams = Specs ('sb:scriptblock,command;file:filepath;' +
      'switch:nonewscope,asjob,indisconnectedsession,enablenetworkaccess,hidecomputername,usessl,allowredirection,disconnected,usesharedprocess,runasadministrator;' +
      'value:computername,session,argumentlist,args,inputobject,credential,configurationname,port,throttlelimit,' +
      'sessionoption,applicationname,connectionuri,authentication,certificatethumbprint,jobname,sessionname,includeparametername,' +
      'vmid,vmname,containerid,hostname,username,keyfilepath,subsystem,sshconnection,sshtransport,connectionname') '' ''
  $state = @{ relativeItem = $false; dynamicLocation = $false; namespaces = @(); defined = @() }
  function ParamKind([string]$pn, [object[]]$tables, [string]$label) {
    $result = $null
    foreach ($table in $tables) {
      if ($table.ContainsKey($pn)) { $kind = $table[$pn] }
      else {
        $kinds = @(@($table.Keys) | Where-Object { $_.StartsWith($pn, $ord) } | ForEach-Object { $table[$_] } | Sort-Object -Unique)
        if ($kinds.Count -ne 1) { throw "unknown $label parameter" }
        $kind = $kinds[0]
      }
      if ($null -ne $result -and $result -ne $kind) { throw "version-dependent $label parameter" }
      $result = $kind
    }
    return $result
  }
  function RebindingText([string] $text) {
    # function:/alias: drives, Function::/Alias:: provider paths.
    return $text -match '(?i)(function|alias)\s*:'
  }
  function VariablePath([string] $text) {
    # variable: drive and Variable:: provider paths, however qualified.
    return $text -match '(?i)variable\s*:'
  }
  function HomePath([string] $text) {
    # A leading ~, also after a Provider:: qualifier, resolves through that
    # provider's Home, which a script can set to a path on any drive.
    return $text -match '^\s*([^:]*::)?~'
  }
  # Member names are checked where a script names a member: member access
  # and method calls (instance or static, with or without a get_/set_
  # accessor prefix), the string member-name operands of ForEach-Object,
  # Select-Object, Group-Object, Measure-Object (whose -Maximum/-Minimum
  # return the member's value), Compare-Object -Property and .ForEach() as
  # written in the text ($PSDefaultParameterValues, which could add
  # operands, is rejected), and any single-word string literal except
  # PSObject.  The match is by name, not by the object's type.  Sort-Object,
  # Where-Object and the Format-* cmdlets are not checked.
  # Engine members lead to the objects that write function:/alias: items,
  # variables, drives or the location, or that run strings as code; every
  # name containing Invoke is included.
  $engineMember = '(?i)^(get_|set_)?(executioncontext|sessionstate|sessionstateproxy|psvariable|drive|runspace|' +
      'setlocation|pushcurrentlocation|addscript|addcommand|createpipeline|createnestedpipeline|newscriptblock|' +
      'newboundscriptblock|getsteppablepipeline|expandstring|getvariablevalue|valuefrompipelinebypropertyname|' +
      'parameters|parametersets|resolveparameter|parametermetadata|\w*invoke\w*)$'
  # Reflection reaches those members without naming them: get*/find* names
  # that mention a member, property, field, method, constructor, event,
  # interface or type (GetType included), Declared*/Defined* collections,
  # metadata resolution and handles, CreateInstance/CreateDelegate, AppDomain
  # access and the PSObject adapter views.  Type literals of types outside
  # the denied families below still yield Type objects.
  $reflectionMember = '(?i)^(get_|set_)?((get|find)\w*(member|propert|field|method|constructor|event|interface|type)\w*|' +
      '(declared|defined)\w*|exportedtypes|implementedinterfaces|resolve(type|method|member|field|signature|string)|' +
      'createinstance\w*|createdelegate|makegeneric\w*|(type|method|field|module)handle|targetsite|assembly|' +
      'getdomain|currentdomain|psobject|psbase|psadapted|psextended)$'
  function MemberReason([string] $name) {
    if ($name -match $engineMember) { return 'opaque engine intrinsics' }
    if ($name -match $reflectionMember) { return 'opaque reflection' }
    return $null
  }
  # A named type is opaque when it resolves into a namespace that holds the
  # engine API, reflection, code generation, deserializers or binders that
  # read or call members by a string name.  The check covers type literals
  # (static member targets, casts, -as/-is operands, method arguments),
  # parameter and variable constraints, generic method type arguments and
  # New-Object type names.  A string that a method converts to a Type at run
  # time is checked only for an assembly qualification.  An enum or
  # exception the name resolves to, and the listed engine value types, are
  # exempt.  Types in other namespaces are not inspected further: this is a
  # denylist of known families, not a type allowlist.
  $deniedNamespace = '(?i)^(System\.(Reflection|Runtime|Linq\.Expressions|Dynamic|ComponentModel|CodeDom|Resources|' +
      'Activities|Workflow|Xaml|Windows|Configuration\.Install|Data\.Services|Messaging|IdentityModel|Text\.Json|' +
      'Xml\.(Xsl|Serialization)|Web\.(UI|Script))|System\.Management\.Automation|' +
      'Microsoft\.(PowerShell|CSharp|VisualBasic|JScript|Build)|Newtonsoft\.Json)(\.|$)'
  # CryptoConfig.CreateFromName constructs a type named by a string.
  $deniedType = '(?i)^System\.(Type|RuntimeType|Activator|AppDomain|Delegate|MulticastDelegate|RuntimeTypeHandle|' +
      'RuntimeMethodHandle|RuntimeFieldHandle|ModuleHandle|TypedReference|ArgIterator|Security\.Cryptography\.CryptoConfig)$'
  $exemptType = '(?i)^(System\.Management\.Automation\.(PSCredential|PSObject|PSCustomObject|PSReference|' +
      'SwitchParameter|WildcardPattern|ErrorRecord|SemanticVersion|Language\.NullString|Host\.\w+)|' +
      'System\.ComponentModel\.Win32Exception|System\.Runtime\.InteropServices\.(RuntimeInformation|OSPlatform|Architecture))$'
  # PowerShell 7 has type accelerators that 5.1 lacks ([pspropertyexpression]
  # reads members by wildcard name), so a short name this host cannot resolve
  # may name any type on the host that runs it.  Such a name is opaque unless
  # the script defines it or it is one of these 7-only accelerators for the
  # ordered dictionary, SemanticVersion and integer types.
  $knownShort = '(?i)^(ordered|semver|short|uint|ulong|ushort)$'
  function CheckTypeName([object] $tn, [bool] $open = $false) {
    if ($tn -is (L GenericTypeName)) {
      # The open name (List in List[int]) resolves only with its arity.
      CheckTypeName $tn.TypeName $true
      foreach ($argument in $tn.GenericArguments) { CheckTypeName $argument }
      return
    }
    if ($tn -is (L ArrayTypeName)) { CheckTypeName $tn.ElementType; return }
    # Resolving an assembly-qualified name loads the named assembly.
    if ($tn.AssemblyName) { throw 'assembly-qualified type name' }
    $name = [string]$tn.FullName
    $resolved = $null
    if (!$open) {
      try { $resolved = $tn.GetReflectionType() } catch { $resolved = $null }
      if ($null -eq $resolved -and !$name.Contains('.') -and $state.defined -notcontains $name -and $name -notmatch $knownShort) {
        throw 'unresolved type name'
      }
    }
    # The running host may resolve a name differently from this one, so the
    # name as written, under System. and under each using namespace counts.
    $names = @($name, ('System.' + $name)) + @($state.namespaces | ForEach-Object { $_ + '.' + $name })
    $allowed = $null
    if ($null -ne $resolved) {
      $names += [string]$resolved.FullName
      # An enum or exception this host resolves the name to is allowed.
      if ($resolved.IsEnum -or [System.Exception].IsAssignableFrom($resolved)) { $allowed = [string]$resolved.FullName }
    }
    foreach ($candidate in $names) {
      if ($candidate -match $exemptType -or ($null -ne $allowed -and $candidate -eq $allowed)) { continue }
      if ($candidate -match $deniedNamespace -or $candidate -match $deniedType) { throw 'opaque engine or reflection type' }
    }
  }
  function CheckTypeText([string] $text) {
    # New-Object type names use type-literal syntax; parse one and check it.
    $t = $null; $e = $null
    $parsed = $tParser::ParseInput('[' + $text + ']', [ref]$t, [ref]$e)
    $types = @($parsed.FindAll({ param($n) $n -is $tType }, $true))
    if (($e -and $e.Count) -or $types.Count -ne 1 -or $types[0].Extent.Text -ne ('[' + $text + ']')) { throw 'opaque type name' }
    CheckTypeName $types[0].TypeName
  }
  function Leaf([string] $name) {
    # Path normalization drops trailing "." components, drive-relative
    # prefixes (C:powershell.exe), NTFS ADS suffixes (::$DATA), and
    # trailing dots and spaces (powershell.exe\., pwsh.exe., script.ps1.).
    $parts = @($name.Trim() -split '[\\/]')
    $last = $parts.Count - 1
    while ($last -gt 0 -and $parts[$last] -match '^[. ]*$') { $last-- }
    $leaf = $parts[$last]
    if ($leaf -match '^[A-Za-z]:(.*)$') { $leaf = $Matches[1] }
    $colon = $leaf.IndexOf(':')
    if ($colon -ge 0) { $leaf = $leaf.Substring(0, $colon) }
    return $leaf.TrimEnd('. ')
  }
  function IsPsName([string] $name) {
    # Cover powershell, powershell_ise, pwsh, and 8.3 short names (POWERS~1.EXE).
    return (Leaf $name) -match '(?i)^(powershell(?:_ise)?|pwsh|powers~\d+)(\.(exe|cmd|bat|com))?$'
  }
  function StaticName([object] $node) {
    # Member-name operands must be literal so the names above stay visible.
    # A wildcard member name binds whichever member it matches, so any
    # wildcard is opaque.
    if ($node -is $tArr) {
      foreach ($item in $node.Elements) { StaticName $item }
      return
    }
    if ($node -is (L HashtableAst)) {
      foreach ($pair in $node.KeyValuePairs) {
        $v = $pair.Item2
        if ($v -isnot $tPipe -or $v.PipelineElements.Count -ne 1 -or
            $v.PipelineElements[0] -isnot $tCmdExpr) { throw 'opaque member name' }
        StaticName $v.PipelineElements[0].Expression
      }
      return
    }
    if ($node -is $tVar -and !$node.Splatted -and $node.IsConstantVariable()) { return }
    if (!(Literal $node)) { throw 'opaque member name' }
    if ($node -is $tSb) { return }
    $text = Text $node
    $reason = MemberReason $text
    if ($reason) { throw $reason }
    if ($tWild::ContainsWildcardCharacters($text)) { throw 'opaque member name' }
  }
  function CheckAddMember([object[]]$e) {
    $type = $null; $note = $false; $pos = @()
    for ($i=1; $i -lt $e.Count; $i++) {
      $p = $e[$i]
      if ($p -is $tVar -and $p.Splatted) { throw 'opaque extended type data' }
      if ($p -is $tParam) {
        $k = ParamKind (PN $p) $addMemberParams 'Add-Member'
        if ($k -eq 'switch') { continue }
        $v = if ($p.Argument) { $p.Argument } else { $i++; $e[$i] }
        if ($k -eq 'type') { $type = $v } elseif ($k -eq 'note') { $note = $true }
      } else { $pos += ,$p }
    }
    if ($null -eq $type -and !$note -and $pos.Count -and $pos[0] -isnot (L HashtableAst)) {
      $f = $pos[0]
      if (!(Literal $f) -or $f -is $tSb -or $null -ne ((Text $f) -as [System.Management.Automation.PSMemberTypes])) { $type = $f }
    }
    if ($null -eq $type) { return }
    $kv = if ((Literal $type) -and $type -isnot $tSb) { (Text $type) -as [System.Management.Automation.PSMemberTypes] } else { $null }
    if ($null -eq $kv -or @('NoteProperty','ScriptProperty','ScriptMethod') -notcontains [string]$kv) { throw 'opaque extended type data' }
  }
  function CheckVariable([object]$n, [object[]]$e, [string]$cmd) {
    $named = $false
    foreach ($pair in @(Operands $n $e $variableParams[$cmd] $variableSlots[$cmd] $cmd 'opaque variable name')) {
      if (@('name','filter','path') -notcontains $pair[0]) { continue }
      if ($pair[0] -ne 'filter') { $named = $true }
      foreach ($t in @(StringValues $pair[1] 'opaque variable name')) { if ($tWild::ContainsWildcardCharacters($t)) { throw 'opaque variable name' } }
    }
    if (!$named) { throw 'opaque variable name' }
  }
  function CheckNewObject([object[]]$e) {
    $type = $null; $pos = @()
    for ($i=1; $i -lt $e.Count; $i++) {
      $p = $e[$i]
      if ($p -is $tVar -and $p.Splatted) { throw 'dynamic New-Object type' }
      if ($p -is $tParam) {
        $k = ParamKind (PN $p) $newObjectParams 'New-Object'
        if ($k -eq 'switch') { continue }
        if ($k -eq 'com') { return }
        $v = if ($p.Argument) { $p.Argument } else { $i++; $e[$i] }
        if ($k -eq 'type') { $type = $v }
      } else { $pos += ,$p }
    }
    if ($null -eq $type) { if (!$pos.Count) { return }; $type = $pos[0] }
    if (!(Literal $type) -or $type -is $tSb) { throw 'dynamic New-Object type' }
    CheckTypeText (Text $type)
  }
  function MemberOperands([object[]]$e) {
    for ($i=1; $i -lt $e.Count; $i++) {
      $p = $e[$i]
      if ($p -is $tParam) {
        $in = Pre (PN $p) @('inputobject') 2
        if ($p.Argument) { if (!$in) { StaticName $p.Argument } } elseif ($in) { $i++ }
        continue
      }
      StaticName $p
    }
  }
  function CheckParam([object[]]$e, [object[]]$tables, [string]$label, [string]$want, [string]$reason) {
    for ($i=1; $i -lt $e.Count; $i++) {
      $p = $e[$i]
      if ($p -is $tVar -and $p.Splatted) { throw $reason }
      if ($p -is $tParam) {
        $k = ParamKind (PN $p) $tables $label
        if ($k -eq 'switch') { continue }
        $v = if ($p.Argument) { $p.Argument } else { $i++; $e[$i] }
        if ($k -eq $want) { if ($want -eq 'prop') { StaticName $v } else { $null = StringValues $v $reason } }
      }
    }
  }
  function ProviderPath([string]$t) {
    if (HomePath $t) { throw 'opaque home-relative path' }
    if (VariablePath $t) {
      if ($tWild::ContainsWildcardCharacters($t) -or $t -notmatch '^\s*(\w[\w.]*\\)?variable::?[\\/]?(?<name>[^\\/:]+)$' -or $Matches['name'] -match '^[.\s]*$') { throw 'opaque variable path' }
      return $true
    }
    if ($t -notmatch '^[^\\/:]+:' -and $t -notmatch '^(\\\\|//)') { $state.relativeItem = $true }
    return $false
  }
  function ItemPath([object]$n) {
    if (!(Literal $n) -or $n -is $tSb) { throw 'opaque command rebinding' }
    $t = Text $n; if (RebindingText $t) { throw 'opaque command rebinding' }; ProviderPath $t
  }
  function NamePath([object]$n) { if ((Literal $n) -and (RebindingText (Text $n))) { throw 'opaque command rebinding' } }
  function VariableName([object]$n) {
    if (!(Literal $n) -or $n -is $tSb) { throw 'opaque variable path' }
    $t = Text $n; if ($tWild::ContainsWildcardCharacters($t)) { throw 'opaque variable path' }; $null = ProviderPath $t
  }
  function Operands([object]$n, [object[]]$e, [object[]]$tables, [object[]]$slots, [string]$label, [string]$reason = 'opaque command rebinding') {
    $pl = $n.Parent
    if ($pl -is $tPipe -and !([object]::ReferenceEquals($pl.PipelineElements[0], $n))) { throw $reason }
    $pos = @(); $open = [System.Collections.ArrayList]@($slots)
    for ($i=1; $i -lt $e.Count; $i++) {
      $p = $e[$i]
      if ($p -is $tVar -and $p.Splatted) { throw $reason }
      if ($p -is $tParam) {
        $k = ParamKind (PN $p) $tables $label
        if ($k -eq 'switch') { continue }
        $v = if ($p.Argument) { $p.Argument } else { $i++; $e[$i] }
        $open.Remove($k); ,@($k, $v)
      } else { $pos += ,$p }
    }
    for ($k=0; $k -lt $pos.Count; $k++) { $kind = if ($k -lt $open.Count) { $open[$k] } else { 'path' }; ,@($kind, $pos[$k]) }
  }
  function CheckItem([object]$n, [object[]]$e, [string]$cmd) {
    $hasPath = $false; $var = $false; $names = @()
    foreach ($pair in @(Operands $n $e $writerParams[$cmd] $writerSlots[$cmd] $cmd)) {
      if ($pair[0] -eq 'path') { $hasPath = $true; if (ItemPath $pair[1]) { $var = $true } }
      elseif ($pair[0] -eq 'filter' -or $pair[0] -eq 'dest') { NamePath $pair[1]; $names += ,$pair[1] }
    }
    if (!$hasPath) { throw 'opaque item path' }
    if ($var) { foreach ($nm in $names) { VariableName $nm } }
  }
  $readerSw = 'recurse,force,name,file,directory,hidden,readonly,system,raw,wait,asbytestream,followsymlink,verbose,vb,debug,db,whatif,wi,confirm,cf,usetransaction,usetx'.Split(',')
  function CheckReader([object]$n, [object[]]$e) {
    $pl = $n.Parent
    if ($pl -is $tPipe -and !([object]::ReferenceEquals($pl.PipelineElements[0], $n))) { throw 'opaque provider path' }
    $hasPath = $false; $pk = ''
    for ($i=1; $i -lt $e.Count; $i++) {
      $p = $e[$i]
      if ($p -is $tParam) {
        $pn = PN $p
        $ip = ($pn -in @('p','lp') -or (Pre $pn @('path','literalpath','pspath') 2))
        $sw = !$ip -and (Pre $pn $readerSw)
        if ($null -eq $p.Argument) { $pk = if ($ip) { 'path' } elseif ($sw) { '' } else { 'other' }; continue }
        $p = $p.Argument; $isPath = $ip
      } else { $isPath = ($pk -ne 'other'); $pk = '' }
      if ($p -is $tVar -and !$p.Splatted -and $p.IsConstantVariable()) { continue }
      foreach ($t in @(StringValues $p 'opaque provider path')) {
        if ($isPath) { $hasPath = $true; $null = ProviderPath $t }
        else { if (HomePath $t) { throw 'opaque home-relative path' }; if (VariablePath $t) { $null = ProviderPath $t } }
      }
    }
    if (!$hasPath) { $state.relativeItem = $true }
  }
  function CheckIcm([object[]]$e) {
    for ($i=1; $i -lt $e.Count; $i++) {
      $p = $e[$i]
      if ($p -is $tVar -and $p.Splatted) { throw 'dynamic Invoke-Command scriptblock' }
      if ($p -is $tParam) {
        $k = ParamKind (PN $p) $icmParams 'Invoke-Command'
        if ($k -eq 'switch') { continue }
        $v = if ($p.Argument) { $p.Argument } else { $i++; $e[$i] }
        if ($k -eq 'sb') { if ($v -isnot $tSb) { throw 'dynamic Invoke-Command scriptblock' } }
        elseif ($k -eq 'file') { if (!(Literal $v)) { throw 'dynamic Invoke-Command file' }; throw 'external PowerShell script needs inspection' }
      } elseif ($p -isnot $tSb) { throw 'dynamic Invoke-Command scriptblock' }
    }
  }
  function CheckSbCmd([object[]]$e, [string]$ll) {
    $pos = 0
    for ($i=1; $i -lt $e.Count; $i++) {
      $p = $e[$i]
      if ($p -is $tVar -and $p.Splatted) { throw 'dynamic scriptblock command' }
      if ($p -is $tParam) {
        $pn = PN $p
        $sb = Pre $pn @('scriptblock','expression','action','filterscript','initializationscript') 2
        $fl = Pre $pn @('filepath') 2
        $pr = ($ll -match '^(where-object|\?|where)$' -and (Pre $pn @('property') 2))
        if ($sb -or $fl -or $pr) {
          $v = if ($p.Argument) { $p.Argument } else { $i++; $e[$i] }
          if ($fl) { throw 'external PowerShell script needs inspection' }
          if ($pr) { StaticName $v } elseif ($v -isnot $tSb) { throw 'dynamic scriptblock command' }
        } elseif ($null -eq $p.Argument -and $pn -notmatch '^(ascustomobject|returnresult|pass\w*|verbose|vb|debug|db|whatif|wi|confirm|cf|runas32|supportevent|forward)$') { $i++ }
        continue
      }
      if ($pos -eq 0) {
        if ($ll -match '^(where-object|\?|where)$') { if ($p -is $tStr) { StaticName $p } elseif ($p -isnot $tSb) { throw 'dynamic scriptblock command' } }
        elseif ($ll -match '^(new-module|nmo|measure-command|start-job|sajb|start-threadjob)$' -and $p -isnot $tSb) { throw 'dynamic scriptblock command' }
      }
      $pos++
    }
  }
  function CheckLocation([object]$n, [object[]]$e) {
    $pl = $n.Parent
    if ($pl -is $tPipe -and !([object]::ReferenceEquals($pl.PipelineElements[0], $n))) { $state.dynamicLocation = $true }
    $ops = 0
    for ($i=1; $i -lt $e.Count; $i++) {
      $p = $e[$i]
      if ($p -is $tParam) { if ($null -eq $p.Argument) { continue }; $p = $p.Argument }
      $ops++
      if (!(Literal $p) -or $p -is $tSb) { $state.dynamicLocation = $true }
      elseif (RebindingText (Text $p)) { throw 'opaque command rebinding' }
      elseif (VariablePath (Text $p)) { throw 'opaque variable path' }
      elseif (HomePath (Text $p)) { $state.dynamicLocation = $true }
    }
    if (!$ops) { $state.dynamicLocation = $true }
  }
  function ParseNested([string]$t, [int]$d) {
    if ($d -gt 16 -or $t.Length -gt 65536) { throw 'opaque nested command' }
    $tk = $null; $er = $null
    $ne = $tParser::ParseInput($t, [ref]$tk, [ref]$er)
    if ($er -and $er.Count) { throw 'nested parse error' }
    CheckAst $ne ($d + 1)
  }
  function CheckAst([object]$root, [int]$depth) {
    $outerDef = $state.defined
    $state.defined = @(Find $root (L TypeDefinitionAst) $false | ForEach-Object { [string]$_.Name })
    if ($root -is (L ScriptBlockAst)) {
      foreach ($u in @($root.UsingStatements)) {
        $k = [string]$u.UsingStatementKind
        if ($k -eq 'Namespace') { $state.namespaces += [string]$u.Name.Value }
        elseif ($k -ne 'Module') { throw 'using statement loads an assembly' }
      }
      if ($root.ScriptRequirements -and @($root.ScriptRequirements.RequiredAssemblies).Count) { throw '#requires loads an assembly' }
    }
    foreach ($n in (Find $root (L CommandAst))) {
      $e = @($n.CommandElements); if (!$e.Count) { continue }
      if ($e[0] -is $tStr -and $e[0].Extent.Text.Contains([char]96)) { throw 'escaped command name requires inspection' }
      $name = Text $e[0]
      if ($null -eq $name) {
        if ($n.InvocationOperator -match 'Ampersand|Dot' -and $e[0] -is $tSb) { continue }
        throw 'dynamic command name'
      }
      $leaf = Leaf $name; $ll = $leaf.ToLowerInvariant()
      if ($leaf -match '\.ps1$') { throw 'external PowerShell script needs inspection' }
      if ($n.InvocationOperator -ne $tTok::Unknown -and $n.InvocationOperator -ne 0 -and !(Literal $e[0])) { throw 'dynamic invocation' }
      if ($ll -match '^(iex|invoke-expression|add-type)$') { throw "blocked command: $leaf" }
      if ($ll -match '^(set-alias|new-alias|sal|nal|import-alias|ipal|new-psdrive|ndr|mount)$') { throw 'opaque command rebinding' }
      $w = $writerNames[$ll]; if ($w) { CheckItem $n $e $w }
      if ($ll -match '^(get-item|gi|get-childitem|gci|dir|ls|get-content|gc|cat|type)$') { CheckReader $n $e }
      $vc = $variableNames[$ll]; if ($vc) { CheckVariable $n $e $vc }
      if ($ll -match '^(tee-object|tee)$') { CheckParam $e $teeParams 'Tee-Object' 'var' 'opaque variable name' }
      if ($ll -match '^(foreach-object|%|foreach|select-object|select|group-object|group|measure-object|measure|sort-object|sort|format-(table|list|custom|wide)|ft|fl|fc|fw)$') { MemberOperands $e }
      if ($ll -match '^(compare-object|compare|diff)$') { CheckParam $e $compareParams 'Compare-Object' 'prop' 'opaque member name' }
      if ($ll -match '^(set-location|push-location|sl|cd|chdir|pushd)$') { CheckLocation $n $e }
      if ($ll -match '^(update-typedata|update-formatdata)$') { throw 'opaque extended type data' }
      if ($ll -eq 'add-member') { CheckAddMember $e }
      if ($ll -eq 'new-object') { CheckNewObject $e }
      if (IsPsName $name) { CheckLauncher $e $depth }
      if ($ll -match '^(start-process|start|saps)$') { CheckStart $e $depth }
      if ($ll -match '^(invoke-command|icm)$') { CheckIcm $e }
      if ($ll -match '^(new-module|nmo|measure-command|start-job|sajb|start-threadjob|trace-command|where-object|\?|where|register-(engine|object)event|set-psbreakpoint|sbp)$') { CheckSbCmd $e $ll }
      for ($i=1; $i -lt $e.Count; $i++) {
        $p = $e[$i]
        if ($p -is $tVar -and $p.Splatted) { throw 'opaque splatted parameters' }
        if ($p -is $tParam) {
          $pn = PN $p
          if ($pn -in @('pv','ov','ev','wv','iv') -or (Pre $pn @('pipelinevariable') 2) -or (Pre $pn @('outvariable') 4) -or
              (Pre $pn @('errorvariable') 6) -or (Pre $pn @('warningvariable') 8) -or (Pre $pn @('informationvariable') 12)) {
            $v = if ($p.Argument) { $p.Argument } elseif ($i + 1 -lt $e.Count) { $e[$i + 1] } else { throw 'opaque variable name' }
            $null = StringValues $v 'opaque variable name'
          }
        }
      }
    }
    foreach ($n in (Find $root $tVar)) {
      $p = [string]$n.VariablePath.UserPath
      if (RebindingText $p) { throw 'opaque command rebinding' }
      if ($p -match '(^|[:\\/])(executioncontext|pscmdlet)$') { throw 'opaque engine intrinsics' }
      if ($p -match '(^|[:\\/])psdefaultparametervalues$') { throw 'opaque default parameter values' }
      if (VariablePath $p) { $null = ProviderPath $p }
    }
    foreach ($n in (Find $root $tMem)) {
      if (!($n.Member -is $tStr)) { throw 'dynamic member name' }
      $m = [string]$n.Member.Value
      if ($n -is $tInvMem) {
        if ($m -match '^(invoke|invokereturnasis|invokecommand|addscript)$') { throw 'dynamic member invocation' }
        if ($n.Static -and $m -eq 'create' -and [string]$n.Expression.TypeName.FullName -match 'scriptblock') { throw 'dynamic scriptblock creation' }
        if ($m -eq 'foreach') { foreach ($a in @($n.Arguments)) { StaticName $a } }
        if ($m -eq 'where') { $wa = @($n.Arguments); if ($wa.Count -and $wa[0] -isnot $tSb) { throw 'dynamic Where scriptblock' } }
        foreach ($g in @($n.GenericTypeArguments)) { if ($null -ne $g) { CheckTypeName $g } }
      }
      if ($m -eq 'invokecommand') { throw 'dynamic InvokeCommand access' }
      $r = MemberReason $m; if ($r) { throw $r }
      if ($n.Static -and $n.Expression -isnot $tType) { throw 'dynamic static member access' }
    }
    foreach ($n in (Find $root (L ConvertExpressionAst))) {
      if ([string]$n.Type.TypeName.FullName -match '(^|\.)scriptblock(\[\])*$') { throw 'string-built scriptblock' }
    }
    foreach ($n in (Find $root (L BinaryExpressionAst))) {
      if ($n.Operator -eq $tTok::As -and $n.Right -isnot $tType) { throw 'dynamic type conversion' }
    }
    foreach ($n in ((Find $root $tType) + (Find $root (L TypeConstraintAst)))) { CheckTypeName $n.TypeName }
    foreach ($n in (Find $root (L AttributeAst))) {
      if ($n.TypeName.AssemblyName) { throw 'assembly-qualified type name' }
      foreach ($a in @($n.NamedArguments)) { if ($a.ArgumentName -eq 'valuefrompipelinebypropertyname') { throw 'opaque property-name parameter binding' } }
    }
    foreach ($n in (Find $root (L AssignmentStatementAst))) {
      foreach ($m in (Find $n.Left $tMem)) {
        if ($m.Member -is $tStr -and [string]$m.Member.Value -match '^(get_|set_)?(parameters|attributes)$') { throw 'opaque command metadata mutation' }
      }
    }
    foreach ($n in ((Find $root $tStr) + (Find $root (L ExpandableStringExpressionAst)))) {
      $t = [string]$n.Value
      if ((RebindingText $t) -or $t -match 'psdriveinfo') { throw 'opaque command rebinding' }
      if ($t -match 'executioncontext|pscmdlet') { throw 'opaque engine intrinsics' }
      if ($t -match 'psdefaultparametervalues') { throw 'opaque default parameter values' }
      if ($t.Length -le 256 -and $t -match '^\s*\w+\s*$') {
        $w = $t.Trim(); $r = MemberReason $w; if ($r -and $w -ne 'psobject') { throw $r }
      }
      if ($t -match ',\s*(version|culture|publickeytoken)\s*=') { throw 'assembly-qualified type name' }
    }
    $state.defined = $outerDef
  }
  function SwitchName([object] $node) {
    if ($node -is $tParam) { return $node.ParameterName.ToLowerInvariant() }
    if ($node -is $tStr -and
        $node.Value.Trim() -match '^[-/\u2013\u2014\u2015](.+)$') { return $Matches[1].ToLowerInvariant() }
    return ''
  }
  function IsSwitchLike([object] $node) {
    if ($node -is $tParam) { return $true }
    if ($node -is $tStr -and $node.Value.Trim() -match '^[-/\u2013\u2014\u2015]') { return $true }
    return $false
  }
  # Launcher option tables mirror interpreter_argv._POWERSHELL_PARAMETERS so
  # shortest accepted prefixes and aliases agree across Windows PowerShell 5.1
  # and PowerShell 7 (e.g., pwsh -i is boolean -Interactive, while -InputFormat
  # requires >= 3 chars or alias -if).
  $launcherSwitchCommon = @(
    @('noprofile', 3, @()),
    @('nologo', 3, @()),
    @('noninteractive', 4, @()),
    @('noexit', 3, @()),
    @('sta', 3, @()),
    @('mta', 3, @())
  )
  $launcherSwitch51 = @(
    @('sta', 2, @())
  )
  $launcherSwitchPwsh = @(
    @('interactive', 1, @()),
    @('login', 1, @()),
    @('noprofileloadtime', 17, @()),
    @('sshservermode', 4, @())
  )
  $launcherValueCommon = @(
    @('executionpolicy', 2, @('ep')),
    @('inputformat', 3, @('if')),
    @('outputformat', 1, @('of')),
    @('windowstyle', 1, @()),
    @('workingdirectory', 4, @('wd')),
    @('configurationname', 6, @())
  )
  $launcherValue51 = @(
    @('version', 1, @())
  )
  $launcherValuePwsh = @(
    @('configurationfile', 17, @()),
    @('custompipename', 14, @()),
    @('settingsfile', 8, @())
  )
  function MatchLauncherOpt([string]$pn, [object[]]$specs) {
    if ($pn -notmatch '^[a-z]+$') { return $false }
    foreach ($spec in $specs) {
      $full = $spec[0]; $minLen = $spec[1]; $aliases = $spec[2]
      if ($aliases -contains $pn) { return $true }
      if ($pn.Length -ge $minLen -and $full.StartsWith($pn, $ord)) { return $true }
    }
    return $false
  }
  function CheckLauncher([object[]]$e, [int]$depth) {
    $hostInfo = $false
    $isPwsh = (Leaf (Text $e[0])) -match '(?i)^pwsh(\.(exe|cmd|bat|com))?$'
    $switchSpecs = if ($isPwsh) { $launcherSwitchCommon + $launcherSwitchPwsh } else { $launcherSwitchCommon + $launcherSwitch51 }
    $valueSpecs = if ($isPwsh) { $launcherValueCommon + $launcherValuePwsh } else { $launcherValueCommon + $launcherValue51 }
    for ($i=1; $i -lt $e.Count; $i++) {
      $p = $e[$i]; $pn = SwitchName $p
      if ($pn -eq '-%' -or $pn -eq '-') { throw 'opaque native argument parsing' }
      if ($isPwsh -and $pn.Length -gt 1 -and $pn.Substring(0, 1) -match '[-\u2013\u2014\u2015]') {
        $pn = $pn.Substring(1)
      }
      $hasAttached = ($p -is $tParam -and $null -ne $p.Argument) -or ($pn -match '[:=]')
      $basePn = if ($pn -match '[:=]') { ($pn -split '[:=]', 2)[0] } else { $pn }
      if ($basePn -and ((Pre $basePn @('encodedcommand','encodedarguments')) -or $basePn -in @('ec','ea'))) { throw 'encoded PowerShell command' }
      if ($basePn -and ((Pre $basePn @('command','commandwithargs')) -or $basePn -eq 'cwa')) {
        if ($hasAttached) { throw 'opaque attached command operand' }
        $i++; if ($i -ge $e.Count) { throw 'missing PowerShell command' }
        if ($e[$i] -is $tSb) { return }
        $body = @()
        for (; $i -lt $e.Count; $i++) {
          if ($e[$i] -is $tParam -and !$e[$i].Argument) { $body += $e[$i].Extent.Text; continue }
          if (!(Literal $e[$i])) { throw 'opaque PowerShell command operand' }
          if ($e[$i] -is $tStr -and $e[$i].Extent.Text.Contains([char]96)) { throw 'opaque PowerShell command quoting' }
          $body += [string]$e[$i].Value
        }
        if (($body -join ' ') -eq '-') { throw 'PowerShell command reads unseen stdin' }
        ParseNested ($body -join ' ') $depth; return
      }
      if ($basePn -and (Pre $basePn @('file'))) {
        if ($hasAttached) { throw 'opaque attached PowerShell host option' }
        $i++; if ($i -ge $e.Count -or !(Literal $e[$i])) { throw 'dynamic PowerShell file' }
        throw 'external PowerShell script needs inspection'
      }
      if ($hasAttached -and $pn) { throw 'opaque attached PowerShell host option' }
      if ($pn -in @('help','h','?','-help')) { $hostInfo = $true; continue }
      if ($isPwsh -and $pn -in @('v','version','-version')) { $hostInfo = $true; continue }
      if ($pn -and (MatchLauncherOpt $pn $switchSpecs)) { continue }
      if ($pn -and (MatchLauncherOpt $pn $valueSpecs)) {
        $i++
        if ($i -ge $e.Count -or !(Literal $e[$i]) -or (IsSwitchLike $e[$i])) { throw 'opaque PowerShell host option' }
        continue
      }
      if ($pn) { throw 'unknown PowerShell host option' }
      if (!(Literal $p)) { throw 'dynamic PowerShell argument' }
      if ((Leaf ([string]$p.Value)) -match '(?i)\.ps1$') {
        if ($i + 1 -lt $e.Count) { throw 'positional PowerShell script with trailing arguments' }
        throw 'external PowerShell script needs inspection'
      }
      $body = @()
      for (; $i -lt $e.Count; $i++) {
        if (!(Literal $e[$i])) { throw 'dynamic PowerShell argument' }
        if ($e[$i] -is $tStr -and $e[$i].Extent.Text.Contains([char]96)) { throw 'opaque PowerShell command quoting' }
        $body += [string]$e[$i].Value
      }
      ParseNested ($body -join ' ') $depth; return
    }
    if (!$hostInfo) { throw 'PowerShell command reads unseen stdin' }
  }
  function StringValues([object]$n, [string]$reason = 'opaque Start-Process arguments') {
    if ($n -is $tArr) { foreach ($x in $n.Elements) { StringValues $x $reason } }
    elseif ($n -is (L ArrayExpressionAst)) {
      foreach ($s in $n.SubExpression.Statements) {
        if ($s -isnot $tPipe -or $s.PipelineElements.Count -ne 1 -or $s.PipelineElements[0] -isnot $tCmdExpr) { throw $reason }
        StringValues $s.PipelineElements[0].Expression $reason
      }
    } elseif ($n -is $tStr -or $n -is $tConst) { [string]$n.Value }
    else { throw $reason }
  }
  function CheckStart([object[]]$e, [int]$depth) {
    $file = $null; $argsNode = $null
    foreach ($p in $e) { if ($p -is $tVar -and $p.Splatted) { throw 'splatting executable operands' } }
    for ($i=1; $i -lt $e.Count; $i++) {
      $p = $e[$i]
      if ($p -is $tParam) {
        $k = ParamKind $p.ParameterName.ToLowerInvariant() $startParams 'Start-Process'
        if ($k -eq 'switch') { continue }
        $v = if ($p.Argument) { $p.Argument } else { $i++; $e[$i] }
        if ($k -eq 'file') { $file = $v } elseif ($k -eq 'args') { $argsNode = $v }
      } elseif ($null -eq $file) { $file = $p } elseif ($null -eq $argsNode) { $argsNode = $p }
    }
    if ($file -isnot $tStr) { throw 'dynamic Start-Process file' }
    if (IsPsName ([string]$file.Value)) {
      if ($null -eq $argsNode) { throw 'unseen PowerShell process input' }
      $joined = (StringValues $argsNode) -join ' '
      if ($joined.Contains([char]96)) { throw 'opaque Start-Process quoting' }
      ParseNested ("& '" + $file.Value.Replace("'", "''") + "' " + $joined) $depth
    }
  }
  CheckAst $ast 0
  if ($state.relativeItem -and $state.dynamicLocation) { throw 'opaque location-dependent item path' }
  [Console]::Out.Write((ConvertTo-Json @{ inspectable = $true; reason = 'ast fully inspectable' } -Compress))
} catch {
  [Console]::Out.Write((ConvertTo-Json @{ inspectable = $false; reason = [string]$_.Exception.Message } -Compress))
  exit 0
}
