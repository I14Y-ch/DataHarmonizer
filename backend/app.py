"""DataHarmonizer — Flask backend for the I14Y schema-matching POC."""
from __future__ import annotations

import io
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv not installed yet; set OPENAI_API_KEY in environment manually

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

from services.export_service import generate_exports
from services.i14y_service import (
    get_concept_by_id,
    get_dataset_metadata,
    get_dataset_structure,
    has_structure,
    load_dataset_from_url,
    search_datasets,
)
try:
    from services.llm_service import expand_query, rank_and_summarise, search_with_mcp
    _LLM_AVAILABLE = True
except ImportError:
    _LLM_AVAILABLE = False

from services.valentine_service import run_matching

app = Flask(__name__, static_folder="../frontend", static_url_path="")
CORS(app)

# ── In-memory session store (POC only — replace with Redis/DB for production) ─
# {session_id: {files: {name: df}, search_results: [...], match_results: {...}}}
SESSIONS: dict[str, dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_xml(content: bytes) -> pd.DataFrame:
    """Flatten an XML file into a DataFrame.  Handles SpiGes-style structure."""
    root = ET.parse(io.BytesIO(content)).getroot()
    raw_tag = root.tag
    ns = raw_tag[1: raw_tag.index("}")] if raw_tag.startswith("{") else ""

    def t(name: str) -> str:
        return f"{{{ns}}}{name}" if ns else name

    records: list[dict] = []
    standorts = list(root.iter(t("Standort")))
    if standorts:
        # SpiGes-style: Standort → Fall tree
        for standort in standorts:
            burnr = standort.findtext(t("burnr"), "")
            for fall in standort.iter(t("Fall")):
                row: dict = {"burnr": burnr}
                for child in fall.iter():
                    local = child.tag.split("}")[-1]
                    if child.text and child.text.strip() and local != "Fall":
                        row[local] = child.text.strip()
                records.append(row)
    else:
        # Generic: flatten first-level children
        for child in root:
            row = {}
            child_local = child.tag.split("}")[-1]
            for sub in child.iter():
                local = sub.tag.split("}")[-1]
                if sub.text and sub.text.strip() and local != child_local:
                    row[local] = sub.text.strip()
            if row:
                records.append(row)

    return pd.DataFrame(records) if records else pd.DataFrame()


def _parse_uploaded_file(file) -> pd.DataFrame:
    name = (file.filename or "").lower()
    content = file.read()
    if name.endswith(".csv"):
        return pd.read_csv(io.BytesIO(content))
    if name.endswith(".xml"):
        return _parse_xml(content)
    raise ValueError(
        f"Unsupported format: {file.filename!r}. Please upload CSV or XML files."
    )


def _multilang(obj: dict | None, *keys: str) -> str:
    if not obj:
        return ""
    for k in keys:
        v = obj.get(k)
        if v:
            return str(v)
    return str(obj) if obj else ""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.post("/api/upload")
def upload():
    """
    Accept dataset files and register them in a session.

    Form fields:
      session_id  (optional) — reuse existing session
      files       (multipart) — one or more CSV / XML files

    Returns: {session_id, uploaded: [{filename, rows, columns, sample}]}
    """
    session_id = request.form.get("session_id") or str(uuid.uuid4())
    if session_id not in SESSIONS:
        SESSIONS[session_id] = {
            "files": {},
            "search_results": [],
            "match_results": None,
        }

    uploaded = []
    for file in request.files.getlist("files"):
        if not file or not file.filename:
            continue
        try:
            df = _parse_uploaded_file(file)
            SESSIONS[session_id]["files"][file.filename] = df
            uploaded.append(
                {
                    "filename": file.filename,
                    "rows": len(df),
                    "columns": df.columns.tolist(),
                    "sample": df.head(3).fillna("").to_dict(orient="records"),
                }
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    return jsonify({"session_id": session_id, "uploaded": uploaded})


@app.post("/api/search")
def search():
    """
    Semantic search on I14Y using the user's question + uploaded column hints.

    JSON body: {session_id, question}
    Returns: {session_id, results: [dataset_card, ...], query}
    """
    body = request.get_json(force=True)
    session_id = body.get("session_id")
    question = (body.get("question") or "").strip()

    # Auto-create session if none exists (user asks without uploading first)
    if not session_id or session_id not in SESSIONS:
        session_id = session_id or str(uuid.uuid4())
        SESSIONS[session_id] = {
            "files": {},
            "search_results": [],
            "match_results": None,
        }

    session = SESSIONS[session_id]

    # Build fallback query from column names
    col_hints: list[str] = []
    for df in session["files"].values():
        col_hints.extend(df.columns.tolist()[:4])

    query = question or " ".join(col_hints[:4])
    if not query:
        return jsonify({"error": "Provide a question or upload a dataset first"}), 400

    try:
        intro = ""
        enriched: list[dict] = []

        # ── Path 1: OpenAI Responses API + I14Y MCP (preferred) ──────────────
        if _LLM_AVAILABLE and question:
            try:
                mcp_result = search_with_mcp(question, col_hints)
                mcp_datasets = mcp_result.get("ranked", [])
                intro = mcp_result.get("intro", "")

                # Always fetch full metadata to check structure_url + download_url.
                # Tier 1: has both a downloadable distribution and a SHACL structure.
                # Tier 2: has a SHACL structure only — matching still works via column names.
                # Fill slots from tier 1 first, then tier 2 up to 3 total.
                # Process ALL MCP candidates before trimming — don't break early.
                tier2_mcp: list[dict] = []
                for ds in mcp_datasets:
                    ds_id = ds.get("id")
                    if ds_id:
                        try:
                            full = get_dataset_metadata(ds_id)
                        except Exception:
                            # Slug-style IDs (e.g. CH_KT_BL_dataset_10040) return 404.
                            # Fall back to REST search using the title the MCP already returned.
                            full = {}
                            search_term = ds.get("title") or ds_id.replace("_", " ")
                            try:
                                hits = search_datasets(search_term, page_size=3)
                                resolved_id = hits[0].get("id") if hits else None
                                full = get_dataset_metadata(resolved_id) if resolved_id else {}
                                if resolved_id:
                                    ds["id"] = resolved_id
                                    ds_id = resolved_id
                            except Exception:
                                full = {}
                        if full:
                            distros = full.get("distributions") or []
                            dl = next(((d.get("downloadUrl") or {}).get("uri") for d in distros if (d.get("downloadUrl") or {}).get("uri")), None)
                            fmt = next(
                                ((d.get("format") or {}).get("code")
                                 for d in distros if (d.get("downloadUrl") or {}).get("uri")), None
                            )
                            ds["has_download"] = bool(dl)
                            ds["download_url"] = dl
                            ds["format"] = ds.get("format") or fmt
                            ds["has_structure"] = has_structure(ds_id)
                            if not ds.get("title"):
                                ds["title"] = _multilang(full.get("title"), "de", "en", "fr")
                            if not ds.get("description"):
                                ds["description"] = _multilang(full.get("description"), "de", "en", "fr")[:400]
                            if not ds.get("publisher"):
                                pub = full.get("publisher") or {}
                                ds["publisher"] = _multilang(pub.get("name") if isinstance(pub.get("name"), dict) else pub, "de", "en", "fr")
                            if not ds.get("themes"):
                                ds["themes"] = [
                                    _multilang(t, "de", "en", "fr") if isinstance(t, dict) else str(t)
                                    for t in (full.get("themes") or [])[:3]
                                ]
                    ds.setdefault("has_download", False)
                    ds.setdefault("has_structure", False)
                    if ds.get("has_download") and ds.get("has_structure"):
                        enriched.append(ds)
                    elif ds.get("has_structure"):
                        tier2_mcp.append(ds)
                # Fill remaining slots from tier2 (no early break — evaluate all first)
                for ds in tier2_mcp:
                    if len(enriched) >= 3:
                        break
                    enriched.append(ds)
                enriched = enriched[:3]

            except Exception:
                pass  # fall through to REST fallback

        # ── Path 2: REST keyword search fallback ─────────────────────────────
        if not enriched:
            raw: list[dict] = []
            seen: set[str] = set()

            if _LLM_AVAILABLE and question:
                try:
                    queries = expand_query(question, col_hints)
                except Exception:
                    queries = [question]
            else:
                queries = [question] if question else []

            if col_hints:
                queries.append(" ".join(col_hints[:3]))

            for q in queries:
                if not q:
                    continue
                for ds in search_datasets(q, page_size=8):
                    ds_id = ds.get("id", "")
                    if ds_id not in seen:
                        raw.append(ds)
                        seen.add(ds_id)
                if len(raw) >= 15:
                    break

            if _LLM_AVAILABLE and question and raw:
                try:
                    llm_result = rank_and_summarise(question, col_hints, raw[:15])
                    raw = llm_result.get("ranked", raw)
                    intro = llm_result.get("intro", "")
                except Exception:
                    pass

            # Scan up to 15 candidates. Evaluate ALL before trimming to 3.
            # Tier 1 (download + structure) is preferred; tier 2 (structure only) fills gaps.
            tier2_rest: list[dict] = []
            for raw_ds in raw[:15]:
                ds_id = raw_ds.get("id")
                llm_reason = raw_ds.get("llm_reason", "")
                try:
                    full = get_dataset_metadata(ds_id)
                except Exception:
                    full = raw_ds

                distros: list[dict] = full.get("distributions") or []
                download_url = next(
                    ((d.get("downloadUrl") or {}).get("uri") for d in distros if (d.get("downloadUrl") or {}).get("uri")), None
                )
                ds_has_structure = has_structure(ds_id) if ds_id else False
                fmt = next(
                    (
                        (d.get("format") or {}).get("code")
                        for d in distros
                        if (d.get("downloadUrl") or {}).get("uri")
                    ),
                    None,
                )
                pub = full.get("publisher") or {}
                publisher_str = _multilang(pub.get("name") if isinstance(pub.get("name"), dict) else pub, "de", "en", "fr")
                card = {
                    "id": ds_id,
                    "title": _multilang(full.get("title"), "de", "en", "fr"),
                    "description": _multilang(full.get("description"), "de", "en", "fr")[:400],
                    "publisher": publisher_str,
                    "has_download": bool(download_url),
                    "download_url": download_url,
                    "format": fmt,
                    "has_structure": ds_has_structure,
                    "themes": [
                        _multilang(t, "de", "en", "fr") if isinstance(t, dict) else str(t)
                        for t in (full.get("themes") or [])[:3]
                    ],
                    "llm_reason": llm_reason,
                }
                if download_url and ds_has_structure:
                    enriched.append(card)
                elif ds_has_structure:
                    tier2_rest.append(card)
            # Fill remaining slots from tier2, then trim
            for card in tier2_rest:
                enriched.append(card)
            enriched = enriched[:3]

        session["search_results"] = enriched
        return jsonify({"session_id": session_id, "results": enriched, "query": query, "intro": intro})

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/compare")
def compare():
    """
    Run Valentine + I14Y schema matching between an uploaded file and an I14Y dataset.

    JSON body: {session_id, dataset_id, source_filename (optional)}
    Returns: {session_id, source, target, compatibility_score, stats, best_matches, col_concepts}
    """
    body = request.get_json(force=True)
    session_id = body.get("session_id")
    dataset_id = body.get("dataset_id")
    source_filename = body.get("source_filename")

    if not session_id or session_id not in SESSIONS:
        return jsonify({"error": "Invalid or missing session_id"}), 400
    if not dataset_id:
        return jsonify({"error": "Missing dataset_id"}), 400

    session = SESSIONS[session_id]
    files = session["files"]

    # Resolve source DataFrame
    if source_filename and source_filename in files:
        df_source = files[source_filename]
        src_name = source_filename
    elif files:
        src_name = next(iter(files))
        df_source = files[src_name]
    else:
        return jsonify({"error": "No uploaded files in this session"}), 400

    # Resolve target dataset metadata
    target_ds: dict | None = next(
        (d for d in session["search_results"] if d["id"] == dataset_id), None
    )
    if target_ds is None:
        try:
            full = get_dataset_metadata(dataset_id)
            distros = full.get("distributions") or []
            target_ds = {
                "id": dataset_id,
                "title": _multilang(full.get("title"), "de", "en", "fr"),
                "download_url": next(
                    ((d.get("downloadUrl") or {}).get("uri") for d in distros if (d.get("downloadUrl") or {}).get("uri")), None
                ),
                "format": next(((d.get("format") or {}).get("code") for d in distros if (d.get("downloadUrl") or {}).get("uri")), None),
            }
        except Exception as exc:
            return jsonify({"error": f"Cannot retrieve dataset {dataset_id}: {exc}"}), 400

    tgt_name: str = target_ds.get("title") or dataset_id

    # Fetch the SHACL structure to extract authoritative concept links (dct:conformsTo).
    # This is always done so that concept matching uses the I14Y-defined concept IDs
    # rather than an imprecise text search on column name strings.
    target_concept_map: dict[str, dict] = {}
    struct = get_dataset_structure(dataset_id)
    for s in struct:
        cid = s.get("concept_id")
        if cid:
            concept = get_concept_by_id(cid)
            if concept:
                target_concept_map[s["name"]] = concept

    # Build target DataFrame (download > SHACL column names > title placeholder)
    df_target: pd.DataFrame | None = None
    if target_ds.get("download_url"):
        try:
            df_target = load_dataset_from_url(
                target_ds["download_url"], target_ds.get("format")
            )
        except Exception:
            pass  # fall through

    if df_target is None and struct:
        df_target = pd.DataFrame(columns=[s["name"] for s in struct])

    if df_target is None or df_target.empty:
        # Last resort: single-column placeholder so matching can run
        df_target = pd.DataFrame(columns=[tgt_name])

    try:
        result = run_matching(df_source, df_target, src_name, tgt_name, known_concepts=target_concept_map)
        session["match_results"] = {
            "result": result,
            "source_name": src_name,
            "target_name": tgt_name,
            "source_columns": df_source.columns.tolist(),
            "target_columns": df_target.columns.tolist(),
        }
        return jsonify(
            {
                "session_id": session_id,
                "source": src_name,
                "target": tgt_name,
                "compatibility_score": result["compatibility_score"],
                "stats": result["stats"],
                "best_matches": result["best_matches"],
                "col_concepts": result["col_concepts"],
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/export/<session_id>")
def export_results(session_id: str):
    """
    Return a ZIP archive containing transformation_recipe.json + mapping_table.csv.
    """
    if session_id not in SESSIONS:
        return jsonify({"error": "Session not found"}), 404

    match_data = SESSIONS[session_id].get("match_results")
    if not match_data:
        return jsonify({"error": "Run /api/compare first to generate results"}), 400

    try:
        zip_bytes = generate_exports(
            match_data["result"],
            match_data["source_name"],
            match_data["target_name"],
            match_data.get("source_columns", []),
            match_data.get("target_columns", []),
        )
        return send_file(
            io.BytesIO(zip_bytes),
            mimetype="application/zip",
            as_attachment=True,
            download_name="transformation_export.zip",
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
