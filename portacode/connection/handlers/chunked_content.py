"""
Chunked content transfer utilities for handling large content over WebSocket.

This module provides functionality to split large content into chunks for reliable
transmission over WebSocket connections, and to reassemble chunks on the client side.
"""

import hashlib
import uuid
from typing import Dict, Any, List, Optional
import logging

logger = logging.getLogger(__name__)

# Maximum size for content before chunking (200KB)
MAX_CONTENT_SIZE = 200 * 1024  # 200KB

# Maximum chunk size (64KB per chunk for reliable WebSocket transmission)
CHUNK_SIZE = 64 * 1024  # 64KB


def should_chunk_content(content: str) -> bool:
    """Determine if content should be chunked based on size."""
    if content is None:
        return False
    
    content_bytes = content.encode('utf-8')
    return len(content_bytes) > MAX_CONTENT_SIZE


def calculate_content_hash(content: str) -> str:
    """Calculate SHA-256 hash of content for verification."""
    if content is None:
        return ""
    
    content_bytes = content.encode('utf-8')
    return hashlib.sha256(content_bytes).hexdigest()


def split_content_into_chunks(content: str, transfer_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Split content into chunks for transmission.
    
    Args:
        content: The content to split
        transfer_id: Optional transfer ID, will generate one if not provided
        
    Returns:
        List of chunk dictionaries ready for transmission
    """
    if content is None:
        return []
    
    if transfer_id is None:
        transfer_id = str(uuid.uuid4())
    
    content_bytes = content.encode('utf-8')
    total_size = len(content_bytes)
    content_hash = hashlib.sha256(content_bytes).hexdigest()
    
    chunks = []
    chunk_index = 0
    offset = 0
    
    while offset < len(content_bytes):
        chunk_data = content_bytes[offset:offset + CHUNK_SIZE]
        chunk_content = chunk_data.decode('utf-8')
        chunk_hash = hashlib.sha256(chunk_data).hexdigest()
        
        chunks.append({
            "transfer_id": transfer_id,
            "chunk_index": chunk_index,
            "chunk_count": (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE,  # Ceiling division
            "chunk_size": len(chunk_data),
            "total_size": total_size,
            "content_hash": content_hash,
            "chunk_hash": chunk_hash,
            "chunk_content": chunk_content,
            "is_final_chunk": offset + CHUNK_SIZE >= len(content_bytes)
        })
        
        chunk_index += 1
        offset += CHUNK_SIZE
    
    logger.info(f"Split content into {len(chunks)} chunks (total size: {total_size} bytes, transfer_id: {transfer_id})")
    return chunks


def create_chunked_response(base_response: Dict[str, Any], content_field: str, content: str) -> List[Dict[str, Any]]:
    """
    Create chunked response messages from a base response and content.
    
    Args:
        base_response: The base response dictionary
        content_field: The field name where content should be placed
        content: The content to chunk
        
    Returns:
        List of response dictionaries with chunked content
    """
    if not should_chunk_content(content):
        # Content is small enough, return as single response
        response = base_response.copy()
        response[content_field] = content
        response["chunked"] = False
        return [response]
    
    # Content needs chunking
    transfer_id = str(uuid.uuid4())
    chunks = split_content_into_chunks(content, transfer_id)
    responses = []
    
    for chunk in chunks:
        response = base_response.copy()
        response["chunked"] = True
        response["transfer_id"] = chunk["transfer_id"]
        response["chunk_index"] = chunk["chunk_index"]
        response["chunk_count"] = chunk["chunk_count"]
        response["chunk_size"] = chunk["chunk_size"]
        response["total_size"] = chunk["total_size"]
        response["content_hash"] = chunk["content_hash"]
        response["chunk_hash"] = chunk["chunk_hash"]
        response["is_final_chunk"] = chunk["is_final_chunk"]
        response[content_field] = chunk["chunk_content"]
        
        responses.append(response)
    
    logger.info(f"Created chunked response with {len(responses)} chunks for transfer_id: {transfer_id}")
    return responses


class ChunkAssembler:
    """
    Helper class to assemble chunked content on the receiving side.
    """
    
    def __init__(self):
        self.transfers: Dict[str, Dict[str, Any]] = {}
    
    def add_chunk(self, chunk_data: Dict[str, Any], content_field: str) -> Optional[str]:
        """
        Add a chunk to the assembler.
        
        Args:
            chunk_data: The chunk data dictionary
            content_field: The field name containing the chunk content
            
        Returns:
            Complete content if all chunks received, None if more chunks needed
            
        Raises:
            ValueError: If chunk data is invalid or verification fails
        """
        transfer_id = chunk_data.get("transfer_id")
        chunk_index = chunk_data.get("chunk_index")
        chunk_count = chunk_data.get("chunk_count")
        chunk_size = chunk_data.get("chunk_size")
        total_size = chunk_data.get("total_size")
        content_hash = chunk_data.get("content_hash")
        chunk_hash = chunk_data.get("chunk_hash")
        chunk_content = chunk_data.get(content_field)
        is_final_chunk = chunk_data.get("is_final_chunk")
        
        if not all([transfer_id, chunk_index is not None, chunk_count, chunk_size, 
                   total_size, content_hash, chunk_hash, chunk_content is not None]):
            raise ValueError("Missing required chunk fields")
        
        # Verify chunk content hash
        chunk_bytes = chunk_content.encode('utf-8')
        if len(chunk_bytes) != chunk_size:
            raise ValueError(f"Chunk size mismatch: expected {chunk_size}, got {len(chunk_bytes)}")
        
        calculated_chunk_hash = hashlib.sha256(chunk_bytes).hexdigest()
        if calculated_chunk_hash != chunk_hash:
            raise ValueError(f"Chunk hash mismatch: expected {chunk_hash}, got {calculated_chunk_hash}")
        
        # Initialize transfer if not exists
        if transfer_id not in self.transfers:
            self.transfers[transfer_id] = {
                "chunk_count": chunk_count,
                "total_size": total_size,
                "content_hash": content_hash,
                "chunks": {},
                "received_chunks": 0
            }
        
        transfer = self.transfers[transfer_id]
        
        # Verify transfer metadata consistency
        if (transfer["chunk_count"] != chunk_count or 
            transfer["total_size"] != total_size or 
            transfer["content_hash"] != content_hash):
            raise ValueError("Transfer metadata mismatch")
        
        # Store chunk if not already received
        if chunk_index not in transfer["chunks"]:
            transfer["chunks"][chunk_index] = chunk_content
            transfer["received_chunks"] += 1
            
            logger.debug(f"Received chunk {chunk_index + 1}/{chunk_count} for transfer {transfer_id}")
        
        # Check if all chunks received
        if transfer["received_chunks"] == chunk_count:
            # Assemble content
            assembled_content = ""
            for i in range(chunk_count):
                if i not in transfer["chunks"]:
                    raise ValueError(f"Missing chunk {i} for transfer {transfer_id}")
                assembled_content += transfer["chunks"][i]
            
            # Verify final content hash
            assembled_bytes = assembled_content.encode('utf-8')
            if len(assembled_bytes) != total_size:
                raise ValueError(f"Final content size mismatch: expected {total_size}, got {len(assembled_bytes)}")
            
            calculated_hash = hashlib.sha256(assembled_bytes).hexdigest()
            if calculated_hash != content_hash:
                raise ValueError(f"Final content hash mismatch: expected {content_hash}, got {calculated_hash}")
            
            # Clean up transfer
            del self.transfers[transfer_id]
            
            logger.info(f"Successfully assembled content from {chunk_count} chunks (transfer_id: {transfer_id}, size: {total_size} bytes)")
            return assembled_content
        
        return None  # More chunks needed
    
    def cleanup_stale_transfers(self, max_age_seconds: int = 300):
        """Clean up transfers older than max_age_seconds."""
        import time
        current_time = time.time()
        
        stale_transfers = []
        for transfer_id, transfer in self.transfers.items():
            # Add timestamp if not exists
            if "start_time" not in transfer:
                transfer["start_time"] = current_time
            
            if current_time - transfer["start_time"] > max_age_seconds:
                stale_transfers.append(transfer_id)
        
        for transfer_id in stale_transfers:
            logger.warning(f"Cleaning up stale transfer: {transfer_id}")
            del self.transfers[transfer_id]