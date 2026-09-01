#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MAX_SOURCE_BYTES = 1024 * 1024 * 1024
READ_BUFFER_BYTES = 64 * 1024
MAX_IMPORT_BYTES = 25 * 1024 * 1024


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required env: {name}")
    return value


def _normalize_solicitation(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def _normalize_title(value: str | None) -> str:
    value = (value or "").casefold().replace("&nbsp;", " ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _value(row: dict[str, str], key: str) -> str | None:
    raw = row.get(key)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _decimal_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().replace(",", "")
    if not cleaned:
        return None
    try:
        return str(Decimal(cleaned))
    except InvalidOperation:
        return None


def _row_is_dnd(row: dict[str, str]) -> bool:
    text = " | ".join(filter(None, (
        _value(row, "contractingEntityName-nomEntitContractante-eng"),
        _value(row, "endUserEntitiesName-nomEntitesUtilisateurFinal-eng"),
    ))).casefold()
    return (
        "national defence" in text
        or "department of national defence" in text
        or "défense nationale" in text
        or text.strip() == "dnd"
    )


def _row_matches_targets(
    row: dict[str, str],
    *,
    target_solicitations: set[str],
    target_titles: set[str],
) -> bool:
    solicitation = _normalize_solicitation(_value(row, "solicitationNumber-numeroSollicitation"))
    title = _normalize_title(_value(row, "title-titre-eng"))
    return bool(
        (solicitation and solicitation in target_solicitations)
        or (title and title in target_titles)
    )


def _record_from_row(
    row: dict[str, str],
    *,
    source_url: str,
    source_kind: str,
    source_row: int,
) -> dict[str, object]:
    status_field = (
        "awardStatus-attributionStatut-eng"
        if source_kind == "AWARD"
        else "contractStatus-statutContrat-eng"
    )
    return {
        "source_kind": source_kind,
        "source_url": source_url,
        "source_row": source_row,
        "title": _value(row, "title-titre-eng"),
        "solicitation_number": _value(row, "solicitationNumber-numeroSollicitation"),
        "contract_number": _value(row, "contractNumber-numeroContrat"),
        "amendment_number": _value(row, "amendmentNumber-numeroModification"),
        "publication_date": _value(row, "publicationDate-datePublication"),
        "award_date": _value(row, "contractAwardDate-dateAttributionContrat"),
        "amendment_date": _value(row, "amendmentDate-dateModification"),
        "contract_amount": _decimal_text(_value(row, "contractAmount-montantContrat")),
        "total_contract_value": _decimal_text(_value(row, "totalContractValue-valeurTotaleContrat")),
        "currency": _value(row, "contractCurrency-contratMonnaie"),
        "status": _value(row, status_field),
        "supplier_name": _value(row, "supplierLegalName-nomLegalFournisseur-eng"),
        "contracting_entity": _value(row, "contractingEntityName-nomEntitContractante-eng"),
        "end_user": _value(row, "endUserEntitiesName-nomEntitesUtilisateurFinal-eng"),
        "procurement_category": _value(row, "procurementCategory-categorieApprovisionnement"),
        "procurement_method": _value(row, "procurementMethod-methodeApprovisionnement-eng"),
        "selection_criteria": _value(row, "selectionCriteria-criteresSelection-eng"),
        "unspsc": _value(row, "unspsc"),
        "gsin": _value(row, "gsin-nibs"),
    }


class _HashingBoundedReader(io.RawIOBase):
    def __init__(self, source, *, max_bytes: int) -> None:
        super().__init__()
        self.source = source
        self.max_bytes = max_bytes
        self.bytes_read = 0
        self.digest = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray) -> int:
        data = self.source.read(len(buffer))
        if not data:
            return 0
        self.bytes_read += len(data)
        if self.bytes_read > self.max_bytes:
            raise ValueError("HISTORY_SOURCE_TOO_LARGE")
        self.digest.update(data)
        buffer[: len(data)] = data
        return len(data)


def _scan_source(
    spec: dict[str, str],
    *,
    target_solicitations: set[str],
    target_titles: set[str],
) -> dict[str, object]:
    source_url = spec["source_url"]
    source_kind = spec["source_kind"]
    request = Request(
        source_url,
        headers={
            "User-Agent": "TenderOS-public-history-worker/1.0",
            "Accept": "text/csv,*/*;q=0.1",
        },
    )
    retrieved_at = datetime.now(timezone.utc).isoformat()
    retained: list[dict[str, object]] = []

    with urlopen(request, timeout=120) as response:  # fixed CanadaBuys URLs supplied by TenderOS
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_SOURCE_BYTES:
            raise ValueError("HISTORY_SOURCE_TOO_LARGE")
        raw = _HashingBoundedReader(response, max_bytes=MAX_SOURCE_BYTES)
        buffered = io.BufferedReader(raw, buffer_size=READ_BUFFER_BYTES)
        text = io.TextIOWrapper(buffered, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(text)
        for source_row, row in enumerate(reader, start=2):
            if not _row_is_dnd(row):
                continue
            if not _row_matches_targets(
                row,
                target_solicitations=target_solicitations,
                target_titles=target_titles,
            ):
                continue
            retained.append(_record_from_row(
                row,
                source_url=source_url,
                source_kind=source_kind,
                source_row=source_row,
            ))
        content_sha256 = raw.digest.hexdigest()
        scanned_bytes = raw.bytes_read
        text.detach()

    print(
        f"history source complete kind={source_kind} retained={len(retained)} "
        f"scanned_bytes={scanned_bytes} sha256={content_sha256}",
        flush=True,
    )
    return {
        "source_kind": source_kind,
        "source_url": source_url,
        "content_sha256": content_sha256,
        "retrieved_at": retrieved_at,
        "record_count": len(retained),
        "records": retained,
    }


def _json_request(url: str, *, token: str, method: str = "GET", payload: object | None = None):
    body = None
    headers = {
        "X-TenderOS-Scheduler-Token": token,
        "Accept": "application/json",
        "User-Agent": "TenderOS-public-history-worker/1.0",
    }
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_IMPORT_BYTES:
            raise ValueError("HISTORY_IMPORT_PAYLOAD_TOO_LARGE")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=120) as response:
        return response.status, json.load(response)


def main() -> int:
    targets_url = _required_env("TENDEROS_HISTORY_TARGETS_URL")
    import_url = _required_env("TENDEROS_HISTORY_IMPORT_URL")
    token = _required_env("TENDEROS_SCHEDULER_TOKEN")

    try:
        status, targets = _json_request(targets_url, token=token)
        if status != 200 or targets.get("status") != "ready":
            raise RuntimeError(f"history targets not ready: HTTP {status}")

        queue_sha = str(targets["queue_source_sha256"])
        target_solicitations = {str(value) for value in targets.get("target_solicitations", []) if value}
        target_titles = {str(value) for value in targets.get("target_titles", []) if value}
        sources = list(targets.get("sources", []))
        if len(sources) != 4:
            raise RuntimeError(f"expected four official history sources, got {len(sources)}")

        print(
            f"history targets queue_sha={queue_sha[:12]} solicitations={len(target_solicitations)} "
            f"titles={len(target_titles)} sources={len(sources)}",
            flush=True,
        )

        snapshots = [
            _scan_source(
                spec,
                target_solicitations=target_solicitations,
                target_titles=target_titles,
            )
            for spec in sources
        ]
        payload = {
            "schema_version": "1.0",
            "queue_source_sha256": queue_sha,
            "snapshots": snapshots,
        }
        status, result = _json_request(import_url, token=token, method="POST", payload=payload)
        if status != 200 or result.get("status") != "complete":
            raise RuntimeError(f"history import did not complete: HTTP {status}")
        print(
            "history import complete "
            f"sources={result.get('sources')} retained_records={result.get('retained_records')} "
            f"matched_notices={result.get('matched_notices')} state={result.get('history_source_state')}",
            flush=True,
        )
        return 0
    except HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        print(f"history worker HTTP error status={exc.code} body={detail}", file=sys.stderr)
        return 75 if exc.code == 409 else 1
    except (URLError, TimeoutError, ValueError, RuntimeError) as exc:
        print(f"history worker failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
