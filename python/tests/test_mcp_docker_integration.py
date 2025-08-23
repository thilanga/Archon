"""
Integration tests for MCP Docker container with health endpoint.

Tests the full Docker setup including health checks and MCP connectivity.
"""

import json
import os
import subprocess
import time
from typing import Dict, Optional

import docker
import pytest
import requests


class TestMCPDockerIntegration:
    """Integration tests for MCP Docker container"""
    
    @classmethod
    def setup_class(cls):
        """Setup Docker client"""
        cls.docker_client = docker.from_env()
        cls.container_name = "archon-mcp"
        cls.mcp_port = 8051
        cls.base_url = f"http://localhost:{cls.mcp_port}"
    
    def get_container(self) -> Optional[docker.models.containers.Container]:
        """Get the MCP container if it exists"""
        try:
            return self.docker_client.containers.get(self.container_name)
        except docker.errors.NotFound:
            return None
    
    def test_container_is_running(self):
        """Test that MCP container is running"""
        container = self.get_container()
        assert container is not None, f"Container {self.container_name} not found"
        assert container.status == "running", f"Container status: {container.status}"
    
    def test_container_health_check(self):
        """Test that container health check is configured and passing"""
        container = self.get_container()
        assert container is not None
        
        # Get container health status
        container.reload()
        health = container.attrs.get("State", {}).get("Health", {})
        
        # Check health status
        assert health.get("Status") == "healthy", f"Container health: {health.get('Status')}"
    
    def test_health_endpoint_accessible(self):
        """Test that /health endpoint is accessible from host"""
        response = requests.get(f"{self.base_url}/health", timeout=5)
        assert response.status_code == 200
        
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "mcp-server"
        assert data["proxy"] == "active"
    
    def test_health_endpoint_response_time(self):
        """Test that health endpoint responds quickly"""
        start_time = time.time()
        response = requests.get(f"{self.base_url}/health", timeout=1)
        elapsed = time.time() - start_time
        
        assert response.status_code == 200
        assert elapsed < 0.5, f"Health check took {elapsed:.2f}s, expected < 0.5s"
    
    def test_mcp_endpoint_accessible(self):
        """Test that /mcp endpoint is accessible (even if it returns 406)"""
        response = requests.get(f"{self.base_url}/mcp", timeout=5)
        # MCP endpoint returns 406 for non-SSE requests, which is expected
        assert response.status_code in [200, 406], f"Unexpected status: {response.status_code}"
    
    def test_container_port_binding(self):
        """Test that container port is correctly bound to localhost only"""
        container = self.get_container()
        assert container is not None
        
        # Get port bindings
        ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
        mcp_port_binding = ports.get(f"{self.mcp_port}/tcp", [])
        
        # Check that port is bound
        assert len(mcp_port_binding) > 0, "MCP port not bound"
        
        # Check that it's bound to localhost only (security)
        for binding in mcp_port_binding:
            host_ip = binding.get("HostIp", "")
            assert host_ip in ["127.0.0.1", "localhost"], f"Port bound to {host_ip}, should be localhost only"
    
    def test_container_logs_no_health_404(self):
        """Test that container logs don't show excessive /health 404 errors"""
        container = self.get_container()
        assert container is not None
        
        # Get recent logs
        logs = container.logs(tail=50).decode("utf-8")
        
        # Count 404 errors for /health
        health_404_count = logs.count('"GET /health HTTP/1.1" 404')
        
        # Should be minimal or none (maybe a few during startup)
        assert health_404_count < 5, f"Found {health_404_count} health check 404s in recent logs"
    
    def test_proxy_forwards_to_mcp(self):
        """Test that proxy correctly forwards requests to MCP server"""
        # Send a request with SSE headers to get proper MCP response
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json"
        }
        
        # Try to initialize MCP session
        response = requests.post(
            f"{self.base_url}/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1},
            timeout=5
        )
        
        # Should get some response (even if error)
        assert response.status_code in [200, 400, 406], f"Unexpected status: {response.status_code}"
    
    def test_container_environment_variables(self):
        """Test that container has correct environment variables"""
        container = self.get_container()
        assert container is not None
        
        # Get environment variables
        env_list = container.attrs.get("Config", {}).get("Env", [])
        env_dict = {}
        for env_str in env_list:
            if "=" in env_str:
                key, value = env_str.split("=", 1)
                env_dict[key] = value
        
        # Check critical environment variables
        assert "SUPABASE_URL" in env_dict
        assert "SUPABASE_SERVICE_KEY" in env_dict
        assert env_dict.get("ARCHON_MCP_PORT", "") == "8051"
    
    def test_container_restart_preserves_health(self):
        """Test that container can be restarted and health endpoint still works"""
        container = self.get_container()
        assert container is not None
        
        # Restart container
        container.restart(timeout=10)
        
        # Wait for container to be healthy again
        max_attempts = 30
        for i in range(max_attempts):
            time.sleep(1)
            try:
                response = requests.get(f"{self.base_url}/health", timeout=1)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                pass
        else:
            pytest.fail("Container did not become healthy after restart")
        
        # Verify health endpoint works
        response = requests.get(f"{self.base_url}/health", timeout=5)
        assert response.status_code == 200


class TestMCPConnectivity:
    """Test MCP connectivity from other services"""
    
    @classmethod
    def setup_class(cls):
        """Setup for connectivity tests"""
        cls.docker_client = docker.from_env()
        cls.api_base_url = "http://localhost:8181"
    
    def test_api_server_can_reach_mcp(self):
        """Test that the main API server can communicate with MCP"""
        response = requests.get(f"{self.api_base_url}/api/mcp/status", timeout=5)
        assert response.status_code == 200
        
        data = response.json()
        assert data["status"] in ["running", "healthy"]
        assert data["container_status"] == "running"
    
    def test_mcp_tools_accessible(self):
        """Test that MCP tools are accessible through API"""
        response = requests.get(f"{self.api_base_url}/api/mcp/tools", timeout=5)
        assert response.status_code == 200
        
        data = response.json()
        assert "server_running" in data
        assert data["server_running"] is True
    
    def test_mcp_health_through_api(self):
        """Test MCP health check through API server"""
        response = requests.get(f"{self.api_base_url}/api/mcp/health", timeout=5)
        assert response.status_code == 200
        
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "mcp"


class TestDockerHealthCheck:
    """Test Docker's built-in health check functionality"""
    
    @classmethod
    def setup_class(cls):
        """Setup Docker client"""
        cls.docker_client = docker.from_env()
    
    def test_health_check_command(self):
        """Test that the health check command in docker-compose works"""
        # Run the health check command directly
        result = subprocess.run(
            ["docker", "exec", "archon-mcp", "python", "-c", 
             "import socket; s=socket.socket(); s.connect(('localhost', 8051)); s.close()"],
            capture_output=True,
            timeout=5
        )
        
        assert result.returncode == 0, f"Health check failed: {result.stderr.decode()}"
    
    def test_all_containers_healthy(self):
        """Test that all Archon containers are healthy"""
        containers = self.docker_client.containers.list(
            filters={"label": "com.docker.compose.project=archon"}
        )
        
        unhealthy = []
        for container in containers:
            # Only check archon-specific containers, skip supabase containers
            if container.name == "archon-mcp" or container.name.startswith("archon-"):
                container.reload()
                health = container.attrs.get("State", {}).get("Health", {})
                status = health.get("Status", "unknown")
                
                # Allow "starting" status for recently restarted containers
                if status not in ["healthy", "starting"]:
                    unhealthy.append(f"{container.name}: {status}")
        
        assert len(unhealthy) == 0, f"Unhealthy containers: {unhealthy}"


@pytest.mark.parametrize("endpoint,expected_status", [
    ("/health", 200),
    ("/mcp", 406),  # 406 is expected for direct access without SSE
    ("/nonexistent", 404),  # MCP returns 404 for non-existent endpoints
])
def test_proxy_routing(endpoint: str, expected_status: int):
    """Test that proxy routes different endpoints correctly"""
    response = requests.get(f"http://localhost:8051{endpoint}", timeout=5)
    assert response.status_code == expected_status


def test_concurrent_health_checks():
    """Test that health endpoint handles concurrent requests"""
    import concurrent.futures
    
    def make_health_request():
        response = requests.get("http://localhost:8051/health", timeout=5)
        return response.status_code
    
    # Make 20 concurrent requests
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(make_health_request) for _ in range(20)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    
    # All should succeed
    assert all(status == 200 for status in results)
    assert len(results) == 20


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])