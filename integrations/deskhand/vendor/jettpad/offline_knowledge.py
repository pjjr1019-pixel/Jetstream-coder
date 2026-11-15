"""Read-only retrieval from the existing checksum-validated Windows corpus."""
import importlib.util
from pathlib import Path

def retrieve(query, documents):
    root=Path(documents)/'jett-knowledge/system'
    source=root/'scripts/knowledge.py'
    if not source.is_file():return []
    spec=importlib.util.spec_from_file_location('jett_offline_knowledge',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    records=module.load(root)
    rows=module.search(records,query,module.snapshot(),Path(documents)/'jett-knowledge/field-notes.jsonl',
                       limit=3,index=root/'index.sqlite3',scope='windows',corpus='windows')
    # Search rejects missing/stale citations; retain evidence and applicability.
    for row in rows:
        if row['id'] not in records:raise ValueError('Knowledge citation does not exist')
    return rows
