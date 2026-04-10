"""
dashboard.py  –  Job-Scanner Dashboard
=======================================
Streamlit-Dashboard mit:
  - Score-Verteilung
  - Bewerbungsstatus
  - Firmen-Ranking

Starten:
  streamlit run dashboard.py
"""

import json
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
            COUNT(*)            AS anzahl,
            AVG(b.score)        AS avg_score,
            MAX(b.score)        AS max_score,
            SUM(CASE WHEN b.score >= 70 THEN 1 ELSE 0 END) AS relevante
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
    "einladung":    "#2980b9",
    "zusage":       "#27ae60",
    "absage":       "#e74c3c",
}

# =============================================================================
# KPI-ZEILE
# =============================================================================

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Stellen gesamt",   len(df_stellen))
c2.metric("Bewertet",         len(df_bewertet))
c3.metric("Score ≥ 70%",      len(df_bewertet[df_bewertet["score"] >= 70]))
c4.metric("Beworben",         len(df_stellen[df_stellen["stufe"].isin(stufen_label.keys())]))
c5.metric("Aktive Gespräche", len(df_stellen[df_stellen["stufe"].isin(["beworben","kennenlernen","einladung"])]))

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
# ZEILE 2: Status + Top-Stellen
# =============================================================================

col_l2, col_r2 = st.columns([1, 2])

with col_l2:
    st.subheader("📋 Bewerbungsstatus")
    df_status = df_stellen[df_stellen["stufe"].notna() & (df_stellen["stufe"] != "")]
    if df_status.empty:
        st.info("Noch keine Bewerbungen erfasst.")
    else:
        for stufe, anzahl in df_status["stufe"].value_counts().items():
            label = stufen_label.get(stufe, stufe)
            farbe = stufen_farbe.get(stufe, "#888")
            st.markdown(
                f'<div style="padding:8px 12px; margin:4px 0; background:{farbe}22; '
                f'border-left:4px solid {farbe}; border-radius:4px;">'
                f'<strong>{label}</strong>: {anzahl}</div>',
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
                badge = f' &nbsp;<span style="font-size:0.8em; background:#eee; padding:2px 6px; border-radius:10px;">{stufen_label.get(row["stufe"], row["stufe"])}</span>'
            st.markdown(
                f'<div style="padding:6px 12px; margin:3px 0; background:#f8f9fa; '
                f'border-radius:4px; border-left:4px solid {farbe};">'
                f'<strong style="color:{farbe};">{int(row["score"])}%</strong> &nbsp;'
                f'<a href="{row["url"]}" target="_blank" style="color:#2c3e50;">{row["titel"][:55]}</a>'
                f'<span style="color:#888; font-size:0.85em;"> — {row["firma"]}</span>{badge}</div>',
                unsafe_allow_html=True
            )

st.divider()

# =============================================================================
# FIRMEN-RANKING
# =============================================================================

st.subheader("🏢 Firmen-Ranking — Wo lohnt sich eine Initiativbewerbung?")

df_firmen_top = df_firmen[df_firmen["relevante"] > 0].head(20).copy()
df_firmen_top["avg_score"] = df_firmen_top["avg_score"].round(1)

if df_firmen_top.empty:
    st.info("Noch keine Firmendaten.")
else:
    fig3 = px.bar(
        df_firmen_top, x="avg_score", y="firma", orientation="h",
        color="relevante", color_continuous_scale=["#f39c12", "#27ae60"],
        labels={"avg_score": "Ø Score (%)", "firma": "Firma", "relevante": "Stellen ≥ 70%"},
        text="relevante",
    )
    fig3.update_traces(texttemplate="%{text} Stelle(n) ≥70%", textposition="outside")
    fig3.update_layout(
        plot_bgcolor="white", yaxis=dict(autorange="reversed"),
        margin=dict(t=20, b=20), coloraxis_showscale=False,
        height=max(300, len(df_firmen_top) * 35),
    )
    st.plotly_chart(fig3, use_container_width=True)

# =============================================================================
# AKTIVE BEWERBUNGEN DETAIL
# =============================================================================

df_aktiv = df_stellen[df_stellen["stufe"].isin(["beworben","kennenlernen","einladung"])].copy()

if not df_aktiv.empty:
    st.divider()
    st.subheader("📌 Aktive Bewerbungen im Detail")
    for _, row in df_aktiv.iterrows():
        farbe = stufen_farbe.get(row["stufe"], "#888")
        label = stufen_label.get(row["stufe"], row["stufe"])
        score_txt = f"{int(row['score'])}%" if pd.notna(row["score"]) else "–"
        datum = (row.get("beworben_am") or row.get("gefunden_am") or "–")
        st.markdown(
            f'<div style="padding:10px 15px; margin:5px 0; background:white; '
            f'border-radius:6px; border-left:5px solid {farbe}; box-shadow:0 1px 3px rgba(0,0,0,0.1);">'
            f'<strong><a href="{row["url"]}" target="_blank" style="color:#2c3e50;">{row["titel"]}</a></strong>'
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
