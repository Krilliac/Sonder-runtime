"""Fail-closed, read-only PowerShell AST inspection.

The helper process only parses text with PowerShell's native parser.  The
candidate is supplied as JSON on stdin; it is never interpolated into the
helper script and the helper has no execution path.
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
  $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
  if ($null -eq $request.source -or $request.source -isnot [string]) { throw 'bad request' }
  $tokens = $null; $errors = $null
  $ast = [System.Management.Automation.Language.Parser]::ParseInput($request.source, [ref]$tokens, [ref]$errors)
  if ($errors -and $errors.Count) { throw 'parse error' }
  if ($request.source.Length -gt 262144) { throw 'input too large' }

  function Literal([object] $node) {
    return ($node -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
      $node -is [System.Management.Automation.Language.ConstantExpressionAst] -or
      $node -is [System.Management.Automation.Language.ScriptBlockExpressionAst])
  }
  function Text([object] $node) {
    if (Literal $node) { return [string]$node.Value }
    return $null
  }
  function ParseNested([string] $text, [int] $depth) {
    if ($depth -gt 16 -or $text.Length -gt 65536) { throw 'opaque nested command' }
    $t = $null; $e = $null
    $nested = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$t, [ref]$e)
    if ($e -and $e.Count) { throw 'nested parse error' }
    CheckAst $nested ($depth + 1)
  }
  function CheckAst([object] $root, [int] $depth) {
    foreach ($node in @($root.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true))) {
      $elements = @($node.CommandElements)
      if (!$elements.Count) { continue }
      $name = Text $elements[0]
      if ($null -eq $name) {
        if ($node.InvocationOperator -match 'Ampersand|Dot' -and
            $elements[0] -is [System.Management.Automation.Language.ScriptBlockExpressionAst]) { continue }
        throw 'dynamic command name'
      }
      $leaf = ($name -split '[\\/]')[-1]
      if ($leaf -match '(?i)\.ps1$') { throw 'external PowerShell script needs inspection' }
      if ($node.InvocationOperator -ne [System.Management.Automation.Language.TokenKind]::Unknown -and
          $node.InvocationOperator -ne 0 -and !(Literal $elements[0])) { throw 'dynamic invocation' }
      if ($leaf -match '(?i)^(iex|invoke-expression)$' -or $leaf -match '(?i)^(add-type)$') { throw "blocked command: $leaf" }
      if ($leaf -match '(?i)^(set-alias|new-alias|sal|nal)$') { throw 'opaque command rebinding' }
      if ($leaf -match '(?i)^(set-item|new-item|si|ni)$') {
        foreach ($element in $elements) {
          if ((Text $element) -match '(?i)^(function|alias):') { throw 'opaque command rebinding' }
        }
      }
      $isPs = $leaf -match '(?i)^(powershell|pwsh|powershell.exe|pwsh.exe)$'
      $isStart = $leaf -match '(?i)^(start-process|start|saps)$'
      if ($isPs) { CheckLauncher $elements $depth }
      if ($isStart) { CheckStart $elements $depth }
      if ($leaf -match '(?i)^(invoke-command|icm)$') {
        for ($i=1; $i -lt $elements.Count; $i++) {
          $p = $elements[$i]
          if ($p -is [System.Management.Automation.Language.CommandParameterAst]) {
            $pn = $p.ParameterName.ToLowerInvariant()
            if ($p.Argument) { $value = $p.Argument } else { $i++; $value = $elements[$i] }
            if ('scriptblock'.StartsWith($pn) -or $pn -eq 'command') {
              if ($value -isnot [System.Management.Automation.Language.ScriptBlockExpressionAst]) { throw 'dynamic Invoke-Command scriptblock' }
            } elseif ('filepath'.StartsWith($pn)) {
              if (!(Literal $value)) { throw 'dynamic Invoke-Command file' }
            }
          } elseif ($p -isnot [System.Management.Automation.Language.ScriptBlockExpressionAst]) {
            throw 'dynamic Invoke-Command scriptblock'
          }
        }
      }
    }
    foreach ($node in @($root.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true))) {
      if (!($node.Member -is [System.Management.Automation.Language.StringConstantExpressionAst])) { throw 'dynamic member name' }
      $member = [string]$node.Member.Value
      if ($member -match '(?i)^(invoke|invokereturnasis|invokecommand|addscript)$') { throw 'dynamic member invocation' }
      if ($node.Static -and $member -match '(?i)^create$' -and [string]$node.Expression.TypeName.FullName -match '(?i)scriptblock') { throw 'dynamic scriptblock creation' }
    }
    foreach ($node in @($root.FindAll({ param($n) $n -is [System.Management.Automation.Language.MemberExpressionAst] }, $true))) {
      if (!($node.Member -is [System.Management.Automation.Language.StringConstantExpressionAst])) { throw 'dynamic member name' }
      if ([string]$node.Member.Value -match '(?i)^invokecommand$') { throw 'dynamic InvokeCommand access' }
    }
    foreach ($node in @($root.FindAll({ param($n) $n -is [System.Management.Automation.Language.ConvertExpressionAst] }, $true))) {
      if ([string]$node.Type.TypeName.FullName -match '(?i)(^|\.)scriptblock$') { throw 'string-built scriptblock' }
    }
  }
  function SwitchName([object] $node) {
    if ($node -is [System.Management.Automation.Language.CommandParameterAst]) { return $node.ParameterName.ToLowerInvariant() }
    if ($node -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
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
        if ($p -is [System.Management.Automation.Language.CommandParameterAst] -and $p.Argument) { throw 'opaque attached command operand' }
        $i++; if ($i -ge $elements.Count) { throw 'missing PowerShell command' }
        if ($elements[$i] -is [System.Management.Automation.Language.ScriptBlockExpressionAst]) { return }
        $body = @()
        for (; $i -lt $elements.Count; $i++) {
          if ($elements[$i] -is [System.Management.Automation.Language.CommandParameterAst] -and !$elements[$i].Argument) {
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
  function StringValues([object] $node) {
    if ($node -is [System.Management.Automation.Language.ArrayLiteralAst]) {
      foreach ($item in $node.Elements) { StringValues $item }
    } elseif ($node -is [System.Management.Automation.Language.ArrayExpressionAst]) {
      foreach ($statement in $node.SubExpression.Statements) {
        if ($statement -isnot [System.Management.Automation.Language.PipelineAst] -or
            $statement.PipelineElements.Count -ne 1 -or
            $statement.PipelineElements[0] -isnot [System.Management.Automation.Language.CommandExpressionAst]) { throw 'opaque Start-Process arguments' }
        StringValues $statement.PipelineElements[0].Expression
      }
    } elseif ($node -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
              $node -is [System.Management.Automation.Language.ConstantExpressionAst]) {
      [string]$node.Value
    } else { throw 'opaque Start-Process arguments' }
  }
  function CheckStart([object[]] $elements, [int] $depth) {
    $file = $null; $argsNode = $null
    for ($i=1; $i -lt $elements.Count; $i++) {
      $p = $elements[$i]
      if ($p -is [System.Management.Automation.Language.VariableExpressionAst] -and $p.Splatted) { throw 'splatting executable operands' }
      if ($p -is [System.Management.Automation.Language.CommandParameterAst]) {
        $pn = $p.ParameterName.ToLowerInvariant()
        if ($pn -in @('wait','nonewwindow','passthru','loaduserprofile','useNewEnvironment')) { continue }
        if ($p.Argument) { $value = $p.Argument } else { $i++; $value = $elements[$i] }
        if ('filepath'.StartsWith($pn)) { $file = $value }
        elseif ('argumentlist'.StartsWith($pn) -or $pn -eq 'args') { $argsNode = $value }
      } elseif ($null -eq $file) { $file = $p }
      elseif ($null -eq $argsNode) { $argsNode = $p }
    }
    if ($file -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { throw 'dynamic Start-Process file' }
    if ([string]$file.Value -match '(?i)(^|[\\/])(powershell|pwsh)(\.exe)?$') {
      if ($null -eq $argsNode) { throw 'unseen PowerShell process input' }
      $joined = (StringValues $argsNode) -join ' '
      ParseNested ("& '" + $file.Value.Replace("'", "''") + "' " + $joined) $depth
    }
  }
  CheckAst $ast 0
  [Console]::Out.Write((ConvertTo-Json @{ inspectable = $true; reason = 'ast fully inspectable' } -Compress))
} catch {
  [Console]::Out.Write((ConvertTo-Json @{ inspectable = $false; reason = [string]$_.Exception.Message } -Compress))
  exit 0
}
'''


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
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _HELPER],
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
