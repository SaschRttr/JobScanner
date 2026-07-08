
    // STATUS_LABELS, INAKTIVE_STATUS, UNBEWERTETE_STATUS, FILTER_STATUS werden
    // von report.py aus status_def.py injiziert (siehe <script> davor).
    const SERVER = window.location.origin;

    const _STUFE_ZU_STATUS = { beworben: 6, absage: 8 };

    function aktualisiereStatusBadge(el, neuerStatus) {
        const badge = el.querySelector('.scanner-status');
        if (!badge) return;
        for (let i = 0; i <= 10; i++) badge.classList.remove('scanner-status-' + i);
        badge.classList.add('scanner-status-' + neuerStatus);
        badge.title = 'Status ' + neuerStatus;
        badge.textContent = STATUS_LABELS[neuerStatus] || String(neuerStatus);
        el.dataset.scannerStatus = String(neuerStatus);
        aktualisiereStatusCounts();
    }

    function aktualisiereStatusCounts() {
        const counts = {};
        const seenUrls = new Set();
        document.querySelectorAll('.stelle[data-scanner-status][data-url]').forEach(el => {
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
                        aktualisiereStatusBadge(el, _STUFE_ZU_STATUS[stufe]);
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
                aktualisiereStatusBadge(el, s.scanner_status);
            }

            // Ockergelb: Lebenslauf oder Notizen vorhanden, aber kein Status gesetzt
            const hatAktivitaet = el.dataset.hatLebenslauf === '1' || !!s.kommentar;
            if (hatAktivitaet && !stufe) {
                el.classList.add('mit-aktivitaet');
            } else {
                el.classList.remove('mit-aktivitaet');
            }
        });
        const statAbsagen = document.getElementById('stat-absagen');
        if (statAbsagen) {
            statAbsagen.textContent = document.querySelectorAll('.stelle.absage').length;
        }
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
                btn.textContent = '⭐ Bewertung starten';
                alert('Fehler: ' + (data.fehler || 'Unbekannt'));
            }
        } catch(e) {
            btn.disabled = false;
            btn.textContent = '⭐ Bewertung starten';
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

        const server = window.location.origin;

        const res = await fetch(server + '/stelle-einfuegen', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, firma, titel })
        });
        const data = await res.json();

        if (!data.ok) {
            statusEl.textContent = 'Fehler: ' + (data.fehler || 'Unbekannt');
            statusEl.style.color = '#e74c3c';
            return;
        }

        statusEl.textContent = 'Eingetragen - Pipeline laeuft...';
        statusEl.style.color = '#27ae60';
        output.style.display = 'block';
        output.textContent = '';

        const quelle = new EventSource(server + '/manuell-stream');
        quelle.onmessage = function(e) {
            if (e.data === 'FERTIG') {
                quelle.close();
                statusEl.textContent = 'Fertig - Seite wird neu geladen...';
                setTimeout(() => location.reload(), 2000);
                return;
            }
            output.textContent += e.data + '\n';
            output.scrollTop = output.scrollHeight;
        };
        quelle.onerror = function() {
            quelle.close();
            statusEl.textContent = 'Verbindungsfehler zum Server';
            statusEl.style.color = '#e74c3c';
        };
    }

    async function neuLadenUndBewerten(btn, stellenUrl) {
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
                btn.textContent = '🔄 Neu laden & bewerten';
                alert('Fehler: ' + (data.fehler || 'Unbekannt'));
                return;
            }
        } catch(e) {
            btn.disabled = false;
            btn.textContent = '🔄 Neu laden & bewerten';
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
    function toggleNichtBewertet(checked) {
        _nurNichtBewertet = checked;
        _aktualisiere();
    }
    function toggleVorgemerkt(checked) {
        _nurVorgemerkt = checked;
        _aktualisiere();
    }
    function setzeFilter(filter) {
        _aktiverFilter = (_aktiverFilter === filter && filter !== null) ? null : filter;
        _aktualisiere();
    }
    function setzeSortierung(sort) {
        _aktiveSortierung = (_aktiveSortierung === sort && sort !== null) ? null : sort;
        _aktualisiere();
    }
    function setzeStatusFilter(status) {
        _aktiverStatusFilter = (_aktiverStatusFilter === status) ? null : status;
        _aktualisiere();
    }
    function setzeFirmaFilter(firma) {
        _aktiverFirmaFilter = firma || null;
        _aktualisiere();
    }
    function _aktualisiere() {
        _aktualisiereFilterBtns();
        const brauchtFlach = _aktiverFilter !== null || _aktiveSortierung !== null || _aktiverStatusFilter !== null || _aktiverFirmaFilter !== null || _nurNichtBewertet || _nurVorgemerkt;
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
    }
    function _aktualisiereFlach() {
        const fa = document.getElementById('flat-ansicht');
        _stellenUrsprung.forEach(({el}) => {
            if (el.parentNode === fa) fa.removeChild(el);
        });
        let gefiltert = _stellenUrsprung.map(o => o.el);
        if (_aktiverFirmaFilter !== null) {
            gefiltert = gefiltert.filter(el => el.dataset.firma === _aktiverFirmaFilter);
        }
        if (_aktiverFilter !== null) {
            gefiltert = gefiltert.filter(el => el.classList.contains(_aktiverFilter));
            // Vergebene/gelöschte Stellen aus Stufen-Filtern ausschließen
            const _inaktiveStatus = new Set(INAKTIVE_STATUS);
            gefiltert = gefiltert.filter(el => {
                const s = parseInt(el.dataset.scannerStatus);
                return isNaN(s) || !_inaktiveStatus.has(s);
            });
        }
        if (_aktiverStatusFilter !== null) {
            gefiltert = gefiltert.filter(el => parseInt(el.dataset.scannerStatus) === _aktiverStatusFilter);
        }
        // Bei gesetztem Firmen-Filter (oder Vorgemerkt-Ansicht) sollen alle
        // betroffenen Stellen sichtbar sein – Geringer-Match/Zu-weit nur
        // ausblenden, wenn keine Firma gewählt ist.
        const zeigeGM = document.getElementById('cb-geringer-match')?.checked || _aktiverFirmaFilter !== null || _nurVorgemerkt;
        if (!zeigeGM) {
            gefiltert = gefiltert.filter(el => !el.dataset.geringerMatch);
        }
        const zeigeZW = document.getElementById('cb-zu-weit')?.checked || _aktiverFirmaFilter !== null || _nurVorgemerkt;
        if (!zeigeZW) {
            gefiltert = gefiltert.filter(el => !el.dataset.zuWeit);
        }
        if (_nurNichtBewertet) {
            const _unbewerteterStatus = new Set(UNBEWERTETE_STATUS);
            gefiltert = gefiltert.filter(el => _unbewerteterStatus.has(parseInt(el.dataset.scannerStatus)));
        }
        if (_nurVorgemerkt) {
            gefiltert = gefiltert.filter(el => el.dataset.vorgemerkt === '1');
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
        info.textContent = [firmaText, filterText, statusText, sortText, nichtBewertetText, vorgemerktText].filter(Boolean).join(' · ')
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
            const el = document.querySelector(`[data-url="${CSS.escape(url)}"]`);
            if (el) aktualisiereStatusBadge(el, 10);
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
                const b = el.querySelector('.passend-toggle');
                if (b) _setzePassendBtn(b, url, passend);
            });
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
