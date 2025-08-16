"""Modular command handler system for Portacode client.

This package provides a flexible system for handling commands from the gateway.
Handlers can be easily added, removed, or modified without touching the main
terminal manager code.
"""

from .base import BaseHandler, AsyncHandler, SyncHandler
from .registry import CommandRegistry
from .terminal_handlers import (
    TerminalStartHandler,
    TerminalSendHandler,
    TerminalStopHandler,
    TerminalListHandler,
)
from .system_handlers import SystemInfoHandler
from .file_handlers import (
    FileReadHandler,
    FileWriteHandler,
    DirectoryListHandler,
    FileInfoHandler,
    FileDeleteHandler,
    FileCreateHandler,
    FolderCreateHandler,
    FileRenameHandler,
    ContentRequestHandler,
)
from .project_state_handlers import (
    ProjectStateFolderExpandHandler,
    ProjectStateFolderCollapseHandler,
    ProjectStateFileOpenHandler,
    ProjectStateTabCloseHandler,
    ProjectStateSetActiveTabHandler,
    ProjectStateDiffOpenHandler,
    ProjectStateDiffContentHandler,
    ProjectStateGitStageHandler,
    ProjectStateGitUnstageHandler,
    ProjectStateGitRevertHandler,
    ProjectStateGitCommitHandler,
)

__all__ = [
    "BaseHandler",
    "AsyncHandler", 
    "SyncHandler",
    "CommandRegistry",
    "TerminalStartHandler",
    "TerminalSendHandler",
    "TerminalStopHandler",
    "TerminalListHandler",
    "SystemInfoHandler",
    # File operation handlers (optional - register as needed)
    "FileReadHandler",
    "FileWriteHandler", 
    "DirectoryListHandler",
    "FileInfoHandler",
    "FileDeleteHandler",
    "FileCreateHandler",
    "FolderCreateHandler",
    "FileRenameHandler",
    "ContentRequestHandler",
    # Project state handlers
    "ProjectStateFolderExpandHandler",
    "ProjectStateFolderCollapseHandler",
    "ProjectStateFileOpenHandler",
    "ProjectStateTabCloseHandler",
    "ProjectStateSetActiveTabHandler",
    "ProjectStateDiffOpenHandler",
    "ProjectStateDiffContentHandler",
    "ProjectStateGitStageHandler",
    "ProjectStateGitUnstageHandler",
    "ProjectStateGitRevertHandler",
    "ProjectStateGitCommitHandler",
] 