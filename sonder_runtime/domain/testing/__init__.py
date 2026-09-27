"""Pure policy for structured test runs: runners, selectors, reports, parsers.

Nothing in this package executes, opens files or reads the environment. The
argv a test run launches is assembled here from host-owned templates; a model
chooses only a runner name and a selector that must match a strict per-runner
grammar.
"""
