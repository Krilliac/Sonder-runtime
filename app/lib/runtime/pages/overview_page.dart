part of '../runtime_screen.dart';

/// Overview: one tile per fact (each opens the page that owns it), then the
/// newest execution events.
class _OverviewPage extends StatelessWidget {
  final _RuntimeScreenState s;
  const _OverviewPage(this.s);

  @override
  Widget build(BuildContext context) {
    final info = s._info;
    final navigator = CategoryNavigator.maybeOf(context);
    return RuntimeOverview(
      rows: overviewRows(
        serverUrl: s.widget.settings.serverUrl,
        info: info,
        offline: s._offline,
        serverError: s._serverError,
        loading: s._loading,
        workRuns: s._workRuns,
        workRunsError: s._workRunsError,
        approvals: s._approvals,
        approvalsError: s._approvalsError,
        now: s.widget.now,
      ),
      activity: recentActivity(info?.executionFeed),
      loading: s._loading || s._loadingExtras,
      onOpen: navigator?.select,
    );
  }
}
