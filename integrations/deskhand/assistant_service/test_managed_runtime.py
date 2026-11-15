import tempfile
import unittest
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from deskhand_runtime import ManagedRuntime


class ManagedRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt" and os.environ.get("DESKHAND_TEST_REAL_RUNTIME") == "1", "optional real Windows runtime test")
    def test_owned_runtime_exits_if_assistant_process_dies(self):
        import ctypes
        from ctypes import wintypes
        owner = subprocess.Popen([sys.executable, "-u", "-c", "from deskhand_runtime import ManagedRuntime; import time; r=ManagedRuntime(); r.start(); print(r.process.pid, flush=True); time.sleep(60)"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        handle = None
        try:
            pid_text = owner.stdout.readline().strip()
            self.assertTrue(pid_text.isdigit(), "runtime fixture did not start")
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x00100000, False, int(pid_text))
            self.assertTrue(handle, "could not observe the owned runtime process")
            owner.kill(); owner.wait(timeout=10)
            self.assertEqual(kernel.WaitForSingleObject(handle, 10000), 0, "runtime survived its owner")
        finally:
            if owner.poll() is None: owner.kill(); owner.wait(timeout=10)
            if handle: kernel.CloseHandle(handle)
            if owner.stdout: owner.stdout.close()
            if owner.stderr: owner.stderr.close()

    def test_missing_runtime_is_reported_without_starting(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = ManagedRuntime(Path(folder) / "missing.exe")
            with self.assertRaisesRegex(RuntimeError, "runtime is not installed"):
                runtime.start()

    def test_owned_process_starts_once_and_is_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "ollama.exe"
            executable.touch()
            process = MagicMock()
            process.poll.return_value = None
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b'{"version":"fixture"}'
            client = MagicMock()
            client.open.return_value = response
            with patch("deskhand_runtime.subprocess.Popen", return_value=process) as launch, patch("deskhand_runtime.build_opener", return_value=client), patch("deskhand_runtime._make_kill_job", return_value=None):
                runtime = ManagedRuntime(executable)
                endpoint = runtime.start()
                self.assertTrue(endpoint.startswith("http://127.0.0.1:"))
                self.assertEqual(runtime.start(), endpoint)
                launch.assert_called_once()
                self.assertEqual(launch.call_args.args[0], [str(executable), "serve"])
                self.assertTrue(launch.call_args.kwargs["env"]["OLLAMA_HOST"].startswith("127.0.0.1:"))
                runtime.close()
                process.terminate.assert_called_once()
                process.wait.assert_called_once()

    def test_exited_runtime_is_restarted_and_old_job_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "ollama.exe"
            executable.touch()
            first, second = MagicMock(), MagicMock()
            first.poll.side_effect = [None, 1]
            second.poll.return_value = None
            first_job, second_job = MagicMock(), MagicMock()
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b'{"version":"fixture"}'
            client = MagicMock(); client.open.return_value = response
            with patch("deskhand_runtime.subprocess.Popen", side_effect=[first, second]), patch("deskhand_runtime.build_opener", return_value=client), patch("deskhand_runtime._make_kill_job", side_effect=[first_job, second_job]):
                runtime = ManagedRuntime(executable)
                runtime.start()
                runtime.start()
                first_job.assert_called_once()
                runtime.close()
                second_job.assert_called_once()

    def test_model_availability_does_not_substitute_another_model(self):
        runtime = ManagedRuntime(Path("unused"))
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"models":[{"name":"available:latest"}]}'
        client = MagicMock(); client.open.return_value = response
        with patch.object(runtime, "start", return_value="http://127.0.0.1:1234"), patch("deskhand_runtime.build_opener", return_value=client):
            runtime.require_model("available")
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                runtime.require_model("missing")
            with self.assertRaises(ValueError):
                runtime.require_model(" ")


if __name__ == "__main__": unittest.main()
