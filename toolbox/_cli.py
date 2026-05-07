import sys
import toolbox
from toolbox.teleserver import main as _teleserver_main
from toolbox.tasker.worker import main as _worker_main


# Simple function commands: called with positional args and --key=value kwargs.
COMMANDS = {
    "git_info": toolbox.git_info,
    "local_ip": toolbox.get_local_ip
}

# Subcommands that own their own argparse.  sys.argv[1] (the subcommand name)
# is stripped before they are invoked so their parsers see a clean argv.
SUBCOMMANDS = {
    "teleserver": _teleserver_main,
    "worker": _worker_main,
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
    all_commands = {**COMMANDS, **SUBCOMMANDS}

    if len(sys.argv) < 2 or sys.argv[1] not in all_commands:
        print("Usage: toolbox <command> [args...]")
        print(f"Commands: {', '.join(all_commands)}")
        sys.exit(1)

    command = sys.argv[1]

    if command in SUBCOMMANDS:
        # Remove the subcommand token so the subcommand's argparse sees sys.argv[1:]
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        SUBCOMMANDS[command]()
    else:
        args, kwargs = _parse_args(sys.argv[2:])
        result = COMMANDS[command](*args, **kwargs)
        if result is not None:
            print(result)
