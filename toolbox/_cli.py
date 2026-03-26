import sys
import toolbox


COMMANDS = {
    "git_info": toolbox.git_info,
}


def _parse_args(raw):
    """Split raw CLI tokens into positional args and keyword kwargs."""
    args, kwargs = [], {}
    for token in raw:
        if token.startswith("--"):
            key, _, value = token[2:].partition("=")
            kwargs[key] = value
        else:
            args.append(token)
    return args, kwargs


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"Usage: toolbox <command> [args...] [--key=value ...]")
        print(f"Commands: {', '.join(COMMANDS)}")
        sys.exit(1)

    command = sys.argv[1]
    args, kwargs = _parse_args(sys.argv[2:])
    result = COMMANDS[command](*args, **kwargs)
    if result is not None:
        print(result)
