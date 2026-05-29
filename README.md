# DataHarmonizer POC

A proof-of-concept web app for **semantic dataset discovery + schema harmonisation**, powered by the [I14Y Interoperability Platform](https://www.i14y.admin.ch) and the [Valentine](https://github.com/delftdata/valentine) schema-matching library.

---

## User Journey

### Step 1 — Dataset Discovery
Upload one or more datasets (CSV or XML) and ask a question.  
The backend searches the **I14Y public API** and returns the **3 most relevant datasets** ranked by semantic relevance.

> *Example: upload `businesses.csv` and ask "Do other cantonal business registries exist?" or "Is there a communal version of this dataset?"*

### Step 2 — Schema Matching
Click **Compare schemas** on any search result.  
The backend:
1. Fetches the I14Y dataset (download URL or JSON-LD structure)
2. Runs **Valentine `JaccardDistanceMatcher`** between your uploaded dataset and the I14Y dataset
3. **Enriches scores** with I14Y concept lookups:
   - Same concept → score overridden to `1.0` (★ `concept_verified`)
   - Same concept type → score boosted ×1.2
   - Conflicting concept types → score penalised ×0.5
4. Classifies each column pair as `exact_match`, `close_match`, or `incompatible`

Results show a **compatibility score**, stats, and a colour-coded column-by-column table.

### Step 3 — Export Transformation Metadata
Download a **ZIP** containing:

| File | Purpose |
|------|---------|
| `mapping_table.csv` | Field-level concept mappings — compatible with I14Y upload format |
| `transformation_recipe.json` | Full transformation plan with actions (`rename`, `transform`, `skip`) and I14Y concept IDs |

---

## Architecture

```
poc/
├── backend/
│   ├── app.py                   Flask app (serves frontend + API)
│   ├── requirements.txt
│   └── services/
│       ├── i14y_service.py      I14Y public API wrapper
│       ├── valentine_service.py Valentine + I14Y hybrid scoring
│       └── export_service.py    ZIP export generator
└── frontend/
    ├── index.html               Single-page chat UI (Open WebUI inspired)
    ├── style.css                Dark theme, CSS variables
    └── app.js                   All client-side logic
```

**Backend endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/upload` | Upload CSV / XML files into a session |
| `POST` | `/api/search` | Semantic I14Y dataset search |
| `POST` | `/api/compare` | Valentine + I14Y schema matching |
| `GET`  | `/api/export/<session_id>` | Download transformation ZIP |

---

## Quick Start

### 1. Create a virtual environment

```bash
cd poc/backend
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

> **Note:** Valentine requires `numpy<2`. The `requirements.txt` already pins this.  
> If you see a NumPy ABI warning, run `pip install "numpy<2"` explicitly and restart.

### 3. Run the server

```bash
python app.py
```

The app will be available at **http://localhost:5000**.

### 4. Try it out

1. Click the attachment button (📎) and upload `../data/businesses.csv`
2. Type: *"Do other cantonal business registries exist on I14Y?"*
3. Hit **Enter** — the app will search I14Y and show up to 3 results
4. Click **Compare schemas** on any result
5. Review the compatibility score and column-level match table
6. Click **Download transformation_export.zip** to get the mapping table + recipe

---

## Sample Data

The `../data/` folder (from the hackathon repo) contains:

| File | Description |
|------|-------------|
| `businesses.csv` | Synthetic cantonal business registry (UID, legal form, NOGA, …) |
| `patients.xml` | Synthetic SpiGes hospital cases |
| `insurance.csv` | Synthetic insurance claims (partial overlap with SpiGes) |

---

## Limitations (POC)

- Sessions are stored **in-memory** — restarting the server clears all sessions
- I14Y datasets without a `downloadUrl` are matched on column names from the JSON-LD structure only
- Valentine `JaccardDistanceMatcher` is purely name-based (no embedding / ML); swap for `SimilarityFlooding` or `Cupid` for higher recall
- No authentication — uses I14Y **public** endpoints only
