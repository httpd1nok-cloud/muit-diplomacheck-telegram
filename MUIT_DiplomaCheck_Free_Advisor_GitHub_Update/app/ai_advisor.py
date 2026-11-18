"""Paid API is deliberately disabled in the free adviser release.

Legacy Railway variables are ignored. No requests go to OpenAI.
"""


def enabled() -> bool:
    return False


def ai_reply(*_args, **_kwargs) -> str:
    raise RuntimeError('External AI is disabled in this free release.')
