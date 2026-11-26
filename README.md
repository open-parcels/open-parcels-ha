<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/open-parcels/open-parcels/master/docs/branding/banner-on-dark.png">
    <img alt="Open Parcels" src="https://raw.githubusercontent.com/open-parcels/open-parcels/master/docs/branding/banner-on-light.png" width="640">
  </picture>
</p>
<p align="center">
  <a href="https://hacs.xyz/docs/faq/custom_repositories/"><img alt="HACS custom repository" src="https://img.shields.io/badge/HACS-custom-41BDF5?logo=homeassistantcommunitystore&logoColor=white"></a>
  <a href="https://github.com/open-parcels/open-parcels-ha/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/open-parcels/open-parcels-ha?style=flat"></a>
  <a href="https://github.com/open-parcels/open-parcels-ha/issues"><img alt="Open issues" src="https://img.shields.io/github/issues/open-parcels/open-parcels-ha"></a>
  <a href="https://github.com/open-parcels/open-parcels-ha/commits"><img alt="Last commit" src="https://img.shields.io/github/last-commit/open-parcels/open-parcels-ha"></a>
  <a href="https://github.com/open-parcels/open-parcels-ha/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/github/license/open-parcels/open-parcels-ha"></a>
</p>

# Open Parcels for Home Assistant

Parcel tracking in Home Assistant from an [Open Parcels](https://github.com/open-parcels/open-parcels)
server. One integration covers every carrier, shop and inbox the server follows:

- each **connection** on the server (a carrier account, a keyless tracking setup, a shop or an inbox) becomes
  a Home Assistant device;
- each **parcel** becomes an entity on its connection's device.

Changes arrive live over the server's event stream. A slow full resync runs every 30 minutes to
catch anything the stream missed, however busy the stream is. The integration never polls in a
tight loop.

Coming from the per-carrier parcel integrations or the Parcel Aggregator? See
[MIGRATION.md](MIGRATION.md) for every renamed entity, attribute, event and service.

## Requirements

- An Open Parcels server 2.0 or newer (its glossary API names), reachable from Home Assistant. This can be the Home Assistant app or a
  container such as `http://192.168.1.10:4000`.
- An API token with the scopes **read** and **write**. Create it in the Open Parcels web interface
  under Settings → API tokens. With the **admin** scope as well, the hub also gets **Back up now**
  and the last backup sensor names the backup file; without it the sensor shows the time and the
  result only.
- Home Assistant 2025.12 or newer.

## Installation

**HACS (custom repository):** HACS → ⋮ → Custom repositories → add
`https://github.com/open-parcels/open-parcels-ha` as an *Integration* → install **Open Parcels** → restart Home Assistant.

**Manual:** copy `custom_components/open_parcels` into your Home Assistant
`config/custom_components/` folder and restart.

Then go to **Settings → Devices & services → Add integration → Open Parcels** and enter the server
URL and the token.

**Discovery:**
- The Open Parcels Home Assistant app announces itself (`discovery: [open_parcels]`). If the HA app
  passes a working `api_token` in its discovery data, you only confirm adding it. Otherwise you are
  asked for a token. When the HA app announces itself again, the entry takes its current address and
  token.
- There is no network (zeroconf) discovery: the server does not advertise itself.

The token can be replaced later with *Reconfigure*. If the server ever refuses the token, Home
Assistant asks for a new one (reauthentication).

## Devices and entities

### Hub device "Open Parcels"

| Entity | Key | State | Attributes |
|---|---|---|---|
| `sensor.open_parcels_receiving_parcels` | `receiving` | number of active receiving parcels | `by_carrier`, `parcels` |
| `sensor.open_parcels_sending_parcels` | `sending` | active sending parcels | `by_carrier`, `parcels` |
| `sensor.open_parcels_delivered_parcels` | `delivered` | recently delivered receiving parcels | `by_carrier`, `parcels` |
| `sensor.open_parcels_sending_delivered_parcels` | `sending_delivered` | recently delivered sending parcels | `by_carrier`, `parcels` |
| `sensor.open_parcels_en_route_to_pickup_point` | `en_route_to_pickup_point` | heading for a pickup point | `by_carrier`, `parcels` |
| `sensor.open_parcels_awaiting_pickup` | `awaiting_pickup` | ready to collect | `by_carrier`, `parcels` |
| `sensor.open_parcels_next_delivery` | `next_delivery` | earliest `expected_from` (timestamp) | `tracking_number`, `sender`, `receiver`, `by_carrier`, `parcel` |
| `sensor.open_parcels_last_successful_update` | `last_update` | last full resync (diagnostic) | |
| `sensor.open_parcels_open_returns` | `open_returns` | returns still going on (servers with returns) | `returns`: order number, shop, items, status, `return_by`, drop-off options, `free`, cost, label URL |
| `calendar.open_parcels_deliveries` | `deliveries` | next expected delivery | the same entries as the server's calendar and ICS feed; a return's last day reads "Return by: <item or shop>" with its drop-off options and cost |
| `button.open_parcels_refresh` | `refresh` | | resyncs everything now |
| `binary_sensor.open_parcels_event_stream` | `event_stream` | event stream connected (diagnostic) | `events_received` |
| `binary_sensor.open_parcels_server_health` | `server_health` | on while the server is `degraded`: a connection or inbox in error or needing sign-in, failing backups or a stalled scheduler (`GET /api/v1/status`, diagnostic) | `status`, `checks` (one state per part), `version`, `started_at`, `checked_at` |
| `sensor.open_parcels_last_backup` | `last_backup` | when the newest backup attempt finished (timestamp, diagnostic) | `ok`, `reason` (`scheduled`, `manual`, `before_delete`), `file`, `bytes`, `error`, `enabled`, `next_at` |
| `button.open_parcels_back_up_now` | `back_up` | | backs the server's database up now; only with an **admin** token |

Parcels that no connection follows (tracked by number only) live on the hub device.

### One device per connection, e.g. "DHL Germany (abc123)"

The device is named `<carrier> <connection label>`. When the label already starts with the carrier's
name, the name is not repeated. It is linked to the hub (`via_device`).

| Entity | State |
|---|---|
| `sensor.<device>_receiving_parcels`, `_en_route_to_pickup_point`, `_awaiting_pickup`, `_delivered_parcels`, `_sending_parcels`, `_sending_delivered_parcels` | count, with `by_carrier` and `parcels` |
| `sensor.<device>_next_delivery` | earliest expected delivery of this connection |
| `sensor.<device>_last_successful_update` | the connection's last successful run on the server (diagnostic) |
| `sensor.<device>_connection_state` | `ok`, `idle`, `needs_sign_in`, `error` or `disabled` (diagnostic), with `detail`, `provider`, `kind`, `last_run_at`, `next_run_at`, `last_ok_at` |
| `binary_sensor.<device>_problem` | on while the connection needs a sign-in or fails (diagnostic) |
| `button.<device>_refresh` | asks the server to run this connection now |
| `button.<device>_<action>` | one per action of the connection's provider marked for Home Assistant (a task that needs no input and is not destructive, e.g. *Import history*); starts it with its defaults |
| `sensor.<device>_job` | the connection's newest job: `queued`, `running`, `paused`, `done`, `partial`, `failed` or `cancelled`, with `job_id`, `action`, `done`, `total`, `phase`, `summary`, `reason`, `resume_at`, `started_at`, `finished_at` |
| `device_tracker.<device>_parcel_<tracking_number>` | one per open parcel (announced, in transit, out for delivery, at a pickup point, returning, problem). State: the status key. On the map once its position is known: the live courier, else the latest event with coordinates. Delivered and finished parcels get no entity; they are listed in the list sensors (`parcel_ids`, `parcels`). Named by the server's *Default parcel label* setting (its label, item names or the tracking number; entity ids always use the number). Entity picture: a grid of its item pictures (1, 2×2 or 3×3), served by Home Assistant under a signed address. |
| `sensor.<device>_order_<shop>_<number>` | optional (*Order entities*): one per open order. State `in_progress` or `partially_delivered`; attributes: shop, order number, `order_id`, `order_status` (the server's order status), `total` (`amount`, `currency`, `partial`: the shop's stated total, a payment receipt's, or the sum of the priced lines), counts, `items` (with the code of the parcel carrying each, or null) and `parcels`. |

A parcel belongs to the connection that follows it as its primary, then a hand-off, then a
inbox.

When a connection is disabled on the server, the server hides the parcels and orders only it
follows; they disappear from Home Assistant too and come back when the connection is enabled again.

### Parcel trackers

The state is the canonical status: `registered`, `in_transit`, `out_for_delivery`,
`at_pickup_point`, `delivered`, `returning`, `problem` or `unknown`. A manual status set on the
server wins over the carrier's status.

Why a tracker and not a sensor: one entity per parcel can carry both the status and the
parcel's position on the map. The price is that Home Assistant shows the state as the raw key
(`in_transit`, not "In transit"), because tracker states are not translated, and it works out
which zones the courier is in (that is kept out of history, see *What the recorder keeps*). For translated
states, use the list sensors or the status in the bus events.

The attributes start with the suite's parcel keys, in the suite's order: `carrier` (display
name), `tracking_number`, `sender`, `receiver`, `status`, `raw_status`, `delivered`, `delivered_at`,
`expected_from`, `expected_to`, `pickup`, `pickup_point`, `url`, `weight`, `dimensions`, `history`,
`raw`. Then come the server's additions: `id`, `carrier_id`, `direction`, `label`,
`carrier_status`, `manual_status`, `items`, `order`, `live` (without coordinates), `lookup`,
`connection_id`, `first_seen_at`, `updated_at`.

- `weight`, `dimensions` and `raw` are always `null`: the server does not publish them.
- `history` is `null` unless the *Include status history* option is on.

Only open parcels have a tracker. A tracker disappears when its parcel is delivered, archived or
removed; delivered parcels stay in the list sensors (see *Delivered parcels* in the options).

### Parcel positions

A parcel tracker points at the courier **right now** while a live session runs, otherwise at the
latest carrier event that came with coordinates (`position_source`: `courier` or `scan`). Without
either it has no coordinates and Home Assistant leaves it off the map; the entity stays, with its
status as the state. Latitude, longitude, accuracy, the fix time, stops-away and the zones Home
Assistant derives from the position (`in_zones`) are kept out of the recorder, so Home Assistant
stores no trail. The courier stays on the map until the parcel is delivered, the last stop
included.

### What the recorder keeps

Kept out of history: the trackers' `sender`, `receiver`, `url`, `pickup_point`, `display_name`,
`label`, `items`, `order`, `history`, `live` and `lookup`; the next-delivery sensors' `tracking_number`,
`sender`, `receiver` and `parcel`; the list sensors' `parcels`; the order sensors' `shop`,
`order_number`, `order_id`, `total`, `items` and `parcels`; the returns sensor's `returns`; the calendar's
`description` and `location`. Still recorded: states, the trackers' `tracking_number` and status keys,
and the calendar's `message` (the next entry's title).

Entity ids carry the tracking number (`device_tracker.<device>_parcel_<tracking_number>`) and order sensor
names the order number, so both appear in the states, statistics and logbook tables. They are
kept so automations written for earlier versions keep working.

Item pictures (`entity_picture`) are served by Home Assistant under a signed address that
expires after 12 to 24 hours; a copied address stops working after that.

## Events and device triggers

These server events are fired on the Home Assistant bus as `open_parcels_<type>`:

| Server event | Home Assistant event / device trigger |
|---|---|
| `parcel.registered` | `open_parcels_parcel_registered` |
| `parcel.status_changed` | `open_parcels_parcel_status_changed` (`old_status`, `new_status`) |
| `parcel.delivered` | `open_parcels_parcel_delivered` (the only event for the hop to delivered) |
| `parcel.delivery_time_changed` | `open_parcels_parcel_delivery_time_changed` (`old_*`, `new_*`) |
| `parcel.sending_registered` | `open_parcels_sending_parcel_registered` |
| `parcel.sending_delivered` | `open_parcels_sending_parcel_delivered` |
| `parcel.live_updated` | `open_parcels_parcel_live_updated` |
| `parcel.removed` | `open_parcels_parcel_removed` |
| `parcel.deleted` | `open_parcels_parcel_deleted` (deleted on the server, or merged into another parcel: `merged_into`) |
| `connection.state_changed` | `open_parcels_connection_state_changed` (`connection_id`, `provider`, `label`, `old_state`, `new_state`, `detail`) |
| `return.created` | `open_parcels_return_created` (`return_id` plus the `returns` attribute's fields) |
| `return.updated` | `open_parcels_return_updated` (the same fields) |
| `connection.removed` | `open_parcels_connection_removed` (`connection_id`, `provider`, `label`, `merged_into`); bus event only, no device trigger |
| `order.created` | `open_parcels_order_created` (`order_id`, `shop`, `order_number`, `display_name`, `status`, `placed_at`, `digital`, `total`, `item_count`, `hub_device_id`): an order email started an order; bus event only |
| `job.paused` | `open_parcels_job_paused` (`job_id`, `connection_id`, `provider`, `label`, `action`, `state`, `reason`, `summary`, `resume_at`, `device_id`, `hub_device_id`); bus event only |
| `job.finished` | `open_parcels_job_finished` (the same fields; `state` is `done`, `partial`, `failed` or `cancelled`); bus event only |
| `backup.finished` with `ok: false` | `open_parcels_backup_failed` (`reason`, `at`, `error`, `hub_device_id`); bus event only. Every result also updates the last backup sensor |

History a connection action imports (Amazon "Import history") is stored but never fired: its
server events carry `imported: true` and fire no bus event, and the batch's `parcel.imported`
only reloads the data. An import of years therefore never runs "delivered" automations; use
`open_parcels_job_finished` to learn that it is done.

Other server events update the data without a bus event of their own: `parcel.live_position`
(the courier's position), `parcel.location_changed` (a parcel placed at a new event),
`connection.added`, `connection.updated` and `connection.run_finished` (a connection's name and state),
`order.updated` and `order.deleted` (the order on the parcels), and `settings.updated` (reloads
everything). A connection's state change, run or removal and every backup also re-read the
server's status for the health sensor (at most every 10 seconds).

Each change fires once. A message the server replays after a reconnect, or the same change sent
again, does not fire a second time.

A deleted parcel or connection disappears at once: its entities, and the connection's device, are removed
from the registries. Parcels the removed connection owned move to their next connection's device (or the
hub). The periodic resync removes anything else the server no longer has.

Parcel events carry the parcel attributes listed above (without `raw`, `items`, `order`,
`history`, `live` and `lookup`, which would fill the recorder's event tables), the server's `old_*` and
`new_*` fields, `device_id` (the parcel's connection device) and `hub_device_id`. Device triggers of
the same names exist on every device: a connection device's triggers fire for its own parcels, the
hub's for all of them.

- Events missed while the stream was disconnected are replayed with `Last-Event-ID`. The server
  keeps them for 24 hours (short-lived courier and connection-run events for 15 minutes). After a
  longer outage it asks for a full reload instead, and the missed events are not fired. After
  every reconnect one reload follows anyway, so a gap cannot stay unnoticed.
- Events from while Home Assistant was not running are not replayed: like the per-carrier
  integrations, a restart does not re-announce old changes.

## Services

| Service | Fields | Does |
|---|---|---|
| `open_parcels.track_parcel` | `tracking_number`, optional `carrier`, `label`, `direction`, `config_entry_id` | starts tracking a number; returns `parcel_id` and `carrier` |
| `open_parcels.archive_parcel` | `tracking_number` or `parcel_id`, optional `config_entry_id` | archives the parcel on the server |
| `open_parcels.refresh` | optional `tracking_number` or `parcel_id`, `config_entry_id` | runs the connections that follow the parcel (if given), then resyncs |
| `open_parcels.run_action` | `device_id` (the connection's device), `action`, optional `input`, `confirm` | starts one of the connection's actions; a destructive one only with `confirm: true`; returns `url` for a link |
| `open_parcels.cancel_job` | `device_id` (the connection's device) | cancels the job the connection is busy with; what it imported stays |

`config_entry_id` is only needed when more than one server is set up.

## Options

- **Full resync interval** (default 30 min).
- **Include status history** (off).
- **Order entities** (off): a sensor per open order, with its items and parcels.
- **Delivered parcels: filter by** days or number of parcels, and the amount (default: last 7
  days).

## Diagnostics

The diagnostics download redacts the token, the URL, tracking numbers, parcel display names, names,
addresses, shops and order numbers, item titles, place names, connection labels and configuration,
and coordinates.

## Development

The gate is the suite's: `ruff check .`, `ruff format --check .`,
`mypy custom_components/open_parcels` (settings in `mypy.ini`) and
`python -m pytest tests/ --cov=custom_components.open_parcels`. Coverage stays above 95 %.
`pre-commit install` runs ruff, mypy and the size limits (1000 lines per file, 200 per
function, `tests/test_size_limits.py`) before each commit.

`requirements-min.txt` pins the oldest supported Home Assistant (2025.12.0, Python 3.13);
run the tests with it as well before a release.

Current Home Assistant does not import on Windows (`fcntl`). On Windows, run the tests in a
container:

```sh
docker run --rm -v "$PWD:/repo" -v opha-venv:/venv -w /repo python:3.14-slim \
  sh -c "python -m venv /venv && /venv/bin/pip install -q -r requirements-dev.txt && /venv/bin/python -m pytest tests"
```

Test fixtures contain made-up values only.
