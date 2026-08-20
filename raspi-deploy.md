# Deployment auf dem Raspi

Ziel: Änderungen aus diesem GitHub-Repo (`SaschRttr/JobScanner`) einfacher auf den
Raspi (`/home/sascha/Jobsuche`, Services `jobscanner.service` + `dashboard.service`)
bekommen.

Aktueller Stand: Code liegt auf dem Pi bisher nur manuell kopiert, kein Git.

## Phase 1 – manuell (jetzt umsetzen)

### 1. Deploy-Key für GitHub erzeugen (auf dem Pi, per SSH)

```bash
ssh-keygen -t ed25519 -C "raspi-jobscanner-deploy" -f ~/.ssh/id_ed25519_deploy -N ""
cat ~/.ssh/id_ed25519_deploy.pub
```

Public Key kopieren → GitHub-Repo **JobScanner** → *Settings* → *Deploy keys* →
*Add deploy key* einfügen (Read-only reicht, kein "Allow write access").

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
    IdentityFile ~/.ssh/id_ed25519_deploy
    IdentitiesOnly yes
EOF
```

Testen: `ssh -T git@github.com` → sollte "successfully authenticated" melden.

### 2. Bestehenden Code durch Git-Checkout ersetzen

```bash
sudo systemctl stop jobscanner.service dashboard.service
mv /home/sascha/Jobsuche /home/sascha/Jobsuche.bak   # Sicherheitsnetz
git clone git@github.com:SaschRttr/JobScanner.git /home/sascha/Jobsuche
cd /home/sascha/Jobsuche
git checkout master
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Danach alles, was nicht in Git ist (config.txt mit Secrets, .env, Datenbank-Dateien,
ggf. sonstige lokale Daten), aus `Jobsuche.bak` zurückkopieren.

```bash
sudo systemctl start jobscanner.service dashboard.service
```

### 3. Ab jetzt manuell updaten

Wenn hier gepusht wurde, auf dem Pi:

```bash
cd /home/sascha/Jobsuche
git pull origin master
sudo systemctl restart jobscanner.service dashboard.service
```

## Phase 2 – automatisch (später, wenn Phase 1 zuverlässig läuft)

Cronjob, der alle paar Minuten prüft, ob es neue Commits gibt, und bei Bedarf
selbst pullt + Services neu startet. Kein offener Port am Pi nötig.

### Deploy-Script

```bash
cat > /home/sascha/Jobsuche/deploy.sh <<'EOF'
#!/bin/bash
cd /home/sascha/Jobsuche
git fetch origin
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/master)
if [ "$LOCAL" != "$REMOTE" ]; then
    git pull origin master
    source venv/bin/activate
    pip install -r requirements.txt -q
    sudo systemctl restart jobscanner.service dashboard.service
    echo "$(date): deployed $REMOTE" >> /home/sascha/Jobsuche/deploy.log
fi
EOF
chmod +x /home/sascha/Jobsuche/deploy.sh
```

### Passwortlosen Service-Restart erlauben

```bash
sudo visudo -f /etc/sudoers.d/jobscanner-deploy
```

Inhalt:

```
sascha ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart jobscanner.service, /usr/bin/systemctl restart dashboard.service
```

### Cronjob (Prüfung alle 5 Minuten)

```bash
crontab -e
```

Zeile hinzufügen:

```
*/5 * * * * /home/sascha/Jobsuche/deploy.sh
```

Damit zieht sich der Pi alle 5 Minuten automatisch neue Commits von `master` und
startet die Services neu, falls sich etwas geändert hat.
