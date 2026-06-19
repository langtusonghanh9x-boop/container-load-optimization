# Container Load 3D Optimization

Streamlit web app for 3D container loading optimization.

## Features

- Upload Excel or CSV cargo data.
- Calculate 3D container stuffing with solid cargo blocks.
- Check loaded packages, volume usage, payload usage, and leftover cargo.
- Suggest additional standard containers when the selected container is not enough.
- Show a final loading plan per container.
- Support multiple selected containers and automatic additional containers.
- Support Lumber Bundle cargo in inches with automatic conversion to millimeters.
- Download loading-plan CSV output.

## Architecture

The Streamlit entrypoint stays in `app.py`, while long-term business logic lives in independent modules:

- `container_optimizer/packing.py`: packing engine based on `py3dbp`.
- `container_optimizer/containers.py`: container catalog and custom container specs.
- `container_optimizer/manager.py`: selected and automatically added container management.
- `container_optimizer/optimization.py`: multi-container optimization strategy.
- `container_optimizer/visualization.py`: Plotly 3D rendering.
- `container_optimizer/reporting.py`: tabular report generation.
- `container_optimizer/cargo.py`: cargo conversion, including Lumber Bundle inch-to-mm handling.

## Required Columns

The importer accepts common aliases, but the sample format is:

| Product Name | Quantity | Length (mm) | Width (mm) | Height (mm) | Weight (kg) |
|---|---:|---:|---:|---:|---:|

Use `sample_products.csv` to test the upload flow.

## Run Locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy Free On Streamlit Community Cloud

1. Create a new GitHub repository, for example `container-optimization`.
2. Upload these files to the repository:
   - `app.py`
   - `container_optimizer/`
   - `requirements.txt`
   - `runtime.txt`
   - `sample_products.csv`
   - `.gitignore`
   - `README.md`
3. Open [Streamlit Community Cloud](https://share.streamlit.io/).
4. Sign in with GitHub.
5. Choose **New app**.
6. Select the repository and branch.
7. Set main file path to `app.py`.
8. Click **Deploy**.

After deployment, Streamlit will provide a public URL like:

```text
https://container-optimization.streamlit.app
```

The app runs on Streamlit servers, so users only need a browser. Your personal computer does not need to stay on.
