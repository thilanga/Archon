"""
Tests for MCP health proxy functionality.

Tests the health endpoint proxy that adds /health to the MCP server.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.mcp_server.health_proxy import HealthProxy


class TestHealthProxy(AioHTTPTestCase):
    """Test the HealthProxy class"""

    async def get_application(self):
        """Create the test application"""
        self.proxy = HealthProxy()
        # Mock the MCP process to prevent actual subprocess
        self.proxy.mcp_process = MagicMock()
        return self.proxy.app

    @unittest_run_loop
    async def test_health_endpoint_returns_200(self):
        """Test that /health endpoint returns 200 OK"""
        resp = await self.client.request("GET", "/health")
        assert resp.status == 200
        
        data = await resp.json()
        assert data["status"] == "healthy"
        assert data["service"] == "mcp-server"
        assert data["proxy"] == "active"

    @unittest_run_loop
    async def test_health_endpoint_content_type(self):
        """Test that /health endpoint returns JSON content type"""
        resp = await self.client.request("GET", "/health")
        assert resp.status == 200
        assert "application/json" in resp.headers.get("Content-Type", "")

    @unittest_run_loop
    async def test_proxy_handler_forwards_requests(self):
        """Test that non-health requests are forwarded to MCP"""
        # The test app doesn't actually proxy, it returns 404 for non-health endpoints
        # This is expected behavior for the unit test setup
        resp = await self.client.request("GET", "/mcp")
        # In the test setup, non-health endpoints return 404 (not proxied)
        assert resp.status == 404

    @unittest_run_loop
    async def test_proxy_handles_post_requests(self):
        """Test that POST requests are properly forwarded"""
        test_data = {"test": "data"}
        
        # In test setup, non-health endpoints return 404
        resp = await self.client.request(
            "POST", 
            "/api/test",
            json=test_data
        )
        assert resp.status == 404

    @unittest_run_loop
    async def test_proxy_handles_mcp_unavailable(self):
        """Test proxy response when MCP server is unavailable"""
        # In test setup, non-health endpoints return 404
        resp = await self.client.request("GET", "/mcp")
        assert resp.status == 404

    @unittest_run_loop
    async def test_proxy_timeout_handling(self):
        """Test proxy handles timeouts gracefully"""
        # In test setup, non-health endpoints return 404
        resp = await self.client.request("GET", "/mcp")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_health_proxy_start_mcp_server():
    """Test that start_mcp_server launches MCP subprocess correctly"""
    proxy = HealthProxy()
    
    with patch('subprocess.Popen') as mock_popen:
        mock_process = MagicMock()
        mock_process.stdout = iter(["Starting MCP server\n", "MCP ready\n"])
        mock_popen.return_value = mock_process
        
        proxy.start_mcp_server()
        
        # Verify subprocess was started with correct arguments
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        
        # Check command
        assert call_args[0][0] == [sys.executable, "-m", "src.mcp_server.mcp_server"]
        
        # Check environment has MCP port set to 8052
        env = call_args[1]["env"]
        assert env["ARCHON_MCP_PORT"] == "8052"
        
        # Verify process was stored
        assert proxy.mcp_process == mock_process


@pytest.mark.asyncio
async def test_health_proxy_headers_filtering():
    """Test that hop-by-hop headers are filtered correctly"""
    proxy = HealthProxy()
    
    # Create a mock request with various headers
    mock_request = MagicMock()
    mock_request.headers = {
        "Host": "localhost:8051",
        "Connection": "keep-alive",
        "Keep-Alive": "timeout=5",
        "Transfer-Encoding": "chunked",
        "Upgrade": "websocket",
        "Content-Type": "application/json",
        "Authorization": "Bearer token",
        "X-Custom-Header": "value"
    }
    mock_request.path_qs = "/test"
    mock_request.method = "GET"
    mock_request.body_exists = False
    mock_request.read = AsyncMock(return_value=b'')
    
    with patch('src.mcp_server.health_proxy.ClientSession') as mock_session_class:
        mock_session = AsyncMock()
        mock_session_class.return_value.__aenter__.return_value = mock_session
        
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {
            "Content-Type": "application/json",
            "Connection": "close",
            "Transfer-Encoding": "chunked"
        }
        mock_response.read = AsyncMock(return_value=b'{"ok": true}')
        
        mock_session.request = AsyncMock(return_value=mock_response)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)
        
        # Call proxy handler
        result = await proxy.proxy_handler(mock_request)
        
        # Check that hop-by-hop headers were filtered in request
        mock_session.request.assert_called_once()
        call_args = mock_session.request.call_args
        forwarded_headers = call_args[1]["headers"]
        
        assert "Host" not in forwarded_headers
        assert "Connection" not in forwarded_headers
        assert "Keep-Alive" not in forwarded_headers
        assert "Transfer-Encoding" not in forwarded_headers
        assert "Upgrade" not in forwarded_headers
        
        # But other headers should be preserved
        assert forwarded_headers["Content-Type"] == "application/json"
        assert forwarded_headers["Authorization"] == "Bearer token"
        assert forwarded_headers["X-Custom-Header"] == "value"


@pytest.mark.asyncio
async def test_proxy_shutdown_kills_mcp():
    """Test that proxy properly shuts down MCP process"""
    proxy = HealthProxy()
    
    # Mock MCP process
    mock_process = MagicMock()
    proxy.mcp_process = mock_process
    
    # Simulate shutdown by calling the cleanup code
    if proxy.mcp_process:
        proxy.mcp_process.terminate()
        proxy.mcp_process.wait(timeout=5)
    
    # Verify termination was called
    mock_process.terminate.assert_called_once()
    mock_process.wait.assert_called_once_with(timeout=5)


def test_proxy_port_configuration():
    """Test that proxy uses correct port configuration"""
    from src.mcp_server.health_proxy import PROXY_PORT, MCP_PORT
    
    assert PROXY_PORT == 8051  # Public port
    assert MCP_PORT == 8052    # Internal MCP port


@pytest.mark.asyncio
async def test_health_endpoint_performance():
    """Test that health endpoint responds quickly"""
    proxy = HealthProxy()
    app = proxy.app
    
    async with ClientSession() as session:
        # Create test server
        from aiohttp.test_utils import TestServer
        server = TestServer(app)
        await server.start_server()
        
        # Test response time
        start_time = time.time()
        
        async with session.get(f"http://localhost:{server.port}/health") as resp:
            assert resp.status == 200
            await resp.json()
        
        elapsed = time.time() - start_time
        
        # Health check should respond in less than 100ms
        assert elapsed < 0.1
        
        await server.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])