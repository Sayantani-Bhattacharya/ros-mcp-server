# test_bug.py
import asyncio
import json
import time
import uuid
import sys

from ros_mcp.utils.websocket import WebSocketManager

ws_manager = WebSocketManager("127.0.0.1", 9091, default_timeout=15.0)

async def test():
    action_name = '/turtle1/rotate_absolute'
    action_type = 'turtlesim/action/RotateAbsolute'
    goal = {'theta': 1.57}
    timeout = 15.0
    goal_id = f"goal_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

    message = {
        "op": "send_action_goal",
        "id": goal_id,
        "action": action_name,
        "action_type": action_type,
        "args": goal,
        "feedback": True,
    }

    with ws_manager:
        _t0 = time.time()
        print(f"[t+0.00s] sending goal...", file=sys.stderr)
        ws_manager.send(message)

        start_time = time.time()
        last_feedback = None
        feedback_count = 0

        while time.time() - start_time < timeout:
            elapsed = time.time() - start_time
            response = ws_manager.receive(timeout - elapsed)

            if response:
                msg = json.loads(response)
                op = msg.get("op")
                print(f"[t+{time.time()-_t0:.2f}s] received op={op}", file=sys.stderr)

                if op == "action_result":
                    print(f"[t+{time.time()-_t0:.2f}s] SUCCESS — got result", file=sys.stderr)
                    print({"success": True, "result": msg.get("values", {})})
                    return

                if op == "action_feedback":
                    feedback_count += 1
                    last_feedback = msg
                    print(f"[t+{time.time()-_t0:.2f}s] feedback #{feedback_count}", file=sys.stderr)
            else:
                print(f"[t+{time.time()-_t0:.2f}s] receive returned None", file=sys.stderr)

            await asyncio.sleep(0.1)

        print(f"[t+{time.time()-_t0:.2f}s] TIMEOUT after {feedback_count} feedbacks", file=sys.stderr)
        print({"success": False, "error": f"timed out", "last_feedback": last_feedback})

asyncio.run(test())