import sqlite3
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
from status_def import STATUS_LABELS, STATUS_EMOJIS, STATUS_FARBEN

# ---------------------------------------------------------------------------
DB = Path(__file__).parent / "jobscanner.db"

st.set_page_config(page_title="Job-Scanner Dashboard", page_icon="🔍", layout="wide")

# Anzeige-Status 4–9 + 11 aus der zentralen Definition (Label + Farbe)
SCANNER_STATUS = {
    sv: (f"{STATUS_EMOJIS[sv]} {STATUS_LABELS[sv]}", STATUS_FARBEN[sv])
    for sv in (4, 5, 6, 7, 8, 9, 11)
}

# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def lade_stellen():
    if not DB.exists():
        return pd.DataFrame()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT s.url, s.firma, s.titel, s.gefunden_am, s.geloescht_am,
               s.status, b.score, b.empfehlung
        FROM stellen s
        LEFT JOIN bewertungen b ON b.url = s.url
        WHERE s.status NOT IN (0, 9)
        ORDER BY b.score DESC NULLS LAST
    """).fetchall()
    con.close()
    return pd.DataFrame([dict(r) for r in rows])


@st.cache_data(ttl=60)
def lade_status_counts():
    """Anzahl Stellen pro Scanner-Status."""
    if not DB.exists():
        return {}
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT status, COUNT(*) AS n FROM stellen GROUP BY status").fetchall()
    con.close()
    return {r[0]: r[1] for r in rows}


@st.cache_data(ttl=60)
def lade_bewerbungen():
    if not DB.exists():
        return pd.DataFrame()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT s.url, s.firma, s.titel, s.status AS scanner_status,
               bs.beworben_am, bs.kommentar,
               b.score
        FROM stellen s
        LEFT JOIN bewerbungsstatus bs ON bs.url = s.url
        LEFT JOIN bewertungen b ON b.url = s.url
        WHERE s.status IN (6, 7, 8)
        ORDER BY bs.beworben_am DESC NULLS LAST
    """).fetchall()
    con.close()
    return pd.DataFrame([dict(r) for r in rows])


@st.cache_data(ttl=60)
def lade_firmen():
    if not DB.exists():
        return pd.DataFrame()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT s.firma,
               COUNT(*)                                        AS anzahl,
               ROUND(AVG(b.score), 1)                         AS avg_score,
               MAX(b.score)                                    AS max_score,
               SUM(CASE WHEN b.score >= 70 THEN 1 ELSE 0 END) AS relevante
        FROM stellen s
        JOIN bewertungen b ON b.url = s.url
        WHERE s.status NOT IN (0, 9)
        GROUP BY s.firma
        HAVING COUNT(*) >= 1
        ORDER BY relevante DESC, avg_score DESC
    """).fetchall()
    con.close()
    return pd.DataFrame([dict(r) for r in rows])


# ---------------------------------------------------------------------------

st.title("🔍 Job-Scanner Dashboard")

if not DB.exists():
    st.error("Datenbank nicht gefunden.")
    st.stop()

df_s      = lade_stellen()
df_b      = lade_bewerbungen()
df_f      = lade_firmen()
counts    = lade_status_counts()

if df_s.empty:
    st.warning("Keine Daten vorhanden.")
    st.stop()

df_bewertet = df_s[df_s["score"].notna()].copy()

# ---------------------------------------------------------------------------
# KPI
# ---------------------------------------------------------------------------
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Stellen gesamt",    sum(counts.values()))
c2.metric("Bewertet",          len(df_bewertet))
c3.metric("Score ≥ 70 %",      int((df_bewertet["score"] >= 70).sum()))
c4.metric("Beworben (aktiv)",  counts.get(6, 0))
c5.metric("Ghosting",          counts.get(7, 0))
c6.metric("Absagen",           counts.get(8, 0))

st.markdown("---")

# ---------------------------------------------------------------------------
# Status-Karten (Scanner-Status 4–9)
# ---------------------------------------------------------------------------
st.subheader("📬 Scanner-Status")
cols = st.columns(len(SCANNER_STATUS))
for i, (sv, (label, farbe)) in enumerate(SCANNER_STATUS.items()):
    n = counts.get(sv, 0)
    cols[i].markdown(
        f'<div style="padding:16px;border-radius:8px;background:{farbe}22;'
        f'border-top:4px solid {farbe};text-align:center;">'
        f'<div style="font-size:2.2em;font-weight:bold;color:{farbe};">{n}</div>'
        f'<div style="font-size:0.85em;color:#444;margin-top:4px;">{label}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

st.markdown("---")

# ---------------------------------------------------------------------------
# Score-Verteilung + Empfehlung
# ---------------------------------------------------------------------------
col_l, col_r = st.columns([2, 1])

with col_l:
    st.subheader("📊 Score-Verteilung")
    fig = px.histogram(
        df_bewertet, x="score", nbins=20,
        color_discrete_sequence=["#3498db"],
        labels={"score": "Score (%)", "count": "Anzahl"},
    )
    fig.add_vline(x=70, line_dash="dash", line_color="#e74c3c",
                  annotation_text="70 % Grenze", annotation_position="top right")
    fig.update_layout(plot_bgcolor="white", yaxis_title="Anzahl",
                      showlegend=False, margin=dict(t=20, b=20))
    st.plotly_chart(fig, use_container_width=True)

with col_r:
    st.subheader("🎯 Empfehlung")
    empf = df_bewertet["empfehlung"].value_counts()
    if not empf.empty:
        farben = {"bewerben": "#27ae60", "nicht bewerben": "#e74c3c", "abwarten": "#f39c12"}
        fig2 = go.Figure(go.Pie(
            labels=empf.index, values=empf.values,
            marker_colors=[farben.get(e, "#95a5a6") for e in empf.index],
            hole=0.4, textinfo="label+value",
        ))
        fig2.update_layout(showlegend=False, margin=dict(t=20, b=20))
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("Keine Bewertungen vorhanden.")

st.markdown("---")

# ---------------------------------------------------------------------------
# Bewerbungsliste + Top-Stellen
# ---------------------------------------------------------------------------
col_l2, col_r2 = st.columns([1, 2])

with col_l2:
    st.subheader("📋 Bewerbungen (Status 6–8)")
    if df_b.empty:
        st.info("Noch keine Bewerbungen.")
    else:
        heute = pd.Timestamp.now()
        for _, row in df_b.iterrows():
            sc_status = int(row.get("scanner_status") or 0)
            label, farbe = SCANNER_STATUS.get(sc_status, ("–", "#aaa"))
            try:
                bam = row["beworben_am"]
                if bam and str(bam) not in ("", "None", "NaT", "nan"):
                    tage = (heute - pd.Timestamp(bam)).days
                    tage_txt = f"{tage}d"
                else:
                    tage_txt = "–"
            except Exception:
                tage_txt = "–"
            st.markdown(
                f'<div style="padding:8px 12px;margin:4px 0;background:{farbe}22;'
                f'border-left:4px solid {farbe};border-radius:4px;">'
                f'<strong>{row["firma"]}</strong><br>'
                f'<span style="font-size:0.82em;color:#555;">{str(row["titel"])[:50]}</span><br>'
                f'<span style="font-size:0.8em;">{label}'
                f'<span style="color:#999;"> · {tage_txt}</span></span>'
                f'</div>',
                unsafe_allow_html=True,
            )

with col_r2:
    st.subheader("🏆 Top Stellen (Score ≥ 70 %)")
    df_top = df_bewertet[df_bewertet["score"] >= 70].head(20)
    if df_top.empty:
        st.info("Keine Stellen mit Score ≥ 70 %.")
    else:
        for _, row in df_top.iterrows():
            farbe = "#27ae60" if row["score"] >= 80 else "#f39c12"
            sc_status = int(row.get("status") or 0)
            st_label, st_farbe = SCANNER_STATUS.get(sc_status, ("", ""))
            badge = (
                f' &nbsp;<span style="font-size:0.75em;background:{st_farbe}22;'
                f'color:{st_farbe};padding:2px 6px;border-radius:10px;">{st_label}</span>'
            ) if st_label else ""
            st.markdown(
                f'<div style="padding:6px 12px;margin:3px 0;background:#f8f9fa;'
                f'border-radius:4px;border-left:4px solid {farbe};">'
                f'<strong style="color:{farbe};">{int(row["score"])}%</strong> &nbsp;'
                f'<a href="{row["url"]}" target="_blank" style="color:#2c3e50;">'
                f'{str(row["titel"])[:55]}</a>'
                f'<span style="color:#888;font-size:0.85em;"> — {row["firma"]}</span>'
                f'{badge}</div>',
                unsafe_allow_html=True,
            )

st.markdown("---")

# ---------------------------------------------------------------------------
# Firmen-Bubble
# ---------------------------------------------------------------------------
st.subheader("🏢 Firmen-Übersicht")
st.caption("Bubble-Größe = Anzahl Stellen · Farbe: grün = viele gute Passungen")

if not df_f.empty:
    df_f["anteil"] = df_f["relevante"] / df_f["anzahl"].clip(lower=1)
    hover = [
        f"<b>{r['firma']}</b><br>Stellen: {r['anzahl']}<br>"
        f"≥70%: {int(r['relevante'])}<br>Ø Score: {r['avg_score']}%"
        for _, r in df_f.iterrows()
    ]
    fig3 = go.Figure(go.Scatter(
        x=df_f["firma"], y=df_f["avg_score"],
        mode="markers",
        marker=dict(
            size=df_f["anzahl"] * 14, sizemode="area",
            color=df_f["anteil"], colorscale="RdYlGn",
            cmin=0, cmax=1, showscale=True,
            colorbar=dict(title="Anteil ≥70%", tickformat=".0%"),
        ),
        text=hover, hovertemplate="%{text}<extra></extra>",
    ))
    fig3.update_layout(
        xaxis=dict(tickangle=-35, title=""),
        yaxis=dict(title="Ø Score (%)"),
        plot_bgcolor="white", height=500,
        margin=dict(t=20, b=140),
    )
    st.plotly_chart(fig3, use_container_width=True)

# ---------------------------------------------------------------------------
if st.button("🔄 Daten neu laden"):
    st.cache_data.clear()
    st.rerun()
