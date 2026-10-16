"""Real disposable child-process ownership; never launches app, worker or Stripe."""
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import dev

@unittest.skipUnless(os.name=='posix','POSIX process-group contract; Windows direct-child cleanup remains limited')
class DevProcessOwnership(unittest.TestCase):
    def test_cleanup_stops_owned_descendants_and_preserves_unrelated_process(self):
        sentinel=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)
        grandchild="import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
        parent_code=f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{grandchild!r}]); time.sleep(60)"
        parent=dev.start_owned_process([sys.executable,'-c',parent_code],stdout=subprocess.PIPE,text=True)
        try:
            readable,_,_=select.select([parent.stdout],[],[],3)
            self.assertEqual(readable,[parent.stdout], 'disposable descendant did not become ready')
            self.assertEqual(parent.stdout.readline().strip(),'ready')
            self.assertEqual(os.getpgid(parent.pid),parent.pid)
            self.assertNotEqual(os.getpgrp(),os.getpgid(parent.pid))
            with patch.object(dev.os,'killpg',wraps=os.killpg) as kill:
                dev.stop_owned_processes([('fixture',parent)],grace_seconds=.2)
            self.assertTrue(kill.call_args_list)
            self.assertTrue(all(call.args[0]==parent.pid for call in kill.call_args_list))
            self.assertIsNotNone(parent.poll())
            self.assertIsNone(sentinel.poll())
            readable,_,_=select.select([parent.stdout],[],[],2)
            self.assertEqual(readable,[parent.stdout])
            self.assertEqual(parent.stdout.read(),'') # no descendant still owns the pipe
        finally:
            if parent.poll() is None:dev.stop_owned_processes([('fixture',parent)],grace_seconds=.1)
            parent.stdout.close()
            sentinel.terminate();sentinel.wait(timeout=3)

    def test_short_lived_owned_process_cleanup_is_idempotent(self):
        process=dev.start_owned_process([sys.executable,'-c','pass'])
        process.wait(timeout=3)
        dev.stop_owned_processes([('done',process)],grace_seconds=.1)
        self.assertEqual(process.returncode,0)
