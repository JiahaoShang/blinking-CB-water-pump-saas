import os
import sys
import unittest
from pathlib import Path

for key in tuple(os.environ):
    if key.startswith(("DIFY_", "DEEPSEEK_", "OPENAI_")):
        os.environ.pop(key, None)
os.environ["AI_MODE"] = "rules"
sys.path.insert(0, str(Path.cwd()))
suite = unittest.defaultTestLoader.discover(".", pattern="test_*.py")
result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
