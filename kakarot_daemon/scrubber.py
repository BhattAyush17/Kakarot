"""In-memory sanitizer and privacy filter."""
import re

SENSITIVE_APPS = {"keepassxc", "bitwarden", "1password", "polkit", "polkit-gnome-authentication-agent-1"}

def is_app_sensitive(app_name: str) -> bool:
    """Check if the application is blacklisted from screen scraping."""
    if not app_name:
        return False
    app_lower = app_name.lower()
    for sensitive in SENSITIVE_APPS:
        if sensitive in app_lower:
            return True
    return False

# Patterns to mask high-entropy secrets
PATTERNS = [
    # Google API Key (Legacy AIza)
    (re.compile(r"AIza[0-9A-Za-z-_]{20,40}"), "[REDACTED_GOOGLE_API_KEY]"),
    # Google API Key (New AQ.)
    (re.compile(r"AQ\.[A-Za-z0-9_-]{30,}"), "[REDACTED_SECRET]"),
    # AWS Access Key ID
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    # Private Key
    (re.compile(r"-----BEGIN .*?PRIVATE KEY-----.*?-----END .*?PRIVATE KEY-----", re.DOTALL), "[REDACTED_PRIVATE_KEY]"),
    # JWT Tokens (simple heuristic for long base64 strings split by dots)
    (re.compile(r"eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.[A-Za-z0-9-_.+/=]*"), "[REDACTED_JWT_TOKEN]"),
    # Env assignment of secrets
    (re.compile(r"(?i)(password|secret|token|key)\s*=\s*[^\s]+"), r"\1=[REDACTED_SECRET]"),
]

def sanitize_text(text: str) -> str:
    """Mask common secrets before network transmission."""
    if not text:
        return ""
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text
