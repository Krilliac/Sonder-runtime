# Packaged WPA export profiles

`wpaexporter` turns an ETW trace (`.etl`) into CSV tables using a WPA
profile (`.wpaProfile`). The crash/profile digest planner
(`sonder_runtime/adapters/debugging/planner.py`) looks for exactly one
packaged profile here:

| File | Purpose | Status |
|---|---|---|
| `cpu_sampled.wpaProfile` | CPU Usage (Sampled) by process, thread and stack, exported as CSV | not shipped yet |

Until `cpu_sampled.wpaProfile` exists in this directory, a request for
`engine=wpaexporter` is refused with `ENGINE_UNAVAILABLE`, and ETL traces go
through `xperf` (experimental) instead.

Rules for adding the profile:

- Author it in WPA on a Windows workstation against a real capture (Windows
  live-validation step 8 of the crash/profile digest plan) and keep only the
  CPU Usage (Sampled) table with the columns the WPA CSV reader understands
  (function or stack, weight or count).
- The profile is data the host owns. It is never taken from a tool argument,
  a capture directory or the project tree, and `wpaexporter` only ever gets
  `-profile <this directory>\cpu_sampled.wpaProfile` from the template.
- Keep it free of machine names, user names and symbol paths; symbol lookup
  is configured by the runtime (`_NT_SYMCACHE_PATH` inside the run directory,
  no symbol servers without console consent).
