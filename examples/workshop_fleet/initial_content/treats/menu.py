"""
Static data backing the cupcake menu shown on the homepage.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Cupcake:
    name: str
    flavor_notes: str
    price: str
    emoji: str = "🧁"


CUPCAKES: list[Cupcake] = [
    Cupcake(
        name="Solar Flare",
        flavor_notes="orange zest + chili honey buttercream",
        price="5.50",
        emoji="🌞",
    ),
    Cupcake(
        name="Nebula Crunch",
        flavor_notes="blueberry jam, popping candy stardust",
        price="4.75",
        emoji="✨",
    ),
    Cupcake(
        name="Lunar Latte",
        flavor_notes="espresso sponge with caramel moon drizzle",
        price="5.00",
        emoji="🌙",
    ),
    Cupcake(
        name="Comet Confetti",
        flavor_notes="vanilla bean base + rainbow meteor sprinkles",
        price="4.25",
        emoji="☄️",
    ),
]
