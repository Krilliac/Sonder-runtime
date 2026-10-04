import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../runtime/model_routing.dart';
import '../theme.dart';

/// The picker's groups, in display order.
enum ModelGroupKind {
  /// `sonder`, `local` and the policy tiers: they follow the bindings.
  routes('Routes'),

  /// Exact models while every route runs on Ollama.
  local('Local models'),

  /// Exact models when a route is bound off Ollama: they still run on
  /// Ollama directly and bypass the binding (see [ModelRouting]).
  direct(ModelRouting.ollamaDirect);

  final String title;
  const ModelGroupKind(this.title);
}

/// One heading and its models.
class ModelGroup {
  final ModelGroupKind kind;
  final List<String> models;
  const ModelGroup(this.kind, this.models);
}

/// Split [models] (the `/v1/models` ids, in the server's order) into
/// routes and exact models, keeping only those matching [query] (case
/// insensitive, on the id and on its label).
List<ModelGroup> groupModels(List<String> models, ModelRouting routing,
    {String query = ''}) {
  final needle = query.trim().toLowerCase();
  bool matches(String id) =>
      needle.isEmpty ||
      id.toLowerCase().contains(needle) ||
      routing.pickerLabel(id).toLowerCase().contains(needle);
  final routes = [
    for (final m in models)
      if (routing.isRoute(m) && matches(m)) m
  ];
  final exact = [
    for (final m in models)
      if (!routing.isRoute(m) && matches(m)) m
  ];
  return [
    if (routes.isNotEmpty) ModelGroup(ModelGroupKind.routes, routes),
    if (exact.isNotEmpty)
      ModelGroup(
          routing.bypassesBinding
              ? ModelGroupKind.direct
              : ModelGroupKind.local,
          exact),
  ];
}

/// The composer's short label for [id]: what will answer. A route bound off
/// Ollama names the model it is served with (`sonder · qwen3:14b`); any
/// other id keeps its picker label (`qwen3:14b · Ollama (direct)`,
/// `sonder (local route)`). The picker rows keep the full binding.
///
/// [dense] (a phone) also drops the default route's `(local route)`.
String compactModelLabel(String id, ModelRouting routing,
    {bool dense = false}) {
  if (routing.isRoute(id)) {
    final provider = routing.routeProvider(id);
    final served = routing.routeServedModel(id);
    if (provider != null && provider != ollamaProvider && served != null) {
      return '$id · $served';
    }
    if (dense && routing.routeBinding(id) == null) return id;
  }
  return routing.pickerLabel(id);
}

/// A model label split for display: the id, then the rest in a quieter
/// tone. The plain text is [label] ([ModelRouting.pickerLabel] by default).
InlineSpan modelLabelSpan(String id, ModelRouting routing, SonderTokens tokens,
    {required TextStyle idStyle, required TextStyle restStyle, String? label}) {
  label ??= routing.pickerLabel(id);
  if (label.startsWith(id) && label.length > id.length) {
    return TextSpan(children: [
      TextSpan(text: id, style: idStyle),
      TextSpan(text: label.substring(id.length), style: restStyle),
    ]);
  }
  return TextSpan(text: label, style: idStyle);
}

/// The composer's model control: the current route or model, opening a
/// searchable picker grouped as routes, local models and Ollama (direct).
class ModelPickerButton extends StatelessWidget {
  final List<String> models;
  final String current;
  final ModelRouting routing;
  final ValueChanged<String> onSelected;

  /// A narrow layout: the label keeps only what tells routes apart.
  final bool dense;

  const ModelPickerButton({
    super.key,
    required this.models,
    required this.current,
    required this.routing,
    required this.onSelected,
    this.dense = false,
  });

  Future<void> _open(BuildContext context) async {
    final picked = await showModelPicker(context,
        models: models, current: current, routing: routing);
    if (picked != null && picked != current) onSelected(picked);
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final label = routing.pickerLabel(current);
    return Tooltip(
      message: 'Choose inference route or model',
      child: Semantics(
        container: true,
        button: true,
        label: 'Model: $label',
        hint: 'Double tap to choose a route or model',
        excludeSemantics: true,
        onTap: () => _open(context),
        child: InkWell(
          key: const Key('model-picker'),
          onTap: () => _open(context),
          borderRadius: BorderRadius.circular(SonderRadius.pill),
          child: ConstrainedBox(
            constraints: const BoxConstraints(minHeight: 48),
            child: Center(
              widthFactor: 1,
              child: Container(
                constraints: const BoxConstraints(minHeight: 28),
                padding: const EdgeInsets.fromLTRB(
                    SonderSpace.md, 0, SonderSpace.xs, 0),
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(SonderRadius.pill),
                  border: Border.all(color: tokens.hairlineStrong),
                ),
                child: Row(mainAxisSize: MainAxisSize.min, children: [
                  Flexible(
                    child: Text.rich(
                      modelLabelSpan(current, routing, tokens,
                          label:
                              compactModelLabel(current, routing, dense: dense),
                          idStyle: tokens.mono(12,
                              color: tokens.text, weight: FontWeight.w500),
                          restStyle: tokens.mono(12, color: tokens.muted)),
                      key: const Key('model-picker-label'),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                  const SizedBox(width: SonderSpace.xxs),
                  Icon(Icons.expand_more, size: 16, color: tokens.muted),
                ]),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// Width below which the picker is a bottom sheet instead of a popover.
const _sheetBelow = 600.0;

/// Open the picker anchored to [context]'s widget (a popover above it on a
/// wide window, a bottom sheet on a phone). Resolves to the picked id, or
/// null when dismissed.
Future<String?> showModelPicker(
  BuildContext context, {
  required List<String> models,
  required String current,
  required ModelRouting routing,
}) {
  final media = MediaQuery.sizeOf(context);
  final panel = _ModelPickerPanel(
      models: models,
      current: current,
      routing: routing,
      autofocus: media.width >= _sheetBelow);
  if (media.width < _sheetBelow) {
    final tokens = SonderTokens.of(context);
    return showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      backgroundColor: tokens.panel,
      shape: RoundedRectangleBorder(
        borderRadius: const BorderRadius.vertical(
            top: Radius.circular(SonderRadius.sheet)),
        side: BorderSide(color: tokens.hairline),
      ),
      builder: (context) => SizedBox(
        height: MediaQuery.sizeOf(context).height * 0.7,
        child: panel,
      ),
    );
  }
  final box = context.findRenderObject() as RenderBox?;
  final overlay =
      Navigator.of(context).overlay?.context.findRenderObject() as RenderBox?;
  Rect anchor = Rect.fromLTWH(media.width / 2, media.height / 2, 0, 0);
  if (box != null && overlay != null && box.hasSize) {
    final topLeft = box.localToGlobal(Offset.zero, ancestor: overlay);
    anchor = topLeft & box.size;
  }
  return Navigator.of(context).push<String>(_PopoverRoute(
    anchor: anchor,
    barrierLabel: MaterialLocalizations.of(context).modalBarrierDismissLabel,
    child: panel,
  ));
}

/// A light popover above (or below) an anchor rectangle.
class _PopoverRoute extends PopupRoute<String> {
  final Rect anchor;
  final Widget child;

  _PopoverRoute({
    required this.anchor,
    required this.child,
    required String barrierLabel,
  }) : _barrierLabel = barrierLabel;

  final String _barrierLabel;

  @override
  Color? get barrierColor => null;

  @override
  bool get barrierDismissible => true;

  @override
  String? get barrierLabel => _barrierLabel;

  @override
  Duration get transitionDuration => SonderMotion.medium;

  @override
  Duration get reverseTransitionDuration => SonderMotion.fast;

  @override
  Widget buildPage(BuildContext context, Animation<double> animation,
      Animation<double> secondaryAnimation) {
    final tokens = SonderTokens.of(context);
    final media = MediaQuery.of(context);
    return CustomSingleChildLayout(
      delegate: _PopoverLayout(anchor: anchor, padding: media.padding),
      child: Material(
        type: MaterialType.card,
        color: tokens.panel,
        elevation: 0,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(SonderRadius.card),
          side: BorderSide(color: tokens.hairlineStrong),
        ),
        clipBehavior: Clip.antiAlias,
        child: child,
      ),
    );
  }

  @override
  Widget buildTransitions(BuildContext context, Animation<double> animation,
      Animation<double> secondaryAnimation, Widget child) {
    if (MediaQuery.maybeDisableAnimationsOf(context) == true) return child;
    final curved = CurvedAnimation(
        parent: animation,
        curve: SonderMotion.enter,
        reverseCurve: SonderMotion.exit);
    return FadeTransition(
      opacity: curved,
      child: AnimatedBuilder(
        animation: curved,
        builder: (context, child) => Transform.translate(
          offset: Offset(0, (1 - curved.value) * SonderSpace.sm),
          child: child,
        ),
        child: child,
      ),
    );
  }
}

/// Places the popover above the anchor when there is more room there (the
/// composer sits at the bottom), left-aligned with it, inside the window.
class _PopoverLayout extends SingleChildLayoutDelegate {
  final Rect anchor;
  final EdgeInsets padding;
  const _PopoverLayout({required this.anchor, required this.padding});

  static const _margin = SonderSpace.lg;
  static const _gap = SonderSpace.xs;
  static const _width = 400.0;
  static const _maxHeight = 460.0;

  bool _above(Size size) =>
      anchor.top - padding.top > size.height - anchor.bottom - padding.bottom;

  @override
  BoxConstraints getConstraintsForChild(BoxConstraints constraints) {
    final size = constraints.biggest;
    final room = _above(size)
        ? anchor.top - padding.top - _margin - _gap
        : size.height - anchor.bottom - padding.bottom - _margin - _gap;
    final width = (size.width - 2 * _margin).clamp(0.0, _width);
    return BoxConstraints(
      minWidth: width,
      maxWidth: width,
      maxHeight: room.clamp(120.0, _maxHeight),
    );
  }

  @override
  Offset getPositionForChild(Size size, Size childSize) {
    final x =
        anchor.left.clamp(_margin, size.width - childSize.width - _margin);
    final y = _above(size)
        ? anchor.top - _gap - childSize.height
        : anchor.bottom + _gap;
    return Offset(x, y);
  }

  @override
  bool shouldRelayout(_PopoverLayout old) =>
      old.anchor != anchor || old.padding != padding;
}

class _ModelPickerPanel extends StatefulWidget {
  final List<String> models;
  final String current;
  final ModelRouting routing;
  final bool autofocus;

  const _ModelPickerPanel({
    required this.models,
    required this.current,
    required this.routing,
    required this.autofocus,
  });

  @override
  State<_ModelPickerPanel> createState() => _ModelPickerPanelState();
}

class _ModelPickerPanelState extends State<_ModelPickerPanel> {
  final _search = TextEditingController();
  final _list = ScrollController();
  final _highlightKey = GlobalKey();
  String _query = '';

  /// Index into the visible (filtered) models of the keyboard highlight.
  int _highlight = 0;

  @override
  void initState() {
    super.initState();
    final all = _visible;
    final at = all.indexOf(widget.current);
    _highlight = at < 0 ? 0 : at;
    _search.addListener(() {
      final next = _search.text;
      if (next == _query) return;
      setState(() {
        _query = next;
        _highlight = 0;
      });
    });
  }

  @override
  void dispose() {
    _search.dispose();
    _list.dispose();
    super.dispose();
  }

  List<ModelGroup> get _groups =>
      groupModels(widget.models, widget.routing, query: _query);

  List<String> get _visible => [for (final g in _groups) ...g.models];

  void _pick(String id) => Navigator.of(context).pop(id);

  /// Keep the keyboard highlight on screen as it moves.
  void _revealHighlight() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final target = _highlightKey.currentContext;
      if (target == null || !mounted) return;
      Scrollable.ensureVisible(target,
          alignment: 0.5,
          duration: SonderMotion.of(context, SonderMotion.fast),
          curve: SonderMotion.standard);
    });
  }

  KeyEventResult _onKey(FocusNode node, KeyEvent event) {
    if (event is! KeyDownEvent && event is! KeyRepeatEvent) {
      return KeyEventResult.ignored;
    }
    final visible = _visible;
    final key = event.logicalKey;
    if (key == LogicalKeyboardKey.arrowDown && visible.isNotEmpty) {
      setState(() => _highlight = (_highlight + 1) % visible.length);
      _revealHighlight();
      return KeyEventResult.handled;
    }
    if (key == LogicalKeyboardKey.arrowUp && visible.isNotEmpty) {
      setState(() =>
          _highlight = (_highlight - 1 + visible.length) % visible.length);
      _revealHighlight();
      return KeyEventResult.handled;
    }
    if ((key == LogicalKeyboardKey.enter ||
            key == LogicalKeyboardKey.numpadEnter) &&
        visible.isNotEmpty) {
      _pick(visible[_highlight.clamp(0, visible.length - 1)]);
      return KeyEventResult.handled;
    }
    return KeyEventResult.ignored;
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final groups = _groups;
    final visible = [for (final g in groups) ...g.models];
    var index = 0;
    final rows = <Widget>[];
    for (final group in groups) {
      rows.add(_GroupHeading(kind: group.kind));
      for (final id in group.models) {
        final i = index++;
        rows.add(_ModelRow(
          key: i == _highlight ? _highlightKey : null,
          id: id,
          routing: widget.routing,
          // Under a model heading the heading already names the host.
          showBinding: group.kind == ModelGroupKind.routes,
          selected: id == widget.current,
          highlighted: i == _highlight,
          onTap: () => _pick(id),
        ));
      }
    }
    return Semantics(
      key: const Key('model-picker-panel'),
      scopesRoute: true,
      namesRoute: true,
      explicitChildNodes: true,
      label: 'Choose a route or model',
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(
                SonderSpace.md, SonderSpace.md, SonderSpace.md, SonderSpace.sm),
            child: Focus(
              onKeyEvent: _onKey,
              child: TextField(
                key: const Key('model-picker-search'),
                controller: _search,
                autofocus: widget.autofocus,
                textInputAction: TextInputAction.done,
                onSubmitted: (_) {
                  if (visible.isNotEmpty) {
                    _pick(visible[_highlight.clamp(0, visible.length - 1)]);
                  }
                },
                style: text.bodyMedium,
                decoration: InputDecoration(
                  hintText: 'Search models',
                  isDense: true,
                  prefixIcon: Icon(Icons.search, size: 18, color: tokens.muted),
                  prefixIconConstraints:
                      const BoxConstraints(minWidth: 40, minHeight: 36),
                ),
              ),
            ),
          ),
          Divider(height: 1, color: tokens.hairline),
          Flexible(
            child: visible.isEmpty
                ? Padding(
                    padding: const EdgeInsets.all(SonderSpace.xl),
                    child: Text(
                      'No model matches "${_query.trim()}".',
                      key: const Key('model-picker-empty'),
                      textAlign: TextAlign.center,
                      style: text.bodySmall,
                    ),
                  )
                : ListView(
                    key: const Key('model-picker-list'),
                    controller: _list,
                    shrinkWrap: true,
                    padding:
                        const EdgeInsets.symmetric(vertical: SonderSpace.xs),
                    children: rows,
                  ),
          ),
        ],
      ),
    );
  }
}

class _GroupHeading extends StatelessWidget {
  final ModelGroupKind kind;
  const _GroupHeading({required this.kind});

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Semantics(
      header: true,
      child: Padding(
        key: Key(switch (kind) {
          ModelGroupKind.routes => 'model-group-routes',
          ModelGroupKind.local => 'model-group-local',
          ModelGroupKind.direct => 'model-group-ollama-direct',
        }),
        padding: const EdgeInsets.fromLTRB(
            SonderSpace.lg, SonderSpace.md, SonderSpace.lg, SonderSpace.xs),
        child: Text(kind.title, style: text.labelSmall),
      ),
    );
  }
}

class _ModelRow extends StatelessWidget {
  final String id;
  final ModelRouting routing;
  final bool showBinding;
  final bool selected;
  final bool highlighted;
  final VoidCallback onTap;

  const _ModelRow({
    super.key,
    required this.id,
    required this.routing,
    required this.showBinding,
    required this.selected,
    required this.highlighted,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final idStyle = tokens.mono(13,
        color: tokens.text,
        weight: selected ? FontWeight.w600 : FontWeight.w400);
    return Semantics(
      button: true,
      selected: selected,
      label: routing.pickerLabel(id),
      excludeSemantics: true,
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: SonderSpace.xs),
        child: Material(
          color: highlighted ? tokens.raised : Colors.transparent,
          borderRadius: BorderRadius.circular(SonderRadius.row),
          child: InkWell(
            key: Key('model-option-$id'),
            onTap: onTap,
            borderRadius: BorderRadius.circular(SonderRadius.row),
            child: ConstrainedBox(
              constraints: const BoxConstraints(minHeight: 48),
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: SonderSpace.md),
                child: Row(children: [
                  SizedBox(
                    width: 24,
                    child: selected
                        ? Icon(Icons.check, size: 16, color: tokens.accentText)
                        : null,
                  ),
                  Expanded(
                    child: Text.rich(
                      showBinding
                          ? modelLabelSpan(id, routing, tokens,
                              idStyle: idStyle,
                              restStyle: tokens.mono(13, color: tokens.muted))
                          : TextSpan(text: id, style: idStyle),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                ]),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
