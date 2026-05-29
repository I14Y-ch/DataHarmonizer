"""I14Y Interoperability Platform — public API wrapper."""
from __future__ import annotations

import io
import re
import time

import pandas as pd
import requests

I14Y_API = "https://api.i14y.admin.ch/api/public/v1"
_TIMEOUT = 10
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def search_datasets(query: str, page_size: int = 5) -> list[dict]:
    """Full-text search across I14Y datasets."""
    resp = requests.get(
        f"{I14Y_API}/search",
        params={"query": query, "types": "Dataset", "pageSize": page_size},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def _is_full_uuid(s: str) -> bool:
    """Return True when s looks like a complete UUID (8-4-4-4-12 hex, 36 chars)."""
    return bool(_UUID_RE.fullmatch(s))


def resolve_dataset_id(dataset_id: str) -> str:
    """
    Resolve a possibly-truncated dataset ID to its full UUID.

    MCP sometimes returns partial IDs like '19748db3' instead of the full
    '19748db3-8bfb-48ad-8206-8fbde648afb7'. When a direct metadata fetch 404s
    and the ID is not a full UUID, we search I14Y with the partial ID string
    and return the UUID of the first result whose UUID starts with that prefix.
    Falls back to the original ID if resolution fails.
    """
    if _is_full_uuid(dataset_id):
        return dataset_id
    # Try text search — find a dataset whose UUID starts with the partial ID
    try:
        hits = search_datasets(dataset_id, page_size=5)
        for hit in hits:
            hit_id = hit.get("id", "")
            if hit_id.startswith(dataset_id):
                return hit_id
    except Exception:
        pass
    return dataset_id


def get_dataset_metadata(dataset_id: str) -> dict:
    """Retrieve full DCAT dataset metadata. Unwraps the {"data": {...}} envelope.
    Automatically resolves truncated/partial UUIDs to their full form."""
    full_id = resolve_dataset_id(dataset_id)
    resp = requests.get(f"{I14Y_API}/datasets/{full_id}", timeout=_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    # The I14Y API wraps single-resource responses in {"data": {...}}
    return body.get("data", body)


def has_structure(dataset_id: str) -> bool:
    """Return True when the dataset has a published SHACL shape with at least one PropertyShape."""
    try:
        dataset_id = resolve_dataset_id(dataset_id)
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
    dataset_id = resolve_dataset_id(dataset_id)
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
        def _pick_label(item: dict) -> str:
            """Extract a plain string label from a JSON-LD item, handling both
            compact keys (sh:name, rdfs:label) and full URI keys."""
            # Try compact keys first
            for key in ("sh:name", "rdfs:label"):
                v = item.get(key)
                if v:
                    if isinstance(v, dict):
                        return v.get("de") or v.get("en") or v.get("fr") or str(v)
                    if isinstance(v, list) and v:
                        entry = v[0]
                        return entry.get("@value", str(entry)) if isinstance(entry, dict) else str(entry)
                    return str(v)
            # Try full URI keys
            for key in (
                "http://www.w3.org/2000/01/rdf-schema#label",
                "http://www.w3.org/ns/shacl#name",
            ):
                v = item.get(key)
                if v:
                    if isinstance(v, list) and v:
                        entry = v[0]
                        return entry.get("@value", str(entry)) if isinstance(entry, dict) else str(entry)
                    return str(v)
            # Fall back to path or @id
            sh_path = item.get("http://www.w3.org/ns/shacl#path") or item.get("sh:path")
            if sh_path:
                if isinstance(sh_path, list) and sh_path:
                    sh_path = sh_path[0]
                if isinstance(sh_path, dict):
                    return sh_path.get("@id", "").split("/")[-1]
            return item.get("@id", "").split("/")[-1]

        for item in graph:
            item_type = str(item.get("@type", ""))
            if "PropertyShape" not in item_type:
                continue
            name = _pick_label(item)
            if isinstance(name, dict):
                name = name.get("de") or name.get("en") or name.get("fr") or str(name)
            if not name or not isinstance(name, str) or not name.strip():
                continue
            # dct:conformsTo carries the authoritative link to the I14Y concept.
            concept_id: str | None = None
            conforms_to = (
                item.get("dct:conformsTo")
                or item.get("conformsTo")
                or item.get("http://purl.org/dc/terms/conformsTo")
            )
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
