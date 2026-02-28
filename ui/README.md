# AIRTA UI

Streamlit UI and FastAPI server for the AIRTA pipeline (config, discovery, diagnostics, compliance tests, risk assessment).

## Run the Streamlit UI

From project root:

```bash
streamlit run ui/streamlit_app.py
```

Then open http://localhost:8501.

## Run the API server with Gunicorn

From project root:

```bash
gunicorn ui.api:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000
```

- **Health:** `GET /health`
- **Config:** `GET /config`
- **Test files:** `GET /test-files`
- **Run pipeline:** `POST /run/pipeline` (JSON body: `component`, `strategy`, `framework`, `skip_discovery`, `skip_diagnostics`, `force_discovery`, optional `test_file`, `report_dir`)
- **Reports:** `GET /reports`, `GET /reports/{timestamp}`

## Using both

1. Start the API: `gunicorn ui.api:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000`
2. Start Streamlit: `streamlit run ui/streamlit_app.py`
3. In the sidebar, set **API base URL** to `http://localhost:8000`. The **Run pipeline** action will then call the API instead of running in-process.

This keeps long pipeline runs in the API process and avoids Streamlit timeouts.
