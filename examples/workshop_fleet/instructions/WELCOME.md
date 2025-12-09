# Workshop Instructions

This folder is mounted read-only inside every student's workspace at `/root/workspace/instructions`.

- Update these files from the host machine to broadcast new steps.
- Students can reference the material live without being able to modify it.

## Agenda

1. Pair your container with Portacode using the provided pairing code.
2. Jump into the Django project that was copied into `/root/workspace`.
3. Follow the launch checklist:
   - `python manage.py migrate`
   - `python manage.py test`
   - `python manage.py runserver 0.0.0.0:8000`
4. Customize Cosmic Cupcake Stand by completing the exercises below.

## Exercises

- Add a new cupcake (or three!) in `treats/menu.py`, refresh, and see it appear.
- Swap the featured cupcake logic to highlight the cheapest dessert instead of choosing randomly.
- Build a model + view that lets classmates vote for their favorite cupcake—start with the `FavoriteCupcake` model that already lives in `treats/models.py`.
- Update `treats/templates/treats/home.html` with your own colors, layout, or images.

Happy hacking!
