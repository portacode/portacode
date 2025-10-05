# NTP Clock Architecture

## Overview

All entities (client, server, device) synchronize to **time.cloudflare.com** for distributed tracing.

## Architecture: Single Package for Everything

All NTP clock implementations (Python and JavaScript) are in the **portacode package** to ensure DRY principles.

## Python Implementation

**Location:** `portacode/utils/ntp_clock.py` (in portacode package)

### Import Path
```python
from portacode.utils.ntp_clock import ntp_clock
```

### Usage Locations
1. **Django Server Consumers** (`server/portacode_django/dashboard/consumers.py`)
2. **Device Base Handlers** (`portacode/connection/handlers/base.py`)
3. **Device Client** (`server/portacode_django/data/services/device_client.py`)
4. **Any Python code with portacode installed**

### Dependencies
- `setup.py`: Added `ntplib>=0.4.0` to `install_requires`
- `server/portacode_django/requirements.txt`: Added `portacode>=1.3.26`

### API
```python
# Get NTP-synchronized timestamp (None if not synced)
ntp_clock.now_ms()      # milliseconds
ntp_clock.now()         # seconds
ntp_clock.now_iso()     # ISO format

# Check sync status
status = ntp_clock.get_status()
# {
#   'server': 'time.cloudflare.com',
#   'offset_ms': 6.04,
#   'last_sync': '2025-10-05T04:37:12.768445+00:00',
#   'is_synced': True
# }
```

## JavaScript Implementation

**Location:** `portacode/static/js/utils/ntp-clock.js` (in portacode package)

### Django Setup

Django will serve static files from the portacode package automatically after `collectstatic`:

```python
# Django settings.py - no changes needed, just ensure:
INSTALLED_APPS = [
    # ... other apps
    'portacode',  # Add portacode as an installed app (optional, for admin integration)
]

# Static files will be collected from portacode package
STATIC_URL = '/static/'
```

After installing portacode (`pip install portacode` or `pip install -e .`), run:
```bash
python manage.py collectstatic
```

This will copy `portacode/static/js/utils/ntp-clock.js` to Django's static files directory.

### Import Path (in Django templates/JS)
```javascript
import ntpClock from '/static/js/utils/ntp-clock.js';
// or relative to your JS file:
import ntpClock from './utils/ntp-clock.js';
```

### Usage Locations
1. **Dashboard WebSocket** (`websocket-service.js`)
2. **Project WebSocket** (`websocket-service-project.js`)

### API
```javascript
// Get NTP-synchronized timestamp (null if not synced)
ntpClock.now()          // milliseconds
ntpClock.nowISO()       // ISO format

// Check sync status
const status = ntpClock.getStatus();
// {
//   server: 'time.cloudflare.com',
//   offset: 6.04,
//   lastSync: '2025-10-05T04:37:12.768445+00:00',
//   isSynced: true
// }
```

## Design Principles

1. **DRY (Don't Repeat Yourself)**
   - **Python:** Single implementation in portacode package (`portacode/utils/ntp_clock.py`)
   - **JavaScript:** Single implementation in portacode package (`portacode/static/js/utils/ntp-clock.js`)
   - Both served from the same package, no duplication across repos

2. **No Fallback Servers**
   - All entities MUST sync to time.cloudflare.com
   - If sync fails, timestamps are None/null
   - Ensures all timestamps are comparable

3. **Auto-Sync**
   - Re-syncs every 5 minutes automatically
   - Initial sync on import/load
   - Max 3 retry attempts before marking as failed

4. **Thread-Safe (Python)**
   - Uses threading.Lock for concurrent access
   - Background daemon thread for periodic sync

## Testing

### Python
```bash
python tools/test_python_ntp_clock.py
```

### JavaScript
The test file is included in the package at `portacode/static/js/test-ntp-clock.html`.

After Django collectstatic, open: `/static/js/test-ntp-clock.html` in browser

Or run directly from package:
```bash
python -c "import portacode, os; print(os.path.join(os.path.dirname(portacode.__file__), 'static/js/test-ntp-clock.html'))"
```
