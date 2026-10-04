/// The default-model picker: the ids `GET /v1/models` lists, searchable and
/// grouped into routes and exact models, with any typed id accepted when the
/// server cannot list them (or does not list the one you want).
library;

import 'package:flutter/material.dart';

import '../runtime/model_routing.dart';
import '../theme.dart';
import '../ui/kit.dart';
import '../ui/sheet.dart';
import '../ui/status_vocab.dart';

/// What the server offers: its `/v1/models` ids and how they are routed.
class ModelChoices {
  final List<String> ids;
  final ModelRouting routing;

  const ModelChoices(this.ids, [this.routing = const ModelRouting()]);
}

/// The trigger in the General page: the current id, in mono, and a chevron.
class ModelPickerButton extends StatelessWidget {
  final String value;
  final VoidCallback onPressed;

  const ModelPickerButton({
    super.key,
    required this.value,
    required this.onPressed,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Tooltip(
      message: 'Choose the default model or route',
      child: OutlinedButton(
        key: const Key('settings-model'),
        onPressed: onPressed,
        style: OutlinedButton.styleFrom(
          padding: const EdgeInsets.fromLTRB(
              SonderSpace.md, 0, SonderSpace.sm, 0),
        ),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 220),
            child: Text(value,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: tokens.mono(13, weight: FontWeight.w500)),
          ),
          const SizedBox(width: SonderSpace.xs),
          Icon(Icons.expand_more, size: 18, color: tokens.text2),
        ]),
      ),
    );
  }
}

/// Opens the picker: a dialog on wide windows, a bottom sheet on phones.
/// Resolves to the chosen id, or null when dismissed. [cached] shows at
/// once; otherwise [load] runs (and can be retried from the picker).
Future<String?> showModelPicker(
  BuildContext context, {
  required String current,
  required Future<ModelChoices> Function() load,
  ModelChoices? cached,
}) =>
    showSonderSheet<String>(
      context,
      builder: (_) => _ModelPicker(current: current, load: load, cached: cached),
    );

class _ModelPicker extends StatefulWidget {
  final String current;
  final Future<ModelChoices> Function() load;
  final ModelChoices? cached;

  const _ModelPicker({
    required this.current,
    required this.load,
    required this.cached,
  });

  @override
  State<_ModelPicker> createState() => _ModelPickerState();
}

class _ModelPickerState extends State<_ModelPicker> {
  final _query = TextEditingController();
  ModelChoices? _choices;
  Object? _error;
  bool _loading = false;

  @override
  void initState() {
    super.initState();
    _choices = widget.cached;
    if (_choices == null) _load();
  }

  @override
  void dispose() {
    _query.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final choices = await widget.load();
      if (!mounted) return;
      setState(() {
        _choices = choices;
        _loading = false;
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _error = error;
        _loading = false;
      });
    }
  }

  void _pick(String id) {
    final value = id.trim();
    if (value.isEmpty) return;
    Navigator.of(context).pop(value);
  }

  List<String> get _matches {
    final ids = _choices?.ids ?? const <String>[];
    final q = _query.text.trim().toLowerCase();
    if (q.isEmpty) return ids;
    return [for (final id in ids) if (id.toLowerCase().contains(q)) id];
  }

  void _submit() {
    final matches = _matches;
    final typed = _query.text.trim();
    if (typed.isNotEmpty && !matches.contains(typed) && matches.length != 1) {
      _pick(typed);
    } else if (matches.isNotEmpty) {
      _pick(matches.contains(typed) ? typed : matches.first);
    }
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final media = MediaQuery.of(context);
    final wide = media.size.width >= sheetDialogBreakpoint;
    final typed = _query.text.trim();
    final choices = _choices;
    final routing = choices?.routing ?? const ModelRouting();
    final matches = _matches;
    final routes = [for (final id in matches) if (routing.isRoute(id)) id];
    final exact = [for (final id in matches) if (!routing.isRoute(id)) id];
    final listed = choices?.ids.contains(typed) ?? false;
    final rows = <Widget>[
      if (typed.isNotEmpty && !listed)
        _Option(
          key: const Key('settings-model-free-text'),
          title: 'Use “$typed”',
          subtitle: choices == null
              ? 'Sent as typed; the server decides if it exists.'
              : 'Not in the server’s list; sent as typed.',
          mono: false,
          icon: Icons.keyboard_return,
          onTap: () => _pick(typed),
        ),
      if (_loading)
        const SkeletonRows(rows: 4, semanticLabel: 'Loading models'),
      if (_error != null) ...[
        const Padding(
          padding: EdgeInsets.fromLTRB(
              SonderSpace.md, SonderSpace.md, SonderSpace.md, SonderSpace.xs),
          child: OutcomeView(ActionOutcome(
            StatusKind.warn,
            "Couldn't list the server's models.",
            detail: 'You can still type any model id above.',
          )),
        ),
        Align(
          alignment: Alignment.centerLeft,
          child: Padding(
            padding: const EdgeInsets.only(left: SonderSpace.xs),
            child: TextButton(
              key: const Key('settings-model-retry'),
              onPressed: _load,
              child: const Text('Try again'),
            ),
          ),
        ),
      ],
      if (choices != null && choices.ids.isEmpty)
        const _Message('The server lists no models. Type an id above.'),
      if (choices != null && choices.ids.isNotEmpty && matches.isEmpty)
        _Message('No listed model matches “$typed”.'),
      if (routes.isNotEmpty) ...[
        const _Heading('Routes'),
        for (final id in routes)
          _Option(
            key: Key('settings-model-option-$id'),
            title: id,
            subtitle: routing.routeBinding(id) ??
                (id == 'sonder' ? 'Local route' : null),
            selected: id == widget.current,
            onTap: () => _pick(id),
          ),
      ],
      if (exact.isNotEmpty) ...[
        _Heading(routing.bypassesBinding ? ModelRouting.ollamaDirect : 'Models'),
        for (final id in exact)
          _Option(
            key: Key('settings-model-option-$id'),
            title: id,
            selected: id == widget.current,
            onTap: () => _pick(id),
          ),
      ],
    ];
    final maxListHeight = (media.size.height * 0.55).clamp(200.0, 420.0);
    return Padding(
      padding: EdgeInsets.only(bottom: media.viewInsets.bottom),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: EdgeInsets.fromLTRB(SonderSpace.xl,
                wide ? SonderSpace.lg : 0, SonderSpace.sm, 0),
            child: Row(children: [
              Expanded(
                child: Semantics(
                  header: true,
                  child: Text('Default model or route',
                      style: text.titleMedium),
                ),
              ),
              IconButton(
                tooltip: 'Close',
                icon: const Icon(Icons.close),
                onPressed: () => Navigator.of(context).pop(),
              ),
            ]),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(SonderSpace.xl, SonderSpace.xs,
                SonderSpace.xl, SonderSpace.sm),
            child: TextField(
              key: const Key('settings-model-search'),
              controller: _query,
              autofocus: wide,
              autocorrect: false,
              style: tokens.mono(13.5, height: 22),
              onChanged: (_) => setState(() {}),
              onSubmitted: (_) => _submit(),
              decoration: InputDecoration(
                hintText: 'Search, or type any model id',
                hintStyle: text.bodyMedium?.copyWith(color: tokens.muted),
                prefixIcon: Icon(Icons.search, size: 18, color: tokens.muted),
              ),
            ),
          ),
          ConstrainedBox(
            constraints: BoxConstraints(maxHeight: maxListHeight),
            child: ListView(
              shrinkWrap: true,
              padding: const EdgeInsets.fromLTRB(
                  SonderSpace.md, 0, SonderSpace.md, SonderSpace.lg),
              children: rows,
            ),
          ),
        ],
      ),
    );
  }
}

class _Heading extends StatelessWidget {
  final String text;
  const _Heading(this.text);

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(
            SonderSpace.sm, SonderSpace.md, SonderSpace.sm, SonderSpace.xs),
        child: Semantics(
          header: true,
          child: Text(text.toUpperCase(),
              style: Theme.of(context).textTheme.labelSmall),
        ),
      );
}

class _Message extends StatelessWidget {
  final String text;
  const _Message(this.text);

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.all(SonderSpace.md),
        child: Text(text, style: Theme.of(context).textTheme.bodySmall),
      );
}

class _Option extends StatelessWidget {
  final String title;
  final String? subtitle;
  final bool selected;
  final bool mono;
  final IconData? icon;
  final VoidCallback onTap;

  const _Option({
    super.key,
    required this.title,
    required this.onTap,
    this.subtitle,
    this.selected = false,
    this.mono = true,
    this.icon,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return HoverSurface(
      onTap: onTap,
      selected: selected,
      semanticLabel: [
        title,
        if (subtitle != null) subtitle!,
        if (selected) 'current',
      ].join(', '),
      child: ExcludeSemantics(
        child: Container(
          constraints: const BoxConstraints(minHeight: 48),
          padding: const EdgeInsets.symmetric(
              horizontal: SonderSpace.sm, vertical: SonderSpace.sm),
          child: Row(children: [
            SizedBox(
              width: SonderSpace.xxl,
              child: selected
                  ? Icon(Icons.check, size: 16, color: tokens.accentText)
                  : icon == null
                      ? null
                      : Icon(icon, size: 16, color: tokens.text2),
            ),
            const SizedBox(width: SonderSpace.xs),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: mono
                          ? tokens.mono(13,
                              weight: selected
                                  ? FontWeight.w600
                                  : FontWeight.w400)
                          : text.bodyMedium
                              ?.copyWith(fontWeight: FontWeight.w500)),
                  if (subtitle != null)
                    Text(subtitle!,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: text.bodySmall),
                ],
              ),
            ),
          ]),
        ),
      ),
    );
  }
}
