from dataclasses import dataclass


@dataclass(frozen=True)
class Request:
    name: str


def normalize(request: Request) -> str:
    return request.name.strip().casefold()


def dispatch(request: Request) -> str:
    return normalize(request)
