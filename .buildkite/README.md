# Protected Janus Python CI preparation

This is additive ordinary CI preparation. Automatic main-push, all pull-request
and manual Actions checks use read-only hosted Ubuntu 24.04 workers while native
commissioning proceeds; checkout never persists credentials. Both Python lanes
retain `fail-fast: false`, so one failure does not cancel the other lane. Hosted
routing keeps automatic coverage working until a separate reviewed Janus cutover.

The pipeline reference calls `janus-ci bootstrap`; configure and verify the
image-owned `hermes-muninndb-shadow-ci` policy before native use. No repository
pipeline file grants admission. Each independent lane preserves the original
360-minute Actions bound and has an empty environment allowlist and artifact list:

| Job | Protected interpreter | Command |
| --- | --- | --- |
| `shadow-python-3-11` | Python 3.11.16 | `python -I -B scripts/buildkite/check.py 3.11` |
| `shadow-python-3-12` | Python 3.12.12 | `python -I -B scripts/buildkite/check.py 3.12` |

The shared image supplies checksum-pinned Python, venv/ensurepip and TLS support.
Jobs run without credentials as UID 10001 in fresh restricted containers. The
native admission, trusted source/merge validation and statuses are tracked by
commissioning evidence for each exact source and image. A successful native run
verifies only that recorded revision; automatic Actions remains until cutover.

Each lane creates private check and wheel virtualenvs. It preserves the original
pip upgrade, editable development install, pytest, wrapper compilation, package
build, wheel install and materialized-plugin import. Child environments allow only
local HOME/HERMES_HOME/XDG/TMPDIR paths and required runtime variables. Inherited
credentials, pip configuration, Python paths and pytest options are not forwarded.
The wheel CLI uses `--no-activate --json`; verification rejects activation and a
plugin path outside the temporary Hermes home. `MANIFEST.in` includes this wrapper
so tests also work in source distributions.

TERM, INT and HUP record the first cancellation signal. Each command runs in its
own process group; cleanup sends TERM, waits at most three seconds, then KILLs
remaining group members and reaps the direct child. The private virtualenvs, homes,
configuration and caches are removed on success, ordinary failure and handled
cancellation. The controller still owns exhaustive UID cleanup, including escaped
process sessions and hard termination; wrapper tests do not prove provider cleanup.

## Separate publishing boundary

`.github/workflows/publish.yml` is unchanged. Its Python 3.12 distribution build,
original wheel/sdist artifact, release-only PyPI job, protected `pypi` environment,
OIDC identity and attestations remain authoritative. This ordinary CI preparation
neither publishes artifacts nor replaces release/manual input or publishing
handoffs. Earlier local release preparation is parked separately and unpublished.

Dependencies retain their existing unpinned pip upgrades and project constraints;
no reproducible dependency-resolution claim is made. Before cutover, verify both
native PR/main lanes, exact source and base identity, failure/cancellation/status
behavior, hosted fork isolation, required statuses and a successful hosted manual
fallback. Automatic Actions must remain until those gates pass.
