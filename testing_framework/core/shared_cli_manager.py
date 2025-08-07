"""Shared CLI connection manager for the entire test session."""

import asyncio
import threading
import subprocess
import os
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any
import logging
import atexit


class SharedCLIManager:
    """Singleton CLI manager that maintains one connection for the entire test session."""
    
    _instance: Optional['SharedCLIManager'] = None
    _lock = threading.Lock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self.log_dir = Path("test_results/shared_cli")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.connection_thread: Optional[threading.Thread] = None
        self.cli_process: Optional[subprocess.Popen] = None
        self.is_connected = False
        self.connection_lock = threading.Lock()
        
        # Create shared log file
        timestamp = int(time.time())
        self.log_file = self.log_dir / f"shared_cli_connection_{timestamp}.log"
        
        self.logger = logging.getLogger("shared_cli_manager")
        self.logger.setLevel(logging.WARNING)
        
        # Register cleanup on exit
        atexit.register(self.cleanup_on_exit)
        
        self._initialized = True
        
    @classmethod
    def get_instance(cls) -> 'SharedCLIManager':
        """Get the singleton instance."""
        return cls()
    
    async def ensure_connected(self, debug: bool = True, timeout: int = 30) -> bool:
        """Ensure CLI connection is active, start if needed."""
        if self.is_connection_active():
            return True
            
        return await self.connect(debug, timeout)
    
    async def connect(self, debug: bool = True, timeout: int = 30) -> bool:
        """Start CLI connection in background thread with output redirection."""
        with self.connection_lock:
            if self.is_connected:
                return True
        
        try:
            # Import the CLI function
            from portacode.cli import cli
            
            
            # Start connection in separate thread
            self.connection_thread = threading.Thread(
                target=self._run_cli_connection,
                args=(debug,),
                daemon=True
            )
            self.connection_thread.start()
            
            # Wait for connection to establish
            start_time = time.time()
            while not self.is_connection_active() and (time.time() - start_time) < timeout:
                await asyncio.sleep(0.5)
                
            if self.is_connection_active():
                self.logger.info("Shared CLI connection established successfully")
                return True
            else:
                self.logger.error("Failed to establish shared CLI connection within timeout")
                return False
                
        except Exception as e:
            self.logger.error(f"Error starting shared CLI connection: {e}")
            return False
    
    def _run_cli_connection(self, debug: bool = True):
        """Run CLI connection in separate thread with output redirection."""
        try:
            
            with open(self.log_file, 'w') as log_file:
                log_file.write(f"=== Shared CLI Connection Log ===\\n")
                log_file.write(f"Started at: {time.ctime()}\\n")
                log_file.write("=" * 50 + "\\n\\n")
                log_file.flush()
                
                # Import the CLI function
                from portacode.cli import cli
                
                # Mark as connected (we assume the import worked)
                with self.connection_lock:
                    self.is_connected = True
                    
                
                # Just keep the thread alive since we're sharing the connection
                # The actual CLI connection will be managed by individual tests
                while self.is_connected:
                    time.sleep(1)
                    
        except Exception as e:
            with self.connection_lock:
                self.is_connected = False
            self.logger.error(f"Shared CLI connection failed: {e}")
            
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
    
    def get_connection_info(self) -> Dict[str, Any]:
        """Get information about the current connection."""
        return {
            "is_connected": self.is_connection_active(),
            "log_file": str(self.log_file),
            "log_exists": self.log_file.exists(),
            "log_size": self.log_file.stat().st_size if self.log_file.exists() else 0,
            "connection_type": "shared"
        }
    
    def cleanup_on_exit(self):
        """Cleanup method called on program exit."""
        try:
            with self.connection_lock:
                self.is_connected = False
            
            if self.cli_process:
                self.cli_process.terminate()
                try:
                    self.cli_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.cli_process.kill()
            
        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}")


class TestCLIProxy:
    """Proxy class that provides the same interface as CLIManager but uses shared connection."""
    
    _actual_cli_manager = None
    _cli_initialized = False
    
    def __init__(self, test_name: str, log_dir: str = None):
        self.test_name = test_name
        self.shared_manager = SharedCLIManager.get_instance()
        self.logger = logging.getLogger(f"cli_proxy.{test_name}")
        self.logger.setLevel(logging.WARNING)
        
    async def connect(self, debug: bool = True, timeout: int = 30) -> bool:
        """Connect using shared manager - creates actual CLI connection if needed."""
        # Import here to avoid circular imports
        from .cli_manager import CLIManager
        
        # Create one actual CLI connection that all tests share
        if not TestCLIProxy._cli_initialized:
            TestCLIProxy._actual_cli_manager = CLIManager("shared_connection", "test_results/shared_cli")
            connected = await TestCLIProxy._actual_cli_manager.connect(debug, timeout)
            if connected:
                TestCLIProxy._cli_initialized = True
            return connected
        else:
            # Reuse existing connection
            return TestCLIProxy._actual_cli_manager.is_connection_active()
    
    def get_log_content(self) -> str:
        """Get log content from actual CLI manager."""
        if TestCLIProxy._actual_cli_manager:
            return TestCLIProxy._actual_cli_manager.get_log_content()
        return ""
    
    def is_connection_active(self) -> bool:
        """Check if actual CLI connection is active."""
        if TestCLIProxy._actual_cli_manager:
            return TestCLIProxy._actual_cli_manager.is_connection_active()
        return False
    
    async def disconnect(self):
        """Disconnect - but don't actually disconnect shared connection."""
        # Don't disconnect the shared connection, just log that this test is done
        self.logger.info(f"Test {self.test_name} finished using shared CLI connection")
    
    def get_connection_info(self) -> Dict[str, Any]:
        """Get connection info with test name context."""
        if TestCLIProxy._actual_cli_manager:
            info = TestCLIProxy._actual_cli_manager.get_connection_info()
            info["test_name"] = self.test_name
            info["connection_type"] = "shared"
            return info
        return {
            "test_name": self.test_name,
            "is_connected": False,
            "connection_type": "shared"
        }