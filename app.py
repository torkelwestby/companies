import requests, pandas as pd, streamlit as st

API = "https://data.brreg.no/enhetsregisteret/api/enheter"

st.set_page_config(page_title="Brreg test", layout="wide")
st.title("Brreg – minimal test")

params = {"page": 0, "size": 10, "kommunenummer": "0301"}  # Oslo 10 stk
r = requests.get(API, params=params, timeout=30)
r.raise_for_status()
data = r.json().get("_embedded", {}).get("enheter", [])

rows = []
for e in data:
    a = e.get("forretningsadresse") or {}
    f = e.get("organisasjonsform") or {}
    n1 = e.get("naeringskode1") or {}
    rows.append({
        "orgnr": e.get("organisasjonsnummer"),
        "navn": e.get("navn"),
        "kommune": a.get("kommune"),
        "ansatte": e.get("antallAnsatte"),
        "orgform": f.get("kode"),
        "nace": n1.get("kode"),
        "hjemmeside": e.get("hjemmeside"),
    })

df = pd.DataFrame(rows)
st.dataframe(df, use_container_width=True, hide_index=True)
