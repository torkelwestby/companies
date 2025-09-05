# app.py — Firmify (Enhetsregisteret søk) med segment-filtre inkl. Fysisk & Topprestasjon
import io
import math
import requests
import pandas as pd
import streamlit as st

ENHETS_API = "https://data.brreg.no/enhetsregisteret/api/enheter"
PAGE_SIZE = 200  # fast side-størrelse mot API

st.set_page_config(page_title="Firmify – Livity", layout="wide")
st.title("Firmify for Livity 𐂐")

# --- Konfig: NACE-grupper for segmenter ---------------------------------------
# Kontor: IT, regnskap/juridisk, rådgivning, salg/markedsføring, kontortjenester, engros/detalj
KONTOR_NACE_PREFIXES = ["62", "63", "69", "70", "71", "73", "74", "78", "82", "46", "47"]

# Helse/omsorg: skole/utdanning, helse, pleie/omsorg, sosialtjenester
HELSE_NACE_PREFIXES = ["85", "86", "87", "88"]

# Fysisk: industri/produksjon (10–33), energi/avfall (35, 38–39), bygg/anlegg (41–43), transport (49–53)
FYSISK_NACE_PREFIXES = (
    [f"{i:02d}" for i in range(10, 34)]  # 10..33
    + ["35", "38", "39", "41", "42", "43", "49", "50", "51", "52", "53"]
)

# Topprestasjon: finans (64–66), advokat/regnskap (69), management consulting (70)
TOPP_NACE_PREFIXES = ["64", "65", "66", "69", "70"]

PUBLIC_ORGFORM = {
    # Vanlige offentlige orgformer (ikke uttømmende)
    "KOMM", "FYLKE", "KF", "FKF", "IKS", "STAT", "SF", "ORGL"
}

# --- Sidebar: filtre -----------------------------------------------------------
with st.sidebar:
    st.header("Filtre")

    # A) Kommuner (valg + egne koder)
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

    # B) Ansatte-intervall
    col1, col2 = st.columns(2)
    with col1:
        min_ansatte = st.number_input("Min ansatte", min_value=0, value=0, step=1)
    with col2:
        max_ansatte = st.number_input("Max ansatte", min_value=0, value=999_999, step=10)

    # C) Segment (bransjeklynger via NACE)
    st.subheader("Segment (bransjeklynger)")
    use_kontor = st.checkbox("Kontor (IT/rådgivning/regnskap/salg)", value=False)
    use_helse = st.checkbox("Helse & omsorg (skole/helse/barnehage)", value=False)
    use_fysisk = st.checkbox("Fysiske virksomheter (bygg/industri/transport)", value=False)
    use_topp = st.checkbox("Topprestasjon (advokat/konsulent/finans)", value=False)

    # D) Sektor
    st.subheader("Sektor")
    sektor_priv = st.checkbox("Privat", value=True)
    sektor_off = st.checkbox("Offentlig", value=True)

    # E) Nettsidekrav
    only_with_site = st.checkbox("Kun selskaper med nettside", value=True)

    st.divider()
    st.subheader("Antall og oppførsel")
    ønsket_antall = st.number_input("Hvor mange selskaper vil du hente?", min_value=1, value=500, step=50)
    shuffle_every_run = st.checkbox("Nye (tilfeldige) selskaper ved hver kjøring", value=True)

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

def segment_hits(codes:list[str]) -> dict:
    return {
        "Kontor": nace_matches(KONTOR_NACE_PREFIXES, codes),
        "Helse/omsorg": nace_matches(HELSE_NACE_PREFIXES, codes),
        "Fysisk": nace_matches(FYSISK_NACE_PREFIXES, codes),
        "Topprestasjon": nace_matches(TOPP_NACE_PREFIXES, codes),
    }

def classify_segment(codes:list[str]) -> str:
    hits = [name for name, ok in segment_hits(codes).items() if ok]
    if not hits:
        return "Annet"
    return ", ".join(hits)  # viser alle segmenter som gjelder

def infer_sector(enhet:dict) -> str:
    # Bruk institusjonell sektorkode hvis mulig, ellers orgform-heuristikk
    sekt = (enhet.get("institusjonellSektorkode") or {}).get("kode")
    if sekt and str(sekt).startswith("6"):
        return "Offentlig"
    orgform_kode = ((enhet.get("organisasjonsform") or {}).get("kode") or "").upper()
    if orgform_kode in PUBLIC_ORGFORM:
        return "Offentlig"
    return "Privat"

def has_website(url:str|None) -> bool:
    return bool(url and url.strip() and len(url.strip()) > 3)

def normalize_enhet_rows(data: dict) -> list[dict]:
    rows = []
    for e in data.get("_embedded", {}).get("enheter", []):
        addr = e.get("forretningsadresse") or {}
        orgf = e.get("organisasjonsform") or {}
        nk1  = (e.get("naeringskode1") or {}).get("kode")
        nk2  = (e.get("naeringskode2") or {}).get("kode")
        nk3  = (e.get("naeringskode3") or {}).get("kode")
        nace_codes = [c for c in [nk1, nk2, nk3] if c]
        seg_label = classify_segment(nace_codes)
        rows.append({
            "orgnr": e.get("organisasjonsnummer"),
            "navn": e.get("navn"),
            "hjemmeside": e.get("hjemmeside"),
            "kommune": addr.get("kommune"),
            "kommunenr": addr.get("kommunenummer"),
            "ansatte": e.get("antallAnsatte"),
            "orgform": orgf.get("kode"),
            "nace_codes": nace_codes,
            "segment_label": seg_label,
            "sektor": infer_sector(e),
        })
    return rows

def pass_segment_filter(row:dict, use_kontor:bool, use_helse:bool, use_fysisk:bool, use_topp:bool) -> bool:
    # Ingen segment-bokser huket => ikke filtrer
    if not (use_kontor or use_helse or use_fysisk or use_topp):
        return True
    # La raden slippe gjennom hvis den treffer minst ett valgt segment
    codes = row.get("nace_codes") or []
    hits = segment_hits(codes)
    return (
        (use_kontor and hits["Kontor"])
        or (use_helse and hits["Helse/omsorg"])
        or (use_fysisk and hits["Fysisk"])
        or (use_topp and hits["Topprestasjon"])
    )

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
                      segment_flags:tuple[bool,bool,bool,bool]=(False,False,False,False),
                      sector_flags:tuple[bool,bool]=(True,True),
                      only_with_site:bool=True,
                      page_size:int=PAGE_SIZE) -> tuple[pd.DataFrame, int]:
    """Hent side for side og filtrer lokalt til vi har 'limit' rader."""
    want_kontor, want_helse, want_fysisk, want_topp = segment_flags
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
            if not pass_segment_filter(r, want_kontor, want_helse, want_fysisk, want_topp):
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

# --- Kjør søk + visning -------------------------------------------------------
colA, colB = st.columns([1, 4])
with colA:
    run = st.button("Hent selskaper", type="primary")
with colB:
    st.caption("Fast API-side-størrelse: 200. Tips: begrens med filtre for raskere svar.")

if run:
    with st.spinner("Henter fra Enhetsregisteret..."):
        base_df, total = fetch_until_limit(
            limit=ønsket_antall,
            kommunenummer=kommunenummer or None,
            min_ansatte=min_ansatte or None,
            max_ansatte=max_ansatte or None,
            sort=None,
            segment_flags=(use_kontor, use_helse, use_fysisk, use_topp),
            sector_flags=(sektor_priv, sektor_off),
            only_with_site=only_with_site,
            page_size=PAGE_SIZE,
        )

    if shuffle_every_run and not base_df.empty:
        base_df = base_df.sample(frac=1.0, random_state=None).reset_index(drop=True)

    # Sett opp visning/kolonner
    out_df = base_df[["navn", "hjemmeside", "kommune", "ansatte", "segment_label", "sektor"]].rename(columns={
        "navn": "Selskapsnavn",
        "hjemmeside": "Nettside",
        "kommune": "Kommune",
        "ansatte": "Antall ansatte",
        "segment_label": "Segment",
        "sektor": "Sektor",
    })

    # Statuslinje
    tot_pages_guess = math.ceil(total / PAGE_SIZE)
    active_segments = ", ".join(
        [name for name, flag in [
            ("Kontor", use_kontor),
            ("Helse", use_helse),
            ("Fysisk", use_fysisk),
            ("Topprestasjon", use_topp),
        ] if flag]
    ) or "Ingen"
    st.markdown(
        f"**Totalt treff hos Brreg:** {total:,}  •  **Returnert (etter filtre):** {len(out_df):,}  "
        f"•  **Est. sider:** {tot_pages_guess}  "
        f"•  **Kun med nettside:** {'Ja' if only_with_site else 'Nei'}  "
        f"•  **Segmentfilter:** {active_segments}  "
        f"•  **Sektor:** "
        f"{'Privat' if sektor_priv else ''}{'/' if sektor_priv and sektor_off else ''}{'Offentlig' if sektor_off else ''}"
    )

    st.dataframe(out_df, width="stretch", hide_index=True)

    # Nedlasting: CSV / Excel
    csv_bytes = out_df.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Last ned som CSV", data=csv_bytes, file_name="potential_livities.csv", mime="text/csv")

    excel_buf = io.BytesIO()
    with pd.ExcelWriter(excel_buf, engine="xlsxwriter") as writer:
        out_df.to_excel(writer, index=False, sheet_name="Enheter")
    st.download_button("⬇️ Last ned som Excel (.xlsx)",
                       data=excel_buf.getvalue(),
                       file_name="potential_livities.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

st.caption("Kilde: Enhetsregisteret (åpne data, Brønnøysundregistrene).")
