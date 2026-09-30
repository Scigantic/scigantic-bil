from __future__ import annotations

import pytest

import scigantic_bil as bil


def test_retrieve_full_record() -> None:
    d = bil.retrieve("ace-cup-eel")
    assert d.bildid == "ace-cup-eel"
    assert d.microscope_type == "Light-sheet"
    assert d.species == "mouse"
    assert d.rights_identifier == "CC-BY-SA-4.0"
    assert d.technique == "other" and "light sheet" in d.technique_other.lower()
    assert d.is_light_sheet
    assert d.contributors and d.contributors[0].orcid.startswith("https://orcid.org/")
    assert d.publications and d.publications[0].doi.startswith("https://doi.org/")
    assert d.url.endswith("/42e4274e0579397f/subject_5/")
    assert d.raw["Dataset"][0]["bildirectory"] == d.bildirectory


def test_retrieve_unknown_id_is_not_found() -> None:
    with pytest.raises(bil.BilNotFoundError):
        bil.retrieve("zzz-zzz-zzz")


def test_retrieve_many_batches_and_drops_unknown() -> None:
    got = bil.retrieve_many(["ace-cup-eel", "ace-bin-run", "zzz-zzz-zzz"])
    assert set(got) == {"ace-cup-eel", "ace-bin-run"}
    assert got["ace-bin-run"].technique == "light sheet microscopy"


def test_fulltext_and_structured_query_disagree_on_purpose() -> None:
    # Documented in api.py: structured match is exact, fulltext is broad.
    exact = bil.query("instrument", microscopetype="Light-sheet")
    broad = bil.fulltext("light sheet")
    assert 0 < len(exact) < 50
    assert len(broad) > 700
    assert set(exact) <= set(broad)


def test_query_rejects_zero_or_two_elements() -> None:
    with pytest.raises(ValueError):
        bil.query("specimen")
    with pytest.raises(ValueError):
        bil.query("specimen", species="mouse", sex="F")


def test_unknown_division_is_a_clear_error() -> None:
    with pytest.raises(bil.BilError):
        bil.query("nope", x="1")


def test_cached_lookup_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    bil.retrieve("ace-bin-run")  # warm
    from scigantic_bil import _client

    def boom(*a: object, **k: object) -> object:
        raise AssertionError("network call on a cached lookup")

    monkeypatch.setattr(_client, "send", boom)
    monkeypatch.setattr("scigantic_bil.api.send", boom)
    assert bil.retrieve("ace-bin-run").bildid == "ace-bin-run"


def test_disable_cache_forces_network(monkeypatch: pytest.MonkeyPatch) -> None:
    bil.disable_cache()
    try:
        calls: list[str] = []
        real = bil.api.send

        def spy(method: str, url: str, **kw: object) -> object:
            calls.append(url)
            return real(method, url, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr("scigantic_bil.api.send", spy)
        bil.retrieve("ace-bin-run")
        assert calls
    finally:
        bil.enable_cache()


def test_send_retries_transient_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    from scigantic_bil import _client

    calls: list[int] = []

    class Resp:
        def __init__(self, code: int) -> None:
            self.status_code = code
            self.url = "https://x/"
            self.text = ""
            self.headers: dict[str, str] = {}

        def close(self) -> None:
            pass

    class Session:
        headers: dict[str, str] = {}

        def request(self, *a: object, **k: object) -> Resp:
            calls.append(1)
            return Resp(503 if len(calls) < 3 else 200)

    monkeypatch.setattr(_client, "_session", Session())
    monkeypatch.setattr(_client.time, "sleep", lambda s: None)
    resp = _client.send("GET", "https://x/")
    assert resp.status_code == 200 and len(calls) == 3


def test_send_does_not_retry_a_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # A read timeout means the server is still rendering the response; the
    # same request again is no faster. Five attempts at 120 s each made one
    # slow 44,000-entry listing a ten-minute failure in CI (2026-09-26).
    import requests

    from scigantic_bil import _client

    calls: list[int] = []

    class Session:
        headers: dict[str, str] = {}

        def request(self, *a: object, **k: object) -> object:
            calls.append(1)
            raise requests.ReadTimeout("read timed out")

    def no_sleep(seconds: float) -> None:
        raise AssertionError("backed off before a retry")

    monkeypatch.setattr(_client, "_session", Session())
    monkeypatch.setattr(_client.time, "sleep", no_sleep)
    with pytest.raises(bil.BilError, match="sent no data for 7 s") as info:
        _client.send("GET", "https://x/", timeout=7.0)
    assert len(calls) == 1 and not isinstance(info.value, bil.BilNotFoundError)

    # The same stall mid-body arrives as a ConnectionError wrapping urllib3's
    # ReadTimeoutError; it is not retried either.
    from urllib3.exceptions import ReadTimeoutError

    class MidBodySession(Session):
        def request(self, *a: object, **k: object) -> object:
            calls.append(1)
            raise requests.ConnectionError(ReadTimeoutError(None, "https://x/", "Read timed out."))

    del calls[:]
    monkeypatch.setattr(_client, "_session", MidBodySession())
    with pytest.raises(bil.BilError, match="sent no data"):
        _client.send("GET", "https://x/", timeout=7.0)
    assert len(calls) == 1


def test_retrieve_many_thousand_ids(catalog: bil.BilCatalog) -> None:
    import random

    ids = random.Random(3).sample([d.bildid for d in catalog.datasets], 300)
    got = bil.retrieve_many(ids)
    assert len(got) >= 295  # a handful of inventory ids can lag the API


def test_retrieve_many_all_unknown_batch_is_empty_not_405() -> None:
    # BIL answers HTTP 405 "POST failure, no entries found" when no id in a
    # batch exists; that is zero hits, not an error (stress test 2026-09-23).
    assert bil.retrieve_many(["zzz-zzz-zzz", "yyy-yyy-yyy"]) == {}
    assert bil.retrieve_many([]) == {}
