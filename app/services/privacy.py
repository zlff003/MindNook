import re


PII_PATTERNS = (
    (
        re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
        "[PHONE]",
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9._%+-])"
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
            r"(?![A-Za-z0-9.-])"
        ),
        "[EMAIL]",
    ),
    (
        re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
        "[ID_CARD]",
    ),
    (
        re.compile(r"(?<!\d)\d{8,12}(?!\d)"),
        "[STUDENT_ID]",
    ),
)


def redact_pii(text: str) -> str:
    redacted = text
    for pattern, replacement in PII_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted
