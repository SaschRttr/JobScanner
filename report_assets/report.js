
    // STATUS_LABELS, INAKTIVE_STATUS, UNBEWERTETE_STATUS, FILTER_STATUS werden
    // von report.py aus status_def.py injiziert (siehe <script> davor).
    const SERVER = window.location.origin;

    const _STUFE_ZU_STATUS = { beworben: 6, absage: 8 };

    // ohneZaehlen=true unterdrückt das (teure) Neuzählen aller Status-Badges - beim
    // Massen-Update in ladeStatus wird pro Karte aktualisiert und erst am Ende EINMAL
    // gezählt, statt N-mal über alle N Karten zu iterieren (sonst O(N²) → auf dem
    // iPhone mit >1000 Karten sekundenlanges Laden).
    function aktualisiereStatusBadge(el, neuerStatus, ohneZaehlen = false) {
        const badge = el.querySelector('.scanner-status');
        if (!badge) return;
        for (let i = 0; i <= 11; i++) badge.classList.remove('scanner-status-' + i);
        badge.classList.add('scanner-status-' + neuerStatus);
        badge.title = 'Status ' + neuerStatus;
        badge.textContent = STATUS_LABELS[neuerStatus] || String(neuerStatus);
        el.dataset.scannerStatus = String(neuerStatus);
        if (!ohneZaehlen) aktualisiereStatusCounts();
    }

    // Zählt wie viele Stellen je Status im aktuell sichtbaren Ausschnitt liegen.
    // Ist ein Firma-/Vorgemerkt-/Merkliste-Filter aktiv, werden nur die davon
    // betroffenen Stellen gezählt - sonst zeigten die Kopfzeilen-Badges immer
    // die globale Zahl an, während z.B. ein Firma-Filter nur einen Bruchteil
    // davon sichtbar ließ.
    function aktualisiereStatusCounts() {
        const counts = {};
        const seenUrls = new Set();
        document.querySelectorAll('.stelle[data-scanner-status][data-url]').forEach(el => {
            if (_nurVorgemerkt) {
                if (el.dataset.vorgemerkt !== '1') return;
            } else {
                // data-ausgeschlossen ("nicht passend") wird vom Status-Filter immer
                // ausgeblendet, egal welcher Status gewählt ist - hier ebenfalls
                // ausschließen, sonst weicht die Kopfzeile vom Filter-Ergebnis ab.
                if (el.dataset.ausgeschlossen) return;
                if (_aktiverFirmaFilter !== null && el.dataset.firma !== _aktiverFirmaFilter) return;
            }
            if (_nurMerkliste && el.dataset.gemerkt !== '1') return;
            const url = el.dataset.url;
            if (url) {
                if (seenUrls.has(url)) return;
                seenUrls.add(url);
            }
            const s = el.dataset.scannerStatus;
            if (s !== '') counts[s] = (counts[s] || 0) + 1;
        });
        FILTER_STATUS.forEach(sv => {
            const el = document.getElementById('stat-status-' + sv);
            if (el) el.textContent = counts[sv] || 0;
        });
    }

    async function speichern(url, feld, wert) {
        const status = JSON.parse(localStorage.getItem('job_status') || '{}');
        if (!status[url]) status[url] = {};

        if (feld === 'stufe') {
            const jetzt = new Date().toLocaleString('de-DE', {
                day:'2-digit', month:'2-digit', year:'numeric',
                hour:'2-digit', minute:'2-digit'
            });
            // Timestamp nur beim ersten Mal setzen
            const tsKey = wert + '_am';
            if (wert && !status[url][tsKey]) {
                status[url][tsKey] = jetzt;
            }
            status[url]['stufe'] = wert;

            // CSS-Klassen aktualisieren
            const el = document.querySelector(`[data-url="${CSS.escape(url)}"]`);
            if (el) {
                ['beworben','kennenlernen','einladung','zusage','absage'].forEach(k => el.classList.remove(k));
                if (wert) {
                    el.classList.add(wert);
                    el.classList.remove('mit-aktivitaet');
                } else {
                    // Status zurückgesetzt → ggf. wieder ockergelb wenn Aktivität vorhanden
                    const hatAkt = el.dataset.hatLebenslauf === '1' || !!status[url]?.kommentar;
                    if (hatAkt) el.classList.add('mit-aktivitaet');
                }
                if (wert && _STUFE_ZU_STATUS[wert]) {
                    const _curSt2 = parseInt(el.dataset.scannerStatus);
                    if (isNaN(_curSt2) || !INAKTIVE_STATUS.includes(_curSt2)) {
                        aktualisiereStatusBadge(el, _STUFE_ZU_STATUS[wert]);
                    }
                }
            }
            // Timestamp anzeigen
            const tsEl = document.querySelector(`[data-url="${CSS.escape(url)}"] .stufen-ts`);
            if (tsEl) {
                tsEl.textContent = (wert && status[url][tsKey]) ? ('seit ' + status[url][tsKey]) : '';
            }

            // Beworben → Stelle verlässt die Merkliste
            if (wert === 'beworben') {
                let entfernt = false;
                document.querySelectorAll(`.stelle[data-url="${CSS.escape(url)}"]`).forEach(el2 => {
                    if (el2.dataset.gemerkt === '1') {
                        entfernt = true;
                        delete el2.dataset.gemerkt;
                        const b = el2.querySelector('.merken-toggle');
                        if (b) _setzeMerkenBtn(b, url, false);
                    }
                });
                if (entfernt) {
                    const zaehler = document.getElementById('stat-merkliste');
                    if (zaehler) zaehler.textContent = String(Math.max(0, (parseInt(zaehler.textContent) || 0) - 1));
                    if (_flatAktiv) _aktualisiereFlach();
                }
            }
        } else {
            status[url][feld] = wert;
        }

        localStorage.setItem('job_status', JSON.stringify(status));

        if (SERVER) {
            await fetch(SERVER + '/status', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url, feld, wert })
            });
        }
    }

    async function ladeStatus() {
        const localStatus = JSON.parse(localStorage.getItem('job_status') || '{}');
        let status = localStatus;

        if (SERVER) {
            try {
                const res = await fetch(SERVER + '/status');
                const dbStatus = await res.json();

                // Zusammenführen: DB gewinnt bei Konflikten, localStorage füllt Lücken
                status = { ...localStatus };
                for (const [url, dbInfo] of Object.entries(dbStatus)) {
                    status[url] = { ...(localStatus[url] || {}), ...dbInfo };
                }

                // Fehlende localStorage-Einträge in DB nachsynchronisieren
                const syncs = [];
                for (const [url, info] of Object.entries(localStatus)) {
                    const db = dbStatus[url] || {};
                    if (info.stufe && !db.stufe) {
                        syncs.push(fetch(SERVER + '/status', {
                            method: 'POST', headers: {'Content-Type':'application/json'},
                            body: JSON.stringify({url, feld:'stufe', wert:info.stufe})
                        }));
                    }
                    if (info.kommentar && !db.kommentar) {
                        syncs.push(fetch(SERVER + '/status', {
                            method: 'POST', headers: {'Content-Type':'application/json'},
                            body: JSON.stringify({url, feld:'kommentar', wert:info.kommentar})
                        }));
                    }
                }
                if (syncs.length > 0) {
                    await Promise.all(syncs.map(p => p.catch(() => {})));
                    console.log(syncs.length + ' Status-Einträge mit Datenbank synchronisiert');
                }
            } catch (e) {
                console.warn('Statusserver nicht erreichbar, nutze localStorage', e);
            }
        }
        document.querySelectorAll('.stelle[data-url]').forEach(el => {
            const s = status[el.dataset.url];
            if (!s) return;

            // Stufe wiederherstellen
            const stufe = s.stufe || '';
            if (stufe) {
                ['beworben','kennenlernen','einladung','zusage','absage'].forEach(k => el.classList.remove(k));
                el.classList.add(stufe);
                if (_STUFE_ZU_STATUS[stufe]) {
                    const _curSt = parseInt(el.dataset.scannerStatus);
                    if (isNaN(_curSt) || !INAKTIVE_STATUS.includes(_curSt)) {
                        aktualisiereStatusBadge(el, _STUFE_ZU_STATUS[stufe], true);
                    }
                }
            }
            // Dropdown setzen
            const sel = el.querySelector('.stufen-select');
            if (sel && stufe) sel.value = stufe;

            // Timestamp anzeigen
            const tsEl = el.querySelector('.stufen-ts');
            if (tsEl) {
                const tsKey = stufe + '_am';
                tsEl.textContent = (stufe && s[tsKey]) ? ('seit ' + s[tsKey]) : '';
            }

            // Kommentar wiederherstellen
            const ta = el.querySelector('.kommentar');
            if (s.kommentar && ta) ta.value = s.kommentar;

            // Nicht-beworben-Grund wiederherstellen
            const nbg = el.querySelector('.nicht-beworben-grund');
            if (s.nicht_beworben_grund && nbg) nbg.value = s.nicht_beworben_grund;

            // Scanner-Status aus DB anwenden (überschreibt eingebackenen HTML-Wert)
            if (s.scanner_status !== undefined) {
                aktualisiereStatusBadge(el, s.scanner_status, true);
            }

            // Ockergelb: Lebenslauf oder Notizen vorhanden, aber kein Status gesetzt
            const hatAktivitaet = el.dataset.hatLebenslauf === '1' || !!s.kommentar;
            if (hatAktivitaet && !stufe) {
                el.classList.add('mit-aktivitaet');
            } else {
                el.classList.remove('mit-aktivitaet');
            }
        });
        aktualisiereStatusCounts();
    }

    window.onload = function() { ladeStatus(); ladeFirmen(); };

    async function ladeFirmen() {
        try {
            const r = await fetch('/firmen');
            const namen = await r.json();
            const sel = document.getElementById('firma-dropdown');
            namen.forEach(n => {
                const opt = document.createElement('option');
                opt.value = opt.textContent = n;
                sel.appendChild(opt);
            });
        } catch(e) {}
    }

    function firmaTest() {
        const sel    = document.getElementById('firma-dropdown');
        const status = document.getElementById('firma-status');
        const output = document.getElementById('firma-output');
        const firma  = sel.value;
        if (!firma) { status.textContent = '⚠️ Bitte Firma wählen'; return; }

        sel.disabled = true;
        status.textContent = `⏳ Scanne ${firma}...`;
        output.style.display = 'block';
        output.textContent = '';

        const quelle = new EventSource('/firma-testen?firma=' + encodeURIComponent(firma));
        quelle.onmessage = function(e) {
            if (e.data === 'FERTIG') {
                quelle.close();
                sel.disabled = false;
                status.textContent = '✅ Fertig';
                return;
            }
            output.textContent += e.data + '\n';
            output.scrollTop = output.scrollHeight;
        };
        quelle.onerror = function() {
            quelle.close();
            sel.disabled = false;
            status.textContent = '❌ Verbindungsfehler';
        };
    }

    // Breite Suche (ganz Deutschland) über eine frei eingegebene Karriere-URL.
    function breitScannen() {
        const urlEl  = document.getElementById('breit-url');
        const nameEl = document.getElementById('breit-name');
        const status = document.getElementById('breit-status');
        const output = document.getElementById('breit-output');
        const url    = urlEl ? urlEl.value.trim() : '';
        const name   = nameEl ? nameEl.value.trim() : '';
        if (!url) { if (status) status.textContent = '⚠️ Bitte eine Karriere-URL eingeben'; return; }

        if (status) status.textContent = '⏳ Breite Suche (ganz Deutschland)... das kann etwas dauern.';
        if (output) { output.style.display = 'block'; output.textContent = ''; }

        let ziel = '/vorschau-scannen?url=' + encodeURIComponent(url);
        if (name) ziel += '&name=' + encodeURIComponent(name);
        const quelle = new EventSource(ziel);
        quelle.onmessage = function(e) {
            if (e.data === 'FERTIG') {
                quelle.close();
                // NICHT automatisch neu laden – sonst verschwindet die Ausgabe
                // sofort und man kann die gefundenen Stellen nicht prüfen.
                // Stattdessen Ausgabe stehen lassen und manuell laden lassen.
                if (status) {
                    status.innerHTML = '✅ Fertig. '
                        + '<button class="scan-btn" onclick="location.reload()">📋 Vorschau laden</button> '
                        + '<a href="' + SERVER + '/vorschau-log" target="_blank" '
                        + 'style="margin-left:6px;">📄 Vollständiges Log öffnen</a>';
                }
                if (output) output.scrollTop = output.scrollHeight;
                return;
            }
            if (output) { output.textContent += e.data + '\n'; output.scrollTop = output.scrollHeight; }
        };
        quelle.onerror = function() {
            quelle.close();
            if (status) status.textContent = '❌ Verbindungsfehler';
        };
    }

    // --- Breite Vorschau (ganz Deutschland) -------------------------------
    // Log-Fenster für den Bewertungs-Fortschritt einblenden und zurückgeben.
    function _vorschauLogAnzeigen(titel) {
        const status = document.getElementById('vorschau-status');
        if (!status) return null;
        status.innerHTML = '<div style="font-weight:bold; margin-bottom:4px;">' + titel + '</div>'
            + '<pre id="vorschau-log" style="max-height:220px; overflow-y:auto; background:#111; color:#0f0; padding:8px; border-radius:4px; font-size:0.8em; white-space:pre-wrap;"></pre>';
        return document.getElementById('vorschau-log');
    }

    // Eine Stelle provisorisch in die DB legen und die Pipeline (Rohtext→
    // Extraktion→KI) streamen. Promise, das bei FERTIG (oder Fehler) auflöst –
    // OHNE Reload, damit der Aufrufer mehrere Stellen nacheinander abarbeiten kann.
    function _vorschauStelleVerarbeiten(url, log) {
        return new Promise((resolve) => {
            fetch(SERVER + '/vorschau-bewerten', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({url})
            }).then(r => r.json()).then(data => {
                if (!data.ok) {
                    if (log) log.textContent += '\n❌ ' + url + ': ' + (data.fehler || 'Fehler') + '\n';
                    return resolve(false);
                }
                const quelle = new EventSource('/stelle-einzeln-stream?url=' + encodeURIComponent(url));
                quelle.onmessage = function(e) {
                    if (e.data === 'FERTIG') { quelle.close(); return resolve(true); }
                    if (log) { log.textContent += e.data + '\n'; log.scrollTop = log.scrollHeight; }
                };
                quelle.onerror = function() {
                    quelle.close();
                    if (log) log.textContent += '\n⚠️ Verbindung zur Pipeline unterbrochen.\n';
                    resolve(false);
                };
            }).catch(() => {
                if (log) log.textContent += '\n❌ Server nicht erreichbar\n';
                resolve(false);
            });
        });
    }

    // "Bewerten" (einzeln): Kandidat bewerten und danach Detailansicht laden.
    async function vorschauBewerten(btn) {
        const zeile = btn.closest('.vorschau-zeile');
        const url   = zeile ? zeile.getAttribute('data-url') : null;
        if (!url) return;
        btn.disabled = true;
        btn.textContent = '⏳...';
        const log = _vorschauLogAnzeigen('⏳ Bewerte Stelle (Rohtext → Extraktion → KI)...');
        if (zeile) zeile.remove();
        await _vorschauStelleVerarbeiten(url, log);
        if (log) log.textContent += '\n✅ Fertig – lade Detailansicht...\n';
        setTimeout(() => location.reload(), 900);
    }

    // "Ausgewählte bewerten" (Batch): alle angehakten Kandidaten nacheinander
    // durch die Pipeline schicken, dann EINMAL die Detailansicht laden.
    async function vorschauBewertenBatch() {
        const zeilen = [...document.querySelectorAll('.vorschau-cb:checked')]
            .map(cb => cb.closest('.vorschau-zeile'))
            .filter(Boolean);
        if (!zeilen.length) { alert('Bitte mindestens eine Stelle auswählen.'); return; }
        const urls = zeilen.map(z => z.getAttribute('data-url'));
        const btn = document.getElementById('vorschau-batch-btn');
        if (btn) btn.disabled = true;
        const log = _vorschauLogAnzeigen('⏳ Bewerte ' + urls.length + ' Stelle(n) nacheinander...');
        let ok = 0;
        for (let i = 0; i < urls.length; i++) {
            if (log) log.textContent += '\n=== ' + (i + 1) + '/' + urls.length + ': ' + urls[i] + ' ===\n';
            const erfolg = await _vorschauStelleVerarbeiten(urls[i], log);
            if (erfolg) { ok++; if (zeilen[i]) zeilen[i].remove(); }
        }
        if (log) log.textContent += '\n✅ ' + ok + '/' + urls.length + ' bewertet – lade Detailansicht...\n';
        setTimeout(() => location.reload(), 1200);
    }

    // Checkbox "Alle auswählen" umschalten + Zähler im Batch-Button aktualisieren.
    function vorschauAlleUmschalten(cb) {
        document.querySelectorAll('.vorschau-cb').forEach(x => { x.checked = cb.checked; });
        vorschauCountAktualisieren();
    }

    function vorschauCountAktualisieren() {
        const n = document.querySelectorAll('.vorschau-cb:checked').length;
        const btn = document.getElementById('vorschau-batch-btn');
        if (btn) btn.textContent = n ? ('🔍 ' + n + ' Ausgewählte bewerten') : '🔍 Ausgewählte bewerten';
    }

    // "Übernehmen": provisorische Stelle endgültig behalten (nur Markierung
    // entfernen, KEINE zweite Bewertung).
    async function vorschauUebernehmen(btn) {
        const karte = btn.closest('.vorschau-prov');
        const url   = karte ? karte.getAttribute('data-url') : null;
        if (!url) return;
        btn.disabled = true;
        btn.textContent = '⏳...';
        try {
            const res = await fetch(SERVER + '/vorschau-uebernehmen', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({url})
            });
            const data = await res.json();
            if (!data.ok) {
                alert('Fehler: ' + (data.fehler || 'Unbekannt'));
                btn.disabled = false; btn.textContent = '✅ Übernehmen';
                return;
            }
            location.reload();
        } catch(e) {
            alert('Server nicht erreichbar');
            btn.disabled = false; btn.textContent = '✅ Übernehmen';
        }
    }

    // "Verwerfen": Kandidat (nur aus Liste) ODER provisorische Stelle (aus DB löschen).
    async function vorschauVerwerfen(btn) {
        const el  = btn.closest('.vorschau-zeile') || btn.closest('.vorschau-prov');
        const url = el ? el.getAttribute('data-url') : null;
        if (!url) return;
        const istProv = el.classList.contains('vorschau-prov');
        if (istProv && !confirm('Provisorisch bewertete Stelle verwerfen? Sie wird aus der Datenbank gelöscht.')) return;
        try {
            const res = await fetch(SERVER + '/vorschau-verwerfen', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({url})
            });
            const data = await res.json();
            if (data.ok && el) el.remove();
            else if (!data.ok) alert('Fehler: ' + (data.fehler || 'Unbekannt'));
        } catch(e) { alert('Server nicht erreichbar'); }
    }

    async function vorschauLeeren() {
        if (!confirm('Komplette Vorschau leeren?')) return;
        try {
            const res = await fetch(SERVER + '/vorschau-leeren', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({})
            });
            const data = await res.json();
            if (data.ok) {
                const box = document.getElementById('vorschau-box');
                if (box) box.remove();
            }
        } catch(e) { alert('Server nicht erreichbar'); }
    }

    function scanStarten() {
        const btn     = document.getElementById('scan-start-btn');
        const stopBtn = document.getElementById('scan-stop-btn');
        const output  = document.getElementById('scan-output');
        const status  = document.getElementById('scan-status');

        btn.disabled = true;
        btn.textContent = '⏳ Scan läuft...';
        stopBtn.style.display = 'inline-block';
        output.style.display = 'block';
        output.textContent = '';
        status.textContent = '';

        const quelle = new EventSource('/starten');

        quelle.onmessage = function(e) {
            if (e.data === 'FERTIG') {
                quelle.close();
                stopBtn.style.display = 'none';
                btn.disabled = false;
                btn.textContent = '🔄 Scan jetzt starten';
                status.textContent = '✅ Fertig – Seite wird neu geladen...';
                setTimeout(() => location.reload(), 2000);
                return;
            }
            output.textContent += e.data + '\n';
            output.scrollTop = output.scrollHeight;
        };

        quelle.onerror = function() {
            quelle.close();
            stopBtn.style.display = 'none';
            btn.disabled = false;
            btn.textContent = '🔄 Scan jetzt starten';
            status.textContent = '❌ Fehler: Flask-Server nicht erreichbar. Läuft webui.py?';
            status.style.color = '#e74c3c';
        };
    }

    async function scanStoppen() {
        const stopBtn = document.getElementById('scan-stop-btn');
        const status  = document.getElementById('scan-status');
        stopBtn.disabled = true;
        stopBtn.textContent = '⏳ Wird abgebrochen...';
        try {
            const r = await fetch('/stoppen');
            const d = await r.json();
            status.textContent = d.nachricht || 'Abbruch angefordert';
        } catch(e) {
            status.textContent = '❌ Fehler beim Abbrechen';
        }
    }

    async function steckbriefGenerieren(btn, stellenUrl) {
        btn.disabled = true;
        btn.textContent = '⏳ Generiere...';
        try {
            const res = await fetch(SERVER + '/steckbrief-erstellen', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: stellenUrl })
            });
            const data = await res.json();
            if (data.ok) {
                location.reload();
            } else {
                btn.disabled = false;
                btn.textContent = '🧠 Steckbrief generieren';
                alert('Fehler: ' + (data.fehler || 'Unbekannt'));
            }
        } catch(e) {
            btn.disabled = false;
            btn.textContent = '🧠 Steckbrief generieren';
            alert('Server nicht erreichbar');
        }
    }

    async function bewertungStarten(btn, stellenUrl) {
        const originalLabel = btn.textContent;
        btn.disabled = true;
        btn.textContent = '⏳ Bewerte...';
        try {
            const res = await fetch(SERVER + '/bewertung-erstellen', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: stellenUrl })
            });
            const data = await res.json();
            if (data.ok) {
                location.reload();
            } else {
                btn.disabled = false;
                btn.textContent = originalLabel;
                alert('Fehler: ' + (data.fehler || 'Unbekannt'));
            }
        } catch(e) {
            btn.disabled = false;
            btn.textContent = originalLabel;
            alert('Server nicht erreichbar');
        }
    }

    function standortBearbeiten(el) {
        const url = el.dataset.url;
        const aktuell = el.textContent.replace('📍', '').replace('✏️', '').trim();
        const vorbelegt = aktuell === 'kein Standort' ? '' : aktuell;

        const wrapper = document.createElement('span');
        wrapper.className = 'standort-label';

        const input = document.createElement('input');
        input.type = 'text';
        input.value = vorbelegt;
        input.placeholder = 'Ort eingeben...';
        input.style.cssText = 'padding:2px 4px; font-size:0.85em; border:1px solid #ccc; border-radius:3px; width:140px;';

        const btn = document.createElement('button');
        btn.textContent = '💾';
        btn.className = 'scan-btn';
        btn.style.cssText = 'padding:2px 8px; font-size:0.85em; margin-left:4px;';
        btn.onclick = async () => {
            const arbeitsort = input.value.trim();
            if (!arbeitsort) return;
            btn.disabled = true;
            input.disabled = true;
            try {
                const res = await fetch(SERVER + '/standort-setzen', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url, arbeitsort })
                });
                const data = await res.json();
                if (data.ok) {
                    location.reload();
                } else {
                    alert('Fehler: ' + (data.fehler || 'Unbekannt'));
                    btn.disabled = false;
                    input.disabled = false;
                }
            } catch(e) {
                alert('Server nicht erreichbar');
                btn.disabled = false;
                input.disabled = false;
            }
        };

        wrapper.appendChild(input);
        wrapper.appendChild(btn);
        el.replaceWith(wrapper);
        input.focus();
    }

    async function bewerbungErstellen(checkbox, stellenUrl, firma, titel) {
        if (!checkbox.checked) return;

        const statusEl = document.getElementById('bew-status-' + firma + '-' + titel);
        checkbox.disabled = true;
        // Label blau färben als visuelles Feedback
        const label = checkbox.closest('label');
        if (label) { label.style.color = '#2980b9'; label.style.fontWeight = 'bold'; }
        statusEl.textContent = '⏳ Wird erstellt...';
        statusEl.style.color = '#2980b9';

        try {
            const server = window.location.origin;

            const res  = await fetch(server + '/bewerbung-erstellen?url=' + encodeURIComponent(stellenUrl));
            const data = await res.json();

            if (data.ok) {
                const box = document.getElementById('bew-box-' + firma + '-' + titel);
                const anschreibenHtml = data.anschreiben_url
                    ? `✉️ <a href="${server + data.anschreiben_url}" style="color:#27ae60;">Anschreiben.docx</a>`
                    : `<span style="color:#c0392b;">⚠️ Anschreiben fehlgeschlagen${data.anschreiben_fehler ? ': ' + data.anschreiben_fehler : ''}</span>`;
                box.innerHTML = `
                    <div style="padding:8px; background:#eafaf1; border-radius:4px; font-size:0.85em;">
                        📄 <a href="${server + data.lebenslauf_url}" style="color:#27ae60; margin-right:12px;">Lebenslauf.docx</a>
                        ${anschreibenHtml}
                    </div>`;
            } else {
                statusEl.textContent = '❌ ' + (data.fehler || 'Unbekannter Fehler');
                statusEl.style.color = '#e74c3c';
                checkbox.disabled = false;
                checkbox.checked  = false;
            }
        } catch (e) {
            statusEl.textContent = '❌ Server nicht erreichbar';
            statusEl.style.color = '#e74c3c';
            checkbox.disabled = false;
            checkbox.checked  = false;
        }
    }

    async function bewerbungNeuGenerieren(link, stellenUrl, firma, titel) {
        const box      = document.getElementById('bew-box-' + firma + '-' + titel);
        const statusEl = document.getElementById('bew-status-' + firma + '-' + titel);
        link.style.pointerEvents = 'none';
        link.style.opacity = '0.5';
        if (statusEl) {
            statusEl.textContent = '⏳ Wird neu erstellt...';
            statusEl.style.color = '#2980b9';
        }

        try {
            const server = window.location.origin;
            const res    = await fetch(server + '/bewerbung-erstellen?force=1&url=' + encodeURIComponent(stellenUrl));
            const data   = await res.json();

            if (data.ok) {
                const anschreibenHtml = data.anschreiben_url
                    ? `✉️ <a href="${server + data.anschreiben_url}" style="color:#27ae60;">Anschreiben.docx</a>`
                    : `<span style="color:#c0392b;">⚠️ Anschreiben fehlgeschlagen${data.anschreiben_fehler ? ': ' + data.anschreiben_fehler : ''}</span>`;
                box.innerHTML = `
                    📄 <a href="${server + data.lebenslauf_url}" style="color:#27ae60; margin-right:12px;">Lebenslauf.docx</a>
                    ${anschreibenHtml}
                    <a href="#" onclick="bewerbungNeuGenerieren(this, '${stellenUrl}', '${firma}', '${titel}'); return false;"
                       style="color:#7f8c8d; margin-left:12px;" title="Lebenslauf & Anschreiben neu generieren">🔄 Neu generieren</a>
                    <span id="bew-status-${firma}-${titel}" style="margin-left:8px; color:#888;"></span>`;
            } else {
                if (statusEl) {
                    statusEl.textContent = '❌ ' + (data.fehler || 'Unbekannter Fehler');
                    statusEl.style.color = '#e74c3c';
                }
                link.style.pointerEvents = 'auto';
                link.style.opacity = '1';
            }
        } catch (e) {
            if (statusEl) {
                statusEl.textContent = '❌ Server nicht erreichbar';
                statusEl.style.color = '#e74c3c';
            }
            link.style.pointerEvents = 'auto';
            link.style.opacity = '1';
        }
    }

    function rueckfragenOeffnen(link, stellenUrl, firma, titel) {
        const formBox = document.getElementById('rueckfragen-form-' + firma + '-' + titel);
        if (!formBox) return;

        // Erneutes Klicken schließt das Formular wieder, statt es zu duplizieren.
        if (formBox.innerHTML) {
            formBox.innerHTML = '';
            return;
        }

        let fragen = [];
        try { fragen = JSON.parse(link.getAttribute('data-rueckfragen')) || []; } catch (e) {}

        const zeilenHtml = fragen.map((f, i) => `
            <div style="margin:10px 0; padding:8px; background:#fff; border:1px solid #e0e0e0; border-radius:4px;">
                <div style="margin-bottom:6px;">${f.hinweis.replace(/</g, '&lt;')}</div>
                <label style="margin-right:12px; cursor:pointer;">
                    <input type="radio" name="rf-bestaetigt-${firma}-${titel}-${i}" value="ja"> Ja
                </label>
                <label style="margin-right:12px; cursor:pointer;">
                    <input type="radio" name="rf-bestaetigt-${firma}-${titel}-${i}" value="nein" checked> Nein
                </label>
                <input type="text" id="rf-detail-${firma}-${titel}-${i}" placeholder="Details (optional, z.B. wo/wann)"
                       style="margin-top:4px; padding:2px 4px; font-size:0.85em; border:1px solid #ccc; border-radius:3px; width:280px; display:block;">
            </div>`).join('');

        const zusatzId = `rf-zusatz-${firma}-${titel}`;

        formBox.innerHTML = `
            <div style="margin-top:8px; padding:8px; background:#fff8f0; border:1px solid #e67e22; border-radius:4px;">
                ${fragen.length ? '<div style="font-weight:bold; margin-bottom:4px;">❓ Offene Rückfragen für diesen Lebenslauf</div>' + zeilenHtml : ''}
                <div style="font-weight:bold; margin:8px 0 4px;">✏️ Eigene Ergänzung</div>
                <div id="${zusatzId}"></div>
                <a href="#" onclick="rueckfragenZeileHinzufuegen('${zusatzId}'); return false;"
                   style="font-size:0.85em; color:#7f8c8d;">+ weitere Angabe</a>
                <div style="margin-top:8px;">
                    <button class="scan-btn"
                        onclick="rueckfragenAbsenden(this, '${stellenUrl}', '${firma}', '${titel}', ${fragen.length}, '${zusatzId}')">💾 Absenden</button>
                    <span id="rf-status-${firma}-${titel}" style="margin-left:8px; color:#888;"></span>
                </div>
            </div>`;

        rueckfragenZeileHinzufuegen(zusatzId);
    }

    function rueckfragenZeileHinzufuegen(zusatzId) {
        const container = document.getElementById(zusatzId);
        if (!container) return;
        const markerListe = (typeof RUECKFRAGEN_MARKER !== 'undefined') ? RUECKFRAGEN_MARKER : [];
        const markerOptionsHtml = markerListe.map(([m, label]) =>
            `<option value="${m}">${label.replace(/</g, '&lt;')}</option>`).join('');
        const zeile = document.createElement('div');
        zeile.style.cssText = 'margin:6px 0; display:flex; gap:6px; align-items:flex-start;';
        zeile.innerHTML = `
            <select class="rf-zusatz-marker" style="padding:2px 4px; font-size:0.85em; border:1px solid #ccc; border-radius:3px;">${markerOptionsHtml}</select>
            <input type="text" class="rf-zusatz-fakt" placeholder="z.B. 'Datenlogger bei Bosch eingesetzt'"
                   style="flex:1; padding:2px 4px; font-size:0.85em; border:1px solid #ccc; border-radius:3px;">`;
        container.appendChild(zeile);
    }

    async function rueckfragenAbsenden(btn, stellenUrl, firma, titel, anzahl, zusatzId) {
        const statusEl = document.getElementById('rf-status-' + firma + '-' + titel);
        btn.disabled = true;
        if (statusEl) { statusEl.textContent = '⏳ Wird verarbeitet...'; statusEl.style.color = '#2980b9'; }

        const link = document.querySelector(`a[onclick*="rueckfragenOeffnen(this, '${stellenUrl}'"]`);
        let fragen = [];
        try { fragen = JSON.parse(link.getAttribute('data-rueckfragen')) || []; } catch (e) {}

        const antworten = [];
        for (let i = 0; i < anzahl; i++) {
            const radio = document.querySelector(`input[name="rf-bestaetigt-${firma}-${titel}-${i}"]:checked`);
            const detailEl = document.getElementById(`rf-detail-${firma}-${titel}-${i}`);
            antworten.push({
                hinweis:    fragen[i] ? fragen[i].hinweis : '',
                bestaetigt: !!radio && radio.value === 'ja',
                detail:     detailEl ? detailEl.value.trim() : '',
            });
        }

        const zusatzfakten = [];
        const zusatzContainer = document.getElementById(zusatzId);
        if (zusatzContainer) {
            zusatzContainer.querySelectorAll('div').forEach(zeile => {
                const sel  = zeile.querySelector('.rf-zusatz-marker');
                const feld = zeile.querySelector('.rf-zusatz-fakt');
                if (sel && feld && feld.value.trim()) {
                    zusatzfakten.push({ marker: sel.value, fakt: feld.value.trim() });
                }
            });
        }

        if (!antworten.some(a => a.bestaetigt) && !zusatzfakten.length && !antworten.length) {
            if (statusEl) { statusEl.textContent = 'Nichts einzutragen.'; statusEl.style.color = '#888'; }
            btn.disabled = false;
            return;
        }

        try {
            const server = window.location.origin;
            const res = await fetch(server + '/rueckfragen-beantworten', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: stellenUrl, antworten, zusatzfakten })
            });
            const data = await res.json();
            if (data.ok) {
                location.reload();
            } else {
                if (statusEl) {
                    statusEl.textContent = '❌ ' + (data.fehler || 'Unbekannter Fehler');
                    statusEl.style.color = '#e74c3c';
                }
                btn.disabled = false;
            }
        } catch (e) {
            if (statusEl) {
                statusEl.textContent = '❌ Server nicht erreichbar';
                statusEl.style.color = '#e74c3c';
            }
            btn.disabled = false;
        }
    }

    // Trägt eine Stelle über /stelle-einfuegen ein und lässt die Teil-Pipeline
    // (rohtext_holen/extraktor/bewertung/report) über /manuell-stream laufen.
    // meldung(text, istFehler) zeigt Statustext an; onZeile(text) ist optional
    // und bekommt jede rohe Log-Zeile (für ein sichtbares Output-Fenster).
    async function _stelleEinfuegenAusfuehren(url, firma, titel, meldung, onZeile) {
        const res = await fetch(SERVER + '/stelle-einfuegen', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, firma, titel })
        });
        const data = await res.json();

        if (!data.ok) {
            meldung('Fehler: ' + (data.fehler || 'Unbekannt'), true);
            return;
        }

        meldung('Eingetragen - Pipeline laeuft...', false);

        // Nur DIESE Stelle durch die Teil-Pipeline schicken (url-Filter), sonst
        // läuft rohtext_holen/extraktor/bewertung über ALLE Stellen und wirkt wie
        // "hängt".
        const quelle = new EventSource(SERVER + '/manuell-stream?url=' + encodeURIComponent(url));
        quelle.onmessage = function(e) {
            if (e.data === 'FERTIG') {
                quelle.close();
                meldung('Fertig - Seite wird neu geladen...', false);
                setTimeout(() => location.reload(), 2000);
                return;
            }
            if (onZeile) onZeile(e.data);
        };
        quelle.onerror = function() {
            quelle.close();
            meldung('Verbindungsfehler zum Server', true);
        };
    }

    async function stelleEinfuegen() {
        const url = document.getElementById('manuell-url').value.trim();
        const firma = document.getElementById('manuell-firma').value.trim();
        const titel = document.getElementById('manuell-titel').value.trim();
        const statusEl = document.getElementById('manuell-status');
        const output = document.getElementById('manuell-output');

        if (!url) {
            statusEl.textContent = 'Bitte eine URL eingeben.';
            statusEl.style.color = '#e74c3c';
            return;
        }

        statusEl.textContent = 'Stelle wird eingetragen...';
        statusEl.style.color = '#2980b9';
        output.style.display = 'block';
        output.textContent = '';

        await _stelleEinfuegenAusfuehren(url, firma, titel,
            (text, istFehler) => {
                statusEl.textContent = text;
                statusEl.style.color = istFehler ? '#e74c3c' : '#27ae60';
            },
            (zeile) => {
                output.textContent += zeile + '\n';
                output.scrollTop = output.scrollHeight;
            }
        );
    }

    // "In Pipeline aufnehmen"-Button bei einem Titel ohne Suchbegriff-Treffer:
    // url/firma/titel stehen schon als data-Attribute an der .kt-eintrag-Zeile.
    async function stelleAusKeinTrefferAufnehmen(btn) {
        const zeile = btn.closest('.kt-eintrag');
        const statusEl = zeile.querySelector('.kt-status');

        // Live-Ausgabe-Box einmalig anlegen, damit man sieht, was die Pipeline
        // gerade macht (rohtext_holen → extraktor → bewertung → report).
        let output = zeile.querySelector('.kt-pipeline-output');
        if (!output) {
            output = document.createElement('pre');
            output.className = 'kt-pipeline-output';
            output.style.cssText = 'margin-top:6px; max-height:180px; overflow:auto; ' +
                'background:#1e1e1e; color:#ddd; font-size:0.8em; padding:6px 8px; ' +
                'border-radius:4px; white-space:pre-wrap; word-break:break-word;';
            zeile.appendChild(output);
        }
        output.style.display = 'block';
        output.textContent = '';

        btn.disabled = true;
        statusEl.textContent = 'Wird eingetragen...';
        statusEl.style.color = '#2980b9';

        await _stelleEinfuegenAusfuehren(zeile.dataset.url, zeile.dataset.firma, zeile.dataset.titel,
            (text, istFehler) => {
                statusEl.textContent = text;
                statusEl.style.color = istFehler ? '#e74c3c' : '#27ae60';
                if (istFehler) btn.disabled = false;
            },
            (zeileText) => {
                output.textContent += zeileText + '\n';
                output.scrollTop = output.scrollHeight;
            }
        );
    }

    // "Stellentext anzeigen"-Button: lädt den Rohtext live per Playwright (ohne
    // die Stelle zu speichern) und zeigt ihn in der .kt-vorschau-Box darunter an.
    // Zweiter Klick blendet nur ein/aus, ohne erneut zu laden.
    async function stellentextVorschau(btn) {
        const zeile   = btn.closest('.kt-eintrag');
        const box     = zeile.querySelector('.kt-vorschau');
        const statusEl = zeile.querySelector('.kt-status');

        if (box.dataset.geladen === '1') {
            box.style.display = box.style.display === 'none' ? 'block' : 'none';
            return;
        }

        btn.disabled = true;
        statusEl.textContent = 'Lädt Stellentext...';
        statusEl.style.color = '#2980b9';
        box.style.display = 'block';
        box.textContent = 'Lädt...';

        try {
            const res = await fetch(SERVER + '/kein-treffer-vorschau', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: zeile.dataset.url })
            });
            const data = await res.json();
            if (!data.ok) {
                box.textContent = 'Fehler: ' + (data.fehler || 'Unbekannt');
                statusEl.textContent = '';
                btn.disabled = false;
                return;
            }
            box.textContent = data.text;
            box.dataset.geladen = '1';
            statusEl.textContent = '';
        } catch (e) {
            box.textContent = 'Server nicht erreichbar';
            statusEl.textContent = '';
        }
        btn.disabled = false;
    }

    // "Als Suchbegriff vormerken"-Button: übernimmt ausgewählte Wort-Chips
    // (kt-chip-active) plus optionalen Freitext als Vorschlag in
    // neue_suchbegriffe.json - landet NICHT direkt in config.txt, sondern wird
    // erst später gesammelt durchgesehen.
    async function suchbegriffHinzufuegen(btn) {
        const zeile = btn.closest('.kt-eintrag');
        const statusEl = zeile.querySelector('.kt-status');
        const freitext = zeile.querySelector('.kt-freitext');

        const begriffe = Array.from(zeile.querySelectorAll('.kt-chip-active')).map(c => c.textContent);
        if (freitext.value.trim()) begriffe.push(freitext.value.trim());

        if (begriffe.length === 0) {
            statusEl.textContent = 'Bitte mind. einen Begriff wählen oder eingeben.';
            statusEl.style.color = '#e74c3c';
            return;
        }

        btn.disabled = true;
        statusEl.textContent = 'Wird vorgemerkt...';
        statusEl.style.color = '#2980b9';

        try {
            const res = await fetch(SERVER + '/suchbegriff-hinzufuegen', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    begriffe,
                    url: zeile.dataset.url,
                    firma: zeile.dataset.firma,
                    titel: zeile.dataset.titel
                })
            });
            const data = await res.json();
            if (!data.ok) {
                statusEl.textContent = 'Fehler: ' + (data.fehler || 'Unbekannt');
                statusEl.style.color = '#e74c3c';
                btn.disabled = false;
                return;
            }
            const mitText = data.stellentext_dabei ? ' (inkl. Stellentext)' : '';
            statusEl.textContent = '✅ vorgemerkt: ' + begriffe.join(', ') + mitText;
            statusEl.style.color = '#27ae60';
            freitext.value = '';
            zeile.querySelectorAll('.kt-chip-active').forEach(c => c.classList.remove('kt-chip-active'));
        } catch (e) {
            statusEl.textContent = 'Server nicht erreichbar';
            statusEl.style.color = '#e74c3c';
            btn.disabled = false;
        }
    }

    // "Als Ausschlussbegriff vormerken"-Button: übernimmt ausgewählte Wort-Chips
    // (kt-chip-active) plus optionalen Freitext als Vorschlag in
    // neue_ausschlussbegriffe.json - landet NICHT direkt in config.txt, sondern
    // wird erst später gesammelt durchgesehen. Analog zu suchbegriffHinzufuegen,
    // aber für die Blacklist [ausschlussbegriffe].
    async function ausschlussbegriffHinzufuegen(btn) {
        const zeile = btn.closest('.kt-eintrag');
        const statusEl = zeile.querySelector('.kt-status');
        const freitext = zeile.querySelector('.kt-freitext');

        const begriffe = Array.from(zeile.querySelectorAll('.kt-chip-active')).map(c => c.textContent);
        if (freitext.value.trim()) begriffe.push(freitext.value.trim());

        if (begriffe.length === 0) {
            statusEl.textContent = 'Bitte mind. einen Begriff wählen oder eingeben.';
            statusEl.style.color = '#e74c3c';
            return;
        }

        btn.disabled = true;
        statusEl.textContent = 'Wird vorgemerkt...';
        statusEl.style.color = '#2980b9';

        try {
            const res = await fetch(SERVER + '/ausschlussbegriff-hinzufuegen', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    begriffe,
                    url: zeile.dataset.url,
                    firma: zeile.dataset.firma,
                    titel: zeile.dataset.titel
                })
            });
            const data = await res.json();
            if (!data.ok) {
                statusEl.textContent = 'Fehler: ' + (data.fehler || 'Unbekannt');
                statusEl.style.color = '#e74c3c';
                btn.disabled = false;
                return;
            }
            statusEl.textContent = '🚫 als Ausschluss vorgemerkt: ' + begriffe.join(', ');
            statusEl.style.color = '#27ae60';
            freitext.value = '';
            zeile.querySelectorAll('.kt-chip-active').forEach(c => c.classList.remove('kt-chip-active'));
        } catch (e) {
            statusEl.textContent = 'Server nicht erreichbar';
            statusEl.style.color = '#e74c3c';
            btn.disabled = false;
        }
    }

    // "✅ In config übernehmen"-Button im Abschnitt "Vorgemerkte Suchbegriffe":
    // schreibt den Begriff in den [suchbegriffe]-Block von config.txt und
    // entfernt ihn aus neue_suchbegriffe.json.
    async function suchbegriffInConfig(btn) {
        const li = btn.closest('.vb-eintrag');
        const statusEl = li.querySelector('.vb-status');
        const begriff = li.dataset.begriff;

        btn.disabled = true;
        statusEl.textContent = 'Übernehme...';
        statusEl.style.color = '#2980b9';

        try {
            const res = await fetch(SERVER + '/suchbegriff-uebernehmen', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ begriff })
            });
            const data = await res.json();
            if (!data.ok) {
                statusEl.textContent = 'Fehler: ' + (data.fehler || 'Unbekannt');
                statusEl.style.color = '#e74c3c';
                btn.disabled = false;
                return;
            }
            statusEl.textContent = data.schon_vorhanden ? '✓ war schon in config.txt' : '✅ in config.txt übernommen';
            statusEl.style.color = '#27ae60';
            btn.style.display = 'none';
            li.style.opacity = '0.55';
        } catch (e) {
            statusEl.textContent = 'Server nicht erreichbar';
            statusEl.style.color = '#e74c3c';
            btn.disabled = false;
        }
    }

    // "🚫 In config übernehmen"-Button im Abschnitt "Vorgemerkte Ausschlussbegriffe":
    // schreibt den Begriff in den [ausschlussbegriffe]-Block von config.txt und
    // entfernt ihn aus neue_ausschlussbegriffe.json.
    async function ausschlussbegriffInConfig(btn) {
        const li = btn.closest('.vb-eintrag');
        const statusEl = li.querySelector('.vb-status');
        const begriff = li.dataset.begriff;

        btn.disabled = true;
        statusEl.textContent = 'Übernehme...';
        statusEl.style.color = '#2980b9';

        try {
            const res = await fetch(SERVER + '/ausschlussbegriff-uebernehmen', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ begriff })
            });
            const data = await res.json();
            if (!data.ok) {
                statusEl.textContent = 'Fehler: ' + (data.fehler || 'Unbekannt');
                statusEl.style.color = '#e74c3c';
                btn.disabled = false;
                return;
            }
            statusEl.textContent = data.schon_vorhanden ? '✓ war schon in config.txt' : '✅ in config.txt übernommen';
            statusEl.style.color = '#27ae60';
            btn.style.display = 'none';
            li.style.opacity = '0.55';
        } catch (e) {
            statusEl.textContent = 'Server nicht erreichbar';
            statusEl.style.color = '#e74c3c';
            btn.disabled = false;
        }
    }

    async function neuLadenUndBewerten(btn, stellenUrl) {
        const _originalLabel = btn.textContent;
        btn.disabled = true;
        btn.textContent = '⏳ Vorbereitung...';
        try {
            const res = await fetch(SERVER + '/stelle-neu-laden', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: stellenUrl })
            });
            const data = await res.json();
            if (!data.ok) {
                btn.disabled = false;
                btn.textContent = _originalLabel;
                alert('Fehler: ' + (data.fehler || 'Unbekannt'));
                return;
            }
        } catch(e) {
            btn.disabled = false;
            btn.textContent = _originalLabel;
            alert('Server nicht erreichbar');
            return;
        }
        const output = document.getElementById('scan-output');
        const status = document.getElementById('scan-status');
        output.style.display = 'block';
        output.textContent = '';
        status.textContent = '⏳ Pipeline läuft...';
        output.scrollIntoView({ behavior: 'smooth' });
        const quelle = new EventSource(SERVER + '/stelle-einzeln-stream?url=' + encodeURIComponent(stellenUrl));
        quelle.onmessage = function(e) {
            if (e.data === 'FERTIG') {
                quelle.close();
                status.textContent = '✅ Fertig – Seite wird neu geladen...';
                setTimeout(() => location.reload(), 2000);
                return;
            }
            output.textContent += e.data + '\n';
            output.scrollTop = output.scrollHeight;
        };
        quelle.onerror = function() {
            quelle.close();
            btn.disabled = false;
            btn.textContent = '🔄 Neu laden & bewerten';
            status.textContent = '❌ Verbindungsfehler';
        };
    }

    // ── Filter & Sortierung ──────────────────────────────────────────
    let _aktiverFilter = null;
    let _aktiveSortierung = null;
    let _aktiverStatusFilter = null;
    let _aktiverFirmaFilter = null;
    let _nurNichtBewertet = false;
    let _nurVorgemerkt = false;
    let _nurMerkliste = false;
    let _flatAktiv = false;
    const _stellenUrsprung = [];

    function toggleGeringerMatch(checked) {
        const section = document.getElementById('geringer-match-section');
        if (section) section.style.display = checked ? '' : 'none';
        if (_flatAktiv) _aktualisiereFlach();
    }
    function toggleZuWeit(checked) {
        const section = document.getElementById('zu-weit-section');
        if (section) section.style.display = checked ? '' : 'none';
        if (_flatAktiv) _aktualisiereFlach();
    }
    function toggleStandortAusserhalb(checked) {
        const section = document.getElementById('standort-ausserhalb-section');
        if (section) section.style.display = checked ? '' : 'none';
        if (_flatAktiv) _aktualisiereFlach();
    }
    function toggleNichtBewertet(checked) {
        _nurNichtBewertet = checked;
        _aktualisiere();
    }
    function toggleVorgemerkt(checked) {
        _nurVorgemerkt = checked;
        _aktualisiere();
    }
    function toggleMerkliste(checked) {
        _nurMerkliste = checked;
        _aktualisiere();
    }
    // _aktiverFilter (Stufe: kennenlernen/einladung/zusage/absage) und
    // _aktiverStatusFilter (Scanner-Status: bewerben/beworben/... ) sind zwei
    // unabhängige Variablen, stellen für den Nutzer aber dieselbe Filterleiste
    // dar ("welche Stellen will ich sehen?"). Ohne gegenseitiges Zurücksetzen
    // blieb ein zuvor gewählter Filter im Hintergrund aktiv und wurde mit dem
    // neuen UND-verknüpft - Stellen verschwanden dadurch scheinbar grundlos
    // (z.B. eine beworbene Stelle bei noch aktivem Kennenlernen-Filter).
    function setzeFilter(filter) {
        _aktiverFilter = (_aktiverFilter === filter && filter !== null) ? null : filter;
        _aktiverStatusFilter = null;
        _aktualisiere();
    }
    function setzeSortierung(sort) {
        _aktiveSortierung = (_aktiveSortierung === sort && sort !== null) ? null : sort;
        _aktualisiere();
    }
    function setzeStatusFilter(status) {
        _aktiverStatusFilter = (_aktiverStatusFilter === status) ? null : status;
        _aktiverFilter = null;
        _aktualisiere();
    }
    function setzeFirmaFilter(firma) {
        _aktiverFirmaFilter = firma || null;
        _aktualisiere();
    }
    function _aktualisiere() {
        _aktualisiereFilterBtns();
        const brauchtFlach = _aktiverFilter !== null || _aktiveSortierung !== null || _aktiverStatusFilter !== null || _aktiverFirmaFilter !== null || _nurNichtBewertet || _nurVorgemerkt || _nurMerkliste;
        if (brauchtFlach && !_flatAktiv) _aktiviereFlach();
        else if (!brauchtFlach && _flatAktiv) _deaktiviereFlach();
        else if (brauchtFlach && _flatAktiv) _aktualisiereFlach();
    }
    function _aktualisiereFilterBtns() {
        const map = {
            'btn-alle':          _aktiverFilter === null && _aktiverStatusFilter === null && _aktiverFirmaFilter === null,
            'btn-beworben':      _aktiverStatusFilter === 6,
            'btn-nicht-beworben': _aktiverStatusFilter === 10,
            'btn-kennenlernen':  _aktiverFilter === 'kennenlernen',
            'btn-einladung':     _aktiverFilter === 'einladung',
            'btn-zusage':        _aktiverFilter === 'zusage',
            'btn-absage':        _aktiverFilter === 'absage',
            'btn-sort-std':      _aktiveSortierung === null,
            'btn-sort-score':    _aktiveSortierung === 'score',
            'btn-sort-auto':     _aktiveSortierung === 'auto',
            'btn-sort-transit':  _aktiveSortierung === 'transit',
        };
        Object.entries(map).forEach(([id, aktiv]) => {
            const btn = document.getElementById(id);
            if (btn) btn.classList.toggle('aktiv', aktiv);
        });
        document.querySelectorAll('.btn-scanner-status').forEach(btn => {
            btn.classList.toggle('aktiv', parseInt(btn.dataset.status) === _aktiverStatusFilter);
        });
        const btnStatusAlle = document.getElementById('btn-status-alle');
        if (btnStatusAlle) btnStatusAlle.classList.toggle('aktiv', _aktiverStatusFilter === null);
    }
    function _aktiviereFlach() {
        const ha = document.getElementById('hauptansicht');
        const fa = document.getElementById('flat-ansicht');
        _stellenUrsprung.length = 0;
        const _seenUrls = new Set();
        ha.querySelectorAll('.stelle[data-url]').forEach(el => {
            const u = el.dataset.url;
            if (u && _seenUrls.has(u)) return;
            if (u) _seenUrls.add(u);
            _stellenUrsprung.push({ el, parent: el.parentNode, nextSibling: el.nextSibling });
        });
        _flatAktiv = true;
        _aktualisiereFlach();
        ha.style.display = 'none';
        fa.style.display = 'block';
    }
    function _deaktiviereFlach() {
        const ha = document.getElementById('hauptansicht');
        const fa = document.getElementById('flat-ansicht');
        for (let i = _stellenUrsprung.length - 1; i >= 0; i--) {
            const {el, parent, nextSibling} = _stellenUrsprung[i];
            if (nextSibling) parent.insertBefore(el, nextSibling);
            else parent.appendChild(el);
        }
        _stellenUrsprung.length = 0;
        _flatAktiv = false;
        fa.style.display = 'none';
        fa.innerHTML = '<div id="flat-ansicht-info"></div>';
        ha.style.display = '';
        aktualisiereStatusCounts();
    }
    function _aktualisiereFlach() {
        const fa = document.getElementById('flat-ansicht');
        _stellenUrsprung.forEach(({el}) => {
            if (el.parentNode === fa) fa.removeChild(el);
        });
        let gefiltert = _stellenUrsprung.map(o => o.el);
        if (_nurVorgemerkt) {
            // data-vorgemerkt wird serverseitig nur für bewerbungsrelevante Stellen
            // gesetzt (Status bewerben/beworben, nicht ausgeschlossen) – hier also
            // ohne weitere Ausschluss-/Status-Filter übernehmen.
            gefiltert = gefiltert.filter(el => el.dataset.vorgemerkt === '1');
        } else {
            // Bereits als "nicht passend" aussortierte Stellen (Ausschlusskriterium,
            // Standort außerhalb/verboten) sollen nie in der Flach-/Sortier-Ansicht
            // auftauchen – dafür gibt es keine Einblenden-Checkbox.
            gefiltert = gefiltert.filter(el => !el.dataset.ausgeschlossen);
            if (_aktiverFirmaFilter !== null) {
                gefiltert = gefiltert.filter(el => el.dataset.firma === _aktiverFirmaFilter);
            }
            if (_aktiverFilter !== null) {
                gefiltert = gefiltert.filter(el => el.classList.contains(_aktiverFilter));
            }
            if (_aktiverStatusFilter !== null) {
                gefiltert = gefiltert.filter(el => parseInt(el.dataset.scannerStatus) === _aktiverStatusFilter);
            } else {
                // Vergebene/gelöschte/abgesagte Stellen ausschließen, außer ein
                // konkreter Status wurde explizit ausgewählt (z.B. über den
                // Status-Filter "Absage erhalten").
                const _inaktiveStatus = new Set(INAKTIVE_STATUS);
                gefiltert = gefiltert.filter(el => {
                    const s = parseInt(el.dataset.scannerStatus);
                    return isNaN(s) || !_inaktiveStatus.has(s);
                });
            }
        }
        // Bei gesetztem Firmen-Filter (oder Vorgemerkt-Ansicht) sollen alle
        // betroffenen Stellen sichtbar sein – Geringer-Match/Zu-weit nur
        // ausblenden, wenn keine Firma gewählt ist.
        const zeigeGM = document.getElementById('cb-geringer-match')?.checked || _aktiverFirmaFilter !== null || _nurVorgemerkt || _nurMerkliste;
        let ausgeblendetGM = 0;
        if (!zeigeGM) {
            ausgeblendetGM = gefiltert.filter(el => el.dataset.geringerMatch).length;
            gefiltert = gefiltert.filter(el => !el.dataset.geringerMatch);
        }
        const zeigeZW = document.getElementById('cb-zu-weit')?.checked || _aktiverFirmaFilter !== null || _nurVorgemerkt || _nurMerkliste;
        let ausgeblendetZW = 0;
        if (!zeigeZW) {
            ausgeblendetZW = gefiltert.filter(el => el.dataset.zuWeit).length;
            gefiltert = gefiltert.filter(el => !el.dataset.zuWeit);
        }
        if (_nurNichtBewertet) {
            const _unbewerteterStatus = new Set(UNBEWERTETE_STATUS);
            gefiltert = gefiltert.filter(el => _unbewerteterStatus.has(parseInt(el.dataset.scannerStatus)));
        }
        if (_nurMerkliste) {
            gefiltert = gefiltert.filter(el => el.dataset.gemerkt === '1');
        }
        if (_aktiveSortierung === 'score') {
            gefiltert = gefiltert
                .filter(el => !el.classList.contains('stelle-geloescht'))
                .slice().sort((a, b) =>
                    parseInt(b.dataset.score || '0') - parseInt(a.dataset.score || '0'));
        } else if (_aktiveSortierung === 'auto') {
            gefiltert = gefiltert
                .filter(el => !el.classList.contains('stelle-geloescht'))
                .slice().sort((a, b) =>
                    (parseInt(a.dataset.autoMin) || 9999) - (parseInt(b.dataset.autoMin) || 9999));
        } else if (_aktiveSortierung === 'transit') {
            gefiltert = gefiltert
                .filter(el => !el.classList.contains('stelle-geloescht'))
                .slice().sort((a, b) =>
                    (parseInt(a.dataset.transitMin) || 9999) - (parseInt(b.dataset.transitMin) || 9999));
        }
        const info = document.createElement('div');
        info.id = 'flat-ansicht-info';
        const filterText = {
            beworben:     '✅ Beworben',
            kennenlernen: '📞 Kennenlernen',
            einladung:    '📅 Einladung',
            zusage:       '🎉 Zusage',
            absage:       '❌ Absage',
        }[_aktiverFilter] || '';
        const statusText = _aktiverStatusFilter !== null ? ('Status: ' + (STATUS_LABELS[_aktiverStatusFilter] || _aktiverStatusFilter)) : '';
        const firmaText = _aktiverFirmaFilter !== null ? ('🏢 ' + _aktiverFirmaFilter) : '';
        const sortText = {
            score:   '⭐ Nach Passung',
            auto:    '🚗 Nach Entfernung (Auto)',
            transit: '🚌 Nach Entfernung (ÖPNV)',
        }[_aktiveSortierung] || '';
        const nichtBewertetText = _nurNichtBewertet ? '❓ Nicht bewertet' : '';
        const vorgemerktText = _nurVorgemerkt ? '⏳ Verfügbarkeit unsicher – wird beim nächsten Lauf gelöscht' : '';
        const merklisteText = _nurMerkliste ? '🔖 Merkliste' : '';
        info.textContent = [firmaText, filterText, statusText, sortText, nichtBewertetText, vorgemerktText, merklisteText].filter(Boolean).join(' · ')
            + ` — ${gefiltert.length} Stelle${gefiltert.length !== 1 ? 'n' : ''}`;
        fa.innerHTML = '';
        fa.appendChild(info);
        if (gefiltert.length === 0) {
            const p = document.createElement('p');
            p.className = 'leer';
            p.textContent = 'Keine Stellen gefunden.';
            fa.appendChild(p);
        } else {
            gefiltert.forEach(el => fa.appendChild(el));
        }
        // Der aktive Filter (Status/Firma/...) kann Treffer haben, die zusätzlich
        // per Geringer-Match- oder Zu-weit-Checkbox ausgeblendet sind - ohne
        // Hinweis wirkte die Liste dann unvollständig oder fälschlich leer
        // (siehe Grenzfall-Zähler-Bug: Kopfzeile zeigte mehr als sichtbar war).
        // Klick blendet die jeweilige Gruppe dauerhaft ein (dieselbe Checkbox
        // wie oben in der Filterleiste).
        [
            { count: ausgeblendetGM, label: 'Geringer Match', cbId: 'cb-geringer-match', fn: toggleGeringerMatch },
            { count: ausgeblendetZW, label: 'Zu weit',        cbId: 'cb-zu-weit',        fn: toggleZuWeit },
        ].forEach(({count, label, cbId, fn}) => {
            if (count === 0) return;
            const p = document.createElement('p');
            p.className = 'ausblende-hinweis';
            p.textContent = `▾ ${count} weitere ausgeblendet (${label}) – anzeigen`;
            p.onclick = () => {
                const cb = document.getElementById(cbId);
                if (cb) cb.checked = true;
                fn(true);
            };
            fa.appendChild(p);
        });
        aktualisiereStatusCounts();
    }

    async function stellePruefen(btn, url) {
        const ergebnisEl = btn.nextElementSibling;
        btn.disabled = true;
        btn.textContent = '⏳ Prüfe...';
        try {
            const res = await fetch('/api/pruefe-stelle', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({url})
            });
            const data = await res.json();
            if (data.ergebnis === 'aktiv') {
                ergebnisEl.textContent = '✅ Erreichbar';
                ergebnisEl.style.color = '#27ae60';
            } else if (data.ergebnis === 'vergaben') {
                ergebnisEl.textContent = `❌ Nicht erreichbar (HTTP ${data.code})`;
                ergebnisEl.style.color = '#e74c3c';
            } else if (data.ergebnis === 'botschutz') {
                ergebnisEl.textContent = '🤖 Bot-Schutz erkannt – nicht automatisch prüfbar, bitte manuell im Browser checken';
                ergebnisEl.style.color = '#888';
            } else {
                ergebnisEl.textContent = `❓ Unklar (HTTP ${data.code ?? '–'})`;
                ergebnisEl.style.color = '#888';
            }
        } catch(e) {
            ergebnisEl.textContent = '⚠️ Fehler';
            ergebnisEl.style.color = '#e74c3c';
        }
        btn.disabled = false;
        btn.textContent = '🔍 Neu prüfen';
    }

    // Eine Stelle, die gerade als "nicht passend"/"nicht beworben" abgelehnt wird,
    // gehört nicht mehr auf die Merkliste (server-seitig löscht db.upsert_stelle()
    // das gemerkt-Feld automatisch mit). Bildet das clientseitig sofort nach, statt
    // auf die Report-Neugenerierung im Hintergrund zu warten.
    function _entferneAusMerkliste(url) {
        let warGemerkt = false;
        document.querySelectorAll(`.stelle[data-url="${CSS.escape(url)}"]`).forEach(el => {
            if (el.dataset.gemerkt === '1') {
                warGemerkt = true;
                delete el.dataset.gemerkt;
                const mb = el.querySelector('.merken-toggle');
                if (mb) _setzeMerkenBtn(mb, url, false);
            }
        });
        if (warGemerkt) {
            const zaehler = document.getElementById('stat-merkliste');
            if (zaehler) zaehler.textContent = String(Math.max(0, (parseInt(zaehler.textContent) || 0) - 1));
            if (_flatAktiv) _aktualisiereFlach();
        }
    }

    async function nichtBeworben(btn, url) {
        if (!confirm('Stelle als "Nicht beworben" markieren?')) return;
        btn.disabled = true;
        btn.textContent = '⏳...';
        try {
            await fetch(SERVER + '/status', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({url, feld: 'nicht_beworben', wert: '1'})
            });
            document.querySelectorAll(`.stelle[data-url="${CSS.escape(url)}"]`).forEach(el => {
                aktualisiereStatusBadge(el, 10);
            });
            // "Nicht beworben" gehört nicht mehr zu "Neue Stellen" - Karte dort sofort
            // entfernen, statt auf die Report-Neugenerierung im Hintergrund zu warten.
            document.querySelectorAll(`.stelle[data-url="${CSS.escape(url)}"][data-section="neue"]`).forEach(el => el.remove());
            _entferneAusMerkliste(url);
            btn.textContent = '🚫 Nicht beworben';
            btn.style.opacity = '0.5';
        } catch(e) {
            btn.disabled = false;
            btn.textContent = '🚫 Nicht beworben';
        }
    }

    // Richtet den Passend-Umschalter passend zum aktuellen Status aus:
    // Status 4 (bewerben) → Button bietet "Nicht passend" an, Status 5 umgekehrt.
    function _setzePassendBtn(b, url, istPassend) {
        if (istPassend) {
            b.textContent = '👎 Nicht passend';
            b.style.background = '#f9ebea'; b.style.borderColor = '#c0392b'; b.style.color = '#c0392b';
            b.onclick = () => passendSetzen(b, url, false);
        } else {
            b.textContent = '📋 Passend – bewerben';
            b.style.background = '#eafaf1'; b.style.borderColor = '#27ae60'; b.style.color = '#27ae60';
            b.onclick = () => passendSetzen(b, url, true);
        }
        b.disabled = false;
    }

    async function passendSetzen(btn, url, passend) {
        btn.disabled = true;
        btn.textContent = '⏳...';
        try {
            const res = await fetch(SERVER + '/passend-setzen', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({url, passend})
            });
            const data = await res.json();
            if (!data.ok) {
                alert('Fehler: ' + (data.fehler || 'Unbekannt'));
                _setzePassendBtn(btn, url, !passend);
                return;
            }
            // Stelle kann mehrfach im Report stehen (Neue Stellen, Top 10, pro Firma)
            document.querySelectorAll(`.stelle[data-url="${CSS.escape(url)}"]`).forEach(el => {
                aktualisiereStatusBadge(el, data.status);
                // Grenzfall-Karten haben zwei Buttons (Passend/Nicht passend) - nach der
                // Entscheidung bleibt nur noch der eine Standard-Umschalter übrig.
                el.querySelectorAll('.passend-toggle').forEach((b, i) => {
                    if (i === 0) _setzePassendBtn(b, url, passend);
                    else b.remove();
                });
            });
            if (!passend) {
                // "Nicht passend" gehört nicht mehr zu "Neue Stellen" - Karte dort sofort
                // entfernen, statt auf die Report-Neugenerierung im Hintergrund zu warten.
                document.querySelectorAll(`.stelle[data-url="${CSS.escape(url)}"][data-section="neue"]`).forEach(el => el.remove());
                _entferneAusMerkliste(url);
            }
        } catch(e) {
            alert('Server nicht erreichbar');
            _setzePassendBtn(btn, url, !passend);
        }
    }

    async function vergebenMarkieren(btn, url) {
        if (!confirm('Stelle manuell als "Vergeben" markieren? (z.B. weil die automatische Prüfung sie nicht erkennen kann)')) return;
        btn.disabled = true;
        btn.textContent = '⏳...';
        try {
            const res = await fetch(SERVER + '/vergeben-setzen', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({url})
            });
            const data = await res.json();
            if (!data.ok) {
                alert('Fehler: ' + (data.fehler || 'Unbekannt'));
                btn.disabled = false;
                btn.textContent = '🗑️ Als vergeben markieren';
                return;
            }
            const el = document.querySelector(`[data-url="${CSS.escape(url)}"]`);
            if (el) aktualisiereStatusBadge(el, data.status);
            btn.textContent = '🗑️ Vergeben markiert';
            btn.style.opacity = '0.5';
        } catch(e) {
            alert('Server nicht erreichbar');
            btn.disabled = false;
            btn.textContent = '🗑️ Als vergeben markieren';
        }
    }

    function _setzeMerkenBtn(b, url, gemerkt) {
        if (gemerkt) {
            b.textContent = '🔖 Von Merkliste entfernen';
            b.style.background = '#fff8e1'; b.style.borderColor = '#ffc107'; b.style.color = '#946c00';
            b.onclick = () => merkenSetzen(b, url, false);
        } else {
            b.textContent = '🔖 Merken';
            b.style.background = '#f7f7f7'; b.style.borderColor = '#bbb'; b.style.color = '#555';
            b.onclick = () => merkenSetzen(b, url, true);
        }
        b.disabled = false;
    }

    async function merkenSetzen(btn, url, gemerkt) {
        btn.disabled = true;
        btn.textContent = '⏳...';
        try {
            const res = await fetch(SERVER + '/merken-setzen', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({url, gemerkt})
            });
            const data = await res.json();
            if (!data.ok) {
                alert('Fehler: ' + (data.fehler || 'Unbekannt'));
                _setzeMerkenBtn(btn, url, !gemerkt);
                return;
            }
            document.querySelectorAll(`.stelle[data-url="${CSS.escape(url)}"]`).forEach(el => {
                if (gemerkt) el.dataset.gemerkt = '1'; else delete el.dataset.gemerkt;
                const b = el.querySelector('.merken-toggle');
                if (b) _setzeMerkenBtn(b, url, gemerkt);
            });
            const zaehler = document.getElementById('stat-merkliste');
            if (zaehler) zaehler.textContent = String((parseInt(zaehler.textContent) || 0) + (gemerkt ? 1 : -1));
            if (_flatAktiv) _aktualisiereFlach();
        } catch(e) {
            alert('Server nicht erreichbar');
            _setzeMerkenBtn(btn, url, !gemerkt);
        }
    }

    async function neueFirmaTesten() {
        const url      = document.getElementById('firma-test-url').value.trim();
        const name     = document.getElementById('firma-test-name').value.trim();
        const checkbox = document.getElementById('firma-config-cb');
        const output   = document.getElementById('scan-output');
        const status   = document.getElementById('scan-status');

        if (!url || !name) {
            status.textContent = '⚠️ Karriere-URL und Firmenname sind Pflichtfelder';
            return;
        }

        output.style.display = 'block';
        output.textContent   = '';
        status.textContent   = '⏳ Teste ' + name + '...';

        let letzteZeile = '';

        const params = new URLSearchParams({url, firmenname: name});
        const quelle = new EventSource('/firmen-testen-stream?' + params.toString());
        quelle.onmessage = function(e) {
            if (e.data === 'FERTIG') {
                quelle.close();
                status.textContent = '✅ Test abgeschlossen';
                if (checkbox.checked && letzteZeile.includes('✅')) {
                    fetch('/firmen-config-hinzufuegen', {
                        method:  'POST',
                        headers: {'Content-Type': 'application/json'},
                        body:    JSON.stringify({firmenname: name, url})
                    }).then(r => r.json()).then(d => {
                        output.textContent += d.ok
                            ? '\n✅ ' + name + ' zur config.txt hinzugefügt'
                            : '\n❌ config.txt Fehler: ' + (d.fehler || '');
                        output.scrollTop = output.scrollHeight;
                    }).catch(() => {
                        output.textContent += '\n❌ Netzwerkfehler beim Speichern in config.txt';
                        output.scrollTop = output.scrollHeight;
                    });
                }
                return;
            }
            letzteZeile = e.data;
            output.textContent += e.data + '\n';
            output.scrollTop = output.scrollHeight;
        };
        quelle.onerror = function() {
            quelle.close();
            status.textContent = '❌ Verbindungsfehler zum Server';
        };
    }
