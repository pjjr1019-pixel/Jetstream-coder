import concurrent.futures
import threading
import unittest

from deskhand_host_rpc import HostRpc


class HostRpcTests(unittest.TestCase):
    def test_correlated_result_and_duplicate_ignored(self):
        emitted = []
        ready = threading.Event()
        rpc = HostRpc(lambda message: (emitted.append(message), ready.set()))
        with concurrent.futures.ThreadPoolExecutor() as pool:
            waiting = pool.submit(rpc.call, "action.execute", {"name": "desktop.list"})
            self.assertTrue(ready.wait(1))
            self.assertFalse(rpc.receive("wrong", {}))
            self.assertTrue(rpc.receive(emitted[0]["host_call"], {"status": "completed"}))
            self.assertFalse(rpc.receive(emitted[0]["host_call"], {}))
            self.assertEqual(waiting.result(1), {"status": "completed"})
        rpc.close()

    def test_lost_result_is_uncertain_and_not_replayed(self):
        emitted = []
        rpc = HostRpc(emitted.append, timeout=.01)
        with self.assertRaisesRegex(TimeoutError, "uncertain"):
            rpc.call("action.execute", {})
        self.assertEqual(len(emitted), 1)
        self.assertFalse(rpc.receive(emitted[0]["host_call"], {}))
        rpc.close()

    def test_close_unblocks_pending_request(self):
        ready = threading.Event()
        rpc = HostRpc(lambda _: ready.set())
        with concurrent.futures.ThreadPoolExecutor() as pool:
            waiting = pool.submit(rpc.call, "task.begin", {})
            self.assertTrue(ready.wait(1))
            rpc.close()
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                waiting.result(1)
