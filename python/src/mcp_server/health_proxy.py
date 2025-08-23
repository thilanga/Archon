#!/usr/bin/env python3
"""
Health endpoint proxy for MCP server.

This proxy:
1. Listens on port 8051 (the public port)
2. Handles /health requests directly
3. Proxies all other requests to MCP server on port 8052
"""

import asyncio
import os
import sys
import logging
import subprocess
import signal
from pathlib import Path
from aiohttp import web, ClientSession, ClientTimeout
import multiprocessing

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("health_proxy")

# Ports configuration
PROXY_PORT = 8051  # Public port that receives all requests
MCP_PORT = 8052    # Internal port where MCP actually runs


class HealthProxy:
    """Proxy server that adds /health endpoint to MCP"""
    
    def __init__(self):
        self.app = web.Application()
        self.mcp_process = None
        self.setup_routes()
        
    def setup_routes(self):
        """Setup proxy routes"""
        # Health endpoint
        self.app.router.add_get('/health', self.health_handler)
        # Proxy everything else
        self.app.router.add_route('*', '/{path:.*}', self.proxy_handler)
        
    async def health_handler(self, request):
        """Handle /health endpoint"""
        return web.json_response({
            "status": "healthy",
            "service": "mcp-server",
            "proxy": "active"
        })
    
    async def proxy_handler(self, request):
        """Proxy all other requests to MCP server"""
        # Build target URL
        target_url = f"http://localhost:{MCP_PORT}{request.path_qs}"
        
        # Get request body if present
        body = await request.read() if request.body_exists else None
        
        # Create headers, removing hop-by-hop headers
        headers = {k: v for k, v in request.headers.items() 
                  if k.lower() not in ['host', 'connection', 'keep-alive', 
                                       'transfer-encoding', 'upgrade']}
        
        # Forward request to MCP server
        timeout = ClientTimeout(total=30)
        async with ClientSession(timeout=timeout) as session:
            try:
                async with session.request(
                    method=request.method,
                    url=target_url,
                    headers=headers,
                    data=body,
                    allow_redirects=False
                ) as response:
                    # Read response
                    resp_body = await response.read()
                    
                    # Copy response headers (excluding hop-by-hop)
                    resp_headers = {k: v for k, v in response.headers.items()
                                  if k.lower() not in ['connection', 'keep-alive', 
                                                       'transfer-encoding', 'content-encoding']}
                    
                    # Return proxied response
                    return web.Response(
                        body=resp_body,
                        status=response.status,
                        headers=resp_headers
                    )
                    
            except asyncio.TimeoutError:
                logger.error(f"Timeout proxying request to {target_url}")
                return web.Response(text="Gateway Timeout", status=504)
            except Exception as e:
                logger.error(f"Error proxying request: {e}")
                return web.Response(text="Bad Gateway", status=502)
    
    def start_mcp_server(self):
        """Start MCP server in subprocess on internal port"""
        env = os.environ.copy()
        env['ARCHON_MCP_PORT'] = str(MCP_PORT)
        
        logger.info(f"Starting MCP server on internal port {MCP_PORT}")
        
        # Start MCP server as subprocess
        self.mcp_process = subprocess.Popen(
            [sys.executable, "-m", "src.mcp_server.mcp_server"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        
        # Start a thread to log MCP output
        import threading
        def log_mcp_output():
            for line in self.mcp_process.stdout:
                # Don't log the 404 health checks we're trying to suppress
                if "GET /health" not in line or "404" not in line:
                    print(line.rstrip())
        
        log_thread = threading.Thread(target=log_mcp_output, daemon=True)
        log_thread.start()
        
    async def start(self):
        """Start the proxy server"""
        # Start MCP server first
        self.start_mcp_server()
        
        # Wait a bit for MCP to start
        await asyncio.sleep(2)
        
        # Start proxy server
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PROXY_PORT)
        await site.start()
        
        logger.info(f"Health proxy listening on port {PROXY_PORT}")
        logger.info(f"Proxying to MCP server on port {MCP_PORT}")
        logger.info("Health endpoint available at /health")
        
        # Keep running
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            if self.mcp_process:
                self.mcp_process.terminate()
                self.mcp_process.wait(timeout=5)


def main():
    """Main entry point"""
    # Handle signals gracefully
    def signal_handler(sig, frame):
        logger.info("Received shutdown signal")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Create and start proxy
    proxy = HealthProxy()
    
    try:
        asyncio.run(proxy.start())
    except KeyboardInterrupt:
        logger.info("Proxy stopped")
    finally:
        if proxy.mcp_process:
            proxy.mcp_process.terminate()


if __name__ == "__main__":
    main()