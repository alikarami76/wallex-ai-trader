"""Pre-validated action menu.

Each cycle the quant and risk layers list the actions that are legal right
now. The model picks one id. It cannot invent a symbol, a size, or an order.
A reply that is not strict JSON, names an id that is not on the menu, or
omits the rationale becomes HOLD.

The shape of this loop — code proposes the legal actions, the model
classifies, risk can still refuse — follows the open-source decision pattern
in https://github.com/imikerussell/beebots and https://github.com/snafu4/jev-loop.
The model is BoxAPI. This module does not call TypeSafe's Jev.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from trader.strategy.parser import extract_json


@dataclass
class MenuAction:
    id: str
    kind: str
    symbol: str = ""
    size_fraction: float | None = None
    summary: str = ""
    client_order_id: str = ""

    def as_log(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "symbol": self.symbol,
            "size_fraction": None if self.size_fraction is None else round(float(self.size_fraction), 6),
            "summary": self.summary,
            "client_order_id": self.client_order_id,
        }


def hold_action() -> MenuAction:
    return MenuAction(id="hold", kind="HOLD", summary="HOLD. Do nothing this cycle.")


def parse_menu_choice(text: str, menu: list[MenuAction]) -> tuple[MenuAction, str]:
    """Return the chosen action, or HOLD with the note ``invalid output``."""
    by_id = {item.id: item for item in menu}
    folded = {item.id.lower(): item for item in menu}
    hold = by_id.get("hold") or hold_action()
    try:
        payload = extract_json(text or "")
    except (ValueError, json.JSONDecodeError, TypeError):
        return hold, "invalid output"
    action_id = str(payload.get("action_id") or "").strip()
    rationale = str(payload.get("rationale") or "").strip()
    chosen = by_id.get(action_id) or folded.get(action_id.lower())
    if chosen is None or not rationale:
        return hold, "invalid output"
    return chosen, ""
