"""Test-only worker barrier. Never shipped in the production API/worker path."""
import os
from pathlib import Path
import time
from procureflow.app import app
from procureflow.worker import drain_once
from procureflow.erp import MockERP
s=app.state.settings
if s.mode!='demo' or s.erp_mode!='mock':
    raise RuntimeError('Killpoint fixture only accepts the disposable mock environment')
marker=Path(os.environ['PF_TEST_KILL_MARKER'])
point=os.environ['PF_TEST_KILL_POINT']
class BarrierERP(MockERP):
    def pause(self):
        marker.write_text(point)
        while True: time.sleep(.1)
    def find(self, key):
        result=super().find(key)
        if point=='before-create': self.pause()
        return result
    def create_draft(self, key, payload):
        result=super().create_draft(key,payload)
        if point=='after-commit': self.pause()
        return result
app.state.service.erp=BarrierERP(s.data_dir/'mock-erp.sqlite3')
drain_once()
