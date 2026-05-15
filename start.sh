#!/bin/bash
# Job-Scanner Pipeline
cd "$(dirname "$0")"
source .venv/bin/activate

python scanner2.py
python rohtext_holen2.py
python vergaben_check.py
python extraktor.py
python bewertung.py
python report.py
