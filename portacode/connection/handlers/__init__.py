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
from .update_handler import UpdatePortacodeHandler
from .file_handlers import (
    FileReadHandler,
    FileWriteHandler,
    DirectoryListHandler,
    FileInfoHandler,
    FileDeleteHandler,
    FileCreateHandler,
    FolderCreateHandler,
    FileRenameHandler,
    FileSearchHandler,
    ContentRequestHandler,
)
from .diff_handlers import FileApplyDiffHandler, FilePreviewDiffHandler
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
from .proxmox_infra import (
    ConfigureProxmoxInfraHandler,
    CreateProxmoxContainerHandler,
    RevertProxmoxInfraHandler,
    StartPortacodeServiceHandler,
    StartProxmoxContainerHandler,
    StopProxmoxContainerHandler,
    RemoveProxmoxContainerHandler,
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
    "ConfigureProxmoxInfraHandler",
    "CreateProxmoxContainerHandler",
    # File operation handlers (optional - register as needed)
    "FileReadHandler",
    "FileWriteHandler", 
    "DirectoryListHandler",
    "FileInfoHandler",
    "FileDeleteHandler",
    "FileCreateHandler",
    "FolderCreateHandler",
    "FileRenameHandler",
    "FileSearchHandler",
    "ContentRequestHandler",
    "FileApplyDiffHandler",
    "FilePreviewDiffHandler",
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
    "StartPortacodeServiceHandler",
    "StartProxmoxContainerHandler",
    "StopProxmoxContainerHandler",
    "RemoveProxmoxContainerHandler",
    "UpdatePortacodeHandler",
    "RevertProxmoxInfraHandler",
] 
