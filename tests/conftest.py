"""Live tests: every test talks to the real Brain Image Library, matching the
rest of the scigantic-* family. A private cache directory per session keeps
runs independent of anything already on the developer's machine."""

from __future__ import annotations

import socket
from urllib.parse import urlsplit

import pytest

import scigantic_bil as bil
from scigantic_bil import _client, brainpi

# The hosts the suite talks to. BIL runs them at two sites, and they fail
# independently: on 2026-11-21 the download and BrainPi hosts refused
# connections while the API host answered.
_HOSTS = sorted(
    {urlsplit(u).hostname or "" for u in (_client.API_BASE, _client.DOWNLOAD_BASE, brainpi._LINKS_URL)}
)


def _reachable(host: str) -> bool:
    for _ in range(2):
        try:
            socket.create_connection((host, 443), timeout=10).close()
            return True
        except OSError:
            continue
    return False


_DOWN = [h for h in _HOSTS if not _reachable(h)]


def pytest_report_header() -> list[str]:
    return [f"BIL hosts unreachable, tests that need them will skip: {', '.join(_DOWN)}"] if _DOWN else []


# Hosts from _DOWN that the running test tried and failed to reach.
_hit: list[str] = []


class _HostDown(OSError):
    """Not a requests exception on purpose: send() retries those with
    backoff, and a host that was down a moment ago needs neither."""


@pytest.fixture(scope="session", autouse=True)
def _outage_guard() -> None:
    """Only when a host was unreachable at session start: a request to it
    fails at once, without the connect wait and the retries (which, across
    the suite, come to more than the CI job allows), and is noted so the
    hooks below can tell an outage from a bug. Requests to the hosts that
    are up are untouched."""
    if not _DOWN:
        return
    session = _client.get_session()
    real = session.request

    def request(method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        host = urlsplit(str(url)).hostname
        if host in _DOWN:
            _hit.append(host)
            raise _HostDown(f"{host} was unreachable when the test session started")
        return real(method, url, *args, **kwargs)

    session.request = request  # type: ignore[method-assign]


def _outage_skip(exc: BaseException | None) -> pytest.skip.Exception | None:
    """A skip when the test failed after a request to a down host failed
    (whatever the test then raised: the CLI turns the error into an exit
    code), or when the error itself names one (zarr and fsspec open their
    own connections). None for anything else, which still fails."""
    if exc is None or not _DOWN or isinstance(exc, pytest.skip.Exception):
        return None
    down = _hit[0] if _hit else next((h for h in _DOWN if h in str(exc)), None)
    return pytest.skip.Exception(f"{down} is unreachable") if down else None


def _skip_on_outage(outcome) -> None:  # type: ignore[no-untyped-def]
    skip = _outage_skip(outcome.excinfo[1] if outcome.excinfo else None)
    if skip:
        outcome.force_exception(skip)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item: pytest.Item):  # type: ignore[no-untyped-def]
    del _hit[:]
    _skip_on_outage((yield))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):  # type: ignore[no-untyped-def]
    _skip_on_outage((yield))


@pytest.fixture(scope="session", autouse=True)
def _isolated_cache(tmp_path_factory: pytest.TempPathFactory) -> None:
    bil.enable_cache(cache_dir=str(tmp_path_factory.mktemp("bil-cache")))


@pytest.fixture(scope="session")
def catalog() -> bil.BilCatalog:
    return bil.BilCatalog.load()


# Two datasets with known, stable shapes, verified live 2026-09-08.
TIFF_STACK = "ace-bin-run"  # Kim lab LSFM, 1,923 single-slice TIFFs, one folder
ZARR_STORE = "ace-cup-eel"  # UC Irvine iDISCO light sheet, one OME-Zarr v2 store, 7 levels
