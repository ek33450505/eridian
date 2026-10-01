"""Fake `claude -p` stream-json CLI for test_brain. Never calls the network.

Behaviour is keyed off the user message text:
  say: <words>   text deltas for <words>, then result
  tool: <words>  a Read tool_use, then text, then result
  slow:          many slow deltas; an interrupt control_request ends the turn
  hang:          init, then silence (turn-timeout tests)
  die:           init, one delta, then exit(1)
  garbage:       a non-JSON line and an unknown-type event, then normal output
"""

import json
import os
import queue
import sys
import threading
import time

SESSION_ID = "11111111-2222-4333-8444-555555555555"
# FAKE_CLAUDE_TOOLS overrides the advertised tools (comma list);
# FAKE_CLAUDE_MCP=1 advertises an MCP server; FAKE_CLAUDE_NO_MCP_KEY=1 omits the key.
TOOLS = (os.environ["FAKE_CLAUDE_TOOLS"].split(",") if "FAKE_CLAUDE_TOOLS" in os.environ
         else ["Glob", "Grep", "Read", "WebSearch"])


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def delta(text: str) -> None:
    emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                            "delta": {"type": "text_delta", "text": text}}})


def result(**extra) -> None:
    fields = {"subtype": "success", "is_error": False,
              "session_id": SESSION_ID, "stop_reason": "end_turn", **extra}
    emit({"type": "result", **fields})


def ack_interrupt(req: dict) -> None:
    emit({"type": "control_response", "response": {
        "subtype": "success", "request_id": req.get("request_id"),
        "response": {"still_queued": []}}})


def mcp_init_field() -> dict:
    if os.environ.get("FAKE_CLAUDE_NO_MCP_KEY"):
        return {}
    if os.environ.get("FAKE_CLAUDE_MCP"):
        return {"mcp_servers": [{"name": "evil", "status": "connected"}]}
    return {"mcp_servers": []}


def handle_turn(text: str, inbox: "queue.Queue[dict]") -> None:
    # FAKE_CLAUDE_NO_INIT=1 skips init; FAKE_CLAUDE_DELTA_BEFORE_INIT=1 emits text first.
    if os.environ.get("FAKE_CLAUDE_DELTA_BEFORE_INIT"):
        delta("early ")
    if not os.environ.get("FAKE_CLAUDE_NO_INIT"):
        emit({"type": "system", "subtype": "init", "session_id": SESSION_ID,
              "tools": TOOLS, **mcp_init_field()})
    if text.startswith("say:"):
        for word in text[4:].split():
            delta(word + " ")
        result()
    elif text.startswith("tool:"):
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}]}})
        emit({"type": "user", "message": {"content": [{"type": "tool_result"}]}})
        delta("done reading")
        result()
    elif text.startswith("slow:"):
        for _ in range(60):
            delta("tick ")
            time.sleep(0.05)
            try:
                msg = inbox.get_nowait()
            except queue.Empty:
                continue
            if msg.get("type") == "control_request":
                ack_interrupt(msg)
                result(stop_reason=None, is_error=True)
                return
        result()
    elif text.startswith("hang:"):
        time.sleep(60)
    elif text.startswith("die:"):
        delta("partial ")
        os._exit(1)
    elif text.startswith("garbage:"):
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
        emit({"type": "mystery_event", "x": 1})
        delta("clean")
        result()
    else:
        delta("ok")
        result()


def main() -> None:
    argv_file = os.environ.get("FAKE_CLAUDE_ARGV_FILE")
    if argv_file:
        with open(argv_file, "w") as f:
            f.write(json.dumps(sys.argv[1:]))

    inbox: "queue.Queue[dict | None]" = queue.Queue()

    def read_stdin() -> None:
        for line in sys.stdin:
            try:
                inbox.put(json.loads(line))
            except json.JSONDecodeError:
                pass
        inbox.put(None)

    threading.Thread(target=read_stdin, daemon=True).start()
    while True:
        msg = inbox.get()
        if msg is None:
            return
        if msg.get("type") == "control_request":
            ack_interrupt(msg)
        elif msg.get("type") == "user":
            handle_turn(msg["message"]["content"], inbox)


if __name__ == "__main__":
    main()
