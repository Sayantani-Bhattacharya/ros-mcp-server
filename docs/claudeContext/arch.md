# ros-mcp-server: Architecture Overview

## What this project does

This is an **MCP (Model Context Protocol) server** that bridges LLMs (like Claude) to ROS (Robot Operating System) robots. The LLM can call tools that transparently translate into ROS operations — reading sensor data, publishing commands, triggering actions, inspecting nodes — all over a WebSocket to rosbridge.

```
LLM (Claude, GPT, etc.)
    |  MCP protocol (stdio or HTTP)
    v
FastMCP Server  (ros_mcp/main.py)
    |  Tool calls  (ros_mcp/tools/*.py)
    v
WebSocketManager  (ros_mcp/utils/websocket.py)
    |  rosbridge protocol (JSON over WebSocket)
    v
rosbridge_server  (running on the robot / ROS host)
    |  ROS 2 / ROS 1 API
    v
ROS nodes, topics, actions, services, parameters
```

## Key concepts: MCP, rosbridge, FastMCP

- **MCP (Model Context Protocol)**: Anthropic's open protocol. An MCP server exposes **tools** (functions the LLM can call), **resources** (data the LLM can read), and **prompts** (pre-built instruction templates). Claude discovers these and calls them autonomously.
- **FastMCP**: Python library wrapping the MCP SDK — turns Python functions decorated with `@mcp.tool()` into MCP-callable tools automatically.
- **rosbridge**: A ROS package that exposes the entire ROS graph over a WebSocket using a JSON protocol. Lets non-ROS code talk to ROS without needing native client libraries. rosbridge understands operations like `call_service`, `subscribe`, `publish`, `send_action_goal`.

## Entry points

| File | Role |
|------|------|
| `server.py` | Top-level entry point (calls `ros_mcp.main.main()`) |
| `ros_mcp/main.py` | Creates `FastMCP` instance + `WebSocketManager`, registers everything, defines CLI args |
| `ros_mcp/integration.py` | Alternative entry used when embedded as a submodule (reads config from env vars) |

## Tools layer (`ros_mcp/tools/`)

Each file registers a group of `@mcp.tool()` functions. All tools receive a shared `WebSocketManager` instance.

| File | Tools |
|------|-------|
| `actions.py` | `get_actions`, `get_action_details`, `get_action_status`, `send_action_goal`, `cancel_action_goal` |
| `topics.py` | Subscribe, publish, list topics |
| `services.py` | Call and list ROS services |
| `nodes.py` | List and inspect ROS nodes |
| `parameters.py` | Get/set ROS parameters |
| `connection.py` | Connect/configure rosbridge IP and port |
| `robot_config.py` | Load robot specification files |
| `images.py` | Receive and decode camera images from topics |

## WebSocket layer (`ros_mcp/utils/websocket.py`)

`WebSocketManager` is the single shared connection to rosbridge. It is **synchronous** (uses `websocket-client`, not `asyncio`), protected by a `threading.RLock`.

Key methods:
- `connect()` — establishes WebSocket if not already connected
- `send(message: dict)` — serializes to JSON and sends; calls `connect()` internally
- `receive(timeout)` — blocking recv with settimeout; on **any** exception (including timeout) calls `self.close()` and returns `None`
- `request(message)` — send + receive in one call; returns parsed dict
- `close()` — tears down the WebSocket
- `__enter__` / `__exit__` — context manager that **closes the connection on exit**

The `with ws_manager:` pattern is used pervasively in tool code. This means every discrete tool call opens and closes a fresh connection.

## Resources (`ros_mcp/resources/`)

- `ros_metadata.py` — exposes ROS graph state as readable resources (nodes, topics, etc.)
- `robot_specs.py` — loads robot specification YAML files from `robot_specifications/`

## Prompts (`ros_mcp/prompts/`)

Pre-built prompt templates that guide the LLM through common testing scenarios (test connection, test topics, test actions, etc.). Exposed via MCP `prompts` so the LLM can request structured instructions.

## Data flow for a typical tool call

1. LLM decides to call e.g. `get_topics()`
2. FastMCP deserializes the call and invokes the Python function
3. The function constructs a rosbridge JSON message (e.g. `{"op": "call_service", "service": "/rosapi/topics", ...}`)
4. `ws_manager.request(message)` sends it and waits for the reply
5. rosbridge forwards the request to ROS, gets the response, sends it back as JSON
6. The tool parses and returns a dict to the LLM

## Action flow (most complex case)

ROS Actions are long-running goal-oriented behaviors (e.g., "rotate 90 degrees"). They have three message types:
- **Goal** — sent by client to start the behavior
- **Feedback** — periodic updates while executing (e.g., current angle)
- **Result** — final outcome when done

For `send_action_goal`:
1. Send `{"op": "send_action_goal", ...}` to rosbridge
2. Enter a while loop, repeatedly calling `ws_manager.receive()` 
3. Classify incoming messages: `action_feedback` → record; `action_result` → return success
4. If outer timeout expires before `action_result` → return timeout error

## Known issue: timeout closes connection (Issue #318)

`WebSocketManager.receive()` calls `self.close()` on **all** exceptions, including `WebSocketTimeoutException`. During `send_action_goal`, a quiet gap between feedback messages triggers a timeout → connection closes → reconnect loses the rosbridge action subscription → `action_result` never arrives even though the robot completed the action.

See `docs/claudeContext/issue_318.md` for full analysis and fix.
