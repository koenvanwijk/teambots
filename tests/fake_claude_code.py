"""Stand-in for `claude -p --output-format stream-json`, used by tests/test_coding.py."""
import json
import sys
import time


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    argv = sys.argv[1:]

    def opt(name):
        return argv[argv.index(name) + 1] if name in argv else ""

    resume, system = opt("--resume"), opt("--append-system-prompt")
    prompt = sys.stdin.read()
    slow = "sleep" in prompt

    def pause(s=0.2):
        if slow:
            time.sleep(s)

    emit({"type": "system", "subtype": "init", "session_id": resume or "sess-1"})
    if "die" in prompt:
        sys.stderr.write("boom")
        sys.stderr.flush()
        sys.exit(2)
    pause()
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}]}})
    pause()
    emit({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}})
    pause()
    text = f"echo:{prompt}|resume:{resume}|system:{system}"
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    pause(30)
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "total_cost_usd": 0.01,
          "usage": {"input_tokens": 5, "output_tokens": 2}})


if __name__ == "__main__":
    main()
