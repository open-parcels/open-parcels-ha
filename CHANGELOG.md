# Changelog

## 0.11.0 (2026-11-26)

Needs an Open Parcels server whose `GET /inbox-form` lists the inbox's actions (2.0.0-dev
builds with "Read old emails again"); it is read on every load.

### Added

- Inbox actions as buttons: "Read old emails again" on every inbox's device, from
  `GET /api/v1/inbox-form` (inboxes are no carrier, so `GET /carriers` does not list them).

## 0.10.0 (2026-11-26)

Needs an Open Parcels server with imported history (`parcel.imported`, server migration 0030).

### Changed

- Server events marked `imported: true` (what an import job stores, such as Amazon's "Import
  history") update the data but fire no bus event, so imported orders and deliveries never run
  automations; `parcel.imported` reloads the data.

## 0.9.0 (2026-11-26)

Needs an Open Parcels server with connection actions and jobs (it reads `GET /jobs` on every
load).

### Added

- Connection actions: a button per action marked for Home Assistant, and the services
  `open_parcels.run_action` and `open_parcels.cancel_job`.
- `sensor.<device>_job`: the connection's newest job, kept current by the `job.*` live events,
  and the bus events `open_parcels_job_paused` and `open_parcels_job_finished`.

## 0.8.0

Needs an Open Parcels server with connection state reasons (2.0.0-dev builds after server commit `ed2426e`). See
[MIGRATION.md](MIGRATION.md#upgrading-from-07).

### Changed (breaking)

- Connection state sensors: the `detail` attribute is replaced by `state_reason` (a code) and
  `carrier_message` (the provider's or carrier's own words); so is `detail` in the bus event
  `open_parcels_connection_state_changed`.
- `error` of `sensor.open_parcels_last_backup` and `open_parcels_backup_failed` is a code
  (`no_space`, `permission_denied`, `read_only`, `not_a_folder`, `failed`).
- The deliveries calendar builds a return's drop-off and cost lines from `drop_off` and `cost`
  (the server no longer sends English `details`) and writes every title and description line in
  Home Assistant's language (English or German), statuses included.
- Discovery texts name the HA app ("The Open Parcels HA app at … offers its API token").

## 0.7.0

Needs an Open Parcels server with `GET /api/v1/status`, `/api/v1/data/backups` and order totals
(2.0.0-dev or newer). See [MIGRATION.md](MIGRATION.md#upgrading-from-06).

### Added

- `binary_sensor.open_parcels_server_health`: on while the server reports `degraded`, with each
  part's state; re-read after connection changes and backups.
- `sensor.open_parcels_last_backup`: the newest backup's time, result, reason, file and size,
  kept current by the `backup.finished` live event.
- `button.open_parcels_back_up_now` (only with a token that holds the admin scope).
- Bus events `open_parcels_order_created` (an email started an order, with its total) and
  `open_parcels_backup_failed`.
- Order sensors: `order_id`, `order_status` and `total` (from `GET /api/v1/orders`).
