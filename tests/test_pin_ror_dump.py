"""scripts/pin_ror_dump.py against recorded Zenodo responses and local zips.

Two real responses are replayed from tests/fixtures/, recorded once:

  zenodo_listing_page1.json   the size=25, page=1 listing of the ROR concept
                              record that the script actually requests, as
                              Zenodo returned it on 2026-10-01
  zenodo_record_22902037.json the record response for the release that
                              config/ror_dump.yaml pins (v2.13)

The committed pin was derived from exactly that listing, so the strongest
test here is that reading the fixture reproduces the pin field for field.
Everything else (zips, downloads, pins with made-up hashes) is built in
tmp_path or served by httpx.MockTransport. Nothing touches the network.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from disambig.httpcache import CachingClient
from disambig.ror_dump import RorDumpHashMismatch, RorDumpPin, iter_dump_records, sha256_file

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import pin_ror_dump  # noqa: E402
from pin_ror_dump import (  # noqa: E402
    LISTING_PAGE_SIZE,
    MAX_DOWNLOAD_ATTEMPTS,
    ListedHit,
    Release,
    download_zip,
    extract_v2_json,
    fetch_hits,
    listing_url,
    parse_listing_page,
    release_from_hit,
    select_hit,
    verify_against_pin,
)

FIXTURES = Path(__file__).parent / "fixtures"
LISTING = FIXTURES / "zenodo_listing_page1.json"
RECORD = FIXTURES / "zenodo_record_22902037.json"
SAMPLE = FIXTURES / "ror_dump_sample.json"
PIN = PROJECT_ROOT / "config" / "ror_dump.yaml"


def listing_payload() -> dict[str, Any]:
    payload = json.loads(LISTING.read_bytes())
    assert isinstance(payload, dict)
    return payload


def listed_hits() -> list[ListedHit]:
    hits, _total = parse_listing_page(listing_payload(), listing_url(1))
    return [ListedHit(hit=hit, page_url=listing_url(1)) for hit in hits]


def newest_hit() -> dict[str, Any]:
    return copy.deepcopy(select_hit(listed_hits(), None).hit)


# --- the recorded listing -------------------------------------------------


def test_recorded_listing_page_is_one_full_page_of_dated_versions() -> None:
    hits, total = parse_listing_page(listing_payload(), listing_url(1))
    assert len(hits) == LISTING_PAGE_SIZE
    assert total >= len(hits)
    versions = [hit["metadata"]["version"] for hit in hits]
    assert all(isinstance(version, str) and version.startswith("v") for version in versions)
    assert len(set(versions)) == len(versions)
    assert all(isinstance(hit["metadata"]["publication_date"], str) for hit in hits)


def test_newest_release_reproduces_the_committed_pin() -> None:
    listed = select_hit(listed_hits(), None)
    release = release_from_hit(listed.hit, listed.page_url)
    pin = RorDumpPin.from_yaml(PIN)
    assert release == Release(
        version=pin.version,
        record_id=pin.record_id,
        doi=pin.zenodo_doi,
        concept_doi=pin.zenodo_concept_doi,
        publication_date=pin.publication_date,
        filename=pin.filename,
        size_bytes=pin.size_bytes,
        md5=pin.md5_zenodo,
        content_url=pin.source_url,
        listing_url=pin.listing_url,
    )


def test_newest_is_chosen_by_publication_date_not_by_listing_order() -> None:
    hits = listed_hits()
    forwards = select_hit(hits, None).hit["id"]
    backwards = select_hit(list(reversed(hits)), None).hit["id"]
    assert forwards == backwards == RorDumpPin.from_yaml(PIN).record_id


def test_select_by_version_finds_an_older_release() -> None:
    hits = listed_hits()
    older = select_hit(hits, "v2.12")
    assert older.hit["metadata"]["version"] == "v2.12"
    release = release_from_hit(older.hit, older.page_url)
    assert release.version == "v2.12"
    assert release.record_id != RorDumpPin.from_yaml(PIN).record_id
    assert release.filename.endswith(".zip")
    assert len(release.md5) == 32


def test_unknown_version_names_the_versions_that_exist() -> None:
    with pytest.raises(ValueError, match=r"'v9\.99' is not on Zenodo.*v2\.13"):
        select_hit(listed_hits(), "v9.99")


def test_no_dated_hits_is_an_error() -> None:
    undated = [ListedHit(hit={"id": 1, "metadata": {"version": "v0.0"}}, page_url=listing_url(1))]
    with pytest.raises(ValueError, match="No dated ROR releases"):
        select_hit(undated, None)


# --- the recorded record response ----------------------------------------


def test_record_response_reads_the_same_as_its_listing_hit() -> None:
    record = json.loads(RECORD.read_bytes())
    record_url = "https://zenodo.org/api/records/22902037"
    from_record = release_from_hit(record, record_url)
    from_listing = release_from_hit(newest_hit(), listing_url(1))
    assert from_record.listing_url == record_url
    assert replace(from_record, listing_url=listing_url(1)) == from_listing


# --- strict reads of malformed shapes --------------------------------------


def _zip_entry(hit: dict[str, Any]) -> dict[str, Any]:
    entry = hit["files"][0]
    assert isinstance(entry, dict)
    return entry


def drop_files(hit: dict[str, Any]) -> None:
    del hit["files"]


def files_as_object(hit: dict[str, Any]) -> None:
    hit["files"] = {}


def size_as_string(hit: dict[str, Any]) -> None:
    _zip_entry(hit)["size"] = str(_zip_entry(hit)["size"])


def size_as_bool(hit: dict[str, Any]) -> None:
    _zip_entry(hit)["size"] = True


def md5_without_prefix(hit: dict[str, Any]) -> None:
    _zip_entry(hit)["checksum"] = _zip_entry(hit)["checksum"].removeprefix("md5:")


def checksum_missing(hit: dict[str, Any]) -> None:
    _zip_entry(hit)["checksum"] = None


def two_zips(hit: dict[str, Any]) -> None:
    hit["files"].append(copy.deepcopy(_zip_entry(hit)))


def no_zip(hit: dict[str, Any]) -> None:
    _zip_entry(hit)["key"] = "ror-data.csv"


def no_version(hit: dict[str, Any]) -> None:
    del hit["metadata"]["version"]


def id_as_string(hit: dict[str, Any]) -> None:
    hit["id"] = str(hit["id"])


def no_publication_date(hit: dict[str, Any]) -> None:
    del hit["metadata"]["publication_date"]


def no_concept_doi(hit: dict[str, Any]) -> None:
    del hit["conceptdoi"]


def no_download_link(hit: dict[str, Any]) -> None:
    del _zip_entry(hit)["links"]["self"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (drop_files, "lists no files"),
        (files_as_object, "lists no files"),
        (size_as_string, "no integer size"),
        (size_as_bool, "no integer size"),
        (md5_without_prefix, "no md5 checksum"),
        (checksum_missing, "no md5 checksum"),
        (two_zips, "has 2 zip files, expected 1"),
        (no_zip, "has 0 zip files, expected 1"),
        (no_version, "declares no version"),
        (id_as_string, "no integer Zenodo record id"),
        (no_publication_date, "no publication date or DOI"),
        (no_concept_doi, "no Zenodo concept DOI"),
        (no_download_link, "no download link"),
    ],
)
def test_release_from_hit_refuses_malformed_shapes(
    mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    hit = newest_hit()
    mutate(hit)
    with pytest.raises(ValueError, match=message):
        release_from_hit(hit, listing_url(1))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "Unexpected Zenodo payload type"),
        ({"hits": []}, "no hits block"),
        ({"hits": {"hits": {}}}, "no hits list"),
        ({"hits": {"hits": [1], "total": 1}}, "hit 0 is a int, not an object"),
        ({"hits": {"hits": [], "total": "94"}}, "no hit total"),
        ({"hits": {"hits": [], "total": True}}, "no hit total"),
        ({"hits": {"hits": []}}, "no hit total"),
    ],
)
def test_parse_listing_page_refuses_malformed_pages(payload: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_listing_page(payload, listing_url(1))


# --- pagination records the page each hit came from ------------------------


def synthetic_hit(number: int) -> dict[str, Any]:
    # Shape only; the versions and dates are test scaffolding.
    return {
        "id": number,
        "metadata": {"version": f"vtest.{number}", "publication_date": f"2000-01-{number:02d}"},
    }


def paged_transport(pages: dict[int, dict[str, Any]], requested: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        page = int(parse_qs(urlparse(str(request.url)).query)["page"][0])
        if page not in pages:
            return httpx.Response(400, content=b"no such page")
        return httpx.Response(200, json=pages[page])

    return httpx.MockTransport(handler)


def test_fetch_hits_paginates_and_records_each_hits_page(tmp_path: Path) -> None:
    pages = {
        1: {"hits": {"hits": [synthetic_hit(3), synthetic_hit(2)], "total": 3}},
        2: {"hits": {"hits": [synthetic_hit(1)], "total": 3}},
    }
    requested: list[str] = []
    http = CachingClient(
        tmp_path / "cache.sqlite",
        "test-agent",
        min_interval_s=0.0,
        transport=paged_transport(pages, requested),
    )
    try:
        hits = fetch_hits(http, refresh=False)
    finally:
        http.close()
    assert requested == [listing_url(1), listing_url(2)]
    assert [listed.hit["id"] for listed in hits] == [3, 2, 1]
    assert [listed.page_url for listed in hits] == [listing_url(1), listing_url(1), listing_url(2)]
    # The pin must name the page the pinned version was actually found on.
    assert select_hit(hits, "vtest.1").page_url == listing_url(2)
    assert select_hit(hits, None).page_url == listing_url(1)


def test_fetch_hits_stops_on_an_empty_page_even_if_the_total_says_more(tmp_path: Path) -> None:
    pages = {
        1: {"hits": {"hits": [synthetic_hit(1)], "total": 5}},
        2: {"hits": {"hits": [], "total": 5}},
    }
    requested: list[str] = []
    http = CachingClient(
        tmp_path / "cache.sqlite",
        "test-agent",
        min_interval_s=0.0,
        transport=paged_transport(pages, requested),
    )
    try:
        hits = fetch_hits(http, refresh=False)
    finally:
        http.close()
    assert len(hits) == 1
    assert requested == [listing_url(1), listing_url(2)]


def test_fetch_hits_fails_on_a_non_200_listing(tmp_path: Path) -> None:
    requested: list[str] = []
    http = CachingClient(
        tmp_path / "cache.sqlite",
        "test-agent",
        min_interval_s=0.0,
        transport=paged_transport({}, requested),
    )
    try:
        with pytest.raises(RuntimeError, match=r"Zenodo listing failed \(400\)"):
            fetch_hits(http, refresh=False)
    finally:
        http.close()


# --- download_zip -------------------------------------------------------------


def release_for(data: bytes, filename: str = "test-ror-data.zip") -> Release:
    return Release(
        version="vtest.0",
        record_id=1,
        doi="10.5281/zenodo.1",
        concept_doi="10.5281/zenodo.0",
        publication_date="2000-01-01",
        filename=filename,
        size_bytes=len(data),
        md5=hashlib.md5(data, usedforsecurity=False).hexdigest(),
        content_url=f"https://zenodo.example.test/api/records/1/files/{filename}/content",
        listing_url=listing_url(1),
    )


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(pin_ror_dump.time, "sleep", slept.append)
    return slept


def serve(statuses: list[int], data: bytes, seen: list[httpx.Request]) -> httpx.MockTransport:
    remaining = iter(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status = next(remaining)
        return httpx.Response(status, content=data if status == 200 else b"")

    return httpx.MockTransport(handler)


def test_download_writes_the_verified_zip_and_leaves_no_part_file(tmp_path: Path) -> None:
    data = b"zip bytes " * 1000
    release = release_for(data)
    dest = tmp_path / "raw" / "ror" / release.version / release.filename
    seen: list[httpx.Request] = []
    download_zip(release, dest, "test-agent", transport=serve([200], data, seen))
    assert dest.read_bytes() == data
    assert not dest.with_name(dest.name + ".part").exists()
    assert len(seen) == 1
    assert seen[0].headers["User-Agent"] == "test-agent"
    assert str(seen[0].url) == release.content_url


def test_download_refuses_a_zip_whose_md5_differs(tmp_path: Path) -> None:
    data = b"zip bytes"
    release = replace(release_for(data), md5="0" * 32)
    dest = tmp_path / release.filename
    with pytest.raises(RorDumpHashMismatch, match=r"has md5 .* Zenodo declares 0{32}"):
        download_zip(release, dest, "test-agent", transport=serve([200], data, []))
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_download_refuses_a_short_zip(tmp_path: Path) -> None:
    data = b"zip bytes"
    release = replace(release_for(data), size_bytes=len(data) + 1)
    dest = tmp_path / release.filename
    with pytest.raises(RorDumpHashMismatch, match=rf"is {len(data)} bytes, Zenodo declares"):
        download_zip(release, dest, "test-agent", transport=serve([200], data, []))
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_download_retries_a_503_then_succeeds(tmp_path: Path, no_sleep: list[float]) -> None:
    data = b"zip bytes"
    release = release_for(data)
    dest = tmp_path / release.filename
    seen: list[httpx.Request] = []
    download_zip(release, dest, "test-agent", transport=serve([503, 200], data, seen))
    assert dest.read_bytes() == data
    assert len(seen) == 2
    assert no_sleep == [1.0]


def test_download_gives_up_after_the_attempt_limit(tmp_path: Path, no_sleep: list[float]) -> None:
    data = b"zip bytes"
    release = release_for(data)
    seen: list[httpx.Request] = []
    always_busy = serve([503] * MAX_DOWNLOAD_ATTEMPTS, data, seen)
    with pytest.raises(RuntimeError, match=f"still 503 after {MAX_DOWNLOAD_ATTEMPTS} attempts"):
        download_zip(release, tmp_path / release.filename, "test-agent", transport=always_busy)
    assert len(seen) == MAX_DOWNLOAD_ATTEMPTS
    assert len(no_sleep) == MAX_DOWNLOAD_ATTEMPTS - 1


def test_download_fails_fast_on_a_non_retryable_status(tmp_path: Path) -> None:
    data = b"zip bytes"
    release = release_for(data)
    forbidden = serve([403], data, [])
    with pytest.raises(RuntimeError, match=r"failed \(403\)"):
        download_zip(release, tmp_path / release.filename, "test-agent", transport=forbidden)


# --- extract_v2_json ------------------------------------------------------------


def build_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


V1_LOOKING = json.dumps(
    [
        {
            "id": "https://ror.org/052gg0110",
            "name": "University of Oxford",
            "aliases": [],
            "addresses": [],
            "status": "active",
            "admin": {"last_modified": {"schema_version": "1.0"}},
        }
    ]
).encode()


def test_extracts_the_single_json_layout(tmp_path: Path) -> None:
    archive = build_zip(
        tmp_path / "single.zip",
        {"vtest-ror-data.json": SAMPLE.read_bytes(), "vtest-ror-data.csv": b"id,name\n"},
    )
    extracted = extract_v2_json(archive, tmp_path / "out")
    assert extracted == tmp_path / "out" / "vtest-ror-data.json"
    assert extracted.read_bytes() == SAMPLE.read_bytes()
    assert len(list(iter_dump_records(extracted))) == 14


def test_extracts_v2_from_the_side_by_side_layout(tmp_path: Path) -> None:
    archive = build_zip(
        tmp_path / "pair.zip",
        {
            "vtest-ror-data_schema_v1.json": V1_LOOKING,
            "vtest-ror-data_schema_v2.json": SAMPLE.read_bytes(),
        },
    )
    extracted = extract_v2_json(archive, tmp_path / "out")
    assert extracted.name == "vtest-ror-data_schema_v2.json"
    assert extracted.read_bytes() == SAMPLE.read_bytes()
    assert not (tmp_path / "out" / "vtest-ror-data_schema_v1.json").exists()


def test_zip_with_no_json_is_an_error(tmp_path: Path) -> None:
    archive = build_zip(tmp_path / "nojson.zip", {"vtest-ror-data.csv": b"id,name\n"})
    with pytest.raises(ValueError, match=r"holds no \.json member"):
        extract_v2_json(archive, tmp_path / "out")


def test_two_jsons_with_no_schema_marker_is_an_error(tmp_path: Path) -> None:
    archive = build_zip(
        tmp_path / "ambiguous.zip",
        {"a.json": SAMPLE.read_bytes(), "b.json": SAMPLE.read_bytes()},
    )
    with pytest.raises(ValueError, match=r"2 \.json members and none is named schema_v2"):
        extract_v2_json(archive, tmp_path / "out")


def test_nested_member_path_is_refused(tmp_path: Path) -> None:
    archive = build_zip(tmp_path / "nested.zip", {"data/vtest-ror-data.json": SAMPLE.read_bytes()})
    with pytest.raises(ValueError, match="is not a plain filename"):
        extract_v2_json(archive, tmp_path / "out")


def test_single_json_that_is_v1_schema_is_refused_after_extraction(tmp_path: Path) -> None:
    archive = build_zip(tmp_path / "v1only.zip", {"vtest-ror-data.json": V1_LOOKING})
    with pytest.raises(ValueError, match=r"schema_version '1\.0', expected 2\.x"):
        extract_v2_json(archive, tmp_path / "out")


def test_single_json_holding_an_empty_array_is_refused(tmp_path: Path) -> None:
    archive = build_zip(tmp_path / "empty.zip", {"vtest-ror-data.json": b"[]"})
    with pytest.raises(ValueError, match="holds no organisation records"):
        extract_v2_json(archive, tmp_path / "out")


# --- verify_against_pin ---------------------------------------------------------


def pin_for(zip_path: Path, json_path: Path) -> RorDumpPin:
    return RorDumpPin(
        version="vtest.0",
        zenodo_doi="10.5281/zenodo.1",
        zenodo_concept_doi="10.5281/zenodo.0",
        record_id=1,
        publication_date="2000-01-01",
        filename=zip_path.name,
        size_bytes=zip_path.stat().st_size,
        md5_zenodo="0" * 32,
        sha256_zip=sha256_file(zip_path),
        v2_json_filename=json_path.name,
        sha256_v2_json=sha256_file(json_path),
        organisation_count=14,
        downloaded_at="2000-01-01T00:00:00+00:00",
        source_url="https://zenodo.example.test/content",
        listing_url=listing_url(1),
    )


@pytest.fixture
def pinned_files(tmp_path: Path) -> tuple[RorDumpPin, Path, Path]:
    json_path = tmp_path / "vtest-ror-data.json"
    json_path.write_bytes(SAMPLE.read_bytes())
    zip_path = build_zip(tmp_path / "vtest-ror-data.zip", {json_path.name: json_path.read_bytes()})
    return pin_for(zip_path, json_path), zip_path, json_path


def test_verify_passes_when_both_hashes_match(
    pinned_files: tuple[RorDumpPin, Path, Path],
) -> None:
    pin, zip_path, json_path = pinned_files
    verify_against_pin(pin, zip_path, json_path)


def test_verify_names_the_zip_when_its_hash_differs(
    pinned_files: tuple[RorDumpPin, Path, Path],
) -> None:
    pin, zip_path, json_path = pinned_files
    with pytest.raises(RorDumpHashMismatch, match=r"\(zip\) has sha256 .* records f{64}"):
        verify_against_pin(replace(pin, sha256_zip="f" * 64), zip_path, json_path)


def test_verify_names_the_json_when_its_hash_differs(
    pinned_files: tuple[RorDumpPin, Path, Path],
) -> None:
    pin, zip_path, json_path = pinned_files
    with pytest.raises(RorDumpHashMismatch, match=r"\(v2 JSON\) has sha256 .* records f{64}"):
        verify_against_pin(replace(pin, sha256_v2_json="f" * 64), zip_path, json_path)


def test_verify_catches_a_file_changed_after_pinning(
    pinned_files: tuple[RorDumpPin, Path, Path],
) -> None:
    pin, zip_path, json_path = pinned_files
    json_path.write_bytes(json_path.read_bytes() + b"\n")
    with pytest.raises(RorDumpHashMismatch, match="not the pinned release"):
        verify_against_pin(pin, zip_path, json_path)
