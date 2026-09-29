import sys, json
sys.path.insert(0, 'src')
from model_benchmarker.webapp.app import _catalog_payload
cat = _catalog_payload()
for e in cat:
    nm = str(e.get('served_name', ''))
    if 'V4' in nm or 'v4' in nm:
        args = ' '.join(str(a) for a in (e.get('launch_args') or []))
        print(nm[:50], '|', args[:160])
