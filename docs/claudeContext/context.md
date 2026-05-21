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
```python
# current (buggy)
except TimeoutError:
    self.close()   # wrong — closes connection on silence
    raise

# proposed
except TimeoutError:
    return None    # silence ≠ broken connection; stay connected
```
All callers of `receive()` then need to handle `None` (continue the loop).

**Concerns with Fix A:**
- `receive()` is shared infrastructure used by topics, services, and actions
- Changes the caller contract: currently callers assume exception = broken, return = message
- Need to audit every caller before applying
- May mask a truly dead connection (silence vs broken can look the same)

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