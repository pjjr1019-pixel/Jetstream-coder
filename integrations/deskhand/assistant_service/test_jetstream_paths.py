"""Embedded paths are explicit and preserve caller-owned configuration."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from deskhand_task_agent import TaskAgentClient
from deskhand_task_agent_lifecycle import TaskAgentLifecycle


class EmbeddedTaskAgentPathTests(unittest.TestCase):
    def test_bundle_directory_is_used_by_client_and_lifecycle(self):
        with patch.dict(os.environ, {'DESKHAND_TASK_AGENT_DIR': 'embedded/TaskAgent'}):
            client = TaskAgentClient()
            lifecycle = TaskAgentLifecycle()
            self.assertEqual(Path(client.task_agent_dir), Path('embedded/TaskAgent'))
            self.assertEqual(lifecycle.task_agent_dir, Path('embedded/TaskAgent'))
            self.assertEqual(Path(client.token_source), Path('embedded/TaskAgent/agent_config.json'))

    def test_explicit_configuration_takes_precedence(self):
        with patch.dict(os.environ, {'DESKHAND_TASK_AGENT_DIR': 'embedded'}):
            client = TaskAgentClient(task_agent_dir='explicit', token_source='secret.json')
            lifecycle = TaskAgentLifecycle(task_agent_dir='explicit')
            self.assertEqual(client.task_agent_dir, 'explicit')
            self.assertEqual(client.token_source, 'secret.json')
            self.assertEqual(lifecycle.task_agent_dir, Path('explicit'))
