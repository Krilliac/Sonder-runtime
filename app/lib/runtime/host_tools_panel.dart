/// Read-only host developer-tool inventory (`GET /v1/tools/inventory`).
///
/// Like the jobs views, the list loads only when its "Details" disclosure
/// opens. 401/403 and 404 read as off-by-design (`– n/a`); the server's
/// two-request admission limit (429) reads as a `! warn` note with Retry.
/// "Rediscover" asks the server to probe the host again
/// (`POST /v1/tools/inventory/refresh`); it never runs anything the server's
/// own registry does not already name.
library;

import 'package:flutter/material.dart';

import '../api.dart';
import '../theme.dart';
import 'overview.dart';
import 'runtime_data.dart';
import 'status_word.dart';
import 'work_runs_panel.dart';

/// The row status for one discovered tool: the tool is present; the mark
/// says what its version probe reported.
StatusKind hostToolStatus(HostTool tool) {
  if (tool.versionStatus.hasVersion) return StatusKind.ok;
  if (tool.versionStatus.probeFailed) return StatusKind.warn;
  return StatusKind.note;
}

class HostToolsPanel extends StatefulWidget {
  final RuntimeDataSource source;

  /// Open the disclosure (and load) on first build; tests and goldens.
  final bool initiallyExpanded;

  const HostToolsPanel(
      {super.key, required this.source, this.initiallyExpanded = false});

  @override
  State<HostToolsPanel> createState() => _HostToolsPanelState();
}

class _HostToolsPanelState extends State<HostToolsPanel> {
  ToolInventory? _inventory;
  Object? _error;
  bool _loading = false;
  bool _rediscovering = false;
  String? _category;
  bool _expanded = false;

  /// Bumped when [HostToolsPanel.source] changes: a read that started against
  /// the previous server is dropped instead of shown under the new one.
  int _generation = 0;

  bool get _busy => _loading || _rediscovering;

  @override
  void initState() {
    super.initState();
    _expanded = widget.initiallyExpanded;
    if (_expanded) _loadAfterFrame();
  }

  void _loadAfterFrame() {
    final generation = _generation;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted && generation == _generation) _load();
    });
  }

  @override
  void didUpdateWidget(covariant HostToolsPanel oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.source != widget.source) {
      _generation++;
      _inventory = null;
      _error = null;
      _category = null;
      _loading = false;
      _rediscovering = false;
      // An open disclosure shows the new server's list, not a stuck spinner.
      if (_expanded) _loadAfterFrame();
    }
  }

  Future<void> _run(Future<ToolInventory> Function() read,
      {bool rediscover = false}) async {
    if (_busy) return;
    final generation = _generation;
    bool current() => mounted && generation == _generation;
    setState(() {
      _loading = !rediscover;
      _rediscovering = rediscover;
      _error = null;
    });
    try {
      final inventory = await read();
      if (!current()) return;
      setState(() => _inventory = inventory);
    } catch (error) {
      if (!current()) return;
      setState(() => _error = error);
    } finally {
      if (current()) {
        setState(() {
          _loading = false;
          _rediscovering = false;
        });
      }
    }
  }

  Future<void> _load() =>
      _run(() => widget.source.toolInventory(category: _category));

  Future<void> _rediscover() => _run(() async {
        final source = widget.source;
        final category = _category;
        final ToolInventory fresh;
        try {
          fresh = await source.refreshToolInventory();
        } on SonderException catch (error) {
          // The server saves the rediscovered snapshot before it sizes the
          // unfiltered answer, so "too large" still means it rediscovered;
          // with a category picked, the filtered read below is the answer.
          if (category == null || !_isTooLarge(error)) rethrow;
          return source.toolInventory(category: category);
        }
        // The refresh answers with the whole snapshot; re-read the filter.
        return category == null
            ? fresh
            : await source.toolInventory(category: category);
      }, rediscover: true);

  void _pickCategory(String? category) {
    if (category == _category) return;
    setState(() => _category = category);
    _load();
  }

  bool get _forbidden {
    final error = _error;
    return error is SonderException &&
        (error.httpStatus == 401 || error.httpStatus == 403);
  }

  bool get _missing {
    final error = _error;
    return error is SonderException && error.httpStatus == 404;
  }

  static bool _isTooLarge(Object? error) =>
      error is SonderException &&
      (error.httpStatus == 413 || error.code == 'TOOL_INVENTORY_TOO_LARGE');

  bool get _tooLarge => _isTooLarge(_error);

  Widget _errorNote(Object error) {
    if (_forbidden) {
      return const RuntimePanelNote(
          status: StatusKind.skipped,
          word: 'n/a',
          text: 'Needs an administrator account.');
    }
    if (_missing) {
      return const RuntimePanelNote(
          status: StatusKind.skipped,
          word: 'n/a',
          text: 'Not available on this server.');
    }
    final message =
        error is SonderException ? error.message : 'Could not load.';
    if (_tooLarge) {
      return RuntimePanelNote(status: StatusKind.note, text: message);
    }
    final busy = error is SonderException && error.httpStatus == 429;
    return RuntimePanelNote(
      status: busy ? StatusKind.warn : StatusKind.fail,
      text: message,
      action: TextButton(
          onPressed: _busy ? null : _load, child: const Text('Retry')),
    );
  }

  Widget _controls(ToolInventory? inventory) {
    final tokens = SonderTokens.of(context);
    final present = inventory == null
        ? hostToolCategories
        : [
            for (final category in hostToolCategories)
              if ((inventory.counts[category] ?? 0) > 0 ||
                  category == _category)
                category,
          ];
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Wrap(
        spacing: 12,
        runSpacing: 4,
        crossAxisAlignment: WrapCrossAlignment.center,
        children: [
          DropdownButton<String?>(
            key: const Key('host-tools-category'),
            value: _category,
            isDense: true,
            style: tokens.mono(12, color: tokens.text2),
            onChanged: _busy ? null : _pickCategory,
            items: [
              const DropdownMenuItem<String?>(
                  value: null, child: Text('All categories')),
              for (final category in present)
                DropdownMenuItem<String?>(
                  value: category,
                  child: Text(inventory == null
                      ? hostToolCategoryLabel(category)
                      : '${hostToolCategoryLabel(category)} '
                          '(${inventory.counts[category] ?? 0})'),
                ),
            ],
          ),
          TextButton.icon(
            key: const Key('host-tools-rediscover'),
            onPressed: _busy || _forbidden || _missing ? null : _rediscover,
            icon: const Icon(Icons.manage_search, size: 18),
            label: Text(_rediscovering ? 'Rediscovering…' : 'Rediscover'),
          ),
        ],
      ),
    );
  }

  Widget _summary(ToolInventory inventory) {
    final tokens = SonderTokens.of(context);
    final total = inventory.total;
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Text(
        [
          if (inventory.os.isNotEmpty) inventory.os,
          if (inventory.machine.isNotEmpty) inventory.machine,
          '$total tool${total == 1 ? '' : 's'}',
          'checked ${compactDuration(inventory.age)} ago',
        ].join(' · '),
        style: tokens.mono(12, color: tokens.muted),
      ),
    );
  }

  Widget _body() {
    final inventory = _inventory;
    final error = _error;
    if (error == null && inventory == null) {
      return const RuntimePanelNote(
          status: StatusKind.unknown, word: 'checking', text: 'Loading…');
    }
    final showControls = !_forbidden && !_missing;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (showControls) _controls(inventory),
        if (error != null) _errorNote(error),
        if (error == null && inventory != null) ...[
          _summary(inventory),
          if (inventory.stale)
            const RuntimePanelNote(
                status: StatusKind.warn,
                text: 'This list is older than its refresh window. '
                    'Rediscover to update it.'),
          if (inventory.tools.isEmpty)
            RuntimePanelNote(
                status: StatusKind.note,
                text: _category == null
                    ? 'No developer tools found on this host.'
                    : 'No ${hostToolCategoryLabel(_category!).toLowerCase()} '
                        'found on this host.'),
          for (final (category, tools) in inventory.byCategory)
            _CategoryGroup(category: category, tools: tools),
          if (inventory.truncated)
            const RuntimePanelNote(
                status: StatusKind.note,
                text: 'The server listed only part of the inventory.'),
          for (final note in inventory.notes)
            RuntimePanelNote(status: StatusKind.note, text: note),
        ],
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    return Theme(
      data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
      child: ExpansionTile(
        key: const Key('host-tools-details'),
        initiallyExpanded: widget.initiallyExpanded,
        tilePadding: EdgeInsets.zero,
        childrenPadding: const EdgeInsets.only(bottom: 8),
        expandedCrossAxisAlignment: CrossAxisAlignment.start,
        title: Text('Host tools · Details',
            style: Theme.of(context).textTheme.labelLarge),
        trailing: _inventory != null || _error != null
            ? IconButton(
                tooltip: 'Refresh host tools',
                onPressed: _busy ? null : _load,
                icon: const Icon(Icons.refresh, size: 18),
              )
            : null,
        onExpansionChanged: (open) {
          _expanded = open;
          if (open && _inventory == null && !_busy) _load();
        },
        children: [_body()],
      ),
    );
  }
}

class _CategoryGroup extends StatelessWidget {
  final String category;
  final List<HostTool> tools;
  const _CategoryGroup({required this.category, required this.tools});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Semantics(
            header: true,
            child: Text('${hostToolCategoryLabel(category)} · ${tools.length}',
                style: Theme.of(context)
                    .textTheme
                    .labelMedium
                    ?.copyWith(color: tokens.muted)),
          ),
          for (final tool in tools) _ToolRow(tool: tool),
        ],
      ),
    );
  }
}

class _ToolRow extends StatelessWidget {
  final HostTool tool;
  const _ToolRow({required this.tool});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final status = tool.versionStatus;
    final version = status.hasVersion && tool.version.isNotEmpty
        ? '${tool.name} ${tool.version}'
        : '${tool.name} · ${status.description}';
    final others = tool.alternatives.length;
    final line = [
      version,
      tool.onPath && tool.source != 'path'
          ? '${hostToolSourceLabel(tool.source)}, on PATH'
          : hostToolSourceLabel(tool.source),
      if (others > 0) '+$others other install${others == 1 ? '' : 's'}',
    ].where((part) => part.isNotEmpty).join(' · ');
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        RuntimeStatusWord(hostToolStatus(tool), width: 116),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(line,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: tokens.mono(12, color: tokens.text2)),
              if (tool.path.isNotEmpty)
                Text(tool.path,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: tokens.mono(11, color: tokens.muted)),
            ],
          ),
        ),
      ]),
    );
  }
}
