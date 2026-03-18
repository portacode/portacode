import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from portacode.connection.handlers.project_state.handlers import (
    ProjectStateGitCommitHandler,
    ProjectStateGitStageHandler,
)


class ProjectStateGitHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage_handler_returns_command_specific_error_response(self):
        manager = SimpleNamespace(
            git_managers={"session-1": SimpleNamespace(stage_file=self._raise_runtime_error("stage failed"))},
            _refresh_project_state=AsyncMock(),
        )
        handler = ProjectStateGitStageHandler(control_channel=AsyncMock(), context={})

        with patch(
            "portacode.connection.handlers.project_state.handlers.get_or_create_project_state_manager",
            return_value=manager,
        ):
            response = await handler.execute(
                {
                    "project_id": "project-1",
                    "source_client_session": "session-1",
                    "file_path": "/repo/README.md",
                }
            )

        self.assertEqual(response["event"], "project_state_git_stage_response")
        self.assertFalse(response["success"])
        self.assertEqual(response["error"], "stage failed")
        self.assertEqual(response["file_path"], "/repo/README.md")
        manager._refresh_project_state.assert_not_awaited()

    async def test_commit_handler_returns_command_specific_error_response(self):
        manager = SimpleNamespace(
            git_managers={"session-1": SimpleNamespace(commit_changes=self._raise_runtime_error("commit failed"))},
            _refresh_project_state=AsyncMock(),
        )
        handler = ProjectStateGitCommitHandler(control_channel=AsyncMock(), context={})

        with patch(
            "portacode.connection.handlers.project_state.handlers.get_or_create_project_state_manager",
            return_value=manager,
        ):
            response = await handler.execute(
                {
                    "project_id": "project-1",
                    "source_client_session": "session-1",
                    "commit_message": "Test commit",
                }
            )

        self.assertEqual(response["event"], "project_state_git_commit_response")
        self.assertFalse(response["success"])
        self.assertEqual(response["error"], "commit failed")
        self.assertEqual(response["commit_message"], "Test commit")
        self.assertIsNone(response["commit_hash"])
        manager._refresh_project_state.assert_not_awaited()

    @staticmethod
    def _raise_runtime_error(message):
        def _raise(*_args, **_kwargs):
            raise RuntimeError(message)

        return _raise


if __name__ == "__main__":
    unittest.main()
