"""Formatting for editable card fields accepting text or a list of text items."""


def persona_text(value):
    if isinstance(value, list):
        return '\n'.join(f'- {item}' for item in value)
    return value or ''
