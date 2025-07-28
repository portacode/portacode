# WebSocket Communication Protocol

This document outlines the WebSocket communication protocol between the Portacode server and the connected client devices.

## Table of Contents

- [Raw Message Format](#raw-message-format)
- [Actions](#actions)
  - [Terminal Actions](#terminal-actions)
    - [`terminal_start`](#terminal_start)
    - [`terminal_send`](#terminal_send)
    - [`terminal_stop`](#terminal_stop)
    - [`terminal_list`](#terminal_list)
  - [System Actions](#system-actions)
    - [`system_info`](#system_info)
  - [File Actions](#file-actions)
    - [`file_read`](#file_read)
    - [`file_write`](#file_write)
    - [`directory_list`](#directory_list)
    - [`file_info`](#file_info)
    - [`file_delete`](#file_delete)
  - [Client Session Management](#client-session-management)
    - [`client_sessions_update`](#client_sessions_update)
- [Events](#events)
  - [Error Events](#error-events)
    - [`error`](#error)
  - [Terminal Events](#terminal-events)
    - [`terminal_started`](#terminal_started)
    - [`terminal_data`](#terminal_data)
    - [`terminal_exit`](#terminal_exit)
    - [`terminal_send_ack`](#terminal_send_ack)
    - [`terminal_stopped`](#terminal_stopped)
    - [`terminal_stop_completed`](#terminal_stop_completed)
    - [`terminal_list`](#terminal_list-event)
  - [System Events](#system-events)
    - [`system_info`](#system_info-event)
  - [File Events](#file-events)
    - [`file_read_response`](#file_read_response)
    - [`file_write_response`](#file_write_response)
    - [`directory_list_response`](#directory_list_response)
    - [`file_info_response`](#file_info_response)
    - [`file_delete_response`](#file_delete_response)
  - [Client Session Events](#client-session-events)
    - [`request_client_sessions`](#request_client_sessions)
  - [Terminal Data](#terminal-data)
    - [Terminal I/O Data](#terminal_data)
  - [Server-Side Events](#server-side-events)
    - [`device_status`](#device_status)
    - [`devices`](#devices)

## Raw Message Format

All communication over the WebSocket is managed by a [multiplexer](./multiplex.py) that wraps every message in a JSON object with a `channel` and a `payload`. This allows for multiple virtual communication channels over a single connection.

**Raw Message Structure:**

```json
{
  "channel": "<channel_id>",
  "payload": {
    // This is where the Action or Event object goes
  }
}
```

*   `channel` (string|integer, mandatory): Identifies the virtual channel the message is for. When sending control commands to the device, they should be sent to channel 0 and when the device responsed to such control commands or sends system events, they will also be send on the zero channel. When a terminal session is created in the device, it is assigned a uuid, the uuid becomes the channel for communicating to that specific terminal.
*   `payload` (object, mandatory): The content of the message, which will be either an [Action](#actions) or an [Event](#events) object.

---

## Actions

Actions are messages sent from the server to the device, placed within the `payload` of a raw message. They instruct the device to perform a specific operation and are handled by the [`BaseHandler`](./base.py) and its subclasses.

**Action Structure (inside the `payload`):**

```json
{
  "command": "<command_name>",
  "payload": {
    "arg1": "value1",
    "...": "..."
  },
  "source_client_session": "channel.abc123"
}
```

*   `command` (string, mandatory): The name of the action to be executed (e.g., `terminal_start`).
*   `payload` (object, mandatory): An object containing the specific arguments for the action.
*   `source_client_session` (string, mandatory): The channel name of the client session that originated this command. This field is automatically added by the server and allows devices to identify which specific client sent the command.

**Note**: Actions do not require targeting information - responses are automatically routed using the client session management system.

### `terminal_start`

Initiates a new terminal session on the device. Handled by [`terminal_start`](./terminal_handlers.py).

**Payload Fields:**

*   `shell` (string, optional): The shell to use (e.g., `/bin/bash`). Defaults to the system's default shell.
*   `cwd` (string, optional): The working directory to start the terminal in. Defaults to the user's home directory.
*   `project_id` (string, optional): The ID of the project this terminal is associated with.

**Responses:**

*   On success, the device will respond with a [`terminal_started`](#terminal_started) event.
*   On error, a generic [`error`](#error) event is sent.

### `terminal_send`

Sends input data to a running terminal session. Handled by [`terminal_send`](./terminal_handlers.py).

**Payload Fields:**

*   `terminal_id` (string, mandatory): The ID of the terminal session to send data to.
*   `data` (string, mandatory): The data to write to the terminal's standard input.

**Responses:**

*   On success, the device will respond with a [`terminal_send_ack`](#terminal_send_ack) event.
*   On error, a generic [`error`](#error) event is sent.

### `terminal_stop`

Terminates a running terminal session. Handled by [`terminal_stop`](./terminal_handlers.py).

**Payload Fields:**

*   `terminal_id` (string, mandatory): The ID of the terminal session to stop.

**Responses:**

*   The device immediately responds with a [`terminal_stopped`](#terminal_stopped) event to acknowledge the request.
*   Once the terminal is successfully stopped, a [`terminal_stop_completed`](#terminal_stop_completed) event is sent.
*   If the terminal is not found, a [`terminal_stop_completed`](#terminal_stop_completed) with a "not_found" status is sent.

### `terminal_list`

Requests a list of all active terminal sessions. Handled by [`terminal_list`](./terminal_handlers.py).

**Payload Fields:**

*   `project_id` (string, optional): If provided, filters terminals by this project ID. If "all", lists all terminals.

**Responses:**

*   On success, the device will respond with a [`terminal_list`](#terminal_list-event) event.

### `system_info`

Requests system information from the device. Handled by [`system_info`](./system_handlers.py).

**Payload Fields:**

This action does not require any payload fields.

**Responses:**

*   On success, the device will respond with a [`system_info`](#system_info-event) event.

### `file_read`

Reads the content of a file. Handled by [`file_read`](./file_handlers.py).

**Payload Fields:**

*   `path` (string, mandatory): The absolute path to the file to read.

**Responses:**

*   On success, the device will respond with a [`file_read_response`](#file_read_response) event.
*   On error, a generic [`error`](#error) event is sent.

### `file_write`

Writes content to a file. Handled by [`file_write`](./file_handlers.py).

**Payload Fields:**

*   `path` (string, mandatory): The absolute path to the file to write to.
*   `content` (string, mandatory): The content to write to the file.

**Responses:**

*   On success, the device will respond with a [`file_write_response`](#file_write_response) event.
*   On error, a generic [`error`](#error) event is sent.

### `directory_list`

Lists the contents of a directory. Handled by [`directory_list`](./file_handlers.py).

**Payload Fields:**

*   `path` (string, optional): The path to the directory to list. Defaults to the current directory.
*   `show_hidden` (boolean, optional): Whether to include hidden files in the listing. Defaults to `false`.

**Responses:**

*   On success, the device will respond with a [`directory_list_response`](#directory_list_response) event.
*   On error, a generic [`error`](#error) event is sent.

### `file_info`

Gets information about a file or directory. Handled by [`file_info`](./file_handlers.py).

**Payload Fields:**

*   `path` (string, mandatory): The path to the file or directory.

**Responses:**

*   On success, the device will respond with a [`file_info_response`](#file_info_response) event.

### `file_delete`

Deletes a file or directory. Handled by [`file_delete`](./file_handlers.py).

**Payload Fields:**

*   `path` (string, mandatory): The path to the file or directory to delete.
*   `recursive` (boolean, optional): If `true`, recursively deletes a directory and its contents. Defaults to `false`.

**Responses:**

*   On success, the device will respond with a [`file_delete_response`](#file_delete_response) event.
*   On error, a generic [`error`](#error) event is sent.

### Client Session Management

### `client_sessions_update`

Sends updated client session information to the device. This is a special internal action used by the server to inform devices about connected client sessions.

**Payload Fields:**

*   `sessions` (array, mandatory): Array of client session objects containing connection information.

**Responses:**

This action does not generate a response event.

---

## Events

Events are messages sent from the device to the server, placed within the `payload` of a raw message. They are sent in response to an action or to notify the server of a state change.

**Event Structure (inside the `payload`):**

```json
{
  "event": "<event_name>",
  // Event-specific fields...
  "device_id": 123,
  "project_id": "<project_uuid>",
  "client_sessions": ["channel.abc123", "channel.def456"]
}
```

**Standard Fields (automatically added by the system):**

*   `event` (string, mandatory): The name of the event being sent (e.g., `terminal_started`).
*   `device_id` (integer, mandatory): The ID of the authenticated device that generated this event. **Added by the server based on the authenticated connection for security** - devices cannot self-identify.
*   `project_id` (string, optional): The project UUID associated with this event, used for project-scoped filtering. Sent by the device.
*   `client_sessions` (array, optional): Array of client session channel names that should receive this event. **Added by the device's terminal manager** based on interested client sessions. When present, the event is sent only to these specific sessions. When absent, the event is broadcast to all sessions for the device owner.

**Event-Specific Fields:**

Each event type includes additional fields specific to its purpose, documented in the individual event sections below.

### <a name="error"></a>`error`

A generic event sent when an error occurs during the execution of an action.

**Event Fields:**

*   `message` (string, mandatory): A description of the error that occurred.

### <a name="terminal_started"></a>`terminal_started`

Confirms that a new terminal session has been successfully started. Triggered by a `terminal_start` action. Handled by [`terminal_start`](./terminal_handlers.py).

**Event Fields:**

*   `terminal_id` (string, mandatory): The unique ID of the newly created terminal session.
*   `channel` (string, mandatory): The channel name for terminal I/O.
*   `project_id` (string, optional): The project ID associated with the terminal.

### <a name="terminal_data"></a>`terminal_data`

Streams real-time terminal output data from a running terminal session to connected clients. This event is automatically generated whenever the terminal process produces output (stdout/stderr). Generated by [`TerminalSession`](./session.py) through the terminal manager's session-aware messaging system.

**Event-Specific Fields:**

*   `channel` (string, mandatory): The terminal UUID identifying which terminal session produced this output. This matches the `terminal_id` from the corresponding `terminal_started` event.
*   `data` (string, mandatory): The raw terminal output data. **See detailed description below.**

**The `data` Field - Detailed Specification:**

The `data` field contains the exact bytes output by the terminal process, decoded as a UTF-8 string with error handling:

*   **Encoding**: UTF-8 with `errors="ignore"` - invalid UTF-8 sequences are silently dropped
*   **Content**: Raw terminal output including:
  - Regular command output (stdout)
  - Error messages (stderr) - combined with stdout in PTY mode  
  - ANSI escape sequences for colors, cursor positioning, screen clearing, etc.
  - Control characters (newlines, tabs, backspace, etc.)
  - Shell prompts and interactive application output
*   **Buffering**: Data is read in 1024-byte chunks from the terminal process and sent immediately (no line buffering)
*   **Binary Safety**: Binary data is handled via UTF-8 decoding with error tolerance
*   **Size**: Individual chunks are typically ≤1024 characters, but can be smaller for real-time responsiveness

**Examples of `data` content:**
```
"Hello, World!\n"                           // Simple command output
"\u001b[32mSuccess\u001b[0m\n"             // ANSI colored text  
"user@host:~/project$ "                     // Shell prompt
"\u001b[2J\u001b[H"                         // Clear screen escape sequence
"Progress: [████████████████████] 100%\r"   // Progress bar with carriage return
```

**Security Note**: The `device_id` field is automatically injected by the server based on the authenticated connection - the device cannot and should not specify its own ID. The `project_id` and `client_sessions` fields are added by the device's terminal manager for proper routing and filtering.

### <a name="terminal_exit"></a>`terminal_exit`

Notifies the server that a terminal session has terminated. This can be due to the process ending or the session being stopped. Handled by [`terminal_start`](./terminal_handlers.py).

**Event Fields:**

*   `terminal_id` (string, mandatory): The ID of the terminal session that has exited.
*   `returncode` (integer, mandatory): The exit code of the terminal process.
*   `project_id` (string, optional): The project ID associated with the terminal.

### <a name="terminal_send_ack"></a>`terminal_send_ack`

Acknowledges the receipt of a `terminal_send` action. Handled by [`terminal_send`](./terminal_handlers.py). This event carries no extra fields.

### <a name="terminal_stopped"></a>`terminal_stopped`

Acknowledges that a `terminal_stop` request has been received and is being processed. Handled by [`terminal_stop`](./terminal_handlers.py).

**Event Fields:**

*   `terminal_id` (string, mandatory): The ID of the terminal being stopped.
*   `status` (string, mandatory): The status of the stop operation (e.g., "stopping", "not_found").
*   `message` (string, mandatory): A descriptive message about the status.
*   `project_id` (string, optional): The project ID associated with the terminal.

### <a name="terminal_stop_completed"></a>`terminal_stop_completed`

Confirms that a terminal session has been successfully stopped. Handled by [`terminal_stop`](./terminal_handlers.py).

**Event Fields:**

*   `terminal_id` (string, mandatory): The ID of the stopped terminal.
*   `status` (string, mandatory): The final status ("success", "timeout", "error", "not_found").
*   `message` (string, mandatory): A descriptive message.
*   `project_id` (string, optional): The project ID associated with the terminal.

### <a name="terminal_list-event"></a>`terminal_list`

Provides the list of active terminal sessions in response to a `terminal_list` action. Handled by [`terminal_list`](./terminal_handlers.py).

**Event Fields:**

*   `sessions` (array, mandatory): A list of active terminal session objects.
*   `project_id` (string, optional): The project ID that was used to filter the list.

### <a name="system_info-event"></a>`system_info`

Provides system information in response to a `system_info` action. Handled by [`system_info`](./system_handlers.py).

**Event Fields:**

*   `info` (object, mandatory): An object containing system information, including:
    *   `cpu_percent` (float): CPU usage percentage.
    *   `memory` (object): Memory usage statistics.
    *   `disk` (object): Disk usage statistics.
    *   `os_info` (object): Operating system details, including `os_type`, `os_version`, `architecture`, `default_shell`, and `default_cwd`.

### <a name="file_read_response"></a>`file_read_response`

Returns the content of a file in response to a `file_read` action. Handled by [`file_read`](./file_handlers.py).

**Event Fields:**

*   `path` (string, mandatory): The path of the file that was read.
*   `content` (string, mandatory): The content of the file.
*   `size` (integer, mandatory): The size of the file in bytes.

### <a name="file_write_response"></a>`file_write_response`

Confirms that a file has been written successfully in response to a `file_write` action. Handled by [`file_write`](./file_handlers.py).

**Event Fields:**

*   `path` (string, mandatory): The path of the file that was written.
*   `bytes_written` (integer, mandatory): The number of bytes written to the file.
*   `success` (boolean, mandatory): Indicates whether the write operation was successful.

### <a name="directory_list_response"></a>`directory_list_response`

Returns the contents of a directory in response to a `directory_list` action. Handled by [`directory_list`](./file_handlers.py).

**Event Fields:**

*   `path` (string, mandatory): The path of the directory that was listed.
*   `items` (array, mandatory): A list of objects, each representing a file or directory in the listed directory.
*   `count` (integer, mandatory): The number of items in the `items` list.

### <a name="file_info_response"></a>`file_info_response`

Returns information about a file or directory in response to a `file_info` action. Handled by [`file_info`](./file_handlers.py).

**Event Fields:**

*   `path` (string, mandatory): The path of the file or directory.
*   `exists` (boolean, mandatory): Indicates whether the file or directory exists.
*   `is_file` (boolean, optional): Indicates if the path is a file.
*   `is_dir` (boolean, optional): Indicates if the path is a directory.
*   `is_symlink` (boolean, optional): Indicates if the path is a symbolic link.
*   `size` (integer, optional): The size of the file in bytes.
*   `modified` (float, optional): The last modification time (timestamp).
*   `accessed` (float, optional): The last access time (timestamp).
*   `created` (float, optional): The creation time (timestamp).
*   `permissions` (string, optional): The file permissions in octal format.
*   `owner_uid` (integer, optional): The user ID of the owner.
*   `group_gid` (integer, optional): The group ID of the owner.

### <a name="file_delete_response"></a>`file_delete_response`

Confirms that a file or directory has been deleted in response to a `file_delete` action. Handled by [`file_delete`](./file_handlers.py).

**Event Fields:**

*   `path` (string, mandatory): The path of the deleted file or directory.
*   `deleted_type` (string, mandatory): The type of the deleted item ("file" or "directory").
*   `success` (boolean, mandatory): Indicates whether the deletion was successful.

### Client Session Events

### <a name="request_client_sessions"></a>`request_client_sessions`

Sent by the device to request the current list of connected client sessions from the server. This is an internal event used during device initialization and reconnection.

**Event Fields:**

This event carries no additional fields.

### Terminal Data

### Terminal I/O Data Formats

Terminal I/O data can be sent in two formats depending on the implementation:

#### Modern Format (Recommended)

Terminal data is sent as a proper [`terminal_data`](#terminal_data) event on the control channel (channel 0) with client session targeting support:

```json
{
  "channel": 0,
  "payload": {
    "event": "terminal_data",
    "channel": "<terminal_uuid>",
    "data": "<terminal_output_string>",
    "device_id": 123,
    "project_id": "<project_uuid>",
    "client_sessions": ["channel.abc123", "channel.def456"]
  }
}
```

This format follows the standard event structure with automatic system field injection (device_id, project_id, client_sessions) for proper routing and security.

#### Legacy Format (Deprecated)

Terminal data sent directly on terminal channels (not on the control channel):

```json
{
  "channel": "<terminal_uuid>",
  "payload": "<terminal_output_string>"
}
```

*   Terminal output is sent as raw string data in the payload
*   Input to terminals is sent the same way but in the opposite direction  
*   No event wrapper or client targeting is used
*   This format broadcasts to all sessions for the device owner

### Server-Side Events

### <a name="device_status"></a>`device_status`

Sent by the server to clients to indicate device online/offline status changes.

**Event Fields:**

*   `device` (object, mandatory): Device status information
  *   `id` (integer, mandatory): Device ID
  *   `online` (boolean, mandatory): Whether the device is online

### <a name="devices"></a>`devices`

Sent by the server to clients to provide initial device list snapshot.

**Event Fields:**

*   `devices` (array, mandatory): Array of device objects with status information