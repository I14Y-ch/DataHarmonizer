"""Generate transformation exports (ZIP: recipe JSON + mapping CSV)."""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime


def generate_exports(
    match_result: dict,
    source_name: str,
    target_name: str,
    source_columns: list[str] | None = None,
    target_columns: list[str] | None = None,
) -> bytes:
    """
    Build a ZIP archive containing:
      - transformation_recipe.json  — machine-readable transformation plan
      - mapping_table.csv           — field-level mapping in i14y-uploadable format

    Returns raw ZIP bytes.
    """
    best_matches = match_result["best_matches"]
    col_concepts = match_result.get("col_concepts", {})

    _action = {"exact_match": "rename", "close_match": "transform"}
    _note = {
        "rename": lambda src, tgt: f"Rename '{src}' → '{tgt}'",
        "transform": lambda src, tgt: (
            f"Manual review needed — verify value formats / code lists "
            f"before mapping '{src}' → '{tgt}'"
        ),
        "skip": lambda src, _: f"No reliable counterpart found for '{src}'",
    }

    # ── 1. transformation_recipe.json ────────────────────────────────────────
    recipe = {
        "source": source_name,
        "source_attributes": source_columns or [],
        "target": target_name,
        "target_attributes": target_columns or [],
        "generated_on": datetime.now().isoformat(),
        "compatibility_score": match_result["compatibility_score"],
        "stats": match_result["stats"],
        "mappings": [],
    }

    for row in sorted(best_matches, key=lambda r: r.get("hybrid_score", 0), reverse=True):
        cat = row.get("category", "incompatible")
        act = _action.get(cat, "skip")
        recipe["mappings"].append(
            {
                "source_col": row["source_col"],
                "target_col": row.get("target_col"),
                "valentine_score": round(float(row.get("score") or 0), 4),
                "hybrid_score": round(float(row.get("hybrid_score") or 0), 4),
                "category": cat,
                "i14y_signal": row.get("i14y_signal"),
                "action": act,
                "note": _note[act](row["source_col"], row.get("target_col", "")),
            }
        )

    # ── 2. mapping_table.csv ─────────────────────────────────────────────────
    csv_rows = []
    for row in best_matches:
        cat = row.get("category", "incompatible")
        if cat == "incompatible":
            continue
        src_c = col_concepts.get(row["source_col"]) or {}
        tgt_c = col_concepts.get(row.get("target_col", "")) or {}
        act = _action.get(cat, "skip")
        csv_rows.append(
            {
                "source_field": row["source_col"],
                "source_concept_id": src_c.get("conceptId", ""),
                "source_concept_title": src_c.get("title", ""),
                "source_concept_type": src_c.get("conceptType", ""),
                "target_field": row.get("target_col", ""),
                "target_concept_id": tgt_c.get("conceptId", ""),
                "target_concept_title": tgt_c.get("title", ""),
                "target_concept_type": tgt_c.get("conceptType", ""),
                "match_type": cat,
                "hybrid_score": round(float(row.get("hybrid_score") or 0), 4),
                "i14y_signal": row.get("i14y_signal") or "",
                "action": act,
            }
        )

    # ── 3. executive_summary.txt ─────────────────────────────────────────────
    stats = match_result["stats"]
    n_exact = stats.get("exact_match", 0)
    n_close = stats.get("close_match", 0)
    n_incompatible = stats.get("incompatible", 0)
    n_total = n_exact + n_close + n_incompatible

    summary_lines = [
        "EXECUTIVE SUMMARY — DataHarmonizer Transformation Report",
        "=" * 60,
        f"Generated on : {recipe['generated_on']}",
        f"Source dataset: {source_name}",
        f"Target dataset: {target_name}",
        "",
        "COMPATIBILITY OVERVIEW",
        "-" * 40,
        f"Overall compatibility score : {match_result['compatibility_score']:.1%}",
        f"Total fields analysed       : {n_total}",
        f"  Exact matches (rename)    : {n_exact}",
        f"  Close matches (transform) : {n_close}",
        f"  Incompatible (no match)   : {n_incompatible}",
        "",
        "PROPOSED TRANSFORMATIONS",
        "-" * 40,
    ]

    for m in recipe["mappings"]:
        if m["category"] == "incompatible":
            summary_lines.append(f"  [SKIP]      {m['source_col']} — no reliable counterpart found")
        elif m["category"] == "exact_match":
            summary_lines.append(f"  [RENAME]    {m['source_col']}  →  {m['target_col']}  (score: {m['hybrid_score']:.2f})")
        else:
            summary_lines.append(f"  [TRANSFORM] {m['source_col']}  →  {m['target_col']}  (score: {m['hybrid_score']:.2f}) — manual review required")
        if m.get("i14y_signal"):
            summary_lines.append(f"              ↳ I14Y signal: {m['i14y_signal']}")

    summary_lines += [
        "",
        "NEXT STEPS",
        "-" * 40,
        "1. Review all TRANSFORM entries — validate value formats and code lists.",
        "2. Apply RENAME mappings directly; no value conversion needed.",
        "3. Decide on a strategy for SKIP fields (drop, keep as-is, or remap manually).",
        "4. Use transformation_recipe.json for automated pipeline integration.",
        "5. Use mapping_table.csv for upload to the I14Y Interoperability Platform.",
        "",
        "Generated by DataHarmonizer.",
    ]

    summary_txt = "\n".join(summary_lines)

    # ── 4. Pack into ZIP ─────────────────────────────────────────────────────
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "transformation_recipe.json",
            json.dumps(recipe, indent=2, ensure_ascii=False),
        )
        if csv_rows:
            csv_buf = io.StringIO()
            writer = csv.DictWriter(csv_buf, fieldnames=list(csv_rows[0].keys()))
            writer.writeheader()
            writer.writerows(csv_rows)
            zf.writestr("mapping_table.csv", csv_buf.getvalue())
        else:
            zf.writestr("mapping_table.csv", "source_field,target_field,match_type\n")
        zf.writestr("executive_summary.txt", summary_txt)

    buf.seek(0)
    return buf.read()
