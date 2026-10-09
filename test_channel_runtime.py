import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from channel_runtime import provision_personal_bridge

class RuntimeCheck(unittest.TestCase):
    def test_isolated_container_config_and_secret_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            def docker(args, **kwargs):
                calls.append(args)
                if args[1]=='inspect': return SimpleNamespace(returncode=1)
                if args[1]=='port': return SimpleNamespace(returncode=0,stdout='127.0.0.1:41234\n')
                return SimpleNamespace(returncode=0)
            with patch.dict(os.environ,{'WHATSAPP_PERSONAL_API_KEY':'fixture-key','WHATSAPP_PERSONAL_PROXY':'http://local-proxy:7897'},clear=True),patch('channel_runtime.subprocess.run',side_effect=docker):
                config=provision_personal_bridge(Path(directory)/'app.db','a'*24,{})
            self.assertEqual(config['session'],'default')
            self.assertEqual(config['api_base'],'http://127.0.0.1:41234')
            file=Path(directory)/'connection-runtime'/('a'*24)/'bridge.env'
            self.assertEqual(file.stat().st_mode&0o777,0o600)
            self.assertIn('WHATSAPP_DEFAULT_ENGINE=NOWEB',file.read_text())
            self.assertIn('WHATSAPP_PROXY_SERVER=',file.read_text())
            command=next(c for c in calls if c[1]=='run')
            self.assertIn('127.0.0.1::3000',command)
            self.assertNotIn('fixture-key',' '.join(command))

if __name__=='__main__':unittest.main()
