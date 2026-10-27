import re
import shlex

TIER_1_READONLY = {
    "cat", "ls", "grep", "ip", "df", "free", "uptime", "find",
    "stat", "pwd", "whoami", "du", "tail", "head", "wc",
    "systemctl", "journalctl"
}

SYSTEMCTL_READONLY = {
    "status", "show", "is-active", "is-enabled", "is-failed"
}

SHELL_OPERATORS = {"|", ">", ">>", "<", "&", "&&", "||", ";"}

# Dangerous constructs that can hide inside a single token (glued redirects,
# command substitution, process substitution, backgrounding).
_DANGEROUS_PATTERNS = [
    re.compile(r"\$\("),      # $(...) command substitution
    re.compile(r"`"),         # backtick substitution
    re.compile(r"<\("),       # <(...) process substitution
    re.compile(r">\("),       # >(...) process substitution
    re.compile(r"\d?>>?"),    # output redirection (also 2>, 2>>)
    re.compile(r"\d?<&?"),    # input redirection / fd duplication
    re.compile(r"\|"),        # pipe (glued or standalone)
]


def _contains_operator(command: str) -> bool:
    """Scan the raw command for shell constructs that shlex may glue into
    larger tokens, never flagging operators inside quoted strings."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    except ValueError:
        return True
    try:
        prev = None
        for token in lexer:
            if token in SHELL_OPERATORS or token in {"`", "${"}:
                return True
            # shlex punctuation mode splits "$(" into "$" + "(" — catch the pair
            if prev in {"$", "<", ">"} and token == "(":
                return True
            if any(p.search(token) for p in _DANGEROUS_PATTERNS):
                return True
            prev = token
    except ValueError:
        return True
    return False


def requires_hitl(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return True

    if not tokens:
        return True

    if _contains_operator(command):
        return True

    # Leading environment assignments (VAR=1 foo ...) can preload hostile
    # env vars; route them to HITL (fail closed).
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        return True
    i = 0

    # Normalize an absolute/relative path to its basename.
    base = tokens[i].rsplit("/", 1)[-1]
    if base not in TIER_1_READONLY:
        return True

    if base == "systemctl":
        if i + 1 >= len(tokens):
            return True
        if tokens[i + 1] not in SYSTEMCTL_READONLY:
            return True

    return False
