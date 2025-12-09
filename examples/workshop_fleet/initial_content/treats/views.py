from __future__ import annotations

import random
from datetime import datetime

from django.shortcuts import render

from .menu import CUPCAKES


def home(request):
    """Render the cupcake menu with a randomly featured treat."""
    featured = random.choice(CUPCAKES)
    return render(
        request,
        "treats/home.html",
        {
            "featured": featured,
            "cupcakes": CUPCAKES,
            "generated_at": datetime.utcnow(),
        },
    )
