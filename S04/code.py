#!/usr/bin/env python3

"""
s03_permission.py - Permission System
Three gates inserted before tool execution:
    Gate 1: Hard deny list (rm -rf /, sudo, ...)
    Gate 2: Rule matching (write outside workspace? destructive cmd?)
    Gate 3: User approval (pause and wait for confirmation)
    +-------+    +--------+    +--------+    +--------+    +------+
    | Tool  | -> | Gate 1 | -> | Gate 2 | -> | Gate 3 | -> | Exec |
    | call  |    | deny?  |    | match? |    | allow? |    |      |
    +-------+    +--------+    +--------+    +--------+    +------+
         |            |             |             |
         v            v             v             v
      (normal)     (blocked)    (ask user)   (user says no?)
Only one line added to the agent loop:
    if not check_permission(block):
        continue
Builds on s02 (multi-tool). Usage:
    python s03_permission/code.py
    Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

import os
import subprocess
import time
from pathlib import Path
from anthropic import Anthropic, RateLimitError
from dotenv import load_dotenv
import platform
import re
from gradio.themes.builder_app import history

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd().resolve()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.getenv("MODEL_ID")

#系统提示词 约束系统使用windows的bash指令
SYSTEM = f"You are a coding agent at {os.getcwd()}.The shell is {platform.system()} ({'cmd.exe' if platform.system()=='Windows' else 'bash'}). All destructive operations require user approval. "


# ═══════════════════════════════════════════════════════════
#  FROM s01 (unchanged)
# ═══════════════════════════════════════════════════════════
#加路径逃逸检测
def  looks_like_escape(command: str) -> bool:
    patterns = [
        r"\.\.",                      # 任何 .. （向上穿越）
        r"[A-Za-z]:[\\/]",            # Windows 盘符 C:\ 或 C:/
        r"(?<![A-Za-z0-9_])/(?!/)",    # Unix 绝对路径（排除 // 和 URL）
    ]
    return any(re.search(p, command) for p in patterns )

def run_bash(command : str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/" ]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"

    if looks_like_escape(command):
        return  ("Error: command may access paths outside the workspace; "
                "refused. Use the file tools for file operations.")

    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "no output"
    except subprocess.TimeoutExpired :
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

# ═══════════════════════════════════════════════════════════
#  NEW in s02: (unchanged)
# ═══════════════════════════════════════════════════════════
def safe_path(p: str) -> Path:
    if p is None:
        raise ValueError("Path is required")
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_read(path : str, limit: int | None = None ) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path : str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return  f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path : str, old_text : str, new_text : str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

def run_glob(pattern : str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "no matches"
    except Exception as e:
        return f"Error: {e}"

# ═══════════════════════════════════════════════════════════
#  FROM s02 (unchanged): Tool Definitions & Dispatch
# ═══════════════════════════════════════════════════════════

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]

TOOL_HANDLERS = {
    "bash": run_bash,"read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s03: Three-Gate Permission Pipeline
# ═══════════════════════════════════════════════════════════
# Gate 1: Hard deny list — always forbidden
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda" ]

def check_deny_list(command: str) ->str|None:
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked：‘{pattern}’ is on the deny list"
    return None

# Gate 2: Rule matching — context-dependent checks
Permission_RULES = [
    {
        "tools": ["write_file", "edit_file",],
        "check": lambda args: not (WORKDIR / args.get("path")).resolve().is_relative_to(WORKDIR),
        "messages": "Writing outside workspace"},
    {
        "tools": ["bash"],
        "check": lambda args: any(kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]),
        "messages": "Potentially destructive command"
    }
]

def check_rules(tool_name: str, args: dict) -> str|None:
    for rule in Permission_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["messages"]
    return None

# Gate 3: User approval — wait for confirmation after rule match
def ask_user(tool_name: str, args: dict, reason: str) -> str:
    print(f"\n\033[33m⚠  {reason}\033[0m")
    print(f"   Tool: {tool_name}({args})")
    choice =input("  Allow?[y/N]").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"

# Pipeline: all three gates chained
def check_permission(block) -> bool:
    if block.name == "bash" :
        reason =check_deny_list(block.input.get("command", ""))
        if reason:
            print(f"\n\033[31m⛔ {reason}\033[0m")
            return False
    reason = check_rules(block.name, block.input)
    if reason:
        decision = ask_user(block.name, block.input, reason)
        if decision == "deny":
            return False
    return True

# ═══════════════════════════════════════════════════════════
#  agent_loop — same as s02, with check_permission() inserted
# ═══════════════════════════════════════════════════════════
def agent_loop(messages: list):
    while True:
        print(messages)

        #简易的防止模型每分钟请求超限
        try:
            response = client.messages.create(
                model=MODEL, system=SYSTEM, messages=messages, tools=TOOLS, max_tokens=8000,
            )
        except RateLimitError:
            time.sleep(60)
            continue

        # Append assistant turn
        messages.append({"role" : "assistant","content":response.content})

        # If the model didn't call a tool, we're done
        if response.stop_reason != "tool_use":
            return

        # Execute each tool call, collect results
        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"\033[33m${block.name}\033[0m]")
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                print(str(output)[:200])
                results.append({"type": "tool_result","tool_use_id": block.id , "content":output})

        # Feed tool results back, loop continues
        messages.append({"role" : "user","content": results})

# ── Entry point ──────────────────────────────────────────
if __name__ == "__main__":
    print("s03: Permission")
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m ")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role" : "user","content": query})
        agent_loop(history)
        # Print the model's final text response
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if getattr(block, "type", None) == "text":
                    print(block.text)
        print()