"""LLM service — OpenAI-powered query expansion, result ranking, and summaries."""
from __future__ import annotations

import json
import os
import re

from openai import OpenAI

_client: OpenAI | None = None

def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY not set. Create poc/backend/.env with OPENAI_API_KEY=sk-..."
            )
        _client = OpenAI(api_key=api_key)
    return _client


MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
I14Y_MCP_URL = "https://mcp.i14y.d.c.bfs.admin.ch/mcp"


def search_with_mcp(question: str, uploaded_columns: list[str]) -> dict:
    """
    Use the OpenAI Responses API with the I14Y remote MCP server to discover,
    search, and summarise relevant datasets in a single LLM call.

    Returns:
        {
          "ranked": [{"id", "title", "description", "publisher", "llm_reason", ...}, ...],
          "intro":  "short intro sentence",
          "used_mcp": True,
        }
    Raises RuntimeError if OPENAI_API_KEY is not set.
    Falls back to empty result on connectivity failure.
    """
    col_hint = (
        f"The user has uploaded a dataset with these columns: {', '.join(uploaded_columns[:12])}. "
        if uploaded_columns
        else ""
    )

    prompt = (
        f"{col_hint}"
        f"Find the 3 most relevant datasets on I14Y for this question: \"{question}\". "
        "Search the catalogue, then return a JSON object with:\n"
        '{"intro": "one sentence summary", '
        '"ranked": [{"id": "...", "title": "...", "description": "...", '
        '"publisher": "...", "llm_reason": "why relevant in one sentence"}, ...]}'
    )

    resp = _get_client().responses.create(
        model=MODEL,
        tools=[{
            "type": "mcp",
            "server_label": "i14y",
            "server_url": I14Y_MCP_URL,
            "require_approval": "never",
        }],
        input=prompt,
    )

    # Extract the final text output from the response
    output_text = ""
    for item in resp.output:
        if getattr(item, "type", None) == "message":
            for block in getattr(item, "content", []):
                if getattr(block, "type", None) == "output_text":
                    output_text += block.text

    # Parse JSON from the model's reply
    try:
        # Extract JSON object even if surrounded by prose
        match = re.search(r'\{.*\}', output_text, re.DOTALL)
        data = json.loads(match.group(0) if match else output_text)
        ranked = data.get("ranked", [])
        intro = data.get("intro", "")
        # Normalise: ensure each entry has the keys our frontend expects
        normalised = []
        for ds in ranked[:3]:
            normalised.append({
                "id": ds.get("id", ""),
                "title": ds.get("title", ""),
                "description": ds.get("description", ""),
                "publisher": ds.get("publisher", ""),
                "llm_reason": ds.get("llm_reason", ds.get("reason", "")),
                "has_download": ds.get("has_download", False),
                "download_url": ds.get("download_url"),
                "format": ds.get("format"),
                "structure_url": ds.get("structure_url"),
                "themes": ds.get("themes", []),
            })
        return {"ranked": normalised, "intro": intro, "used_mcp": True}
    except Exception:
        return {"ranked": [], "intro": "", "used_mcp": True}


def expand_query(question: str, uploaded_columns: list[str]) -> list[str]:
    """
    Turn the user's natural-language question into 1–3 focused I14Y search queries.
    Returns a list of query strings ranked by expected relevance.
    """
    col_hint = (
        f"The user has uploaded a dataset with these columns: {', '.join(uploaded_columns[:12])}."
        if uploaded_columns
        else ""
    )

    system = (
        "You are a data catalogue expert for the Swiss federal government. "
        "Your job is to translate a user's natural language question into short, "
        "effective keyword search queries for the I14Y interoperability platform. "
        "I14Y indexes Swiss open datasets, concepts, and mapping tables. "
        "Return ONLY a JSON array of 1 to 3 short query strings (no explanation). "
        "Queries should be in the same language as the user's question when possible, "
        "but also include German equivalents if the question is in French or English, "
        "because most I14Y metadata is in German."
    )

    user = f"{col_hint}\nUser question: {question}\n\nReturn a JSON array of search queries."

    resp = _get_client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2,
        max_tokens=200,
        response_format={"type": "json_object"},
    )

    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
        # Accept {"queries": [...]} or {"results": [...]} or a bare list
        if isinstance(data, list):
            queries = data
        else:
            queries = data.get("queries") or data.get("results") or list(data.values())[0]
        return [str(q) for q in queries if q][:3]
    except Exception:
        return [question]


def rank_and_summarise(
    question: str,
    uploaded_columns: list[str],
    raw_datasets: list[dict],
) -> dict:
    """
    Given up to N raw I14Y dataset cards, pick the best 3 and produce a short
    natural-language summary explaining why each is relevant.

    Returns:
        {
          "ranked": [enriched dataset dict with added "llm_reason" key, ...],
          "intro":  "short paragraph introducing the results",
        }
    """
    if not raw_datasets:
        return {"ranked": [], "intro": "No datasets found on I14Y for your question."}

    col_hint = (
        f"Uploaded dataset columns: {', '.join(uploaded_columns[:12])}."
        if uploaded_columns
        else ""
    )

    # Build a compact catalogue representation for the LLM
    catalogue = [
        {
            "index": i,
            "title": ds.get("title", ""),
            "publisher": ds.get("publisher", ""),
            "description": (ds.get("description") or "")[:300],
            "has_download": ds.get("has_download", False),
            "format": ds.get("format", ""),
        }
        for i, ds in enumerate(raw_datasets)
    ]

    system = (
        "You are a data catalogue expert for the Swiss federal government. "
        "Given a user question and a list of I14Y dataset candidates, "
        "select the 3 most relevant datasets and explain in one short sentence "
        "why each is relevant to the user's question. "
        "Also write a 1-sentence intro summarising what you found. "
        "Return ONLY valid JSON in this exact shape:\n"
        '{"intro": "...", "ranked": [{"index": 0, "reason": "..."}, ...]}'
    )

    user = (
        f"{col_hint}\n"
        f"User question: {question}\n\n"
        f"Datasets:\n{json.dumps(catalogue, ensure_ascii=False, indent=2)}"
    )

    resp = _get_client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2,
        max_tokens=500,
        response_format={"type": "json_object"},
    )

    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
        intro = data.get("intro", "")
        ranked_meta = data.get("ranked", [])

        ranked_datasets = []
        for entry in ranked_meta[:3]:
            idx = int(entry.get("index", 0))
            if 0 <= idx < len(raw_datasets):
                ds = dict(raw_datasets[idx])
                ds["llm_reason"] = entry.get("reason", "")
                ranked_datasets.append(ds)

        # Fallback: include remaining un-ranked datasets if < 3 matched
        seen = {d["id"] for d in ranked_datasets}
        for ds in raw_datasets:
            if len(ranked_datasets) >= 3:
                break
            if ds["id"] not in seen:
                ranked_datasets.append(ds)

        return {"ranked": ranked_datasets, "intro": intro}

    except Exception:
        # Graceful degradation: return raw results unchanged
        return {"ranked": raw_datasets[:3], "intro": ""}
