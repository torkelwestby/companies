# app.py
import io
import math
import random
import unicodedata
import requests
import pandas as pd
import streamlit as st

ENHETS_API = "https://data.brreg.no/enhetsregisteret/api/enheter"

st.set_page_config(page_title="Selskapsfinner – Brreg", layout="wide")
st.title("Selskapsfinner – Enhetsregisteret + (valgfritt) Regnskap")

# --- Konfig: NACE-grupper for "type" ------------------------------------------
KONTOR_NACE_PREFIXES = [
    "62",  # IT-konsulent / programvare
    "63",  # Informasjonstjenester
    "69",  # Juridisk og regnskap
    "70",  # Hovedkontor/management consulting
    "71",  # Arkitekt/teknisk
    "73",  # Reklame/markedsanalyse
    "74",  # Annen faglig, vitenskapelig og teknisk tjenesteyting
    "78",  # Bemanning
    "82",  # Kontortjenester / call center
    "46",  # Engroshandel (salg B2B)
    "47",  # Detaljhandel (salg B2C) – kan treffe butikker, men ofte relevant for "salg"
]
HELSE_NACE_PREFIXES = [
    "85",  # Utdanning (skole, barnehage m.m. – barnehage oftest 88.91, men 85 dekker skole)
    "86",  # Helse
    "87",  # Pleie- og omsorg med overnatting
    "88",  # Sosialtjenester (inkl. barnehage 88.91)
]

PUBLIC_ORGFORM = {
    # Vanlige offentlige organisasjonsformer (ikke uttømmende)
    "KOMM", "FYLKE", "KF", "FKF", "IKS", "STAT", "SF", "ORGL"
}

# --- Sidebar: filtre -----------------------------------------------------------
with st.sidebar:
    st.header("Filtre")

    # A) Kommuner
    KOMMUNE_CHOICES = {
        "Oslo (0301)": "0301",
        "Bergen (4601)": "4601",
        "Trondheim (5001)": "5001",
        "Stavanger (1103)": "1103",
        "Drammen (3005)": "3005",
        "Bærum (3024)": "3024",
    }
    valgt_kommuner_navn = st.multiselect(
        "Velg kommuner",
        options=list(KOMMUNE_CHOICES.keys()),
        default=["Oslo (0301)"],
        help="Huk av én eller flere kommuner."
    )
    valgt_kommunenr = [KOMMUNE_CHOICES[n] for n in valgt_kommuner_navn]
    andre_kommuner_raw = st.text_input("Egne kommunenummer (komma-separert)", value="")
    if andre_kommuner_raw.strip():
        ekstra = [k.strip() for k in andre_kommuner_raw.split(",") if k.strip()]
        kommunenummer = list(dict.fromkeys(valgt_kommunenr + ekstra))
    else:
        kommunenummer = valgt_kommunenr

    # B) Ansatte
    col1, col2 = st.columns(2)
    with col1:
        min_ansatte = st.number_input("Min ansatte", min_value=0, value=0, step=1)
    with col2:
        max_ansatte = st.number_input("Max ansatte", min_value=0, value=999_999, step=1)

    # C) Type (NACE-grupper)
    st.subheader("Type (bransjeklynger)")
    use_kontor = st.checkbox("Kontorbedrifter (IT/rådgivning/regnskap/salg)", value=False)
    use_helse = st.checkbox("Helse & omsorg (barnehage/skole/helse)", value=False)

    # D) Sektor
    st.subheader("Sektor")
    sektor_priv = st.checkbox("Privat", value=True)
    sektor_off = st.checkbox("Offentlig", value=True)

    # E) Nettsidekrav
    only_with_site = st.checkbox("Kun selskaper med nettside", value=True)

    st.divider()
    st.subheader("Antall, sortering og ytelse")
    ønsket_antall = st.number_input("Hvor mange selskaper vil du hente?", min_value=1, value=500, step=50)
    api_pagesize = st.select_slider("Rader per API-side", options=[50, 100, 200, 500], value=200)
    shuffle_every_run = st.checkbox("Nye (tilfeldige) selskaper ved hver kjøring", value=True)

    st.divider()
    st.subheader("Regnskap (valgfritt)")
    enrich_fin = st.checkbox("Berik med regnskap (årsresultat, lønnskostnader)", value=True,
                             help="Best effort fra Regnskapsregisteret sitt åpne API (siste år). Mangler settes tomt.")
    max_regnskap_calls = st.slider("Maks antall regnskapsoppslag", 10, 2000, 500, 10)

# --- Hjelpefunksjoner ----------------------------------------------------------
@st.cache_data(show_spinner=False)
def fetch_page(params: dict) -> dict:
    r = requests.get(ENHETS_API, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def build_params(page:int, size:int, kommunenummer=None, min_ansatte=None, max_ansatte=None, sort=None) -> dict:
    p = {"page": page, "size": size}
    if kommunenummer:
        p["kommunenummer"] = ",".join(kommunenummer)
    if min_ansatte is not None:
        p["fraAntallAnsatte"] = min_ansatte
    if max_ansatte is not None:
        p["tilAntallAnsatte"] = max_ansatte
    if sort:
        p["sort"] = sort
    return p

def nace_matches(prefixes:list[str], codes:list[str]) -> bool:
    if not prefixes:
        return True
    if not codes:
        return False
    return any(any(code.startswith(p) for p in prefixes) for code in codes)

def classify_type(codes:list[str]) -> str:
    is_kontor = nace_matches(KONTOR_NACE_PREFIXES, codes)
    is_helse  = nace_matches(HELSE_NACE_PREFIXES, codes)
    if is_kontor and not is_helse:
        return "Kontor"
    if is_helse and not is_kontor:
        return "Helse/omsorg"
    if is_kontor and is_helse:
        return "Blandet"
    return "Annet"

def infer_sector(enhet:dict) -> str:
    # Prøv institusjonell sektorkode, ellers orgform-heuristikk
    sekt = (enhet.get("institusjonellSektorkode") or {}).get("kode")
    if sekt and str(sekt).startswith("6"):
        return "Offentlig"
    orgform_kode = ((enhet.get("organisasjonsform") or {}).get("kode") or "").upper()
    if orgform_kode in PUBLIC_ORGFORM:
        return "Offentlig"
    return "Privat"

def has_website(url:str|None) -> bool:
    if not url:
        return False
    u = url.strip()
    return len(u) > 3

def normalize_enhet_rows(data: dict) -> list[dict]:
    rows = []
    for e in data.get("_embedded", {}).get("enheter", []):
        addr = e.get("forretningsadresse") or {}
        orgf = e.get("organisasjonsform") or {}
        nk1  = (e.get("naeringskode1") or {}).get("kode")
        nk2  = (e.get("naeringskode2") or {}).get("kode")
        nk3  = (e.get("naeringskode3") or {}).get("kode")
        nace_codes = [c for c in [nk1, nk2, nk3] if c]
        rows.append({
            "orgnr": e.get("organisasjonsnummer"),
            "navn": e.get("navn"),
            "hjemmeside": e.get("hjemmeside"),
            "kommune": (e.get("forretningsadresse") or {}).get("kommune"),
            "kommunenr": (e.get("forretningsadresse") or {}).get("kommunenummer"),
            "ansatte": e.get("antallAnsatte"),
            "orgform": orgf.get("kode"),
            "nace_codes": nace_codes,
            "type_label": classify_type(nace_codes),
            "sektor": infer_sector(e),
        })
    return rows

def pass_type_filter(row:dict, use_kontor:bool, use_helse:bool) -> bool:
    if not (use_kontor or use_helse):
        return True
    label = row["type_label"]
    ok = False
    if use_kontor and label in ("Kontor", "Blandet"):
        ok = True
    if use_helse and label in ("Helse/omsorg", "Blandet"):
        ok = True
    return ok

def pass_sector_filter(row:dict, sektor_priv:bool, sektor_off:bool) -> bool:
    if sektor_priv and sektor_off:
        return True
    if not (sektor_priv or sektor_off):
        return True
    return ((row["sektor"] == "Privat" and sektor_priv) or
            (row["sektor"] == "Offentlig" and sektor_off))

def fetch_until_limit(limit:int,
                      kommunenummer=None,
                      min_ansatte=None, max_ansatte=None,
                      sort:str|None=None,
                      type_flags:tuple[bool,bool]=(False,False),
                      sector_flags:tuple[bool,bool]=(True,True),
                      only_with_site:bool=True,
                      page_size:int=200) -> tuple[pd.DataFrame, int]:
    """Hent side for side og filtrer lokalt til vi har 'limit' rader."""
    want_kontor, want_helse = type_flags
    priv_ok, off_ok = sector_flags

    page = 0
    collected = []
    total_elements = None
    total_pages = None

    while len(collected) < limit:
        params = build_params(page, page_size, kommunenummer, min_ansatte, max_ansatte, sort)
        data = fetch_page(params)

        if total_elements is None:
            meta = data.get("page", {}) or {}
            total_elements = meta.get("totalElements", 0)
            total_pages = meta.get("totalPages", 1)

        rows = normalize_enhet_rows(data)
        for r in rows:
            if only_with_site and not has_website(r["hjemmeside"]):
                continue
            if not pass_type_filter(r, want_kontor, want_helse):
                continue
            if not pass_sector_filter(r, priv_ok, off_ok):
                continue
            collected.append(r)
            if len(collected) >= limit:
                break

        page += 1
        if total_pages is not None and page >= total_pages:
            break

    df = pd.DataFrame(collected)
    return df, (total_elements or len(df))

# --- Regnskapsberikelse (best effort) -----------------------------------------
REGN_ENDPOINT_CANDIDATES = [
    # Vi forsøker disse i rekkefølge; API-et er i "preview" og har endret path'er over tid
    ("https://data.brreg.no/regnskapsregisteret/regnskap/api/regnskap", "organisasjonsnummer"),
    ("https://data.brreg.no/regnskapsregisteret/regnskap/regnskap", "organisasjonsnummer"),
    ("https://data.brreg.no/regnskapsregisteret/regnskap/v3/regnskap", "organisasjonsnummer"),
    ("https://data.brreg.no/regnskapsregisteret/regnskap/regnskap", "orgnr"),
]

def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

def _flatten(d, parent_key="", sep="."):
    items = []
    if isinstance(d, dict):
        for k, v in d.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            items.extend(_flatten(v, new_key, sep=sep))
    elif isinstance(d, list):
        for i, v in enumerate(d):
            new_key = f"{parent_key}{sep}{i}" if parent_key else str(i)
            items.extend(_flatten(v, new_key, sep=sep))
    else:
        items.append((parent_key, d))
    return items

def _find_numeric(payload:dict, key_hints:list[str]) -> float|None:
    """Søk grovt etter tallfelt i vilkårlig JSON: matcher nøkkelstier som inneholder hint (case-insensitiv, ascii)."""
    flat = _flatten(payload)
    hints = [h.lower() for h in key_hints]
    for k, v in flat:
        if not isinstance(v, (int, float)):
            continue
        k_ascii = _strip_accents(k).lower()
        if any(h in k_ascii for h in hints):
            return float(v)
    return None

@st.cache_data(show_spinner=False)
def fetch_regnskap_for(orgnr: str) -> dict|None:
    """Returner rå JSON for siste års nøkkeltall, eller None hvis ikke funnet."""
    for base, param in REGN_ENDPOINT_CANDIDATES:
        try:
            r = requests.get(base, params={param: orgnr}, timeout=20)
            if r.status_code == 200 and r.headers.get("content-type","").startswith("application/json"):
                return r.json()
        except requests.RequestException:
            pass
    return None

def enrich_with_financials(df: pd.DataFrame, max_calls:int = 500) -> pd.DataFrame:
    if df.empty:
        df["årsresultat"] = pd.NA
        df["lønn pr ansatt"] = pd.NA
        return df

    # Begrens antall oppslag for ytelse
    to_lookup = df["orgnr"].head(max_calls).tolist()
    fin_map = {}
    for orgnr in to_lookup:
        payload = fetch_regnskap_for(orgnr)
        if not payload:
            fin_map[orgnr] = (None, None)
            continue
        arsres = _find_numeric(payload, ["aarsresultat", "arsresultat", "resultat etter skatt", "årsresultat"])
        lonn   = _find_numeric(payload, ["loennskostnader", "lonnskostnader", "lønnskostnader"])
        fin_map[orgnr] = (arsres, lonn)

    df = df.copy()
    df["årsresultat"] = df["orgnr"].map(lambda x: fin_map.get(x, (None, None))[0])
    df["lønnskostnader"] = df["orgnr"].map(lambda x: fin_map.get(x, (None, None))[1])

    # Lønn pr ansatt
    def _per_emp(row):
        try:
            if row.get("ansatte") and row.get("lønnskostnader") is not None:
                if row["ansatte"] > 0:
                    return float(row["lønnskostnader"]) / float(row["ansatte"])
        except Exception:
            return None
        return None

    df["lønn pr ansatt"] = df.apply(_per_emp, axis=1)

    # Rydd rekkefølge og visningskolonner
    return df

# --- Kjør søk + visning -------------------------------------------------------
colA, colB = st.columns([1, 4])
with colA:
    run = st.button("Hent selskaper", type="primary")
with colB:
    st.caption("Tips: Øk 'Rader per API-side' ved store uttrekk. Regnskapsberikelse bruker flere nettverkskall.")

if run:
    with st.spinner("Henter fra Enhetsregisteret..."):
        base_df, total = fetch_until_limit(
            limit=ønsket_antall,
            kommunenummer=kommunenummer or None,
            min_ansatte=min_ansatte or None,
            max_ansatte=max_ansatte or None,
            sort=None,  # vi randomiserer uansett etterpå hvis ønsket
            type_flags=(use_kontor, use_helse),
            sector_flags=(sektor_priv, sektor_off),
            only_with_site=only_with_site,
            page_size=api_pagesize,
        )

    if shuffle_every_run and not base_df.empty:
        base_df = base_df.sample(frac=1.0, random_state=None).reset_index(drop=True)

    df = base_df.copy()

    # Regnskapsberikelse (valgfritt)
    if enrich_fin:
        with st.spinner("Henter regnskap (best effort – siste år)..."):
            df = enrich_with_financials(df, max_calls=max_regnskap_calls)

    # Sett opp visning/kolonner
    show_cols = [
        "navn", "hjemmeside", "kommune", "ansatte",
        "type_label",  # vår "type"
    ]
    if "årsresultat" in df.columns:
        show_cols += ["årsresultat"]
    if "lønn pr ansatt" in df.columns:
        show_cols += ["lønn pr ansatt"]

    # Omdøp kolonneetiketter
    rename_map = {
        "navn": "Selskapsnavn",
        "hjemmeside": "Nettside",
        "kommune": "Kommune",
        "ansatte": "Antall ansatte",
        "type_label": "Type",
        "årsresultat": "Årsresultat",
        "lønn pr ansatt": "Lønnskostnader per ansatt",
    }
    out_df = df[show_cols].rename(columns=rename_map)

    # Vis statuslinje
    st.markdown(
        f"**Totalt treff hos Brreg:** {total:,}  •  **Returnert (etter filtre):** {len(out_df):,}  "
        f"•  **Kun med nettside:** {'Ja' if only_with_site else 'Nei'}  "
        f"•  **Typefilter:** "
        f"{'Kontor ' if use_kontor else ''}{'Helse ' if use_helse else '' or 'Ingen'}  "
        f"•  **Sektor:** "
        f"{'Privat' if sektor_priv else ''}{'/' if sektor_priv and sektor_off else ''}{'Offentlig' if sektor_off else ''}"
    )

    st.dataframe(out_df, width="stretch", hide_index=True)

    # --- Nedlasting: CSV / Excel ---
    csv_bytes = out_df.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Last ned som CSV", data=csv_bytes, file_name="enheter_filtrert.csv", mime="text/csv")

    excel_buf = io.BytesIO()
    with pd.ExcelWriter(excel_buf, engine="xlsxwriter") as writer:
        out_df.to_excel(writer, index=False, sheet_name="Enheter")
    st.download_button("⬇️ Last ned som Excel (.xlsx)",
                       data=excel_buf.getvalue(),
                       file_name="enheter_filtrert.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

st.caption("Kilder: Enhetsregisteret (åpne data). Regnskapsberikelse forsøkes via Regnskapsregisteret (åpen del, siste år).")
