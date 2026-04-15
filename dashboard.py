"""
dashboard.py  –  Job-Scanner Dashboard
=======================================
Streamlit-Dashboard mit:
  - Score-Verteilung
  - Bewerbungsstatus (Karten pro Stufe)
  - Firmen-Bubble-Diagramm
  - Top-Stellen

Starten:
  streamlit run dashboard.py
"""

import sqlite3
from pathlib import Path
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
import pandas as pd

# =============================================================================
# KONFIGURATION
# =============================================================================

BASIS_PFAD = Path(__file__).parent
DB_PFAD    = BASIS_PFAD / "jobscanner.db"

st.set_page_config(
    page_title="Job-Scanner Dashboard",
    page_icon="🔍",
    layout="wide",
)

# =============================================================================
# DATEN LADEN
# =============================================================================

@st.cache_data(ttl=30)
def lade_daten():
    if not DB_PFAD.exists():
        return None, None

    con = sqlite3.connect(DB_PFAD)
    con.row_factory = sqlite3.Row

    stellen = con.execute("""
        SELECT
            s.url, s.firma, s.titel, s.gefunden_am, s.geloescht_am,
            b.score, b.empfehlung,
            bs.stufe, bs.beworben_am, bs.kennenlernen_am,
            bs.einladung_am, bs.ergebnis_am
        FROM stellen s
        LEFT JOIN bewertungen b       ON s.url = b.url
        LEFT JOIN bewerbungsstatus bs ON s.url = bs.url
        WHERE s.status != 0
        ORDER BY b.score DESC
    """).fetchall()

    firmen = con.execute("""
        SELECT
            s.firma,
            COUNT(*)                                          AS anzahl,
            AVG(b.score)                                      AS avg_score,
            MAX(b.score)                                      AS max_score,
            SUM(CASE WHEN b.score >= 70 THEN 1 ELSE 0 END)   AS relevante
        FROM stellen s
        JOIN bewertungen b ON s.url = b.url
        WHERE s.status != 0
        GROUP BY s.firma
        ORDER BY relevante DESC, avg_score DESC
    """).fetchall()

    con.close()

    df_stellen = pd.DataFrame([dict(r) for r in stellen])
    df_firmen  = pd.DataFrame([dict(r) for r in firmen])
    return df_stellen, df_firmen


# =============================================================================
# LAYOUT
# =============================================================================

st.title("🔍 Job-Scanner Dashboard")

df_stellen, df_firmen = lade_daten()

if df_stellen is None:
    st.error("Datenbank nicht gefunden. Bitte zuerst einen Scan starten.")
    st.stop()

if df_stellen.empty:
    st.warning("Noch keine Daten vorhanden.")
    st.stop()

df_bewertet = df_stellen[df_stellen["score"].notna()].copy()

stufen_label = {
    "beworben":     "✅ Beworben",
    "kennenlernen": "📞 Kennenlerngespräch",
    "einladung":    "📅 Gesprächseinladung",
    "zusage":       "🎉 Zusage",
    "absage":       "❌ Absage",
}
stufen_farbe = {
    "beworben":     "#f1c40f",
    "kennenlernen": "#2980b9",
    "einladung":    "#8e44ad",
    "zusage":       "#27ae60",
    "absage":       "#e74c3c",
}

# =============================================================================
# KPI-ZEILE
# =============================================================================

c1, c2, c3, c4 = st.columns(4)
c1.metric("Stellen gesamt",  len(df_stellen))
c2.metric("Bewertet",        len(df_bewertet))
c3.metric("Score ≥ 70%",     len(df_bewertet[df_bewertet["score"] >= 70]))
c4.metric("Bewerbungen",     len(df_stellen[df_stellen["stufe"].isin(stufen_label.keys())]))

st.divider()

# =============================================================================
# BEWERBUNGSSTATUS — eine Karte pro Stufe
# =============================================================================

st.subheader("📬 Bewerbungen nach Status")

cols = st.columns(5)
for i, (stufe, label) in enumerate(stufen_label.items()):
    anzahl = len(df_stellen[df_stellen["stufe"] == stufe])
    farbe  = stufen_farbe[stufe]
    cols[i].markdown(
        f'<div style="padding:16px; border-radius:8px; background:{farbe}22; '
        f'border-top:4px solid {farbe}; text-align:center;">'
        f'<div style="font-size:2em; font-weight:bold; color:{farbe};">{anzahl}</div>'
        f'<div style="font-size:0.85em; color:#444; margin-top:4px;">{label}</div>'
        f'</div>',
        unsafe_allow_html=True
    )

st.divider()

# =============================================================================
# ZEILE 1: Score-Verteilung + Empfehlung
# =============================================================================

col_l, col_r = st.columns([2, 1])

with col_l:
    st.subheader("📊 Score-Verteilung")
    fig = px.histogram(
        df_bewertet, x="score", nbins=20,
        color_discrete_sequence=["#3498db"],
        labels={"score": "Score (%)", "count": "Anzahl"},
    )
    fig.add_vline(x=70, line_dash="dash", line_color="#e74c3c",
                  annotation_text="Bewerben-Grenze (70%)",
                  annotation_position="top right")
    fig.update_layout(plot_bgcolor="white", yaxis_title="Anzahl",
                      showlegend=False, margin=dict(t=20, b=20))
    st.plotly_chart(fig, use_container_width=True)

with col_r:
    st.subheader("🎯 Empfehlung")
    empf = df_bewertet["empfehlung"].value_counts()
    farben_empf = {"bewerben": "#27ae60", "nicht bewerben": "#e74c3c", "abwarten": "#f39c12"}
    fig2 = go.Figure(go.Pie(
        labels=empf.index, values=empf.values,
        marker_colors=[farben_empf.get(e, "#95a5a6") for e in empf.index],
        hole=0.4, textinfo="label+value",
    ))
    fig2.update_layout(showlegend=False, margin=dict(t=20, b=20))
    st.plotly_chart(fig2, use_container_width=True)

st.divider()

# =============================================================================
# ZEILE 2: Bewerbungsstatus-Liste + Top-Stellen
# =============================================================================

col_l2, col_r2 = st.columns([1, 2])

with col_l2:
    st.subheader("📋 Bewerbungsstatus")
    df_status = df_stellen[df_stellen["stufe"].notna() & (df_stellen["stufe"] != "")]
    if df_status.empty:
        st.info("Noch keine Bewerbungen erfasst.")
    else:
        heute = pd.Timestamp.now()
        for _, row in df_status.sort_values("beworben_am", na_position="last").iterrows():
            label = stufen_label.get(row["stufe"], row["stufe"])
            farbe = stufen_farbe.get(row["stufe"], "#888")
            if row.get("beworben_am"):
                try:
                    tage = (heute - pd.Timestamp(row["beworben_am"])).days
                    tage_txt = f"{tage} Tag(e)"
                except Exception:
                    tage_txt = "–"
            else:
                tage_txt = "–"
            st.markdown(
                f'<div style="padding:8px 12px; margin:4px 0; background:{farbe}22; '
                f'border-left:4px solid {farbe}; border-radius:4px;">'
                f'<strong>{row["firma"]}</strong><br>'
                f'<span style="font-size:0.85em; color:#555;">{row["titel"][:50]}</span><br>'
                f'<span style="font-size:0.8em;">{label} &nbsp;|&nbsp; '
                f'<span style="color:#888;">offen seit {tage_txt}</span></span>'
                f'</div>',
                unsafe_allow_html=True
            )

with col_r2:
    st.subheader("🏆 Top Stellen (Score ≥ 70%)")
    df_top = df_bewertet[df_bewertet["score"] >= 70].head(15)
    if df_top.empty:
        st.info("Noch keine Stellen mit Score ≥ 70%.")
    else:
        for _, row in df_top.iterrows():
            farbe = "#27ae60" if row["score"] >= 80 else "#f39c12"
            badge = ""
            if row.get("stufe") and row["stufe"]:
                badge = (
                    f' &nbsp;<span style="font-size:0.8em; background:#eee; '
                    f'padding:2px 6px; border-radius:10px;">'
                    f'{stufen_label.get(row["stufe"], row["stufe"])}</span>'
                )
            st.markdown(
                f'<div style="padding:6px 12px; margin:3px 0; background:#f8f9fa; '
                f'border-radius:4px; border-left:4px solid {farbe};">'
                f'<strong style="color:{farbe};">{int(row["score"])}%</strong> &nbsp;'
                f'<a href="{row["url"]}" target="_blank" style="color:#2c3e50;">'
                f'{row["titel"][:55]}</a>'
                f'<span style="color:#888; font-size:0.85em;"> — {row["firma"]}</span>'
                f'{badge}</div>',
                unsafe_allow_html=True
            )

st.divider()

# =============================================================================
# FIRMEN-BUBBLE-DIAGRAMM
# Größe  = Anzahl Stellen insgesamt
# Farbe  = Anteil Stellen mit Score ≥ 70% (rot → grün)
# Y-Achse = Ø Score
# =============================================================================

st.subheader("🏢 Firmen-Übersicht — Wo lohnt sich eine Initiativbewerbung?")
st.caption("Größe der Bubble = Anzahl Stellen · Farbe: rot = wenig Passungen, grün = viele Passungen")

df_bubble = df_firmen.copy()
df_bubble["avg_score"] = df_bubble["avg_score"].round(1)
df_bubble["relevante"] = df_bubble["relevante"].fillna(0).astype(int)

# Anteil guter Passungen (0..1) als Farbwert
df_bubble["anteil"] = df_bubble["relevante"] / df_bubble["anzahl"].clip(lower=1)

if df_bubble.empty:
    st.info("Noch keine Firmendaten.")
else:
    hover_texte = [
        f"<b>{r['firma']}</b><br>"
        f"Stellen gesamt: {r['anzahl']}<br>"
        f"Davon ≥ 70%: {r['relevante']}<br>"
        f"Ø Score: {r['avg_score']}%<br>"
        f"Max Score: {int(r['max_score'])}%"
        for _, r in df_bubble.iterrows()
    ]

    fig3 = go.Figure(go.Scatter(
        x=df_bubble["firma"],
        y=df_bubble["avg_score"],
        mode="markers",
        marker=dict(
            size=df_bubble["anzahl"] * 14,   # Größe proportional zur Stellenanzahl
            sizemode="area",
            color=df_bubble["anteil"],
            colorscale="RdYlGn",
            cmin=0,
            cmax=1,
            showscale=True,
            colorbar=dict(
                title="Anteil ≥70%",
                tickformat=".0%",
            ),
        ),
        text=hover_texte,
        hovertemplate="%{text}<extra></extra>",
    ))

    fig3.update_layout(
        xaxis=dict(tickangle=-35, title=""),
        yaxis=dict(title="Ø Score (%)"),
        plot_bgcolor="white",
        height=520,
        margin=dict(t=20, b=140),
    )
    st.plotly_chart(fig3, use_container_width=True)

# =============================================================================
# KUMULATIVER BEWERBUNGSVERLAUF (letzter Monat)
# =============================================================================

st.divider()
st.subheader("📈 Bewerbungsverlauf — kumuliert (letzter Monat)")
st.caption("Aufaddierte Bewerbungen mit gesetztem Status, farblich nach Ergebnis")

df_verlauf = df_stellen[
    df_stellen["stufe"].notna() & (df_stellen["stufe"] != "") &
    df_stellen["beworben_am"].notna()
].copy()

if df_verlauf.empty:
    st.info("Noch keine Bewerbungen mit Datum erfasst.")
else:
    df_verlauf["beworben_am"] = pd.to_datetime(df_verlauf["beworben_am"], errors="coerce")
    df_verlauf = df_verlauf.dropna(subset=["beworben_am"])

    # Auf letzten Monat einschränken
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=30)
    df_verlauf = df_verlauf[df_verlauf["beworben_am"] >= cutoff]

    if df_verlauf.empty:
        st.info("Keine Bewerbungen in den letzten 30 Tagen.")
    else:
        df_verlauf["datum"] = df_verlauf["beworben_am"].dt.date

        # Datumsreihe für den gesamten Zeitraum aufbauen
        alle_tage = pd.date_range(
            start=df_verlauf["datum"].min(),
            end=pd.Timestamp.now().date(),
            freq="D"
        )

        def kumulativ(df_teil):
            """Zählt pro Tag und gibt kumulative Summe zurück."""
            pro_tag = df_teil.groupby("datum").size().reindex(alle_tage.date, fill_value=0)
            return pro_tag.cumsum()

        # Alle mit Status
        gesamt_kum   = kumulativ(df_verlauf)
        # Nur Absagen
        absagen_kum  = kumulativ(df_verlauf[df_verlauf["stufe"] == "absage"])
        # Nur positive (beworben / kennenlernen / einladung / zusage)
        positiv_kum  = kumulativ(df_verlauf[df_verlauf["stufe"].isin(
            ["beworben", "kennenlernen", "einladung", "zusage"]
        )])

        fig_verl = go.Figure()

        fig_verl.add_trace(go.Scatter(
            x=list(alle_tage.date), y=gesamt_kum.values,
            mode="lines+markers", name="Gesamt",
            line=dict(color="#3498db", width=2),
            marker=dict(size=5),
        ))
        fig_verl.add_trace(go.Scatter(
            x=list(alle_tage.date), y=positiv_kum.values,
            mode="lines+markers", name="Aktiv / Positiv",
            line=dict(color="#27ae60", width=2),
            marker=dict(size=5),
        ))
        fig_verl.add_trace(go.Scatter(
            x=list(alle_tage.date), y=absagen_kum.values,
            mode="lines+markers", name="Absagen",
            line=dict(color="#e74c3c", width=2, dash="dot"),
            marker=dict(size=5),
        ))

        fig_verl.update_layout(
            plot_bgcolor="white",
            xaxis=dict(title="Datum", tickformat="%d.%m."),
            yaxis=dict(title="Anzahl (kumuliert)", dtick=1),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
            height=350,
            margin=dict(t=40, b=20),
            hovermode="x unified",
        )
        st.plotly_chart(fig_verl, use_container_width=True)

# =============================================================================
# AKTIVE BEWERBUNGEN DETAIL
# =============================================================================

df_aktiv = df_stellen[
    df_stellen["stufe"].isin(["beworben", "kennenlernen", "einladung"])
].copy()

if not df_aktiv.empty:
    st.divider()
    st.subheader("📌 Aktive Bewerbungen im Detail")
    for _, row in df_aktiv.iterrows():
        farbe    = stufen_farbe.get(row["stufe"], "#888")
        label    = stufen_label.get(row["stufe"], row["stufe"])
        score_txt = f"{int(row['score'])}%" if pd.notna(row["score"]) else "–"
        datum    = row.get("beworben_am") or row.get("gefunden_am") or "–"
        st.markdown(
            f'<div style="padding:10px 15px; margin:5px 0; background:white; '
            f'border-radius:6px; border-left:5px solid {farbe}; '
            f'box-shadow:0 1px 3px rgba(0,0,0,0.1);">'
            f'<strong><a href="{row["url"]}" target="_blank" style="color:#2c3e50;">'
            f'{row["titel"]}</a></strong>'
            f' &nbsp;<span style="color:#888;">— {row["firma"]}</span><br>'
            f'<span style="font-size:0.85em; color:{farbe};">{label}</span>'
            f' &nbsp;|&nbsp; Score: <strong>{score_txt}</strong>'
            f' &nbsp;|&nbsp; <span style="color:#888;">Beworben: {str(datum)[:10]}</span>'
            f'</div>',
            unsafe_allow_html=True
        )

st.divider()
if st.button("🔄 Daten neu laden"):
    st.cache_data.clear()
    st.rerun()