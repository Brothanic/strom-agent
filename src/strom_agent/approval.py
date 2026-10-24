"""Human-in-the-loop decisions for tools marked `requires_approval`.

`Toolbox.call` asks one of these functions before running such a tool. The
decision is made here, in code: the model only sees the outcome, so nothing
it writes can approve its own action.
"""

from collections.abc import Callable


def ask_on_terminal(tool_name: str, preview: str, read_line: Callable[[str], str] = input,
                    write: Callable[[str], None] = print) -> bool:
    """Show the exact content and accept only an explicit 'y'."""
    write(f"\nThe agent wants to run {tool_name} with exactly this content:\n")
    write(preview)
    try:
        answer = read_line("Approve? [y/N] ")
    except EOFError:  # no interactive input available, e.g. a container without -it
        write("No input available: rejected.")
        return False
    return answer.strip().lower() == "y"


def reject_without_prompt(tool_name: str, preview: str) -> bool:
    """Used in evals: never prompt, always reject. The trace records the rejection."""
    return False
