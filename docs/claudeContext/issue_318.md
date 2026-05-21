# Issue #318: `send_action_goal` times out even when action succeeds

**Link:** https://github.com/robotmcp/ros-mcp-server/issues/318

## The symptom

`send_action_goal('/turtle1/rotate_absolute', ...)` returns `{"success": False, "error": "Action timed out after 15 seconds"}` even though:
- Feedback messages ARE being received during execution
- `get_action_status` called afterward confirms the action completed (STATUS_SUCCEEDED)

## ROS Actions background (conceptual)

A ROS action is a three-phase async protocol built on top of ROS topics:
```
Client                    Action Server
  |------ Goal --------->|   (start doing something)
  |<----- Feedback ------| x N  (progress updates)
  |<----- Result --------|   (final outcome)
```
rosbridge wraps this as `send_action_goal` / `action_feedback` / `action_result` WebSocket messages.

The critical property: **the client must maintain the same WebSocket connection** for rosbridge to know where to deliver feedback and result messages. If the connection drops, rosbridge forgets about that goal.

## The root cause: `receive()` destroys the connection on timeout

`WebSocketManager.receive()` in [ros_mcp/utils/websocket.py:354-380](../../../ros_mcp/utils/websocket.py):

```python
def receive(self, timeout: float | None = None) -> Union[str, bytes] | None:
    with self.lock:
        self.connect()
        if self.ws:
            try:
                self.ws.settimeout(actual_timeout)
                raw = self.ws.recv()        # blocks until message or timeout
                return raw
            except Exception as e:          # catches EVERYTHING including timeout
                self.close()               # ← kills the connection on timeout!
                return None
```

A `WebSocketTimeoutException` is raised when `recv()` waits longer than `actual_timeout` with no message. This is treated identically to a real connection error — the socket gets closed.

## How this breaks `send_action_goal`

In [ros_mcp/tools/actions.py:614-666](../../../ros_mcp/tools/actions.py):

```python
with ws_manager:                           # connection opens here
    send_error = ws_manager.send(message)  # goal sent to rosbridge
    
    while time.time() - start_time < timeout:
        elapsed_time = time.time() - start_time
        response = ws_manager.receive(timeout - elapsed_time)  # blocking
        
        if response:
            msg_data = json.loads(response)
            if msg_data.get("op") == "action_result":
                return {...}    # success path
            if msg_data.get("op") == "action_feedback":
                feedback_count += 1
        else:
            pass  # no message, continue
        
        await asyncio.sleep(0.1)
```

**Failure scenario, step by step:**

| t (s) | Event |
|-------|-------|
| 0 | Goal sent. rosbridge registers client as subscriber for this action. |
| 0.3 | `receive(15.0)` → feedback #1 arrives |
| 0.6 | `receive(14.5)` → feedback #2 arrives |
| 1.0 | `receive(14.0)` → feedback #3 arrives |
| 1.4 | `receive(13.6)` → **no message for 13.6s** → timeout exception → `self.close()` is called |
| 1.4 | `receive()` returns `None`. Loop continues. |
| 1.5 | Next `receive()` call → `connect()` reconnects with a **new** WebSocket |
| 10.0 | Robot finishes rotating. rosbridge sends `action_result` to the **old** (now dead) connection. |
| 15.0 | While loop exits. Returns timeout error. |

The action result is delivered to the connection that no longer exists. The new connection never subscribed for this goal.

## Why `get_action_status` still shows success

`get_action_status` subscribes to `{action_name}/_action/status` — an independent ROS topic that reflects the action server's internal state. It doesn't depend on having been the original goal sender. So it sees STATUS_SUCCEEDED regardless.

## The fix

Separate `WebSocketTimeoutException` from real errors in `receive()`:

```python
import websocket  # websocket-client library

def receive(self, timeout: float | None = None) -> Union[str, bytes] | None:
    with self.lock:
        self.connect()
        if self.ws:
            try:
                actual_timeout = timeout if timeout is not None else self.default_timeout
                self.ws.settimeout(actual_timeout)
                raw = self.ws.recv()
                return raw
            except websocket.WebSocketTimeoutException:
                # Timeout = no message arrived. Connection is still alive. Don't close it.
                return None
            except Exception as e:
                # Real error (connection reset, etc.) — close and return None
                print(f"[WebSocket] Receive error: {e}", file=sys.stderr)
                self.close()
                return None
```

**Why this works:** After a quiet gap, `receive()` returns `None` but the connection stays open. The while loop in `send_action_goal` keeps looping, the same connection receives the eventual `action_result` from rosbridge, and success is returned.

## Secondary issue: `with ws_manager:` closes on exit

The `__exit__` method calls `self.close()`. The entire `send_action_goal` loop runs inside `with ws_manager:`, so if the loop completes (times out), the connection closes on the way out — this is fine. But it means even a fixed `receive()` still needs the outer `with ws_manager:` block to survive until the result arrives, which it does since the `with` block doesn't exit until the function returns.

## What to verify after the fix

1. `send_action_goal` with a short action (turtlesim rotate) returns `action_result` correctly
2. `send_action_goal` with a longer action (feedback gap > default_timeout) still completes
3. Real connection errors (rosbridge crashed mid-action) still close the connection
4. Other tools using `receive()` in short request/reply patterns (e.g. `get_action_status`) still work because real errors still call `close()`

## Files to change

- `ros_mcp/utils/websocket.py` — the `receive()` method (lines ~354-380)
