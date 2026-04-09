import json
from pathlib import Path
from collections import Counter
b = json.loads(Path('bekannte_stellen.json').read_text(encoding='utf-8'))
print(Counter(v['status'] for v in b.values()))
