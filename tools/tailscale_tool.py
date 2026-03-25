"""Tailscale VPN integration for Hermes Agent - secure mesh network for burner orchestration."""

import json
import subprocess
from typing import Optional
from tools.registry import registry


def _run_tailscale(args: list[str], timeout: int = 30) -> dict:
    """Run tailscale CLI command and return parsed output."""
    try:
        result = subprocess.run(
            ["tailscale"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Command timed out after {timeout}s"}
    except FileNotFoundError:
        return {"success": False, "error": "tailscale CLI not found - install tailscale"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tailscale_status(task_id: str = None) -> str:
    """Get status of all nodes on your Tailscale network (tailnet).
    
    Returns list of all burners/devices with their:
    - Tailscale IP (100.x.x.x)
    - Node name
    - User/email
    - OS type
    - Connection status (active/idle)
    
    Use this to see which burners are online and available for task execution.
    """
    result = _run_tailscale(["status", "--json"])
    
    if not result.get("success"):
        return json.dumps({
            "error": "Failed to get Tailscale status",
            "details": result.get("stderr") or result.get("error"),
        })
    
    try:
        data = json.loads(result["stdout"])
        nodes = []
        
        for node_id, peer in data.get("Peer", {}).items():
            nodes.append({
                "name": peer.get("HostName", "unknown"),
                "dns_name": peer.get("DNSName", ""),
                "tailscale_ip": peer.get("TailscaleIPs", ["unknown"])[0],
                "os": peer.get("OS", "unknown"),
                "status": "active" if peer.get("Active") else "idle",
                "last_seen": peer.get("LastSeen", ""),
            })
        
        # Add self
        if "Self" in data:
            self_node = data["Self"]
            nodes.insert(0, {
                "name": self_node.get("HostName", "this-machine"),
                "dns_name": self_node.get("DNSName", ""),
                "tailscale_ip": self_node.get("TailscaleIPs", ["unknown"])[0],
                "os": self_node.get("OS", "unknown"),
                "status": "active (self)",
                "is_self": True,
            })
        
        return json.dumps({
            "success": True,
            "tailnet_name": data.get("BackendState", "unknown"),
            "total_nodes": len(nodes),
            "nodes": nodes,
        })
    
    except json.JSONDecodeError:
        # Fallback to text parsing
        return json.dumps({
            "success": True,
            "raw_output": result["stdout"],
        })


def tailscale_connect(node: str, command: str = None, task_id: str = None) -> str:
    """Connect to a remote burner via Tailscale SSH and optionally run a command.
    
    Args:
        node: Node name, DNS name, or Tailscale IP (e.g., "burner-1", "100.70.55.115")
        command: Optional command to execute on remote burner. If None, just test connection.
    
    Returns:
        Command output or connection status.
    
    Examples:
        - Test connection: tailscale_connect(node="burner-home")
        - Run command: tailscale_connect(node="burner-home", command="pm2 list")
        - Check status: tailscale_connect(node="100.70.55.115", command="uptime")
    """
    # First verify node exists
    status_result = tailscale_status()
    status_data = json.loads(status_result)
    
    if not status_data.get("success"):
        return json.dumps({"error": "Cannot verify node - Tailscale status failed"})
    
    # Find the node
    target = None
    for node_info in status_data.get("nodes", []):
        if (node_info.get("name") == node or 
            node_info.get("dns_name", "").rstrip(".") == node or
            node_info.get("tailscale_ip") == node):
            target = node_info
            break
    
    if not target:
        return json.dumps({
            "error": f"Node '{node}' not found on tailnet",
            "available_nodes": [n.get("name") for n in status_data.get("nodes", [])],
        })
    
    tailscale_ip = target["tailscale_ip"]
    
    if command:
        # Execute command via tailscale ssh
        # Note: requires 'tailscale up --ssh' on remote node
        result = _run_tailscale(
            ["ssh", f"root@{tailscale_ip}", command],
            timeout=60
        )
        
        if result.get("success"):
            return json.dumps({
                "success": True,
                "node": target["name"],
                "tailscale_ip": tailscale_ip,
                "command": command,
                "output": result["stdout"],
            })
        else:
            return json.dumps({
                "success": False,
                "node": target["name"],
                "error": result.get("stderr") or result.get("error"),
                "hint": "Enable Tailscale SSH on remote: sudo tailscale up --ssh",
            })
    else:
        # Just test connectivity
        result = _run_tailscale(
            ["ping", "-c", "1", "-W", "3", tailscale_ip],
            timeout=10
        )
        
        return json.dumps({
            "success": result.get("success", False),
            "node": target["name"],
            "tailscale_ip": tailscale_ip,
            "os": target.get("os"),
            "status": target.get("status"),
            "reachable": result.get("success", False),
        })


def tailscale_funnel(port: int, enable: bool = True, task_id: str = None) -> str:
    """Manage Tailscale Funnel to expose a local service publicly.
    
    Tailscale Funnel makes a local port accessible from anywhere (not just tailnet).
    Useful for temporary access to burner services.
    
    Args:
        port: Local port to expose (e.g., 4321 for proto-parsec)
        enable: True to enable, False to disable
    """
    if enable:
        result = _run_tailscale(["funnel", "--bg", f"localhost:{port}"])
        
        if result.get("success"):
            # Get the funnel URL
            url_result = _run_tailscale(["funnel", "status"])
            return json.dumps({
                "success": True,
                "action": "enabled",
                "port": port,
                "message": f"Port {port} exposed via Tailscale Funnel",
                "status": url_result.get("stdout", ""),
                "warning": "Funnel exposes this port to the PUBLIC internet",
            })
        else:
            return json.dumps({
                "success": False,
                "error": result.get("stderr") or result.get("error"),
                "hint": "Enable Funnel first: tailscale set --funnel=true",
            })
    else:
        result = _run_tailscale(["funnel", "off", str(port)])
        return json.dumps({
            "success": result.get("success", False),
            "action": "disabled",
            "port": port,
            "message": result.get("stdout", "Funnel disabled"),
        })


def tailscale_netcheck(task_id: str = None) -> str:
    """Run network connectivity check to find nearest DERP relay and latency.
    
    Useful for diagnosing connection issues with remote burners.
    """
    result = _run_tailscale(["netcheck"], timeout=30)
    
    return json.dumps({
        "success": result.get("success", False),
        "output": result.get("stdout", ""),
        "errors": result.get("stderr", ""),
    })


def tailscale_up(auth_key: str = None, ssh: bool = True, task_id: str = None) -> str:
    """Bring up Tailscale connection on this burner.
    
    Args:
        auth_key: Optional auth key for headless setup
        ssh: Enable Tailscale SSH server (default: True)
    """
    args = ["up"]
    
    if auth_key:
        args.extend(["--authkey", auth_key])
    
    if ssh:
        args.append("--ssh")
    
    result = _run_tailscale(args, timeout=30)
    
    return json.dumps({
        "success": result.get("success", False),
        "output": result.get("stdout", ""),
        "errors": result.get("stderr", ""),
        "message": "Tailscale is now active" if result.get("success") else "Failed to bring up Tailscale",
    })


# Register tools with Hermes
registry.register(
    name="tailscale_status",
    toolset="tailscale",
    schema={
        "name": "tailscale_status",
        "description": "Get status of all nodes on your Tailscale network (tailnet). Shows burners/devices with IPs, OS, and connection status.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    handler=lambda args, **kw: tailscale_status(task_id=kw.get("task_id")),
    check_fn=lambda: True,  # Always available if tailscale is installed
)

registry.register(
    name="tailscale_connect",
    toolset="tailscale",
    schema={
        "name": "tailscale_connect",
        "description": "Connect to a remote burner via Tailscale SSH. Test connection or run commands on remote machines securely through the VPN mesh.",
        "parameters": {
            "type": "object",
            "properties": {
                "node": {
                    "type": "string",
                    "description": "Node name, DNS name, or Tailscale IP (e.g., 'burner-home', '100.70.55.115')",
                },
                "command": {
                    "type": "string",
                    "description": "Optional command to execute on remote burner. If omitted, just tests connectivity.",
                },
            },
            "required": ["node"],
        },
    },
    handler=lambda args, **kw: tailscale_connect(
        node=args.get("node", ""),
        command=args.get("command"),
        task_id=kw.get("task_id"),
    ),
    check_fn=lambda: True,
    requires_approval=True,  # SSH access is sensitive
)

registry.register(
    name="tailscale_funnel",
    toolset="tailscale",
    schema={
        "name": "tailscale_funnel",
        "description": "Expose a local port publicly via Tailscale Funnel. Use for temporary remote access to burner services.",
        "parameters": {
            "type": "object",
            "properties": {
                "port": {
                    "type": "integer",
                    "description": "Local port to expose (e.g., 4321)",
                },
                "enable": {
                    "type": "boolean",
                    "description": "True to enable, False to disable",
                },
            },
            "required": ["port", "enable"],
        },
    },
    handler=lambda args, **kw: tailscale_funnel(
        port=args.get("port", 0),
        enable=args.get("enable", True),
        task_id=kw.get("task_id"),
    ),
    check_fn=lambda: True,
    requires_approval=True,  # Public exposure is sensitive
)

registry.register(
    name="tailscale_netcheck",
    toolset="tailscale",
    schema={
        "name": "tailscale_netcheck",
        "description": "Run network connectivity check to diagnose connection issues with remote burners.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    handler=lambda args, **kw: tailscale_netcheck(task_id=kw.get("task_id")),
    check_fn=lambda: True,
)

registry.register(
    name="tailscale_up",
    toolset="tailscale",
    schema={
        "name": "tailscale_up",
        "description": "Bring up Tailscale connection on this burner. Enables SSH server by default.",
        "parameters": {
            "type": "object",
            "properties": {
                "auth_key": {
                    "type": "string",
                    "description": "Optional auth key for headless setup",
                },
                "ssh": {
                    "type": "boolean",
                    "description": "Enable Tailscale SSH server (default: true)",
                },
            },
            "required": [],
        },
    },
    handler=lambda args, **kw: tailscale_up(
        auth_key=args.get("auth_key"),
        ssh=args.get("ssh", True),
        task_id=kw.get("task_id"),
    ),
    check_fn=lambda: True,
    requires_approval=True,
)
