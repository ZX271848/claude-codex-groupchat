"""Build the coordinator UI from its own source, independent of the legacy client."""
from pathlib import Path
HERE = Path(__file__).resolve().parent
(HERE / 'ui.html').write_bytes((HERE / 'ui' / 'index.html').read_bytes())
print('Coordinator UI built from ui/index.html')
