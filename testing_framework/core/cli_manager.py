"""CLI connection manager with threading and file output."""

import asyncio
import threading
import subprocess
import os
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any
import logging


class CLIManager:
    """Manages CLI connections with background threading and output redirection."""
    
    def __init__(self, test_name: str, log_dir: str = "test_results"):
        self.test_name = test_name
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        
        self.connection_thread: Optional[threading.Thread] = None
        self.cli_process: Optional[subprocess.Popen] = None
        self.is_connected = False
        self.connection_lock = threading.Lock()
        
        # Create unique log file for this test
        timestamp = int(time.time())
        self.log_file = self.log_dir / f"{self.test_name}_{timestamp}_cli.log"
        
        self.logger = logging.getLogger(f"cli_manager.{test_name}")
        self.logger.setLevel(logging.WARNING)  # Only show warnings and errors
        
    async def connect(self, debug: bool = True, timeout: int = 30) -> bool:
        """Start CLI connection in background thread with output redirection."""
        try:
            # Import the CLI function
            from portacode.cli import cli
            
            self.logger.info(f"Starting CLI connection for test: {self.test_name}")
            
            # Start connection in separate thread
            self.connection_thread = threading.Thread(
                target=self._run_cli_connection,
                args=(debug,),
                daemon=True
            )
            self.connection_thread.start()
            
            # Wait for connection to establish
            start_time = time.time()
            while not self.is_connected and (time.time() - start_time) < timeout:
                await asyncio.sleep(0.5)
                
            if self.is_connected:
                self.logger.info("CLI connection established successfully")
                return True
            else:
                self.logger.error("Failed to establish CLI connection within timeout")
                return False
                
        except Exception as e:
            self.logger.error(f"Error starting CLI connection: {e}")
            return False
    
    def _run_cli_connection(self, debug: bool = True):
        """Run CLI connection in separate thread with output redirection."""
        try:
            # Redirect stdout and stderr to log file
            with open(self.log_file, 'w') as log_file:
                log_file.write(f"=== CLI Connection Log for Test: {self.test_name} ===\\n")
                log_file.write(f"Started at: {time.ctime()}\\n")
                log_file.write("=" * 50 + "\\n\\n")
                log_file.flush()
                
                # Import and run CLI
                from portacode.cli import cli
                
                # Capture original stdout/stderr
                original_stdout = sys.stdout
                original_stderr = sys.stderr
                
                try:
                    # Redirect output to both log file and capture
                    class TeeOutput:
                        def __init__(self, file_obj, original_stream):
                            self.file_obj = file_obj
                            self.original_stream = original_stream
                            
                        def write(self, text):
                            self.file_obj.write(text)
                            self.file_obj.flush()
                            # Also write to original stream for debugging
                            if hasattr(self.original_stream, 'write'):
                                self.original_stream.write(text)
                            
                        def flush(self):
                            self.file_obj.flush()
                            if hasattr(self.original_stream, 'flush'):
                                self.original_stream.flush()
                    
                    # Set up tee outputs
                    sys.stdout = TeeOutput(log_file, original_stdout)
                    sys.stderr = TeeOutput(log_file, original_stderr)
                    
                    # Mark as connected before starting CLI
                    with self.connection_lock:
                        self.is_connected = True
                    
                    # Run CLI with connect command
                    args = ['connect', '--non-interactive']
                    if debug:
                        args.append('--debug')
                    
                    cli(args)
                    
                finally:
                    # Restore original stdout/stderr
                    sys.stdout = original_stdout
                    sys.stderr = original_stderr
                    
        except Exception as e:
            with self.connection_lock:
                self.is_connected = False
            self.logger.error(f"CLI connection failed: {e}")
            
            # Write error to log file
            try:
                with open(self.log_file, 'a') as log_file:
                    log_file.write(f"\\nERROR: {e}\\n")
            except:
                pass
    
    def get_log_content(self) -> str:
        """Get the current content of the CLI log file."""
        try:
            with open(self.log_file, 'r') as f:
                return f.read()
        except Exception as e:
            self.logger.error(f"Error reading log file: {e}")
            return ""
    
    def is_connection_active(self) -> bool:
        """Check if the CLI connection is still active."""
        with self.connection_lock:
            return self.is_connected and (
                self.connection_thread is not None and 
                self.connection_thread.is_alive()
            )
    
    async def disconnect(self):
        """Disconnect the CLI connection."""
        try:
            with self.connection_lock:
                self.is_connected = False
            
            if self.cli_process:
                self.cli_process.terminate()
                try:
                    self.cli_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.cli_process.kill()
            
            self.logger.info("CLI connection disconnected")
            
        except Exception as e:
            self.logger.error(f"Error disconnecting CLI: {e}")
    
    def get_connection_info(self) -> Dict[str, Any]:
        """Get information about the current connection."""
        return {
            "test_name": self.test_name,
            "is_connected": self.is_connection_active(),
            "log_file": str(self.log_file),
            "log_exists": self.log_file.exists(),
            "log_size": self.log_file.stat().st_size if self.log_file.exists() else 0
        }