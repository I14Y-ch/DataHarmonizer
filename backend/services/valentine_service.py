"""Schema matching using Valentine + I14Y concept enrichment."""
from __future__ import annotations

import math

import pandas as pd
from valentine import valentine_match
from valentine.algorithms import JaccardDistanceMatcher

from .i14y_service import lookup_concepts_batch

EXACT_THRESHOLD = 0.85
CLOSE_THRESHOLD = 0.50


def _safe_float(v) -> float:
    try:
        f = float(v)
        return 0.0 if math.isnan(f) else f
    except (TypeError, ValueError):
        return 0.0


def run_matching(
    df_source: pd.DataFrame,
    df_target: pd.DataFrame,
    source_name: str = "source",
    target_name: str = "target",
    known_concepts: dict[str, dict] | None = None,
) -> dict:
    """
    Run Valentine schema matching enriched with I14Y concept signals.

    Returns a dict with:
      - compatibility_score : float [0–1]
      - col_concepts        : {col_name: concept_info}
      - all_pairs           : [{source_col, target_col, score, hybrid_score, i14y_signal, category}]
      - best_matches        : best pair per source_col
      - stats               : {exact_match, close_match, incompatible}
    """
    # Valentine needs at least one row
    if len(df_source) == 0:
        df_source = pd.DataFrame(
            [[None] * len(df_source.columns)], columns=df_source.columns
        )
    if len(df_target) == 0:
        df_target = pd.DataFrame(
            [[None] * len(df_target.columns)], columns=df_target.columns
        )

    matcher = JaccardDistanceMatcher()
    matches = valentine_match(
        df_source, df_target, matcher, df1_name=source_name, df2_name=target_name
    )

    rows = [
        {
            "source_col": col_a,
            "target_col": col_b,
            "score": _safe_float(matches[((tbl_a, col_a), (tbl_b, col_b))]),
        }
        for (tbl_a, col_a), (tbl_b, col_b) in matches
    ]

    if not rows:
        return {
            "compatibility_score": 0.0,
            "col_concepts": {},
            "all_pairs": [],
            "best_matches": [],
            "stats": {
                "exact_match": 0,
                "close_match": 0,
                "incompatible": len(df_source.columns),
            },
        }

    df_all = pd.DataFrame(rows).sort_values("score", ascending=False)

    # I14Y concept lookup:
    # - Target columns: use the concept ID linked via dct:conformsTo in the SHACL shape (authoritative).
    # - Source columns (uploaded CSV): no SHACL link, fall back to text search by column name.
    all_cols = list(set(df_source.columns) | set(df_target.columns))
    pre_known = known_concepts or {}
    cols_to_search = [c for c in all_cols if c not in pre_known]
    col_concepts = {**pre_known, **lookup_concepts_batch(cols_to_search)}

    # ── Hybrid scoring ────────────────────────────────────────────────────────
    def _hybrid(row: dict) -> tuple[float, str | None]:
        ca = col_concepts.get(row["source_col"])
        cb = col_concepts.get(row["target_col"])
        s = row["score"]

        if ca and cb:
            if ca["conceptId"] == cb["conceptId"]:
                return 1.0, f'concept_verified ({ca["title"]})'
            if ca["conceptType"] == cb["conceptType"]:
                return min(s * 1.2, 1.0), f'type_boost ({ca["conceptType"]})'
            return s * 0.5, f'type_conflict ({ca["conceptType"]} vs {cb["conceptType"]})'

        return s, None

    df_all[["hybrid_score", "i14y_signal"]] = df_all.apply(
        lambda r: _hybrid(r.to_dict()), axis=1, result_type="expand"
    )

    # ── Classification ────────────────────────────────────────────────────────
    def _classify(row) -> str:
        signal = row.get("i14y_signal")
        if isinstance(signal, str) and signal.startswith("concept_verified"):
            return "exact_match"
        s = _safe_float(row.get("hybrid_score", 0))
        if s >= EXACT_THRESHOLD:
            return "exact_match"
        if s >= CLOSE_THRESHOLD:
            return "close_match"
        return "incompatible"

    df_all["category"] = df_all.apply(_classify, axis=1)

    # Best match per source column
    best = (
        df_all.sort_values("hybrid_score", ascending=False)
        .groupby("source_col", as_index=False)
        .first()
    )

    n_src = len(df_source.columns)
    compat = float((best["category"] != "incompatible").sum() / n_src) if n_src else 0.0

    def _clean_row(row: dict) -> dict:
        return {
            k: (None if (isinstance(v, float) and math.isnan(v)) else v)
            for k, v in row.items()
        }

    return {
        "compatibility_score": round(compat, 4),
        "col_concepts": {k: v for k, v in col_concepts.items() if v},
        "all_pairs": [_clean_row(r) for r in df_all.to_dict(orient="records")],
        "best_matches": [_clean_row(r) for r in best.to_dict(orient="records")],
        "stats": {
            "exact_match": int((best["category"] == "exact_match").sum()),
            "close_match": int((best["category"] == "close_match").sum()),
            "incompatible": int((best["category"] == "incompatible").sum()),
        },
    }
