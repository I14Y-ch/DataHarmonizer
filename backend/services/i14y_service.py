"""I14Y Interoperability Platform — public API wrapper."""
from __future__ import annotations

import io
import time

import pandas as pd
import requests

I14Y_API = "https://api.i14y.admin.ch/api/public/v1"
_TIMEOUT = 10


def search_datasets(query: str, page_size: int = 5) -> list[dict]:
    """Full-text search across I14Y datasets."""
    resp = requests.get(
        f"{I14Y_API}/search",
        params={"query": query, "types": "Dataset", "pageSize": page_size},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def get_dataset_metadata(dataset_id: str) -> dict:
    """Retrieve full DCAT dataset metadata. Unwraps the {"data": {...}} envelope."""
    resp = requests.get(f"{I14Y_API}/datasets/{dataset_id}", timeout=_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    # The I14Y API wraps single-resource responses in {"data": {...}}
    return body.get("data", body)


def has_structure(dataset_id: str) -> bool:
    """Return True when the dataset has a published SHACL shape with at least one PropertyShape."""
    try:
        resp = requests.get(
            f"{I14Y_API}/datasets/{dataset_id}/structures/exports/JsonLd",
            timeout=_TIMEOUT,
        )
        if not resp.ok:
            return False
        body = resp.json()
        # API returns either a top-level array or {"@graph": [...]}
        items = body if isinstance(body, list) else body.get("@graph", [])
        return any("PropertyShape" in str(item.get("@type", "")) for item in items)
    except Exception:
        return False


def get_dataset_structure(dataset_id: str) -> list[dict]:
    """
    Extract field/column names and their linked I14Y concept IDs from the dataset's SHACL shape.

    Returns a list of {"name": str, "concept_id": str | None} — one entry per PropertyShape.
    The concept_id is taken from the authoritative dct:conformsTo field on the shape, which
    links each attribute to its I14Y concept. Returns an empty list when unavailable.
    """
    try:
        resp = requests.get(
            f"{I14Y_API}/datasets/{dataset_id}/structures/exports/JsonLd",
            timeout=_TIMEOUT,
        )
        if not resp.ok:
            return []
        body = resp.json()
        # API returns either a top-level array or {"@graph": [...]}
        graph = body if isinstance(body, list) else body.get("@graph", [])
        columns: list[dict] = []
        for item in graph:
            item_type = str(item.get("@type", ""))
            if "PropertyShape" not in item_type:
                continue
            name = (
                item.get("sh:name")
                or item.get("rdfs:label")
                or (item.get("sh:path") or {}).get("@id", "").split("/")[-1]
                or item.get("@id", "").split("/")[-1]
            )
            if isinstance(name, dict):
                name = name.get("de") or name.get("en") or name.get("fr") or str(name)
            if not name or not isinstance(name, str) or not name.strip():
                continue
            # dct:conformsTo carries the authoritative link to the I14Y concept.
            concept_id: str | None = None
            conforms_to = item.get("dct:conformsTo") or item.get("conformsTo")
            if isinstance(conforms_to, dict):
                raw_id = conforms_to.get("@id", "")
                if "/concepts/" in raw_id:
                    concept_id = raw_id.split("/concepts/")[-1].split("/")[0]
            elif isinstance(conforms_to, str) and "/concepts/" in conforms_to:
                concept_id = conforms_to.split("/concepts/")[-1].split("/")[0]
            columns.append({"name": name.strip(), "concept_id": concept_id})
        return columns
    except Exception:
        return []


def get_concept_by_id(concept_id: str) -> dict | None:
    """Fetch an I14Y concept by its ID and return a normalised concept dict."""
    try:
        resp = requests.get(f"{I14Y_API}/concepts/{concept_id}", timeout=5)
        resp.raise_for_status()
        c = resp.json()
        title = c.get("title") or {}
        return {
            "conceptId": c.get("id") or concept_id,
            "conceptType": c.get("conceptType"),
            "title": title.get("de") or title.get("en") or title.get("fr") or concept_id,
        }
    except Exception:
        return None


def load_dataset_from_url(url: str, media_type: str | None = None) -> pd.DataFrame:
    """Download and parse a publicly available dataset file."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    url_lower = url.lower()
    mt = (media_type or "").lower()

    if "csv" in mt or url_lower.endswith(".csv"):
        return pd.read_csv(io.StringIO(resp.text))
    if "json" in mt or url_lower.endswith(".json"):
        data = resp.json()
        return pd.DataFrame(data) if isinstance(data, list) else pd.json_normalize(data)
    if url_lower.endswith(".xlsx") or "excel" in mt or "spreadsheet" in mt:
        return pd.read_excel(io.BytesIO(resp.content))
    # Fallback: try CSV
    try:
        return pd.read_csv(io.StringIO(resp.text))
    except Exception:
        raise ValueError(f"Cannot parse dataset from {url!r} (media_type={media_type!r})")


def lookup_concept(col_name: str) -> dict | None:
    """Return the best-matching I14Y concept for a column name, or None."""
    try:
        resp = requests.get(
            f"{I14Y_API}/search",
            params={"query": col_name, "types": "Concept", "pageSize": 1},
            timeout=5,
        )
        resp.raise_for_status()
        items = resp.json().get("data", [])
        if not items:
            return None
        c = items[0]
        title = c.get("title") or {}
        return {
            "conceptId": c.get("id"),
            "conceptType": c.get("conceptType"),
            "title": (
                title.get("de") or title.get("en") or title.get("fr") or str(title)
            ),
        }
    except Exception:
        return None


def lookup_concepts_batch(col_names: list[str]) -> dict[str, dict | None]:
    """Look up I14Y concepts for multiple column names with rate-limiting."""
    result: dict[str, dict | None] = {}
    for col in col_names:
        result[col] = lookup_concept(col)
        time.sleep(0.08)  # ~12 req/s — polite to the public API
    return result
