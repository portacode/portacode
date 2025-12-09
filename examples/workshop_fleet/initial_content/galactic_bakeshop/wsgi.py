"""
WSGI config for galactic_bakeshop project.
"""

from __future__ import annotations

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "galactic_bakeshop.settings")

application = get_wsgi_application()
