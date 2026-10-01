"""Pin one ROR data dump release and put it on disk, verified.

ROR ships a new release roughly fortnightly, and organisations are renamed,
merged, split and withdrawn between them. Two things in this benchmark move
when the release moves: the parent-child and any-relationship matching rules,
and the renamed/merged/split stratum, which is built from predecessor and
successor relationships that exist only in the dump. A benchmark that cannot
say which release it used is not reproducible, so the release is pinned in
config/ror_dump.yaml and every load re-verifies the files by hash.

The pin is deliberately sticky. A bare re-run does not chase the newest
release: it re-resolves the version already in the pin, verifies the files,
and says nothing has changed. It logs when a newer release exists but will not
move the pin on its own, because a release moving mid-project would silently
change match rates. Moving it takes an explicit --version, or --refresh to
re-download and re-verify the same release.

The resume unit is the whole zip. It is one file of under 40MB (37.5MB for
v2.13), so an interrupted download restarts rather than resuming at a byte
offset; a completed and hash-verified zip is never fetched twice.

Usage:
  uv run scripts/pin_ror_dump.py
  uv run scripts/pin_ror_dump.py --version v2.12
  uv run scripts/pin_ror_dump.py --refresh
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from disambig.httpcache import RETRYABLE_STATUSES, CachingClient  # noqa: E402
from disambig.logging_setup import configure_logging, new_run_id  # noqa: E402
from disambig.ror_dump import (  # noqa: E402
    RorDump,
    RorDumpHashMismatch,
    RorDumpPin,
    iter_dump_records,
    require_v2_schema,
    sha256_file,
)

log = structlog.get_logger(__name__)

# Same descriptive User-Agent, with the project contact, that the other
# scripts send (see scripts/propose_candidates.py). Zenodo has no polite pool
# to join, so the contact address is the whole courtesy.
USER_AGENT = "openresearch.wtf disambiguation-benchmark/0.1 (mailto:m.hahnel@digital-science.com)"
ZENODO_API = "https://zenodo.org/api"
# ROR's Zenodo concept (parent) record: every release is a version of this.
ROR_CONCEPT_ID = 6347574
# Zenodo caps anonymous requests at 25 hits per page and answers 400 above
# that; the listing paginates rather than authenticating.
LISTING_PAGE_SIZE = 25
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
DOWNLOAD_TIMEOUT_S = 120.0
MAX_DOWNLOAD_ATTEMPTS = 5


def listing_url(page: int) -> str:
    return (
        f"{ZENODO_API}/records?q=parent.id:{ROR_CONCEPT_ID}"
        f"&sort=newest&all_versions=true&size={LISTING_PAGE_SIZE}&page={page}"
    )


@dataclass(frozen=True)
class Release:
    """One ROR release on Zenodo, as the listing API describes it."""

    version: str
    record_id: int
    doi: str
    concept_doi: str
    publication_date: str
    filename: str
    size_bytes: int
    md5: str
    content_url: str
    listing_url: str


@dataclass(frozen=True)
class ListedHit:
    """One listing hit together with the page URL it was read from.

    The pin records the listing page on which the pinned release was found, so
    a stranger can fetch that one page and see the same hit. Zenodo lists ROR
    releases 25 to a page and there are far more than 25, so the page is not
    always the first.
    """

    hit: dict[str, Any]
    page_url: str


def parse_listing_page(payload: object, url: str) -> tuple[list[dict[str, Any]], int]:
    """Strict read of one Zenodo search page: (hits on this page, total hits)."""
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected Zenodo payload type for {url}")
    block = payload.get("hits")
    if not isinstance(block, dict):
        raise ValueError(f"Zenodo listing has no hits block: {url}")
    page_hits = block.get("hits")
    if not isinstance(page_hits, list):
        raise ValueError(f"Zenodo listing has no hits list: {url}")
    for position, hit in enumerate(page_hits):
        if not isinstance(hit, dict):
            raise ValueError(
                f"Zenodo listing hit {position} is a {type(hit).__name__}, not an object: {url}"
            )
    total = block.get("total")
    if not isinstance(total, int) or isinstance(total, bool):
        raise ValueError(f"Zenodo listing has no hit total: {url}")
    return page_hits, total


def fetch_hits(http: CachingClient, refresh: bool) -> list[ListedHit]:
    """Every version of the ROR concept record, newest first, each with its page."""
    hits: list[ListedHit] = []
    page = 1
    while True:
        url = listing_url(page)
        response = http.get(url, refresh=refresh)
        if response.status != 200:
            raise RuntimeError(f"Zenodo listing failed ({response.status}) for {url}")
        page_hits, total = parse_listing_page(response.json(), url)
        hits.extend(ListedHit(hit=hit, page_url=url) for hit in page_hits)
        if not page_hits or len(hits) >= total:
            log.info("ror_releases_listed", releases=len(hits), total=total, pages=page)
            return hits
        page += 1


def _version_of(hit: dict[str, Any]) -> str | None:
    metadata = hit.get("metadata")
    if not isinstance(metadata, dict):
        return None
    version = metadata.get("version")
    return version if isinstance(version, str) and version.strip() else None


def select_hit(hits: Sequence[ListedHit], version: str | None) -> ListedHit:
    """Pick a release by version, or the most recently published one.

    The newest release is chosen by publication date rather than by trusting
    the listing's own sort order, so the answer does not depend on Zenodo's
    ranking staying what it is today.
    """
    if version is not None:
        for listed in hits:
            if _version_of(listed.hit) == version:
                return listed
        available = [found for listed in hits if (found := _version_of(listed.hit)) is not None]
        raise ValueError(
            f"ROR release {version!r} is not on Zenodo under concept {ROR_CONCEPT_ID}. "
            f"Recent versions: {', '.join(available[:8])}"
        )
    dated = [
        listed
        for listed in hits
        if isinstance(listed.hit.get("metadata"), dict)
        and isinstance(listed.hit["metadata"].get("publication_date"), str)
    ]
    if not dated:
        raise ValueError(f"No dated ROR releases under Zenodo concept {ROR_CONCEPT_ID}")
    return max(
        dated,
        key=lambda listed: (listed.hit["metadata"]["publication_date"], listed.hit.get("id", 0)),
    )


def release_from_hit(hit: dict[str, Any], page_url: str) -> Release:
    """Strict read of one listing hit. Anything absent is an error, not a default.

    A Zenodo record response (/api/records/<id>) has the same shape as a
    listing hit, so this reads either.
    """
    version = _version_of(hit)
    if version is None:
        raise ValueError(f"Zenodo record {hit.get('id')!r} declares no version")
    metadata = hit["metadata"]
    publication_date = metadata.get("publication_date")
    record_id = hit.get("id")
    doi = hit.get("doi")
    concept_doi = hit.get("conceptdoi")
    if not isinstance(record_id, int) or isinstance(record_id, bool):
        raise ValueError(f"ROR {version} has no integer Zenodo record id")
    if not isinstance(publication_date, str) or not isinstance(doi, str):
        raise ValueError(f"ROR {version} has no publication date or DOI")
    if not isinstance(concept_doi, str):
        raise ValueError(f"ROR {version} has no Zenodo concept DOI")

    files = hit.get("files")
    if not isinstance(files, list):
        raise ValueError(f"ROR {version} (Zenodo {record_id}) lists no files")
    zips = [
        entry
        for entry in files
        if isinstance(entry, dict)
        and isinstance(entry.get("key"), str)
        and entry["key"].endswith(".zip")
    ]
    if len(zips) != 1:
        keys = [entry.get("key") for entry in files if isinstance(entry, dict)]
        raise ValueError(f"ROR {version} has {len(zips)} zip files, expected 1: {keys}")
    entry = zips[0]
    size_bytes = entry.get("size")
    checksum = entry.get("checksum")
    links = entry.get("links")
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
        raise ValueError(f"ROR {version} zip has no integer size (got {size_bytes!r})")
    if not isinstance(checksum, str) or not checksum.startswith("md5:"):
        raise ValueError(f"ROR {version} zip has no md5 checksum (got {checksum!r})")
    if not isinstance(links, dict) or not isinstance(links.get("self"), str):
        raise ValueError(f"ROR {version} zip has no download link")
    return Release(
        version=version,
        record_id=record_id,
        doi=doi,
        concept_doi=concept_doi,
        publication_date=publication_date,
        filename=entry["key"],
        size_bytes=size_bytes,
        md5=checksum.removeprefix("md5:"),
        content_url=links["self"],
        listing_url=page_url,
    )


def download_zip(
    release: Release,
    dest: Path,
    user_agent: str,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Stream the zip down, verifying Zenodo's declared size and md5.

    Written to a .part file and renamed only once both checks pass, so an
    interrupted run can never leave a short zip that a later run would trust.
    transport is for tests (httpx.MockTransport); production leaves it unset.
    """
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    delay = 1.0
    with httpx.Client(
        headers={"User-Agent": user_agent},
        timeout=DOWNLOAD_TIMEOUT_S,
        follow_redirects=True,
        transport=transport,
    ) as client:
        for attempt in range(MAX_DOWNLOAD_ATTEMPTS):
            digest = hashlib.md5(usedforsecurity=False)
            written = 0
            with client.stream("GET", release.content_url) as response:
                if response.status_code in RETRYABLE_STATUSES:
                    if attempt == MAX_DOWNLOAD_ATTEMPTS - 1:
                        raise RuntimeError(
                            f"GET {release.content_url} still {response.status_code} after "
                            f"{MAX_DOWNLOAD_ATTEMPTS} attempts"
                        )
                    log.warning(
                        "ror_download_retry",
                        url=release.content_url,
                        status=response.status_code,
                        attempt=attempt,
                        sleep=delay,
                    )
                    time.sleep(delay)
                    delay *= 2
                    continue
                if response.status_code != 200:
                    raise RuntimeError(f"GET {release.content_url} failed ({response.status_code})")
                with part.open("wb") as handle:
                    for chunk in response.iter_bytes(DOWNLOAD_CHUNK_BYTES):
                        handle.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
            if written != release.size_bytes:
                part.unlink()
                raise RorDumpHashMismatch(
                    f"{release.filename} is {written} bytes, Zenodo declares {release.size_bytes}"
                )
            actual_md5 = digest.hexdigest()
            if actual_md5 != release.md5:
                part.unlink()
                raise RorDumpHashMismatch(
                    f"{release.filename} has md5 {actual_md5}, Zenodo declares {release.md5}"
                )
            part.replace(dest)
            log.info("ror_zip_downloaded", path=str(dest), bytes=written, md5=actual_md5)
            return
    raise RuntimeError(f"GET {release.content_url} exhausted its retries")


def extract_v2_json(zip_path: Path, dest_dir: Path) -> Path:
    """Extract the v2-schema JSON from the release zip.

    Releases up to 2025 shipped v1 and v2 schema files side by side, named
    ..._schema_v1.json and ..._schema_v2.json; v2.13 ships one JSON with no
    schema suffix alongside a CSV. Both layouts are handled, and anything else
    is an error rather than a guess, because loading a v1 file would parse into
    empty names and quietly wrong counts. The first record's schema stamp is
    checked after extraction as a second guard.
    """
    with zipfile.ZipFile(zip_path) as archive:
        json_names = [name for name in archive.namelist() if name.endswith(".json")]
        if not json_names:
            raise ValueError(f"{zip_path} holds no .json member: {archive.namelist()}")
        v2_names = [name for name in json_names if "schema_v2" in name]
        if len(v2_names) == 1:
            member = v2_names[0]
        elif len(json_names) == 1:
            member = json_names[0]
        else:
            raise ValueError(
                f"{zip_path} holds {len(json_names)} .json members and none is named "
                f"schema_v2: {json_names}"
            )
        # The archive comes off the network, so refuse anything that would
        # write outside dest_dir.
        if Path(member).name != member:
            raise ValueError(f"{zip_path} member {member!r} is not a plain filename")
        dest = dest_dir / member
        dest_dir.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, dest.open("wb") as handle:
            while block := source.read(DOWNLOAD_CHUNK_BYTES):
                handle.write(block)
    first = next(iter_dump_records(dest), None)
    if first is None:
        raise ValueError(f"{dest} holds no organisation records")
    require_v2_schema(first.record, f"{zip_path.name}:{member}")
    log.info("ror_json_extracted", path=str(dest), bytes=dest.stat().st_size)
    return dest


def verify_against_pin(pin: RorDumpPin, zip_path: Path, json_path: Path) -> None:
    """Both files must still hash to what the pin says. Mismatch is fatal."""
    for path, expected, label in (
        (zip_path, pin.sha256_zip, "zip"),
        (json_path, pin.sha256_v2_json, "v2 JSON"),
    ):
        actual = sha256_file(path)
        if actual != expected:
            raise RorDumpHashMismatch(
                f"{path} ({label}) has sha256 {actual}, but the pin for ROR {pin.version} "
                f"records {expected}. The file on disk is not the pinned release."
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version", help="ROR release to pin, e.g. v2.12. Defaults to the pinned release."
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch, re-extract and re-verify even when the pin already matches.",
    )
    parser.add_argument("--pin", type=Path, default=PROJECT_ROOT / "config" / "ror_dump.yaml")
    parser.add_argument("--data-root", type=Path, default=PROJECT_ROOT / "data")
    args = parser.parse_args()

    run_id = new_run_id()
    configure_logging(PROJECT_ROOT / "logs", run_id)

    http = CachingClient(
        args.data_root / "interim" / "http_cache.sqlite", USER_AGENT, min_interval_s=1.0
    )
    try:
        hits = fetch_hits(http, refresh=args.refresh)
    finally:
        http.close()

    newest_listed = select_hit(hits, None)
    newest = release_from_hit(newest_listed.hit, newest_listed.page_url)
    existing = RorDumpPin.from_yaml(args.pin) if args.pin.exists() else None
    target_version = args.version or (existing.version if existing is not None else newest.version)
    if target_version == newest.version:
        release = newest
    else:
        target_listed = select_hit(hits, target_version)
        release = release_from_hit(target_listed.hit, target_listed.page_url)
    if existing is not None and existing.version != newest.version:
        log.info("ror_newer_release_available", pinned=existing.version, newest=newest.version)
    if existing is not None and existing.version != release.version:
        log.warning("ror_pin_version_change", was=existing.version, now=release.version)

    target_dir = args.data_root / "raw" / "ror" / release.version
    zip_path = target_dir / release.filename

    if existing is not None and existing.version == release.version and not args.refresh:
        json_path = existing.json_path(args.data_root)
        if zip_path.exists() and json_path.exists():
            verify_against_pin(existing, zip_path, json_path)
            log.info("ror_pin_unchanged", version=existing.version, pin=str(args.pin))
            print(
                json.dumps(
                    {
                        "action": "unchanged",
                        "version": existing.version,
                        "organisations": existing.organisation_count,
                        "pin": str(args.pin),
                    }
                )
            )
            return
        log.info(
            "ror_pinned_files_absent",
            version=existing.version,
            zip_present=zip_path.exists(),
            json_present=json_path.exists(),
        )

    if zip_path.exists() and not args.refresh:
        log.info("ror_zip_present", path=str(zip_path))
    else:
        download_zip(release, zip_path, USER_AGENT)
    sha256_zip = sha256_file(zip_path)
    # A published Zenodo version is immutable, so a differing hash on the same
    # version means the file or the pin is wrong. Never continue.
    if (
        existing is not None
        and existing.version == release.version
        and sha256_zip != existing.sha256_zip
    ):
        raise RorDumpHashMismatch(
            f"{zip_path} has sha256 {sha256_zip}, but the pin for ROR {release.version} "
            f"records {existing.sha256_zip}. A published release does not change."
        )

    json_path = extract_v2_json(zip_path, target_dir)
    sha256_v2_json = sha256_file(json_path)
    if (
        existing is not None
        and existing.version == release.version
        and sha256_v2_json != existing.sha256_v2_json
    ):
        raise RorDumpHashMismatch(
            f"{json_path} has sha256 {sha256_v2_json}, but the pin for ROR "
            f"{release.version} records {existing.sha256_v2_json}."
        )

    with RorDump.load(json_path) as dump:
        summary = dump.summary()
        organisation_count = len(dump)

    pin = RorDumpPin(
        version=release.version,
        zenodo_doi=release.doi,
        zenodo_concept_doi=release.concept_doi,
        record_id=release.record_id,
        publication_date=release.publication_date,
        filename=release.filename,
        size_bytes=release.size_bytes,
        md5_zenodo=release.md5,
        sha256_zip=sha256_zip,
        v2_json_filename=json_path.name,
        sha256_v2_json=sha256_v2_json,
        organisation_count=organisation_count,
        downloaded_at=datetime.now(UTC).isoformat(),
        source_url=release.content_url,
        listing_url=release.listing_url,
    )
    pin.to_yaml(args.pin)
    # The dump was loaded before the pin existed, so stamp the release on the
    # summary here rather than printing a null version for a pinned release.
    summary = {**summary, "version": pin.version, "publication_date": pin.publication_date}
    log.info("ror_pin_written", pin=str(args.pin), **summary)
    print(json.dumps({"action": "pinned", "pin": str(args.pin), **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
