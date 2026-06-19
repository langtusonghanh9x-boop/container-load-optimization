# Container Load 3D Optimization

Streamlit web app for 3D container loading optimization.

## Features

- Upload Excel or CSV cargo data.
- Calculate 3D container stuffing with solid cargo blocks.
- Check loaded packages, volume usage, payload usage, and leftover cargo.
- Suggest additional standard containers when the selected container is not enough.
- Show a final loading plan per container.

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
