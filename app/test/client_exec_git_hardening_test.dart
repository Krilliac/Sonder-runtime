// The native desktop client executor must not turn an acceptEdits write into
// code execution. acceptEdits lets file changes through without a prompt and
// keeps the execute prompt, while the git tools are classed read-only, so a
// repository config that makes git launch a program (core.fsmonitor, filter
// and textconv drivers, diff.external, hooks) would reach execution with no
// execute prompt. These tests pin both halves: writes to git-executable
// configuration and credential stores are refused, and every git subprocess
// runs with that configuration neutralised.
//
// Each hardening test ends with a positive control: the same payload run
// through plain `git` must create the marker, so a missing marker means the
// hardening held and not that the payload was broken.
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/local_manager_models.dart';
import 'package:sonder_runtime/local_manager_native.dart';

final String _sep = Platform.pathSeparator;

bool _gitAvailable() {
  try {
    return Process.runSync('git', ['--version']).exitCode == 0;
  } on ProcessException {
    return false;
  }
}

void _git(String cwd, List<String> args) {
  final res = Process.runSync('git', args, workingDirectory: cwd);
  if (res.exitCode != 0) {
    throw StateError('git ${args.join(' ')} failed: ${res.stderr}');
  }
}

Future<Directory> _initRepo() async {
  final ws = await Directory.systemTemp.createTemp('sonder_git_hardening_');
  addTearDown(() async {
    try {
      await ws.delete(recursive: true);
    } catch (_) {}
  });
  _git(ws.path, ['init', '-q']);
  _git(ws.path, ['config', 'user.email', 'test@example.com']);
  _git(ws.path, ['config', 'user.name', 'Test']);
  _git(ws.path, ['config', 'commit.gpgSign', 'false']);
  return ws;
}

File _file(Directory ws, String rel) =>
    File('${ws.path}$_sep${rel.replaceAll('/', _sep)}');

/// A command git would run, and the marker file it creates when it does.
(String, File) _payload(Directory ws) {
  final marker = File(
      '${ws.parent.path}$_sep${ws.uri.pathSegments.where((s) => s.isNotEmpty).last}_marker.txt');
  addTearDown(() async {
    if (await marker.exists()) await marker.delete();
  });
  final script = File(
      '${ws.parent.path}$_sep${ws.uri.pathSegments.where((s) => s.isNotEmpty).last}_payload.sh');
  final markerPosix = marker.path.replaceAll('\\', '/');
  script.writeAsStringSync('echo pwned > $markerPosix\n');
  addTearDown(() async {
    if (await script.exists()) await script.delete();
  });
  return ('sh ${script.path.replaceAll('\\', '/')}', marker);
}

void _appendConfig(Directory ws, String text) {
  final config = _file(ws, '.git/config');
  config.writeAsStringSync(config.readAsStringSync() + text);
}

Future<ClientToolExecResult> _run(
  Directory ws,
  String tool,
  Map<String, dynamic> args, {
  String mode = 'acceptEdits',
  String risk = '',
}) =>
    LocalManager.executeClientTool(
      callId: 'c',
      tool: tool,
      args: args,
      risk: risk,
      mode: mode,
      workspaceRoot: ws.path,
    );

void main() {
  final skipGit = _gitAvailable() ? false : 'git is not installed';

  group('write refusal', () {
    final refused = <String>[
      '.git/config',
      '.git/hooks/pre-commit',
      '.git/info/attributes',
      '.GIT/config',
      'sub/.git/config',
      'sub/../.git/config',
      './.git/config',
      '.gitattributes',
      'sub/.GitAttributes',
      '.gitmodules',
      if (Platform.isWindows) ...[
        r'.git\config',
        '.git./config',
        '.git /config',
        r'.git::$INDEX_ALLOCATION/config',
        '.git:foo',
      ],
    ];
    for (final rel in refused) {
      test('acceptEdits file_write refuses git control path $rel', () async {
        final ws = await _initRepo();
        final before = _file(ws, '.git/config').readAsStringSync();
        final res = await _run(ws, 'file_write',
            {'path': rel, 'content': '[core]\n\tfsmonitor = sh h.sh\n'});
        expect(res.ok, isFalse);
        expect(res.denied, isTrue);
        expect(res.error, contains('git'));
        expect(_file(ws, '.git/config').readAsStringSync(), before);
      }, skip: skipGit);
    }

    test('absolute path to .git/config is refused', () async {
      final ws = await _initRepo();
      final before = _file(ws, '.git/config').readAsStringSync();
      final res = await _run(ws, 'file_write',
          {'path': _file(ws, '.git/config').path, 'content': 'x'});
      expect(res.denied, isTrue);
      expect(_file(ws, '.git/config').readAsStringSync(), before);
    }, skip: skipGit);

    for (final mode in ['acceptEdits', 'auto', 'bypass']) {
      test(
          'file_edit, file_delete and directory_create under .git are refused in $mode',
          () async {
        final ws = await _initRepo();
        final before = _file(ws, '.git/config').readAsStringSync();
        final edit = await _run(
            ws,
            'file_edit',
            {
              'path': '.git/config',
              'old': '[core]',
              'new': '[core]\n\tfsmonitor = x'
            },
            mode: mode);
        final delete =
            await _run(ws, 'file_delete', {'path': '.git/HEAD'}, mode: mode);
        final mkdir = await _run(
            ws, 'directory_create', {'path': '.git/hooks/extra'},
            mode: mode);
        for (final res in [edit, delete, mkdir]) {
          expect(res.ok, isFalse);
          expect(res.denied, isTrue);
        }
        expect(_file(ws, '.git/config').readAsStringSync(), before);
        expect(_file(ws, '.git/HEAD').existsSync(), isTrue);
        expect(Directory(_file(ws, '.git/hooks/extra').path).existsSync(),
            isFalse);
      }, skip: skipGit);
    }

    final credentials = <String>[
      '.ssh/authorized_keys',
      '.ssh/config',
      'sub/.SSH/known_hosts',
      '.aws/credentials',
      'id_rsa',
      '.netrc',
      '.git-credentials',
      '.npmrc',
      '.env',
      '.env.local',
      'prod.env',
      'server.pem',
      'db_secret.yaml',
      '.docker/config.json',
      '.config/gh/hosts.yml',
      '.config/gcloud/access_tokens.db',
    ];
    for (final rel in credentials) {
      test('file_write refuses credential store $rel, bypass included',
          () async {
        final ws = await Directory.systemTemp.createTemp('sonder_cred_');
        addTearDown(() => ws.delete(recursive: true));
        for (final mode in ['acceptEdits', 'bypass']) {
          final res = await _run(
              ws, 'file_write', {'path': rel, 'content': 'x'},
              mode: mode);
          expect(res.ok, isFalse, reason: mode);
          expect(res.denied, isTrue, reason: mode);
          expect(res.error, contains('credential'), reason: mode);
        }
        expect(_file(ws, rel).existsSync(), isFalse);
      });
    }

    test('ordinary files with similar names stay writable', () async {
      final ws = await _initRepo();
      for (final rel in [
        '.gitignore',
        '.github/workflows/ci.yml',
        'docs/.git-notes.md',
        'gitconfig.txt',
        'config.json',
        'env.py',
        'docs/ssh.md',
        'keys.txt',
      ]) {
        final res =
            await _run(ws, 'file_write', {'path': rel, 'content': 'ok\n'});
        expect(res.ok, isTrue, reason: '$rel: ${res.error}');
      }
    }, skip: skipGit);
  });

  group('git hardening', () {
    test('the acceptEdits fsmonitor chain from the finding does not execute',
        () async {
      final ws = await _initRepo();
      final marker = _file(ws, 'marker.txt');
      final original = _file(ws, '.git/config').readAsStringSync();

      final script = await _run(ws, 'file_write',
          {'path': 'h.sh', 'content': 'echo pwned > marker.txt\n'});
      final config = await _run(ws, 'file_write', {
        'path': '.git/config',
        'content':
            original.replaceFirst('[core]', '[core]\n\tfsmonitor = sh h.sh'),
      });
      final status = await _run(ws, 'git_status', {});
      final runProgram =
          await _run(ws, 'run_program', {'command': 'git status'});

      expect(script.ok, isTrue, reason: script.error);
      expect(config.ok, isFalse);
      expect(config.denied, isTrue);
      expect(status.ok, isTrue, reason: status.error);
      expect(runProgram.needsApproval, isTrue);
      expect(marker.existsSync(), isFalse);

      // The same config already on disk (a repository the tools did not
      // write) is neutralised too; plain git runs it (control).
      _file(ws, '.git/config').writeAsStringSync(
          original.replaceFirst('[core]', '[core]\n\tfsmonitor = sh h.sh'));
      final hardened = await _run(ws, 'git_status', {});
      expect(hardened.ok, isTrue, reason: hardened.error);
      expect(marker.existsSync(), isFalse);
      Process.runSync('git', ['status'], workingDirectory: ws.path);
      expect(marker.existsSync(), isTrue,
          reason: 'control: plain git must run the fsmonitor payload');
    }, skip: skipGit);

    Future<Directory> filterRepo(String name) async {
      final ws = await _initRepo();
      _file(ws, '.gitattributes').writeAsStringSync('*.txt filter=$name\n');
      _file(ws, 'a.txt').writeAsStringSync('same\n');
      _git(ws.path, ['add', '.']);
      _git(ws.path, ['commit', '-q', '-m', 'init']);
      return ws;
    }

    void bumpMtime(Directory ws) {
      final f = _file(ws, 'a.txt');
      f.setLastModifiedSync(
          f.lastModifiedSync().add(const Duration(minutes: 2)));
    }

    test('git_status neutralises a filter clean driver', () async {
      final ws = await filterRepo('evil');
      final (command, marker) = _payload(ws);
      _appendConfig(ws, '[filter "evil"]\n\tclean = $command\n');
      bumpMtime(ws);

      final res = await _run(ws, 'git_status', {});

      expect(res.ok, isTrue, reason: res.error);
      expect(marker.existsSync(), isFalse);
      Process.runSync('git', ['status'], workingDirectory: ws.path);
      expect(marker.existsSync(), isTrue,
          reason: 'control: plain git must run the clean filter');
    }, skip: skipGit);

    test('a driver name containing = fails closed', () async {
      final ws = await filterRepo('a=b');
      final (command, marker) = _payload(ws);
      _appendConfig(ws, '[filter "a=b"]\n\tclean = $command\n');
      bumpMtime(ws);

      final res = await _run(ws, 'git_status', {});

      expect(marker.existsSync(), isFalse);
      expect(res.ok, isFalse);
      expect(res.error, contains('refusing to run git'));
      Process.runSync('git', ['status'], workingDirectory: ws.path);
      expect(marker.existsSync(), isTrue,
          reason: "control: plain git must run the clean filter named 'a=b'");
    }, skip: skipGit);

    for (final kind in ['external', 'textconv']) {
      test('git_diff and git_log neutralise diff $kind', () async {
        final ws = await _initRepo();
        _file(ws, 'a.txt').writeAsStringSync('one\n');
        if (kind == 'textconv') {
          _file(ws, '.gitattributes').writeAsStringSync('*.txt diff=evil\n');
        }
        _git(ws.path, ['add', '.']);
        _git(ws.path, ['commit', '-q', '-m', 'init']);
        _file(ws, 'a.txt').writeAsStringSync('two two\n');
        final (command, marker) = _payload(ws);
        _appendConfig(
            ws,
            kind == 'external'
                ? '[diff]\n\texternal = $command\n'
                : '[diff "evil"]\n\ttextconv = $command\n');

        final diff = await _run(ws, 'git_diff', {});
        final log = await _run(ws, 'git_log', {'limit': 3});

        expect(diff.ok, isTrue, reason: diff.error);
        expect(log.ok, isTrue, reason: log.error);
        expect(marker.existsSync(), isFalse);
        Process.runSync('git', ['diff'], workingDirectory: ws.path);
        expect(marker.existsSync(), isTrue,
            reason: 'control: plain git diff must run the $kind program');
      }, skip: skipGit);
    }

    test(
        'an embedded bare repository written without a .git component cannot execute',
        () async {
      final ws = await Directory.systemTemp.createTemp('sonder_bare_');
      addTearDown(() => ws.delete(recursive: true));
      final (command, marker) = _payload(ws);
      final writes = [
        await _run(ws, 'directory_create', {'path': 'evil/objects'}),
        await _run(ws, 'directory_create', {'path': 'evil/refs'}),
        await _run(ws, 'file_write',
            {'path': 'evil/HEAD', 'content': 'ref: refs/heads/main\n'}),
        await _run(ws, 'file_write', {
          'path': 'evil/config',
          'content': '[core]\n\trepositoryformatversion = 0\n\tbare = false\n'
              '\tworktree = ..\n\tfsmonitor = $command\n',
        }),
      ];
      for (final w in writes) {
        expect(w.ok, isTrue, reason: w.error);
      }

      await _run(ws, 'git_status', {'cwd': 'evil'});

      expect(marker.existsSync(), isFalse);
      Process.runSync('git', ['status'],
          workingDirectory: '${ws.path}${_sep}evil');
      expect(marker.existsSync(), isTrue,
          reason: 'control: plain git must treat evil/ as a repository');
    }, skip: skipGit);

    test('git tools still work on a normal repository', () async {
      final ws = await _initRepo();
      _file(ws, 'a.txt').writeAsStringSync('one\n');
      _git(ws.path, ['add', '.']);
      _git(ws.path, ['commit', '-q', '-m', 'first commit']);
      _file(ws, 'a.txt').writeAsStringSync('two\n');

      final status = await _run(ws, 'git_status', {});
      final diff = await _run(ws, 'git_diff', {'path': 'a.txt'});
      final log = await _run(ws, 'git_log', {'limit': 5});
      final branch = await _run(ws, 'git_branch', {});

      expect(status.ok, isTrue, reason: status.error);
      expect(status.output, contains('a.txt'));
      expect(diff.ok, isTrue, reason: diff.error);
      expect(diff.output, contains('+two'));
      expect(log.output, contains('first commit'));
      expect(branch.ok, isTrue, reason: branch.error);
    }, skip: skipGit);
  });

  test('a declared risk lower than the local classification is ignored',
      () async {
    final ws = await Directory.systemTemp.createTemp('sonder_risk_');
    addTearDown(() => ws.delete(recursive: true));
    final res = await _run(ws, 'run_program', {'command': 'git --version'},
        mode: 'manual', risk: 'read_only');
    expect(res.needsApproval, isTrue);
    expect(res.ok, isFalse);
  });
}
