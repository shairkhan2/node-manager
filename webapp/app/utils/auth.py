from fastapi import Request


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("user"))


