# Changelog

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
