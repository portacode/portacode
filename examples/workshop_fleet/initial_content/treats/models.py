from django.db import models


class FavoriteCupcake(models.Model):
    """Example model students can extend for voting/leaderboards."""

    name = models.CharField(max_length=80)
    votes = models.PositiveIntegerField(default=0)

    def __str__(self) -> str:
        return f"{self.name} ({self.votes} votes)"
