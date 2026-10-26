"""Production entry establishes a private data root before any service or socket."""
import asyncio,importlib.util,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('background_bootstrap',ROOT/'native/FridayDesktop/backend/desktop_backend.py')
backend=importlib.util.module_from_spec(spec);spec.loader.exec_module(backend)
import uvicorn
from homeplane.store import local_database_path

class Boot(unittest.IsolatedAsyncioTestCase):
 async def test_persistent_entry_works_from_unrelated_working_directory(self):
  previous=Path.cwd()
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder).resolve();base=root/'private-home';unrelated=root/'other';unrelated.mkdir()
   observed=[]
   def service(**kwargs):
    observed.append(Path.cwd())
    assert local_database_path(base/'data/desktop/people.sqlite').is_relative_to(base)
    return SimpleNamespace(runtime_log=kwargs['runtime_log'])
   class Server:
    def __init__(self,config):self.config=config
    async def serve(self):observed.append(Path.cwd())
   try:
    os.chdir(unrelated)
    with patch.object(backend,'BASE',base),patch.object(backend,'STATE',base/'data/desktop'),patch.object(backend,'DesktopService',service),patch.object(uvicorn,'Server',Server):
     await backend.serve(persistent=True)
    self.assertEqual(observed,[base,base])
    self.assertEqual((base/'data/desktop').stat().st_mode & 0o777,0o700)
   finally:os.chdir(previous)

if __name__=='__main__':unittest.main(verbosity=2)
