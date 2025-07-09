# Portacode Handler System

This directory contains the modular command handler system for the Portacode client. The system provides a clean, extensible architecture for processing commands from the gateway.

## Architecture Overview

The handler system consists of:

- **Base Handler Classes**: `BaseHandler`, `AsyncHandler`, `SyncHandler`
- **Command Registry**: `CommandRegistry` for managing and dispatching handlers
- **Session Management**: `SessionManager` for terminal session lifecycle
- **Specific Handlers**: Individual command implementations

## Existing Handlers

### Terminal Handlers (`terminal_handlers.py`)

1. **`TerminalStartHandler`** - `terminal_start`
   - Starts new terminal sessions
   - Supports shell and cwd parameters
   - Handles both Windows (ConPTY) and Unix (PTY) systems

2. **`TerminalSendHandler`** - `terminal_send` 
   - Sends data to existing terminal sessions
   - Requires terminal_id and data parameters

3. **`TerminalStopHandler`** - `terminal_stop`
   - Terminates terminal sessions
   - Cleans up resources and sends exit events

4. **`TerminalListHandler`** - `terminal_list`
   - Lists all active terminal sessions
   - Returns session metadata and buffer contents

### System Handlers (`system_handlers.py`)

1. **`SystemInfoHandler`** - `system_info`
   - Provides system resource information
   - Returns CPU, memory, and disk usage data

## Adding New Handlers

### 1. Asynchronous Handlers

For commands that need to perform I/O operations, use `AsyncHandler`:

```python
from .base import AsyncHandler
from typing import Any, Dict

class FileReadHandler(AsyncHandler):
    """Handler for reading file contents."""
    
    @property
    def command_name(self) -> str:
        return "file_read"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Read file contents asynchronously."""
        file_path = message.get("path")
        if not file_path:
            raise ValueError("path parameter is required")
        
        # Async file operations
        import aiofiles
        async with aiofiles.open(file_path, 'r') as f:
            content = await f.read()
        
        return {
            "event": "file_read_response",
            "path": file_path,
            "content": content,
        }
```

### 2. Synchronous Handlers

For CPU-bound operations or simple synchronous tasks, use `SyncHandler`:

```python
from .base import SyncHandler
from typing import Any, Dict
import os

class DirectoryListHandler(SyncHandler):
    """Handler for listing directory contents."""
    
    @property
    def command_name(self) -> str:
        return "directory_list"
    
    def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """List directory contents synchronously."""
        path = message.get("path", ".")
        
        try:
            items = []
            for item in os.listdir(path):
                item_path = os.path.join(path, item)
                items.append({
                    "name": item,
                    "is_dir": os.path.isdir(item_path),
                    "size": os.path.getsize(item_path) if os.path.isfile(item_path) else 0,
                })
            
            return {
                "event": "directory_list_response",
                "path": path,
                "items": items,
            }
        except Exception as e:
            raise RuntimeError(f"Failed to list directory: {e}")
```

### 3. Complex Handler with Context Access

Handlers can access shared context and services:

```python
from .base import AsyncHandler
from typing import Any, Dict

class ProcessManagementHandler(AsyncHandler):
    """Handler for managing processes on the device."""
    
    @property 
    def command_name(self) -> str:
        return "process_management"
    
    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Manage processes with access to session manager."""
        action = message.get("action")  # "list", "kill", "start"
        
        # Access session manager from context
        session_manager = self.context.get("session_manager")
        if not session_manager:
            raise RuntimeError("Session manager not available")
        
        if action == "list":
            import psutil
            processes = []
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent']):
                try:
                    processes.append(proc.info)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            return {
                "event": "process_list_response",
                "processes": processes,
            }
        
        elif action == "kill":
            pid = message.get("pid")
            if not pid:
                raise ValueError("pid parameter required for kill action")
            
            import psutil
            try:
                process = psutil.Process(pid)
                process.terminate()
                return {
                    "event": "process_killed",
                    "pid": pid,
                }
            except psutil.NoSuchProcess:
                raise ValueError(f"Process {pid} not found")
        
        else:
            raise ValueError(f"Unknown action: {action}")
```

### 4. Handler with Custom Response Handling

You can override the `handle` method for custom response behavior:

```python
from .base import BaseHandler
from typing import Any, Dict, Optional

class StreamingHandler(BaseHandler):
    """Handler that streams responses."""
    
    @property
    def command_name(self) -> str:
        return "streaming_data"
    
    async def handle(self, message: Dict[str, Any], reply_channel: Optional[str] = None) -> None:
        """Handle streaming data with custom response handling."""
        try:
            # Send initial response
            await self.send_response({
                "event": "streaming_started",
                "message": "Starting data stream",
            }, reply_channel)
            
            # Stream data
            for i in range(10):
                await asyncio.sleep(0.1)
                await self.send_response({
                    "event": "streaming_data",
                    "chunk": i,
                    "data": f"Data chunk {i}",
                }, reply_channel)
            
            # Send completion
            await self.send_response({
                "event": "streaming_completed",
                "message": "Stream completed",
            }, reply_channel)
            
        except Exception as exc:
            await self.send_error(str(exc), reply_channel)
```

## Registering Handlers

### 1. Automatic Registration

Add your handler to the `__init__.py` file and register it in `TerminalManager._register_default_handlers()`:

```python
# In __init__.py
from .file_handlers import FileReadHandler, DirectoryListHandler

# In terminal.py _register_default_handlers()
def _register_default_handlers(self) -> None:
    """Register the default command handlers."""
    # ... existing handlers ...
    self._command_registry.register(FileReadHandler)
    self._command_registry.register(DirectoryListHandler)
```

### 2. Dynamic Registration

Register handlers at runtime:

```python
# In your code
terminal_manager.register_handler(MyCustomHandler)

# List all registered commands
commands = terminal_manager.list_commands()
print(f"Available commands: {commands}")

# Unregister if needed
terminal_manager.unregister_handler("my_command")
```

## Handler Guidelines

### Best Practices

1. **Command Naming**: Use descriptive, action-oriented names (`file_read`, `system_info`)
2. **Error Handling**: Always validate input parameters and provide meaningful error messages
3. **Response Format**: Use consistent response formats with `event` field
4. **Resource Management**: Clean up resources in exception handlers
5. **Logging**: Use appropriate logging levels for debugging and monitoring

### Parameter Validation

```python
def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
    # Validate required parameters
    required_params = ["param1", "param2"]
    for param in required_params:
        if param not in message:
            raise ValueError(f"Missing required parameter: {param}")
    
    # Validate parameter types
    if not isinstance(message.get("count"), int):
        raise ValueError("count parameter must be an integer")
    
    # Your logic here...
```

### Error Handling

```python
async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
    try:
        # Your logic here
        result = await some_async_operation()
        return {"event": "success", "result": result}
    except FileNotFoundError:
        raise ValueError("File not found")
    except PermissionError:
        raise RuntimeError("Permission denied")
    except Exception as e:
        raise RuntimeError(f"Unexpected error: {e}")
```

## Testing Handlers

### Unit Testing

```python
import pytest
from unittest.mock import Mock, AsyncMock
from portacode.connection.handlers.your_handler import YourHandler

@pytest.mark.asyncio
async def test_your_handler():
    # Mock control channel
    control_channel = AsyncMock()
    
    # Mock context
    context = {"session_manager": Mock()}
    
    # Create handler
    handler = YourHandler(control_channel, context)
    
    # Test execute
    message = {"param1": "value1"}
    result = await handler.execute(message)
    
    assert result["event"] == "expected_event"
    assert result["data"] == "expected_data"
```

### Integration Testing

```python
# Test with real TerminalManager
terminal_manager = TerminalManager(mock_multiplexer)
terminal_manager.register_handler(YourHandler)

# Simulate command
message = {"cmd": "your_command", "param1": "value1"}
# Process through control loop...
```

## Communication Protocol

### Command Format

Commands from the gateway follow this format:

```json
{
  "cmd": "command_name",
  "param1": "value1", 
  "param2": "value2",
  "reply_channel": "optional_reply_channel"
}
```

### Response Format

Responses to the gateway should follow this format:

```json
{
  "event": "event_name",
  "data": "response_data",
  "reply_channel": "reply_channel_if_provided"
}
```

### Error Format

Error responses:

```json
{
  "event": "error",
  "message": "Error description",
  "reply_channel": "reply_channel_if_provided"
}
```

## Advanced Topics

### Custom Context

You can extend the context with custom services:

```python
# In TerminalManager._set_mux()
self._context = {
    "session_manager": self._session_manager,
    "mux": mux,
    "file_manager": FileManager(),
    "process_manager": ProcessManager(),
}
```

### Handler Chaining

Handlers can call other handlers:

```python
async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
    # Get another handler
    other_handler = self.context.get("command_registry").get_handler("other_command")
    if other_handler:
        intermediate_result = await other_handler.execute(message)
        # Process intermediate result...
    
    return final_result
```

### Middleware Pattern

Create middleware handlers that wrap other handlers:

```python
class LoggingMiddleware(BaseHandler):
    def __init__(self, control_channel, context, wrapped_handler):
        super().__init__(control_channel, context)
        self.wrapped_handler = wrapped_handler
    
    async def handle(self, message, reply_channel=None):
        logger.info(f"Executing command: {message.get('cmd')}")
        start_time = time.time()
        
        try:
            result = await self.wrapped_handler.handle(message, reply_channel)
            duration = time.time() - start_time
            logger.info(f"Command completed in {duration:.2f}s")
            return result
        except Exception as e:
            logger.error(f"Command failed: {e}")
            raise
```

This modular system provides a clean, extensible architecture for adding new functionality to the Portacode client while maintaining backward compatibility and code organization. 