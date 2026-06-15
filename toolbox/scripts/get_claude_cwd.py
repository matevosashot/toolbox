# Equivalent of:
#   cat ~/.claude/projects/*/{sess_id}.jsonl | jq -r '.cwd' | grep -v null | tail -1
# but without relying on jq.
import glob
import json
import os


def get_claude_cwd(session_id):
    """Return the last non-null ``cwd`` recorded in a Claude session log.

    Searches ``~/.claude/projects/*/<session_id>.jsonl`` for JSONL records
    and returns the ``cwd`` value of the last record that has a non-null one.
    Returns ``None`` if nothing matches.
    """
    pattern = os.path.expanduser(
        os.path.join("~", ".claude", "projects", "*", f"{session_id}.jsonl")
    )

    last_cwd = None
    for path in glob.glob(pattern):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = record.get("cwd")
                if cwd is not None:
                    last_cwd = cwd

    return last_cwd
