# Claude Context: ros-mcp-server Issue #318

> This file is maintained for use in Claude Code sessions.
> Start every session with: "Read `/docs/claude/context.md` first, then let's continue."
> Update this file as you learn more, make decisions, or close open questions.

---

## My Goal

I am studying this repo to understand the **LLM-robotics intersection** deeply.
This is voluntary open source work. The aim is not to develop coding skills in general —
it is to develop real understanding of how LLMs connect to robot systems at the architecture level.

---

## The Repo

**robotmcp/ros-mcp-server** — connects LLMs (Claude, GPT, Gemini) to ROS robots via MCP.
The bridge between the LLM and the robot is **rosbridge**, a WebSocket server that translates
JSON messages into ROS commands and streams ROS events back.

### Key files
| File | Role |
|------|------|
| `ros_mcp/utils/websocket.py` | `WebSocketManager` — all WebSocket I/O lives here |
| `ros_mcp/tools/actions.py` | Action tools: `send_action_goal`, `get_action_status`, etc. |
| `ros_mcp/tools/topics.py` | Topic tools — also uses `WebSocketManager.receive()` |
| `ros_mcp/tools/services.py` | Service tools — also uses `WebSocketManager.receive()` |
| `ros_mcp/main.py` | Entry point, wires everything together |

### Architecture summary
```
LLM (Claude/GPT)
     ↓ MCP tool call
ros-mcp-server (FastMCP, Python)
     ↓ JSON over WebSocket
rosbridge (ROS node)
     ↓ native ROS
Robot / Simulator
```

### Refactor context (PR stack by stex2005)
The repo is mid-way through a 7-step structured refactor migrating hardcoded `/rosapi/` strings
to helper functions and standardizing tool patterns:

| Step | PR | Status |
|------|----|--------|
| Step 0: detection + Docker infra | #276 | merged |
| Step 1: connection + robot config | #278 | merged |
| Step 2: nodes | #280 | open |
| Step 3: topics | #281 | open |
| Step 4: services | #316 | merged |
| Step 5: actions | #321 | open |
| Step 6: parameters | — | not created |
| Step 7: resources | — | not created |

---

## The Bug — Issue #318

**Title:** `send_action_goal` times out even when action succeeds

### ROS Actions — conceptual background

A ROS action is a three-phase async protocol built on top of ROS topics:
```
Client                    Action Server
  |------ Goal --------->|   (start doing something)
  |<----- Feedback ------| x N  (progress updates)
  |<----- Result --------|   (final outcome)
```
rosbridge wraps this as `send_action_goal` / `action_feedback` / `action_result` WebSocket messages.

**The critical property:** rosbridge tracks *which WebSocket connection* sent a goal and routes feedback/result back to that same connection. If the connection drops and reconnects, rosbridge forgets about the goal — the result message gets delivered to nobody.

### What happens
```
send_action_goal('/turtle1/rotate_absolute', 'turtlesim/action/RotateAbsolute', {'theta': 1.0}, timeout=15)
# Returns: success=True, error="Action timed out after 15.0 seconds", last_feedback={...}
```
The robot **did** execute the action. `get_action_status` confirms STATUS_SUCCEEDED = 4.
But the MCP tool reports a timeout.

### Why it happens — the conceptual chain
1. `send_action_goal` sends a goal to the action server via rosbridge
2. It then loops: call `ws.receive()` → wait for `action_result` message
3. Each `receive()` has a short per-call timeout (e.g. 0.5s) to avoid blocking forever
4. If there is a gap between feedback messages longer than that timeout, `receive()` raises TimeoutError
5. **The bug:** `receive()` calls `self.close()` on TimeoutError — closing the entire WebSocket connection
6. The next `receive()` reconnects — but the rosbridge subscription is gone on the new connection
7. The action result message arrives — but nobody is listening anymore
8. The outer 15s timer expires → MCP reports "timed out"

### Key insight
A **timeout** (silence on the channel) and a **connection error** (broken socket) are two different things.
The current code treats them identically — both trigger `self.close()`.

### The buggy code

`WebSocketManager.receive()` in `ros_mcp/utils/websocket.py`:
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

The while loop in `send_action_goal` (`ros_mcp/tools/actions.py`):
```python
with ws_manager:                            # connection opens here
    send_error = ws_manager.send(message)   # goal sent to rosbridge

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

### Step-by-step failure timeline

| t (s) | Event |
|-------|-------|
| 0 | Goal sent. rosbridge registers client as subscriber for this action. |
| 0.3 | `receive(15.0)` → feedback #1 arrives |
| 0.6 | `receive(14.5)` → feedback #2 arrives |
| 1.0 | `receive(14.0)` → feedback #3 arrives |
| 1.4 | `receive(13.6)` → **no message for 13.6s** → `WebSocketTimeoutException` → `self.close()` called |
| 1.4 | `receive()` returns `None`. Loop continues. |
| 1.5 | Next `receive()` call → `connect()` opens a **new** WebSocket |
| 10.0 | Robot finishes rotating. rosbridge sends `action_result` to the **old** (now dead) connection. |
| 15.0 | While loop exits. Returns timeout error. |

### Why `get_action_status` still shows success

`get_action_status` subscribes to `{action_name}/_action/status` — an independent ROS topic that reflects the action server's internal state. It doesn't depend on having been the original goal sender. So it reads STATUS_SUCCEEDED = 4 regardless of whether the MCP connection was alive to receive the `action_result`.

### Why it works outside MCP's async runtime
In a plain synchronous test the connection is steady and timing is predictable.
Inside MCP's asyncio runtime, timing is less deterministic — gaps between messages
are more likely to exceed the per-call timeout, triggering the close.

### Underlying library
The repo uses `websocket-client` (sync library), not `websockets` (async).
This sync socket is being called from inside an asyncio context — a known source
of timing sensitivity and subtle race conditions.

---

## What PR #321 Does and Does NOT Do

PR #321 (Step 5) explicitly marks issue #318 as **"out of scope"**.

What it does touch in `send_action_goal`:
- Removes `asyncio.sleep(0.1)` from the feedback loop (was causing missed results — small improvement)
- Migrates hardcoded `/rosapi/` strings to helper functions
- Refactors to match the standard tool pattern

What it does NOT do:
- Does not fix `WebSocketManager.receive()` close-on-timeout behavior
- Does not address the root cause

---

## The Fix — Decision Points

### Fix A: Don't close on timeout (surgical)
In `WebSocketManager.receive()`, stop treating TimeoutError as fatal:

#### Pseudo Code
```python
# current (buggy)
except TimeoutError:
    self.close()   # wrong — closes connection on silence
    raise

# proposed
except TimeoutError:
    return None    # silence ≠ broken connection; stay connected
```

#### Proposed Code

catch `WebSocketTimeoutException` separately from real errors:

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
All callers of `receive()` then need to handle `None` (continue the loop).

**Concerns with Fix A:**
- `receive()` is shared infrastructure used by topics, services, and actions
- Changes the caller contract: currently callers assume exception = broken, return = message
- Need to audit every caller before applying
- May mask a truly dead connection (silence vs broken can look the same)

### Secondary issue: `with ws_manager:` closes on exit

The `__exit__` method of `WebSocketManager` calls `self.close()`. The entire `send_action_goal` loop runs inside `with ws_manager:`, so the connection is kept alive for the duration of the function — this is fine. But it means the fix to `receive()` only helps if the `with ws_manager:` block isn't exited prematurely (e.g. on an early return path). Worth confirming all return paths are inside the block.

### Fix B: Restructure to a single long-lived receive (architectural)
Instead of many `receive(timeout=0.5)` calls in a loop, pass the full remaining
budget as the timeout for each call. Fewer chances to hit the gap.

**Concerns with Fix B:**
- Larger change, touches loop structure in `send_action_goal`
- Still doesn't fix the underlying `close()` behavior for other callers

### Current thinking
Fix A is the right scope for this issue. But it needs:
1. Full audit of all `receive()` callers and their assumptions
2. A decision on how to detect truly dead connections (not just quiet ones)
3. A regression test

---

## What to Verify After the Fix

1. `send_action_goal` with a short action (turtlesim rotate) returns `action_result` correctly
2. `send_action_goal` with a longer action (feedback gap > `default_timeout`) still completes
3. Real connection errors (rosbridge crashed mid-action) still close the connection
4. Other tools using `receive()` in short request/reply patterns (e.g. `get_action_status`) still work — real errors still call `close()`

## Files to Change

- `ros_mcp/utils/websocket.py` — the `receive()` method (lines ~354–380)

## Open Questions

- [ ] What does `close()` actually do — does it clear subscription state, or just close the socket?
- [ ] Is there an auto-reconnect mechanism? If so, does it re-subscribe?
- [ ] Which other tools call `receive()` and what do they assume about its return value?
- [ ] Is the async/sync mismatch (websocket-client inside asyncio) contributing independently?
- [ ] Should the fix live in `websocket.py` or should `send_action_goal` have its own receive loop that doesn't use the shared method?

---

## Next Steps

1. Read `ros_mcp/utils/websocket.py` in full
2. Run `grep -rn "\.receive(" ros_mcp/` to find all callers
3. Read `send_action_goal` in `actions.py` on the `feature/step5` branch
4. Answer the open questions above
5. Then design the fix

---

## Session Log

| Date | What happened |
|------|---------------|
| 2026-05-20 | Initial analysis in Claude.ai chat. Understood the bug, PR scope, and fix options. Created this file. |