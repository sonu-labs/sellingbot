"""VIP / loyalty spend and refund hooks used by allapps.py and server4.py."""


async def aspend(user_id: int, amount: float, source: str = "") -> None:
    """Async spend hook (no-op unless external loyalty tracking is attached)."""
    return None


async def arefund(user_id: int, amount: float, source: str = "") -> None:
    """Async refund hook (no-op unless external loyalty tracking is attached)."""
    return None


def spend(user_id: int, amount: float, source: str = "") -> None:
    return None


def refund(user_id: int, amount: float, source: str = "") -> None:
    return None
