# Edge2 Smart Inventory Management System

A QR-code-based inventory management system built with Python Flask, extended with real hardware integration — Raspberry Pi GPIO-driven LED column indicators and I2C temperature/humidity sensors with fire-risk push alerts. Items are organised in a `Column → Box → Item` hierarchy and tracked in real time via QR scanning.

---

## Features

### Core Inventory
- Hierarchical storage structure — Columns → Boxes → Items
- Real-time stock tracking with quantity and per-item minimum stock thresholds
- Low stock alerts with sidebar badge counter, dashboard out-of-stock banner
- Bulk restock ("Restock All") and individual item restock
- CSV import for bulk inventory upload, CSV export for transactions/inventory
- Project tagging on transactions + returnable/non-returnable item flag

### QR Scanner
- Camera-based QR code scanning (mobile-optimised, jsQR — loaded locally, no CDN)
- Workflow: Scan Column → Select Box → Select Item → Choose Action (Take / Return / Restock) → Confirm
- Manual column ID entry as fallback
- Quick Return from My Items page without re-scanning

### Hardware Integration (Raspberry Pi)
- **GPIO LED column indicators** — each column is auto-assigned a GPIO pin from a fixed pool (`17, 27, 22, 23, 24, 25` + `100–115`, 22 slots total). Scanning or acting on a column fires a background POST to the Pi's LED API so the physical LED for that column lights up.
- **Pi communication is fire-and-forget** — requests run in a background thread with a short timeout; if the Pi is offline the app never breaks.
- **I2C temperature/humidity sensors** — multiple physical sensors (`temp_sensors` table, keyed by I2C channel + location label) can each be linked to one or more columns. Sensor readings are POSTed to the app from the Pi and stored in `temperature_logs`.
- **Threshold-based fire-risk alerts** — every incoming reading is checked in the background:
  - ≥ 50°C → 🟡 High Temperature Warning
  - ≥ 65°C → 🟠 Possible Fire Warning
  - ≥ 75°C → 🔴 Critical Fire Risk
- **Web Push notifications (PWA)** — browser push via VAPID keys + `pywebpush`, delivered to all subscribed users through the service worker (`static/service-worker.js`) regardless of whether the app tab is open.
- **Sensor management UI** (`/temp-sensors`) — add/edit/delete sensors, assign them to columns, view live + historical readings per sensor.
- Pi-side endpoints authenticate with a bearer token (`PI_API_TOKEN`), separate from user sessions.

### User Management
- Role-based access control: Admin, Storekeeper, Employee
- Admin: full access including user management, analytics, delete operations
- Storekeeper: inventory management, restocking, reports
- Employee: scanner, personal borrowing history, profile

### Reporting & Analytics
- Transaction log with search and filter
- Advanced reports — filter by date, user, column, transaction type
- Analytics dashboard — stock health, daily activity chart, top items, usage by user

### Other
- Profile page with password change and borrowing overview
- Active borrowings tracker per user
- Mobile responsive UI with sidebar toggle, PWA install support
- Confirm modals for destructive actions, toast notifications
- Custom 404/500 error pages

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3, Flask |
| Database | SQLite (via `sqlite3`) |
| Frontend | Jinja2 templates, vanilla JS |
| QR Generation | `qrcode`, Pillow |
| QR Scanning | jsQR.js (local) |
| Push Notifications | Web Push API, `pywebpush`, `py-vapid`, Service Worker |
| Hardware Control | Raspberry Pi (GPIO LEDs, I2C temp/humidity sensors) |
| Production Server | Gunicorn |
| Hosting | Railway |

---

## Hardware Architecture

```
┌─────────────────┐        HTTP (Bearer token)        ┌──────────────────────┐
│  Flask App       │◄──────────────────────────────────│  Raspberry Pi         │
│  (Railway/local) │   POST /api/temperature            │  - I2C temp sensors   │
│                   │──────────────────────────────────►│  - GPIO LEDs per      │
│                   │   POST {pi_url}/led                │    column             │
└─────────────────┘                                     └──────────────────────┘
        │
        │ Web Push (VAPID)
        ▼
┌─────────────────┐
│  Subscribed      │
│  browsers (PWA)  │
└─────────────────┘
```

- **Flask → Pi:** on a column action, the app looks up `columns.gpio_pin` and POSTs `{gpio_pin, column}` to `PI_API_URL/led` with a `PI_API_TOKEN` bearer header, to light the corresponding LED.
- **Pi → Flask:** the Pi reads I2C temperature/humidity sensors and POSTs `{temperature, humidity, sensor_id}` to `/api/temperature`, authenticated with the same bearer token.
- **Flask → Users:** if a reading crosses a threshold, the app pushes a browser notification to every subscribed user via Web Push, independent of the Pi and independent of whether anyone has the app open.

---

## Getting Started (Local)

### Prerequisites
- Python 3.11+ (see `mise.toml` / `.python-version`)
- pip

### Installation

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/edge2-inventory.git
cd edge2-inventory

# Create and activate virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # Mac/Linux

# Install dependencies
pip install -r requirements.txt

# Run the app
python app.py
```

The app will be available at `http://localhost:5000`

### Environment Variables

| Variable | Purpose |
|---|---|
| `VAPID_PRIVATE_KEY` | Web Push private key (EC, PEM body only) |
| `VAPID_PUBLIC_KEY` | Web Push public key, sent to browsers for subscription |
| `VAPID_CLAIMS_EMAIL` | Contact email used in VAPID claims |
| `PI_API_URL` | Base URL of the Raspberry Pi's LED API (e.g. `http://<pi-ip>:5001`) |
| `PI_API_TOKEN` | Shared bearer token — used by the app to call the Pi's `/led` endpoint, and by the Pi to authenticate calls to `/api/temperature` |

> If `PI_API_URL` is unset, LED triggers are silently skipped — the app runs fine without a Pi attached.

### Generating VAPID Keys (for Push Notifications)

```bash
# With py-vapid installed (already in requirements.txt)
vapid --gen

# This creates private_key.pem / public_key.pem in the current folder.
# Extract just the key bodies (no PEM header/footer, no line breaks) and
# set them as VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY in your .env
```

Add the resulting values to a `.env` file in the project root:

```bash
VAPID_PRIVATE_KEY=your_private_key_body
VAPID_PUBLIC_KEY=your_public_key_body
VAPID_CLAIMS_EMAIL=admin@edge2.com
```

### First-Run Database Setup

No manual migration step is needed — `app.py` creates `instance/inventory.db` and all tables automatically on first run, and seeds default users/columns/boxes/items. Just run:

```bash
python app.py
```

If you ever need a clean slate, stop the app and delete `instance/inventory.db`, then restart.

---

## Software Setup — Service Worker / PWA (Push Notifications)

The service worker (`static/service-worker.js`) is what lets browsers receive push alerts even when the app tab is closed. No build step is required — it's plain JS served as a static file — but the following need to be true for it to work:

1. **HTTPS or localhost only**
   Service workers only register on `https://` origins or `http://localhost`. Push notifications will silently fail to register on plain HTTP in production, so Railway (which serves HTTPS by default) works out of the box; local dev on `localhost:5000` also works.

2. **VAPID public key must be present**
   `base.html` reads `vapid_public_key` from the context processor (`inject_globals()` in `app.py`), which comes from `VAPID_PUBLIC_KEY`. If that env var is empty, push setup silently no-ops — set it before testing (see "Generating VAPID Keys" above).

3. **Registration happens automatically on page load**
   Once logged in, `base.html`'s `initPushNotifications()` script runs on every page load:
   - Registers `/service-worker.js` with scope `/`
   - Prompts the browser's native "Allow notifications?" permission dialog
   - Subscribes via `PushManager` using the VAPID public key
   - Syncs the subscription to the server via `POST /api/push/subscribe`
   No manual steps needed beyond accepting the browser permission prompt on first login.

4. **Verify it's working**
   - Check browser DevTools → Application → Service Workers to confirm `service-worker.js` shows as "activated and running".
   - Trigger a manual test push: `POST /api/push/test` (as a logged-in user) or `POST /api/push/test-sync` for a synchronous per-subscriber result — both are already wired as Flask routes.
   - A real-world trigger is any temperature reading ≥ 50°C posted to `/api/temperature`.

5. **Resetting a broken subscription**
   If notifications stop arriving after redeploying (VAPID keys changed, etc.), clear the site's service worker in DevTools → Application → Service Workers → Unregister, then reload the page to re-subscribe. Dead subscriptions (410/404 from the push service) are also auto-pruned server-side by `send_push_to_all()`.

---

## Hardware Setup — Raspberry Pi (LEDs & Temperature Sensors)

The Pi side is a separate lightweight script (`pi_server.py` or legacy `pi_api.py`) that isn't part of this repo — it runs on the Pi itself and talks to this Flask app over HTTP.

1. **Wire the hardware**
   - Connect LEDs to the GPIO pins that will be auto-assigned to columns (pool: `17, 27, 22, 23, 24, 25, 100–115`).
   - Connect I2C temperature/humidity sensors (e.g. via a TCA9548A multiplexer if using more than one on the same address) to the Pi's I2C bus, and note each sensor's `i2c_channel`.

2. **Set up the Pi-side server**
   - Install Flask (or your chosen micro-framework) and a GPIO library (`RPi.GPIO` or `gpiozero`) plus an I2C library (`smbus2`/`adafruit-circuitpython-*` depending on sensor model) on the Pi.
   - Expose a `POST /led` endpoint that accepts `{gpio_pin, column}` and drives the corresponding GPIO pin, authenticated with the same bearer token as `PI_API_TOKEN`.
   - Run a loop/cron job that reads each sensor and `POST`s `{temperature, humidity, sensor_id}` to this app's `/api/temperature` endpoint with header `Authorization: Bearer <PI_API_TOKEN>`.

3. **Point the Flask app at the Pi**
   - Set `PI_API_URL` (e.g. `http://192.168.1.50:5001`) and `PI_API_TOKEN` (any strong shared secret) as environment variables on the Flask app's host.
   - Use the same `PI_API_TOKEN` value on the Pi script.

4. **Register sensors in the app**
   - Log in as admin/storekeeper, go to `/temp-sensors`, and add each physical sensor (name, location label, i2c_channel) and assign it to one or more columns.

5. **Enable push notifications in the browser**
   - Open the app, allow notification permission when prompted (handled by `static/service-worker.js`).
   - Fire a test alert from an admin account via the `/api/push/test` endpoint (or a "Send Test Push" button if present in the UI) to confirm delivery.

> The app works fully without a Pi connected — GPIO/LED calls are skipped, and temperature endpoints simply won't receive data until a Pi starts posting to them.

### Deploying to Railway

```bash
# Procfile is already set to: web: gunicorn app:app
railway login
railway init
railway up

# Then set the environment variables in the Railway dashboard:
# VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY, VAPID_CLAIMS_EMAIL, PI_API_URL, PI_API_TOKEN
```

---

## Default Login Credentials

| Role | Email | Password |
|---|---|---|
| Admin | admin@edge2.com | admin123 |
| Storekeeper | store@edge2.com | store123 |
| Employee | alice@edge2.com | alice123 |
| Employee | bob@edge2.com | bob123 |

> **Note:** Change these credentials before any production use.

---

## Project Structure

```
inventory_system/
├── app.py                     # Main Flask application (routes, DB schema, Pi/push logic)
├── requirements.txt           # Python dependencies
├── Procfile                   # Gunicorn start command for Railway
├── mise.toml                  # Python version pin
├── .env                       # VAPID keys (local, not committed)
├── instance/
│   └── inventory.db           # SQLite database (runtime)
├── static/
│   ├── js/
│   │   └── jsQR.min.js        # QR code scanning library (local, no CDN)
│   ├── qrcodes/                # Generated column QR code images (runtime)
│   └── service-worker.js      # PWA service worker — handles push notifications
└── templates/
    ├── base.html               # Base layout, sidebar, CSS variables
    ├── dashboard.html          # Role-aware dashboard, out-of-stock banner
    ├── scanner.html             # QR scanner workflow
    ├── columns.html / add_column.html / edit_column.html / column_detail.html
    ├── boxes.html / add_box.html / edit_box.html / box_detail.html
    ├── items.html / add_item.html / edit_item.html / item_detail.html
    ├── import_items.html       # CSV bulk import
    ├── inventory.html          # Full inventory overview
    ├── low_stock.html          # Low stock page + restock-all
    ├── transactions.html       # Transaction log
    ├── reports.html            # Advanced reports with filters
    ├── analytics.html          # Admin analytics dashboard
    ├── temp_sensors.html       # Sensor management (add/edit/assign to columns)
    ├── projects.html / add_project.html
    ├── profile.html / user_profile.html
    ├── my_items.html            # Active borrowings + quick return
    ├── history.html             # Personal transaction history
    ├── users.html / add_user.html   # User management (admin)
    ├── login.html
    ├── stub.html
    ├── 404.html / 500.html
```

---

## Database Schema

Key tables beyond the core `users / columns / boxes / inventory_items / transactions`:

```sql
-- Columns carry a GPIO pin for their physical LED
ALTER TABLE columns ADD COLUMN gpio_pin INTEGER;

CREATE TABLE push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    subscription_json TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, subscription_json)
);

CREATE TABLE temp_sensors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    location_label TEXT NOT NULL,
    i2c_channel INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE temp_sensor_columns (
    sensor_id INTEGER NOT NULL,
    column_id INTEGER NOT NULL,
    PRIMARY KEY (sensor_id, column_id),
    FOREIGN KEY(sensor_id) REFERENCES temp_sensors(id) ON DELETE CASCADE,
    FOREIGN KEY(column_id) REFERENCES columns(id) ON DELETE CASCADE
);

CREATE TABLE temperature_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temperature REAL NOT NULL,
    humidity REAL,
    sensor_id INTEGER REFERENCES temp_sensors(id) ON DELETE SET NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## Key API Endpoints (Hardware / Push)

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/trigger-led` | POST | (Session auth) Light the LED for a given column |
| `/api/column/preview-gpio` | GET | Preview the next auto-assigned GPIO pin |
| `/api/temperature` | POST | (Pi bearer token) Log a temperature/humidity reading, fires threshold alerts |
| `/api/temperature/latest` | GET | Latest reading |
| `/api/temperature/history` | GET | Last 100 readings |
| `/api/temperature/by-sensor` | GET | Readings grouped by sensor |
| `/temp-sensors` | GET | Sensor management page |
| `/temp-sensors/save` | POST | Add/edit a sensor and its column assignments |
| `/temp-sensors/delete/<id>` | POST | Remove a sensor |
| `/api/push/vapid-public-key` | GET | Public key for browser subscription |
| `/api/push/subscribe` | POST | Register a browser for push notifications |
| `/api/push/unsubscribe` | POST | Remove a push subscription |
| `/api/push/test` | POST | Send a test push to all subscribers |

---

## Roles & Permissions

| Feature | Admin | Storekeeper | Employee |
|---|:---:|:---:|:---:|
| Dashboard (full stats) | ✓ | ✓ | — |
| Scanner | ✓ | ✓ | ✓ |
| My Items / History | ✓ | ✓ | ✓ |
| Inventory / Items / Boxes / Columns | ✓ | ✓ | — |
| Restock | ✓ | ✓ | — |
| Transactions | ✓ | ✓ | — |
| Reports & Export | ✓ | ✓ | — |
| Temp Sensor Management | ✓ | ✓ | — |
| Analytics | ✓ | — | — |
| User Management | ✓ | — | — |
| Delete Operations | ✓ | — | — |

---
