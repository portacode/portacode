# Cosmic Cupcake Stand (Django Edition)

Every workshop container starts with this tiny Django project already sitting in `/root/workspace`. It showcases a playful dessert shop with a rotating menu so students immediately have something visual to tweak.

## What's Inside

```
initial_content/
├── galactic_bakeshop/      # Django project settings + URLs
├── treats/                 # Custom app with a view, URLConf, and tests
├── templates/treats/       # HTML the view renders
├── manage.py               # Standard Django entrypoint
└── requirements.txt        # (Already preinstalled in the image) Django version
```

Highlights:

- The homepage randomly spotlights one of several cosmic cupcakes.
- `treats/tests.py` includes a basic integration test so students can instantly run `python manage.py test`.
- The instructions nudge students to add flavors, forms, and database-backed models.

## Quick Start Inside a Container

```bash
# Already in /root/workspace when the container boots
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

Visit the forwarded Portacode port to see the cupcake stand.

## Suggested Exercises

1. Add a new dessert entry to `treats/menu.py` (or make them load from a database model).
2. Create a form that lets visitors vote for their favorite cupcake.
3. Replace the simple randomizer with a view that filters by flavor tags.

Feel free to fork this starter into your own Django adventures!
