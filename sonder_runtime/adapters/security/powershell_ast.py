"""Fail-closed, read-only PowerShell AST inspection.

The helper process only parses text with PowerShell's native parser.  The
candidate is supplied as JSON on stdin; it is never interpolated into the
helper script and the helper has no execution path.

An inspectable verdict only means that none of the helper's checks matched.
They look for dynamic code and command names; the rebinding spellings and
item writers it recognises (function:/alias: paths and variables, alias and
drive cmdlets, item writers whose path operand is not a visible literal);
the engine and reflection member names and type families it lists, and
member names that the member-reading cmdlets it lists receive from
expressions (Compare-Object -Property included); parameter attributes that
bind by property name (ValueFromPipelineByPropertyName); $ExecutionContext,
$PSCmdlet and $PSDefaultParameterValues named as a variable (any scope or
provider spelling) or inside a string literal; *-Variable cmdlets given a
computed, wildcard, piped or missing name, and Tee-Object -Variable given a
computed or splatted name; Get-Item, Get-ChildItem and Get-Content given a computed or piped path;
item readers and writers given a variable: path that is not one literal
variable name, or a home-relative (~) path, which follows a provider's
settable Home; relative item paths in text that also changes the location
to a computed, home-relative or missing path; and short type names this
host cannot resolve.  The checks run under the invariant culture.  This is
a denylist of recognised routes, not proof that the text cannot rebind a
command or reach engine objects in some other way, and scripts, modules,
data files or programs the text runs or loads from disk are not inspected.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from threading import Lock
from collections import OrderedDict
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class PowerShellInspection:
    inspectable: bool
    reason: str

    def __bool__(self) -> bool:
        return self.inspectable


_MAX_SOURCE: Final = 256 * 1024
# Windows cold starts on loaded hosts exceeded five seconds during qualification.
# Only PowerShell execution calls pay this bounded cost; syntax cache hits do not.
_TIMEOUT: Final = 8.0
_CACHE_SIZE: Final = 128
_positive_cache: "OrderedDict[tuple[str, int, int, str], PowerShellInspection]" = OrderedDict()
_cache_lock = Lock()

# Keep this literal and independent of the candidate.  ParseInput constructs
# an AST and does not invoke commands; all policy decisions are AST based.
_HELPER: Final = r'''
$ErrorActionPreference = 'Stop'
try {
  # Case-insensitive -match follows the thread culture, under which (tr-TR,
  # az) 'I' does not match 'i', while PowerShell resolves command, variable,
  # member and drive names without regard to culture.
  [System.Threading.Thread]::CurrentThread.CurrentCulture = [System.Globalization.CultureInfo]::InvariantCulture
  $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
  if ($null -eq $request.source -or $request.source -isnot [string]) { throw 'bad request' }
  $tokens = $null; $errors = $null
  $ast = [System.Management.Automation.Language.Parser]::ParseInput($request.source, [ref]$tokens, [ref]$errors)
  if ($errors -and $errors.Count) { throw 'parse error' }
  if ($request.source.Length -gt 262144) { throw 'input too large' }
  $tStr = [System.Management.Automation.Language.StringConstantExpressionAst]
  $tParam = [System.Management.Automation.Language.CommandParameterAst]
  $tSb = [System.Management.Automation.Language.ScriptBlockExpressionAst]
  $tVar = [System.Management.Automation.Language.VariableExpressionAst]
  $tPipe = [System.Management.Automation.Language.PipelineAst]
  $tType = [System.Management.Automation.Language.TypeExpressionAst]

  function Literal([object] $node) {
    return ($node -is $tStr -or
      $node -is [System.Management.Automation.Language.ConstantExpressionAst] -or
      $node -is $tSb)
  }
  function Text([object] $node) {
    if (Literal $node) { return [string]$node.Value }
    return $null
  }
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
  $writerParams = @{
    'set-item' = (Specs ($pathParams + ';data:value;switch:force,passthru' + $should) $tx ';path:lp')
    'new-item' = (Specs ('path:path;filter:name;data:value,target;value:itemtype,type,credential;switch:force' + $should) $tx '')
    'set-content' = (Specs ($pathParams + ';data:value;value:encoding,stream;switch:passthru,force,nonewline' + $should) $tx ';path:lp;switch:asbytestream')
    'copy-item' = (Specs ($pathParams + ';dest:destination;value:fromsession,tosession;switch:container,force,recurse,passthru' + $should) $tx ';path:lp')
    'move-item' = (Specs ($pathParams + ';dest:destination;switch:force,passthru' + $should) $tx ';path:lp')
    'rename-item' = (Specs ('path:path,literalpath,pspath;dest:newname;value:credential;switch:force,passthru' + $should) $tx ';path:lp')
  }
  # Positional slots in binding order; named binding removes a slot.
  $writerSlots = @{ 'set-item' = @('path','data'); 'new-item' = @('path'); 'set-content' = @('path','data');
    'copy-item' = @('path','dest'); 'move-item' = @('path','dest'); 'rename-item' = @('path','dest') }
  # mkdir (alias md) is the built-in function that forwards its Path, Name
  # and Value to New-Item, so it writes function: items the same way.
  $writerNames = @{ 'set-item' = 'set-item'; 'si' = 'set-item'; 'new-item' = 'new-item'; 'ni' = 'new-item';
    'mkdir' = 'new-item'; 'md' = 'new-item';
    'set-content' = 'set-content'; 'sc' = 'set-content'; 'copy-item' = 'copy-item'; 'copy' = 'copy-item'; 'cp' = 'copy-item'; 'cpi' = 'copy-item';
    'move-item' = 'move-item'; 'move' = 'move-item'; 'mv' = 'move-item'; 'mi' = 'move-item';
    'rename-item' = 'rename-item'; 'ren' = 'rename-item'; 'rni' = 'rename-item' }
  # Member-type and type-name operands of the cmdlets that can rename
  # members or construct a named type.
  $addMemberParams = Specs ('type:membertype,type;value:name,value,secondvalue,inputobject,typename;' +
      'note:notepropertyname,notepropertyvalue,notepropertymembers;switch:force,passthru') '' ''
  $newObjectParams = Specs 'type:typename;com:comobject;value:argumentlist,args,property;switch:strict' '' ''
  # Name and Include/Exclude operands of the *-Variable cmdlets, which can
  # read $ExecutionContext or write $PSDefaultParameterValues by name.
  $variableParams = @{
    'get-variable' = (Specs 'name:name;filter:include,exclude;value:scope;switch:valueonly' '' '')
    'set-variable' = (Specs ('name:name;filter:include,exclude;data:value;value:description,option,visibility,scope;switch:force,passthru' + $should) '' ';switch:append')
    'new-variable' = (Specs ('name:name;data:value;value:description,option,visibility,scope;switch:force,passthru' + $should) '' '')
    'clear-variable' = (Specs ('name:name;filter:include,exclude;value:scope;switch:force,passthru' + $should) '' '')
    'remove-variable' = (Specs ('name:name;filter:include,exclude;value:scope;switch:force' + $should) '' '')
  }
  $variableSlots = @{ 'get-variable' = @('name'); 'set-variable' = @('name','data'); 'new-variable' = @('name','data');
    'clear-variable' = @('name'); 'remove-variable' = @('name') }
  $variableNames = @{ 'get-variable' = 'get-variable'; 'gv' = 'get-variable'; 'set-variable' = 'set-variable'; 'set' = 'set-variable';
    'sv' = 'set-variable'; 'new-variable' = 'new-variable'; 'nv' = 'new-variable'; 'clear-variable' = 'clear-variable';
    'clv' = 'clear-variable'; 'remove-variable' = 'remove-variable'; 'rv' = 'remove-variable' }
  $teeParams = Specs 'var:variable;value:filepath,literalpath,pspath,inputobject;switch:append' '' ';value:encoding'
  $compareParams = Specs 'prop:property;value:referenceobject,differenceobject,syncwindow,culture;switch:casesensitive,excludedifferent,includeequal,passthru' '' ''
  # Relative item paths resolve against the current location: the path
  # operands of item writers and of Get-Item/Get-ChildItem/Get-Content
  # (which read the current location when given no path).  A non-literal,
  # home-relative or missing Set-Location/Push-Location operand may move the
  # location onto the function, alias or variable drive, so the two together
  # are opaque; a literal move onto those drives throws.  A writer whose Path
  # operand the text does not show throws instead of counting as relative.
  # Using-namespace prefixes widen the names a short type name can resolve
  # to; defined holds the class and enum names of the script being checked.
  $state = @{ relativeItem = $false; dynamicLocation = $false; namespaces = @(); defined = @() }
  function ParamKind([string] $pn, [object[]] $tables, [string] $label) {
    $result = $null
    foreach ($table in $tables) {
      if ($table.ContainsKey($pn)) { $kind = $table[$pn] }
      else {
        $kinds = @(@($table.Keys) | Where-Object { $_.StartsWith($pn, [StringComparison]::Ordinal) } |
          ForEach-Object { $table[$_] } | Sort-Object -Unique)
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
      'expandstring|getvariablevalue|valuefrompipelinebypropertyname|parameters|parametersets|resolveparameter|' +
      'parametermetadata|\w*invoke\w*)$'
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
  $exemptType = '(?i)^(System\.Management\.Automation\.(PSCredential|PSObject|PSCustomObject|PSReference|ScriptBlock|' +
      'SwitchParameter|WildcardPattern|ErrorRecord|SemanticVersion|Language\.NullString|Host\.\w+)|' +
      'System\.ComponentModel\.Win32Exception|System\.Runtime\.InteropServices\.(RuntimeInformation|OSPlatform|Architecture))$'
  # PowerShell 7 has type accelerators that 5.1 lacks ([pspropertyexpression]
  # reads members by wildcard name), so a short name this host cannot resolve
  # may name any type on the host that runs it.  Such a name is opaque unless
  # the script defines it or it is one of these 7-only accelerators for the
  # ordered dictionary, SemanticVersion and integer types.
  $knownShort = '(?i)^(ordered|semver|short|uint|ulong|ushort)$'
  function CheckTypeName([object] $tn, [bool] $open = $false) {
    if ($tn -is [System.Management.Automation.Language.GenericTypeName]) {
      # The open name (List in List[int]) resolves only with its arity.
      CheckTypeName $tn.TypeName $true
      foreach ($argument in $tn.GenericArguments) { CheckTypeName $argument }
      return
    }
    if ($tn -is [System.Management.Automation.Language.ArrayTypeName]) { CheckTypeName $tn.ElementType; return }
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
    $parsed = [System.Management.Automation.Language.Parser]::ParseInput('[' + $text + ']', [ref]$t, [ref]$e)
    $types = @($parsed.FindAll({ param($n) $n -is $tType }, $true))
    if (($e -and $e.Count) -or $types.Count -ne 1 -or $types[0].Extent.Text -ne ('[' + $text + ']')) { throw 'opaque type name' }
    CheckTypeName $types[0].TypeName
  }
  function Leaf([string] $name) {
    # Path normalization drops trailing "." components and trailing dots and
    # spaces (powershell.exe\., pwsh.exe., script.ps1.).
    $parts = @($name.Trim() -split '[\\/]')
    $last = $parts.Count - 1
    while ($last -gt 0 -and $parts[$last] -match '^[. ]*$') { $last-- }
    return $parts[$last].TrimEnd('. ')
  }
  function IsPsName([string] $name) {
    # An 8.3 short name (POWERS~1.EXE) names the same file.
    return (Leaf $name) -match '(?i)^(powershell|pwsh|powers~\d+)(\.exe)?$'
  }
  function StaticName([object] $node) {
    # Member-name operands must be literal so the names above stay visible.
    # A wildcard member name binds whichever member it matches, so any
    # wildcard is opaque.
    if ($node -is [System.Management.Automation.Language.ArrayLiteralAst]) {
      foreach ($item in $node.Elements) { StaticName $item }
      return
    }
    if ($node -is [System.Management.Automation.Language.HashtableAst]) {
      foreach ($pair in $node.KeyValuePairs) {
        $v = $pair.Item2
        if ($v -isnot $tPipe -or $v.PipelineElements.Count -ne 1 -or
            $v.PipelineElements[0] -isnot [System.Management.Automation.Language.CommandExpressionAst]) { throw 'opaque member name' }
        StaticName $v.PipelineElements[0].Expression
      }
      return
    }
    if (!(Literal $node)) { throw 'opaque member name' }
    if ($node -is $tSb) { return }
    $text = Text $node
    $reason = MemberReason $text
    if ($reason) { throw $reason }
    if ([System.Management.Automation.WildcardPattern]::ContainsWildcardCharacters($text)) { throw 'opaque member name' }
  }
  function CheckAddMember([object[]] $elements) {
    # An AliasProperty reaches whatever member its (possibly computed) string
    # names; CodeProperty and CodeMethod bind reflected methods; PropertySet
    # and MemberSet group members by name.  Only note properties and script
    # members (whose literal script blocks are inspected like any other)
    # stay inspectable.
    $type = $null; $note = $false; $positionals = @()
    for ($i=1; $i -lt $elements.Count; $i++) {
      $p = $elements[$i]
      if ($p -is $tVar -and $p.Splatted) { throw 'opaque extended type data' }
      if ($p -is $tParam) {
        $kind = ParamKind $p.ParameterName.ToLowerInvariant() $addMemberParams 'Add-Member'
        if ($kind -eq 'switch') { continue }
        if ($p.Argument) { $value = $p.Argument } else { $i++; $value = $elements[$i] }
        if ($kind -eq 'type') { $type = $value } elseif ($kind -eq 'note') { $note = $true }
      } else { $positionals += ,$p }
    }
    # The first positional operand is the member type unless a note-property
    # set is chosen; a literal that converts to a member type is one.
    if ($null -eq $type -and !$note -and $positionals.Count -and
        $positionals[0] -isnot [System.Management.Automation.Language.HashtableAst]) {
      $first = $positionals[0]
      if (!(Literal $first) -or $first -is $tSb -or
          $null -ne ((Text $first) -as [System.Management.Automation.PSMemberTypes])) { $type = $first }
    }
    if ($null -eq $type) { return }
    $kindValue = $null
    if ((Literal $type) -and $type -isnot $tSb) {
      $kindValue = (Text $type) -as [System.Management.Automation.PSMemberTypes]
    }
    if ($null -eq $kindValue -or @('NoteProperty','ScriptProperty','ScriptMethod') -notcontains [string]$kindValue) {
      throw 'opaque extended type data'
    }
  }
  function CheckVariable([object] $node, [object[]] $elements, [string] $command) {
    # A computed, wildcard, piped or missing name can read $ExecutionContext
    # or write $PSDefaultParameterValues, so Name and Include/Exclude must be
    # literals without wildcards; literal names are checked with the other
    # string literals.  Surplus positionals (reported as paths) count as names.
    $named = $false
    foreach ($pair in @(Operands $node $elements $variableParams[$command] $variableSlots[$command] $command 'opaque variable name')) {
      if (@('name','filter','path') -notcontains $pair[0]) { continue }
      if ($pair[0] -ne 'filter') { $named = $true }
      foreach ($text in @(StringValues $pair[1] 'opaque variable name')) {
        if ([System.Management.Automation.WildcardPattern]::ContainsWildcardCharacters($text)) { throw 'opaque variable name' }
      }
    }
    if (!$named) { throw 'opaque variable name' }
  }
  function CheckNewObject([object[]] $elements) {
    # COM objects are external programs; .NET type names are checked like type literals.
    $type = $null; $positionals = @()
    for ($i=1; $i -lt $elements.Count; $i++) {
      $p = $elements[$i]
      if ($p -is $tVar -and $p.Splatted) { throw 'dynamic New-Object type' }
      if ($p -is $tParam) {
        $kind = ParamKind $p.ParameterName.ToLowerInvariant() $newObjectParams 'New-Object'
        if ($kind -eq 'switch') { continue }
        if ($kind -eq 'com') { return }
        if ($p.Argument) { $value = $p.Argument } else { $i++; $value = $elements[$i] }
        if ($kind -eq 'type') { $type = $value }
      } else { $positionals += ,$p }
    }
    if ($null -eq $type) {
      if (!$positionals.Count) { return }
      $type = $positionals[0]
    }
    if (!(Literal $type) -or $type -is $tSb) { throw 'dynamic New-Object type' }
    CheckTypeText (Text $type)
  }
  function MemberOperands([object[]] $elements) {
    # Every operand except -InputObject's may be a member name or its
    # arguments; parameter tables are unnecessary because all are checked.
    for ($i=1; $i -lt $elements.Count; $i++) {
      $p = $elements[$i]
      if ($p -is $tParam) {
        $pn = $p.ParameterName.ToLowerInvariant()
        $isInput = $pn.Length -ge 2 -and 'inputobject'.StartsWith($pn, [StringComparison]::Ordinal)
        if ($p.Argument) { if (!$isInput) { StaticName $p.Argument } }
        elseif ($isInput) { $i++ }
        continue
      }
      StaticName $p
    }
  }
  function CheckParam([object[]] $elements, [object[]] $tables, [string] $label, [string] $want, [string] $reason) {
    for ($i=1; $i -lt $elements.Count; $i++) {
      $p = $elements[$i]
      if ($p -is $tVar -and $p.Splatted) { throw $reason }
      if ($p -is $tParam) {
        $kind = ParamKind $p.ParameterName.ToLowerInvariant() $tables $label
        if ($kind -eq 'switch') { continue }
        if ($p.Argument) { $value = $p.Argument } else { $i++; $value = $elements[$i] }
        if ($kind -eq $want) {
          if ($want -eq 'prop') { StaticName $value } else { $null = StringValues $value $reason }
        }
      }
    }
  }
  function ProviderPath([string] $text) {
    # Returns whether a literal path is on the variable: drive.  Such a path
    # must name one variable: a drive listing, wildcards or extra components
    # can reach any variable, $ExecutionContext included.  A home-relative
    # path can be on any drive.
    if (HomePath $text) { throw 'opaque home-relative path' }
    if (VariablePath $text) {
      if ([System.Management.Automation.WildcardPattern]::ContainsWildcardCharacters($text) -or
          $text -notmatch '(?i)^\s*(\w[\w.]*\\)?variable::?[\\/]?(?<name>[^\\/:]+)$' -or $Matches['name'] -match '^[.\s]*$') {
        throw 'opaque variable path'
      }
      return $true
    }
    # Drive- or provider-qualified and UNC paths do not depend on the location.
    if ($text -notmatch '^[^\\/:]+:' -and $text -notmatch '^(\\\\|//)') { $state.relativeItem = $true }
    return $false
  }
  function ItemPath([object] $node) {
    # Returns whether the writer's path is on the variable: drive.
    if (!(Literal $node) -or $node -is $tSb) { throw 'opaque command rebinding' }
    $text = Text $node
    if (RebindingText $text) { throw 'opaque command rebinding' }
    return (ProviderPath $text)
  }
  function NamePath([object] $node) {
    # Copy/Move/Rename cannot cross providers and filters only narrow the
    # Path parameter, so the Path operand carries the provider; only a
    # literal rebinding spelling is decisive here (see VariableName for
    # variable: paths).
    if ((Literal $node) -and (RebindingText (Text $node))) { throw 'opaque command rebinding' }
  }
  function VariableName([object] $node) {
    # Under a variable: path, a destination, name or filter operand selects
    # the variable that is written (Copy-Item variable:x <name> creates it).
    if (!(Literal $node) -or $node -is $tSb) { throw 'opaque variable path' }
    $text = Text $node
    if ([System.Management.Automation.WildcardPattern]::ContainsWildcardCharacters($text)) { throw 'opaque variable path' }
    $null = ProviderPath $text
  }
  function Operands([object] $node, [object[]] $elements, [object[]] $tables, [object[]] $slots, [string] $label,
      [string] $reason = 'opaque command rebinding') {
    # Yields @(kind, node) pairs for every operand of a provider command.
    $pipeline = $node.Parent
    if ($pipeline -is $tPipe -and
        !([object]::ReferenceEquals($pipeline.PipelineElements[0], $node))) { throw $reason }
    $positionals = @(); $open = [System.Collections.ArrayList]@($slots)
    for ($i=1; $i -lt $elements.Count; $i++) {
      $p = $elements[$i]
      if ($p -is $tVar -and $p.Splatted) { throw $reason }
      if ($p -is $tParam) {
        $kind = ParamKind $p.ParameterName.ToLowerInvariant() $tables $label
        if ($kind -eq 'switch') { continue }
        if ($p.Argument) { $value = $p.Argument } else { $i++; $value = $elements[$i] }
        $open.Remove($kind)
        ,@($kind, $value)
      } else { $positionals += ,$p }
    }
    for ($k=0; $k -lt $positionals.Count; $k++) {
      # Surplus positionals are checked as paths.
      if ($k -lt $open.Count) { $kind = $open[$k] } else { $kind = 'path' }
      ,@($kind, $positionals[$k])
    }
  }
  function CheckItem([object] $node, [object[]] $elements, [string] $command) {
    $hasPath = $false; $variable = $false; $names = @()
    foreach ($pair in @(Operands $node $elements $writerParams[$command] $writerSlots[$command] $command)) {
      if ($pair[0] -eq 'path') { $hasPath = $true; if (ItemPath $pair[1]) { $variable = $true } }
      elseif ($pair[0] -eq 'filter' -or $pair[0] -eq 'dest') { NamePath $pair[1]; $names += ,$pair[1] }
    }
    # A Path operand the text does not show (pipeline input, default
    # parameter values) can name any drive, so it is not treated as relative.
    if (!$hasPath) { throw 'opaque item path' }
    if ($variable) { foreach ($name in $names) { VariableName $name } }
  }
  function CheckReader([object] $node, [object[]] $elements) {
    # Get-Item, Get-ChildItem and Get-Content return variables or their
    # values from the variable: drive.  Provider dynamic parameters differ by
    # drive, so every operand counts as a possible path: each must be a
    # literal (or $true/$false/$null), and pipeline input is opaque.
    $pipeline = $node.Parent
    if ($pipeline -is $tPipe -and
        !([object]::ReferenceEquals($pipeline.PipelineElements[0], $node))) { throw 'opaque provider path' }
    $paths = 0
    for ($i=1; $i -lt $elements.Count; $i++) {
      $p = $elements[$i]
      if ($p -is $tParam) {
        if ($null -eq $p.Argument) { continue }
        $p = $p.Argument
      }
      if ($p -is $tVar -and !$p.Splatted -and $p.IsConstantVariable()) { continue }
      foreach ($text in @(StringValues $p 'opaque provider path')) { $paths++; $null = ProviderPath $text }
    }
    # With no path operand the current location is read.
    if (!$paths) { $state.relativeItem = $true }
  }
  function CheckLocation([object] $node, [object[]] $elements) {
    # Any literal function/alias/variable drive spelling throws; every other
    # operand that is not a literal (splatting, pipeline input, expressions)
    # makes the location unknown, which matters for relative item paths.  So
    # does a home-relative operand, and no operand at all, which PowerShell 7
    # documents as a move to the home directory.
    $pipeline = $node.Parent
    if ($pipeline -is $tPipe -and
        !([object]::ReferenceEquals($pipeline.PipelineElements[0], $node))) { $state.dynamicLocation = $true }
    $operands = 0
    for ($i=1; $i -lt $elements.Count; $i++) {
      $p = $elements[$i]
      if ($p -is $tParam) {
        if ($null -eq $p.Argument) { continue }
        $p = $p.Argument
      }
      $operands++
      if (!(Literal $p) -or $p -is $tSb) { $state.dynamicLocation = $true }
      elseif (RebindingText (Text $p)) { throw 'opaque command rebinding' }
      elseif (VariablePath (Text $p)) { throw 'opaque variable path' }
      elseif (HomePath (Text $p)) { $state.dynamicLocation = $true }
    }
    if (!$operands) { $state.dynamicLocation = $true }
  }
  function ParseNested([string] $text, [int] $depth) {
    if ($depth -gt 16 -or $text.Length -gt 65536) { throw 'opaque nested command' }
    $t = $null; $e = $null
    $nested = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$t, [ref]$e)
    if ($e -and $e.Count) { throw 'nested parse error' }
    CheckAst $nested ($depth + 1)
  }
  function CheckAst([object] $root, [int] $depth) {
    # Classes and enums defined at the top level of a script shadow type
    # accelerators of the same name throughout that script, and only there.
    $outerDefined = $state.defined
    $state.defined = @(@($root.FindAll({ param($n) $n -is [System.Management.Automation.Language.TypeDefinitionAst] }, $false)) |
      ForEach-Object { [string]$_.Name })
    if ($root -is [System.Management.Automation.Language.ScriptBlockAst]) {
      foreach ($usingStatement in @($root.UsingStatements)) {
        $kind = [string]$usingStatement.UsingStatementKind
        if ($kind -eq 'Namespace') { $state.namespaces += [string]$usingStatement.Name.Value }
        elseif ($kind -ne 'Module') { throw 'using statement loads an assembly' }
      }
      if ($root.ScriptRequirements -and @($root.ScriptRequirements.RequiredAssemblies).Count) { throw '#requires loads an assembly' }
    }
    foreach ($node in @($root.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true))) {
      $elements = @($node.CommandElements)
      if (!$elements.Count) { continue }
      # Escaped command-name spellings are not a canonical command identity.
      # Inspect arguments normally; uncertain command heads require approval.
      if ($elements[0] -is $tStr -and
          $elements[0].Extent.Text.Contains([char]96)) { throw 'escaped command name requires inspection' }
      $name = Text $elements[0]
      if ($null -eq $name) {
        if ($node.InvocationOperator -match 'Ampersand|Dot' -and
            $elements[0] -is $tSb) { continue }
        throw 'dynamic command name'
      }
      $leaf = Leaf $name
      if ($leaf -match '(?i)\.ps1$') { throw 'external PowerShell script needs inspection' }
      if ($node.InvocationOperator -ne [System.Management.Automation.Language.TokenKind]::Unknown -and
          $node.InvocationOperator -ne 0 -and !(Literal $elements[0])) { throw 'dynamic invocation' }
      if ($leaf -match '(?i)^(iex|invoke-expression)$' -or $leaf -match '(?i)^(add-type)$') { throw "blocked command: $leaf" }
      # Custom drives can expose the Function/Alias providers under any name.
      if ($leaf -match '(?i)^(set-alias|new-alias|sal|nal|import-alias|ipal|new-psdrive|ndr|mount)$') { throw 'opaque command rebinding' }
      $writer = $writerNames[$leaf.ToLowerInvariant()]
      if ($writer) { CheckItem $node $elements $writer }
      if ($leaf -match '(?i)^(get-item|gi|get-childitem|gci|dir|ls|get-content|gc|cat|type)$') { CheckReader $node $elements }
      $variableCommand = $variableNames[$leaf.ToLowerInvariant()]
      if ($variableCommand) { CheckVariable $node $elements $variableCommand }
      if ($leaf -match '(?i)^(tee-object|tee)$') { CheckParam $elements $teeParams 'Tee-Object' 'var' 'opaque variable name' }
      if ($leaf -match '(?i)^(foreach-object|%|foreach|select-object|select|group-object|group|measure-object|measure)$') { MemberOperands $elements }
      if ($leaf -match '(?i)^(compare-object|compare|diff)$') { CheckParam $elements $compareParams 'Compare-Object' 'prop' 'opaque member name' }
      if ($leaf -match '(?i)^(set-location|push-location|sl|cd|chdir|pushd)$') { CheckLocation $node $elements }
      # Extended type data can alias or redefine any member, from computed
      # strings or from type and format files that are not inspected.
      if ($leaf -match '(?i)^(update-typedata|update-formatdata)$') { throw 'opaque extended type data' }
      if ($leaf -match '(?i)^add-member$') { CheckAddMember $elements }
      if ($leaf -match '(?i)^new-object$') { CheckNewObject $elements }
      $isPs = IsPsName $name
      $isStart = $leaf -match '(?i)^(start-process|start|saps)$'
      if ($isPs) { CheckLauncher $elements $depth }
      if ($isStart) { CheckStart $elements $depth }
      if ($leaf -match '(?i)^(invoke-command|icm)$') {
        for ($i=1; $i -lt $elements.Count; $i++) {
          $p = $elements[$i]
          if ($p -is $tParam) {
            $pn = $p.ParameterName.ToLowerInvariant()
            if ($p.Argument) { $value = $p.Argument } else { $i++; $value = $elements[$i] }
            if ('scriptblock'.StartsWith($pn) -or $pn -eq 'command') {
              if ($value -isnot $tSb) { throw 'dynamic Invoke-Command scriptblock' }
            } elseif ('filepath'.StartsWith($pn)) {
              if (!(Literal $value)) { throw 'dynamic Invoke-Command file' }
            }
          } elseif ($p -isnot $tSb) {
            throw 'dynamic Invoke-Command scriptblock'
          }
        }
      }
    }
    # Function/alias drive variables are writable from assignments, foreach
    # loop variables and other binding positions; reject them anywhere.
    foreach ($node in @($root.FindAll({ param($n) $n -is $tVar }, $true))) {
      $path = [string]$node.VariablePath.UserPath
      if (RebindingText $path) { throw 'opaque command rebinding' }
      # $ExecutionContext and $PSCmdlet in any scope or provider spelling.
      if ($path -match '(?i)(^|:)(executioncontext|pscmdlet)$') { throw 'opaque engine intrinsics' }
      # Default parameter values add operands the text does not show (a
      # Set-Item path, a Select-Object member name), in any spelling.
      if ($path -match '(?i)(^|:)psdefaultparametervalues$') { throw 'opaque default parameter values' }
    }
    foreach ($node in @($root.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true))) {
      if (!($node.Member -is $tStr)) { throw 'dynamic member name' }
      $member = [string]$node.Member.Value
      if ($member -match '(?i)^(invoke|invokereturnasis|invokecommand|addscript)$') { throw 'dynamic member invocation' }
      if ($node.Static -and $member -match '(?i)^create$' -and [string]$node.Expression.TypeName.FullName -match '(?i)scriptblock') { throw 'dynamic scriptblock creation' }
      # .ForEach('Name', args) reaches members by string name.
      if ($member -match '(?i)^foreach$') { foreach ($a in @($node.Arguments)) { StaticName $a } }
      # Generic method type arguments ($x.M[T](), PowerShell 7 syntax) name
      # types outside any type expression; 5.1 does not have the property.
      foreach ($g in @($node.GenericTypeArguments)) { if ($null -ne $g) { CheckTypeName $g } }
    }
    # InvokeMemberExpressionAst derives from MemberExpressionAst, so method
    # calls are matched here as well.
    foreach ($node in @($root.FindAll({ param($n) $n -is [System.Management.Automation.Language.MemberExpressionAst] }, $true))) {
      if (!($node.Member -is $tStr)) { throw 'dynamic member name' }
      $member = [string]$node.Member.Value
      if ($member -match '(?i)^invokecommand$') { throw 'dynamic InvokeCommand access' }
      $reason = MemberReason $member
      if ($reason) { throw $reason }
      # A static member of a computed type ($t::X) hides which type it is.
      if ($node.Static -and $node.Expression -isnot $tType) { throw 'dynamic static member access' }
    }
    foreach ($node in @($root.FindAll({ param($n) $n -is [System.Management.Automation.Language.ConvertExpressionAst] }, $true))) {
      if ([string]$node.Type.TypeName.FullName -match '(?i)(^|\.)scriptblock$') { throw 'string-built scriptblock' }
    }
    # -as converts to the type its right operand names at run time.
    foreach ($node in @($root.FindAll({ param($n) $n -is [System.Management.Automation.Language.BinaryExpressionAst] -and
        $n.Operator -eq [System.Management.Automation.Language.TokenKind]::As }, $true))) {
      if ($node.Right -isnot $tType) { throw 'dynamic type conversion' }
    }
    # Every type literal and type constraint; PSDriveInfo (the drive
    # registration argument) is in the engine namespace.
    foreach ($node in @($root.FindAll({ param($n) $n -is $tType -or
        $n -is [System.Management.Automation.Language.TypeConstraintAst] }, $true))) {
      CheckTypeName $node.TypeName
    }
    foreach ($node in @($root.FindAll({ param($n) $n -is [System.Management.Automation.Language.AttributeAst] }, $true))) {
      if ($node.TypeName.AssemblyName) { throw 'assembly-qualified type name' }
      # ValueFromPipelineByPropertyName reads a member named by a parameter or
      # its [Alias()] without a member-access AST.
      foreach ($arg in @($node.NamedArguments)) {
        if ($arg.ArgumentName -match '(?i)^valuefrompipelinebypropertyname$') { throw 'opaque property-name parameter binding' }
      }
    }
    # Command metadata (Get-Command results) is live: assigning through a
    # .Parameters[...] or .Attributes[...] chain can turn on property-name
    # binding or add aliases after this check.  Reading Parameters,
    # ParameterSets, ResolveParameter and ParameterMetadata is already refused
    # by MemberReason, which is the primary defence; this walk is a second
    # layer for the member Attributes, whose target type is not known here
    # (FileSystemInfo.Attributes is legitimate to read).  Any assignment whose
    # left side passes through a member named Parameters or Attributes is
    # refused, including each element of a multi-assignment left side.
    foreach ($node in @($root.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true))) {
      $pending = [System.Collections.Generic.Stack[object]]::new()
      $pending.Push($node.Left)
      while ($pending.Count -gt 0) {
        $left = $pending.Pop()
        while ($null -ne $left) {
          if ($left -is [System.Management.Automation.Language.ArrayLiteralAst]) {
            foreach ($element in @($left.Elements)) { $pending.Push($element) }
            $left = $null
          } elseif ($left -is [System.Management.Automation.Language.MemberExpressionAst]) {
            if ($left.Member -is $tStr -and [string]$left.Member.Value -match '(?i)^(get_|set_)?(parameters|attributes)$') {
              throw 'opaque command metadata mutation'
            }
            $left = $left.Expression
          } elseif ($left -is [System.Management.Automation.Language.IndexExpressionAst]) {
            $left = $left.Target
          } elseif ($left -is [System.Management.Automation.Language.ConvertExpressionAst] -or
              $left -is [System.Management.Automation.Language.ParenExpressionAst]) {
            $left = if ($left -is [System.Management.Automation.Language.ConvertExpressionAst]) { $left.Child } else { $left.Pipeline }
            if ($left -is [System.Management.Automation.Language.PipelineAst] -and $left.PipelineElements.Count -eq 1 -and
                $left.PipelineElements[0] -is [System.Management.Automation.Language.CommandExpressionAst]) {
              $left = $left.PipelineElements[0].Expression
            } elseif ($left -is [System.Management.Automation.Language.PipelineAst]) { $left = $null }
          } else { $left = $null }
        }
      }
    }
    # String literals, whatever consumes them: rebinding paths, PSDriveInfo,
    # ExecutionContext, PSCmdlet and PSDefaultParameterValues (the *-Variable
    # cmdlets, Tee-Object -Variable, the variable: drive), a single word that
    # is an engine or reflection member name, and assembly-qualified type
    # names (method arguments convert them to types, loading the assembly).
    foreach ($node in @($root.FindAll({ param($n) $n -is $tStr -or
        $n -is [System.Management.Automation.Language.ExpandableStringExpressionAst] }, $true))) {
      $text = [string]$node.Value
      if ((RebindingText $text) -or $text -match '(?i)psdriveinfo') { throw 'opaque command rebinding' }
      if ($text -match '(?i)(executioncontext|pscmdlet)') { throw 'opaque engine intrinsics' }
      if ($text -match '(?i)psdefaultparametervalues') { throw 'opaque default parameter values' }
      if ($text.Length -le 256 -and $text -match '^\s*\w+\s*$') {
        # PSObject is also the usual New-Object type name; as a member name
        # it is still checked wherever a member is named.
        $word = $text.Trim()
        $reason = MemberReason $word
        if ($reason -and $word -ne 'psobject') { throw $reason }
      }
      if ($text -match '(?i),\s*(version|culture|publickeytoken)\s*=') { throw 'assembly-qualified type name' }
    }
    $state.defined = $outerDefined
  }
  function SwitchName([object] $node) {
    if ($node -is $tParam) { return $node.ParameterName.ToLowerInvariant() }
    if ($node -is $tStr -and
        $node.Value -match '^[-/\u2013\u2014\u2015](.+)$') { return $Matches[1].ToLowerInvariant() }
    return ''
  }
  function CheckLauncher([object[]] $elements, [int] $depth) {
    $hostInfo = $false
    for ($i=1; $i -lt $elements.Count; $i++) {
      $p = $elements[$i]; $pn = SwitchName $p
      if ($pn -in @('help','h','?','-help')) { $hostInfo = $true; continue }
      if ((Text $elements[0]) -match '(?i)(^|[\\/])pwsh(\.exe)?$' -and $pn -in @('v','version','-version')) { $hostInfo = $true; continue }
      if ($pn -eq '-%' -or $pn -eq '-') { throw 'opaque native argument parsing' }
      if ($pn -and ('encodedcommand'.StartsWith($pn) -or 'encodedarguments'.StartsWith($pn) -or $pn -in @('ec','ea'))) { throw 'encoded PowerShell command' }
      if ($pn -and ('command'.StartsWith($pn) -or 'commandwithargs'.StartsWith($pn) -or $pn -eq 'cwa')) {
        if ($p -is $tParam -and $p.Argument) { throw 'opaque attached command operand' }
        $i++; if ($i -ge $elements.Count) { throw 'missing PowerShell command' }
        if ($elements[$i] -is $tSb) { return }
        $body = @()
        for (; $i -lt $elements.Count; $i++) {
          if ($elements[$i] -is $tParam -and !$elements[$i].Argument) {
            $body += $elements[$i].Extent.Text; continue
          }
          if (!(Literal $elements[$i])) { throw 'opaque PowerShell command operand' }
          $body += [string]$elements[$i].Value
        }
        if (($body -join ' ') -eq '-') { throw 'PowerShell command reads unseen stdin' }
        ParseNested ($body -join ' ') $depth
        return
      }
      if ($pn -and 'file'.StartsWith($pn)) {
        $i++
        if ($i -ge $elements.Count -or !(Literal $elements[$i])) { throw 'dynamic PowerShell file' }
        throw 'external PowerShell script needs inspection'
      }
      if ($pn -and (@('nologo','noprofile','noexit','noninteractive','sta','mta','login') | Where-Object { $_.StartsWith($pn) })) { continue }
      if ($pn -and (@('executionpolicy','inputformat','outputformat','version','windowstyle','workingdirectory') | Where-Object { $_.StartsWith($pn) })) {
        $i++
        if ($i -ge $elements.Count -or !(Literal $elements[$i])) { throw 'opaque PowerShell host option' }
        continue
      }
      if ($pn) { throw 'unknown PowerShell host option' }
      if (!(Literal $p)) { throw 'dynamic PowerShell argument' }
      # powershell.exe accepts positional command text, pwsh accepts a script
      # path. Literal paths remain ordinary external program calls.
      if ([string]$p.Value -match '(?i)\.ps1$') { throw 'external PowerShell script needs inspection' }
      $body = @()
      for (; $i -lt $elements.Count; $i++) {
        if (!(Literal $elements[$i])) { throw 'dynamic PowerShell argument' }
        $body += [string]$elements[$i].Value
      }
      ParseNested ($body -join ' ') $depth
      return
    }
    if (!$hostInfo) { throw 'PowerShell command reads unseen stdin' }
  }
  function StringValues([object] $node, [string] $reason = 'opaque Start-Process arguments') {
    if ($node -is [System.Management.Automation.Language.ArrayLiteralAst]) {
      foreach ($item in $node.Elements) { StringValues $item $reason }
    } elseif ($node -is [System.Management.Automation.Language.ArrayExpressionAst]) {
      foreach ($statement in $node.SubExpression.Statements) {
        if ($statement -isnot $tPipe -or
            $statement.PipelineElements.Count -ne 1 -or
            $statement.PipelineElements[0] -isnot [System.Management.Automation.Language.CommandExpressionAst]) { throw $reason }
        StringValues $statement.PipelineElements[0].Expression $reason
      }
    } elseif ($node -is $tStr -or
              $node -is [System.Management.Automation.Language.ConstantExpressionAst]) {
      [string]$node.Value
    } else { throw $reason }
  }
  function CheckStart([object[]] $elements, [int] $depth) {
    $file = $null; $argsNode = $null
    foreach ($p in $elements) {
      if ($p -is $tVar -and $p.Splatted) { throw 'splatting executable operands' }
    }
    for ($i=1; $i -lt $elements.Count; $i++) {
      $p = $elements[$i]
      if ($p -is $tParam) {
        # Only known value-taking parameters consume the next element.
        $kind = ParamKind $p.ParameterName.ToLowerInvariant() $startParams 'Start-Process'
        if ($kind -eq 'switch') { continue }
        if ($p.Argument) { $value = $p.Argument } else { $i++; $value = $elements[$i] }
        if ($kind -eq 'file') { $file = $value }
        elseif ($kind -eq 'args') { $argsNode = $value }
      } elseif ($null -eq $file) { $file = $p }
      elseif ($null -eq $argsNode) { $argsNode = $p }
    }
    if ($file -isnot $tStr) { throw 'dynamic Start-Process file' }
    if (IsPsName ([string]$file.Value)) {
      if ($null -eq $argsNode) { throw 'unseen PowerShell process input' }
      $joined = (StringValues $argsNode) -join ' '
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
'''
# CreateProcess caps a command line at 32767 characters, so the helper is
# passed without indentation and whole-line comments.  It has no here-strings,
# backtick continuations or block comments, so its tokens are unchanged.
_HELPER_COMMAND: Final = "\n".join(
    line.strip() for line in _HELPER.splitlines() if line.strip() and not line.strip().startswith("#")
)


def _powershell_executable() -> str | None:
    if os.name != "nt":
        candidate = shutil.which("pwsh")
        if candidate and os.path.isabs(candidate):
            candidate = os.path.realpath(candidate)
            cwd = os.path.realpath(os.getcwd())
            if not candidate.startswith(cwd + os.sep):
                return candidate
        return None
    roots = [os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles")]
    for root in roots:
        if root:
            path = os.path.join(root, "PowerShell", "7", "pwsh.exe")
            if os.path.isfile(path):
                return os.path.abspath(path)
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if system_root:
        path = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if os.path.isfile(path):
            return os.path.abspath(path)
    return None


def _powershell_environment() -> dict[str, str]:
    keys = ("SystemRoot", "WINDIR", "ComSpec", "PATH", "PATHEXT", "TEMP", "TMP",
            "USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramData", "ProgramFiles", "ProgramW6432", "HOME")
    return {key: os.environ[key] for key in keys if os.environ.get(key)}


def inspect_powershell(source: str) -> PowerShellInspection:
    """Parse *source* without executing it; parser failures require approval."""
    if not isinstance(source, str) or len(source) > _MAX_SOURCE:
        return PowerShellInspection(False, "invalid or oversized PowerShell source")
    executable = _powershell_executable()
    if executable is None:
        return PowerShellInspection(False, "PowerShell parser unavailable")
    try:
        stat = os.stat(executable)
        identity = (executable, stat.st_mtime_ns, stat.st_size, source)
    except OSError:
        return PowerShellInspection(False, "PowerShell parser unavailable")
    with _cache_lock:
        cached = _positive_cache.get(identity)
        if cached is not None:
            _positive_cache.move_to_end(identity)
            return cached
    startupinfo = subprocess.STARTUPINFO() if os.name == "nt" else None
    if startupinfo is not None:
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    try:
        completed = subprocess.run(
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _HELPER_COMMAND],
            input=json.dumps({"source": source}, ensure_ascii=True),
            capture_output=True, text=True, encoding="utf-8", timeout=_TIMEOUT,
            startupinfo=startupinfo, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=_powershell_environment(),
            check=False,
        )
        if completed.returncode != 0:
            raise subprocess.SubprocessError("parser helper exited nonzero")
        result = json.loads(completed.stdout.strip())
        if not isinstance(result, dict) or not isinstance(result.get("inspectable"), bool) or not isinstance(result.get("reason"), str):
            raise ValueError("malformed parser response")
        verdict = PowerShellInspection(result["inspectable"], result["reason"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return PowerShellInspection(False, f"PowerShell inspection failed closed: {type(exc).__name__}")
    if verdict.inspectable:
        with _cache_lock:
            _positive_cache[identity] = verdict
            _positive_cache.move_to_end(identity)
            while len(_positive_cache) > _CACHE_SIZE:
                _positive_cache.popitem(last=False)
    return verdict


def inspect_powershell_argv(argv: list[str] | tuple[str, ...]) -> PowerShellInspection:
    """Inspect a structured PowerShell argv without allowing shell expansion."""
    if not argv or not all(isinstance(item, str) for item in argv):
        return PowerShellInspection(False, "invalid PowerShell argv")

    def quote(item: str) -> str:
        # Preserve parameter tokens so CommandParameterAst remains visible;
        # quote every other argument as a literal PowerShell string.
        if item.startswith("-") and item.replace("-", "").replace("_", "").isalnum():
            return item
        return "'" + item.replace("'", "''") + "'"

    return inspect_powershell("& " + " ".join(quote(item) for item in argv))
