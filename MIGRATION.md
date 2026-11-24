# Moving from the per-carrier integrations and the Parcel Aggregator

Open Parcels replaces two kinds of integration:
- the per-carrier integrations (`dhl`, `gls`, `hermes`, `amazon_orders`, …), with one device per
  configured account;
- the Parcel Aggregator (`parcel_aggregator`).

The entity keys, attribute names, status values and event names stay the same wherever the
server provides the same data. What changes is listed below.

**Keep the entity IDs of a carrier account.** Label the Open Parcels connection like the old config
entry's title. For example, the DHL entry "Germany (7c6c9a)" produced the device "DHL Germany
(7c6c9a)". A connection with the label `Germany (7c6c9a)` and the provider `dhl` produces the same
device name, so its entity IDs share the old device prefix (`sensor.dhl_germany_7c6c9a_…`). The
direction sensors are named for the glossary now: `_incoming_parcels` became `_receiving_parcels`
and `_outgoing_…` became `_sending_…`, so automations on those must be updated.
A **disabled** config entry keeps its entities in the registry, so their IDs stay taken and the
new entities would get a `_2` suffix. To avoid that, do one of these before adding Open Parcels:
- delete the old entry;
- or rename the old entities' IDs (for example add `_old`) and then disable the entry.

Otherwise, rename the new entities afterwards under Settings → Entities.

## Upgrading from 0.5 (Open Parcels server 2.0)

Release 0.6 speaks only the server 2.0 API (its glossary names); update the server and this
integration together. On the first start the config entry is migrated in place (version 1 to 2):
entity unique ids and device identifiers move to the new names, so **existing entity ids, areas,
customisations and history stay**. What does change, and needs a look in automations:

| 0.5 | 0.6 |
|---|---|
| attribute `barcode` | `tracking_number` |
| attributes `planned_from`, `planned_to` | `expected_from`, `expected_to`; new `expected_days` (`from`/`to` local days) when the expected delivery is whole days |
| `direction` `incoming` / `outgoing` | `receiving` / `sending` (also in `track_parcel`) |
| attribute and event field `source_id`, `plugin` | `connection_id`, `provider` |
| service field `tracking_code` | `tracking_number` |
| events `open_parcels_outgoing_parcel_registered`, `_outgoing_parcel_delivered` | `open_parcels_sending_parcel_registered`, `_sending_parcel_delivered` (device trigger types too) |
| events `open_parcels_source_state_changed`, `_source_removed` | `open_parcels_connection_state_changed`, `_connection_removed` |
| new installs: `sensor.*_incoming_parcels`, `*_outgoing_*`, `*_source_state` | `*_receiving_parcels`, `*_sending_*`, `*_connection_state` (migrated entries keep their old entity ids) |

## Entities

| Old | New | Notes |
|---|---|---|
| `sensor.parcel_aggregator_incoming_parcels` | `sensor.open_parcels_receiving_parcels` | renamed key (`receiving`, was `incoming`); same state and attributes (`by_carrier`, `parcels`) |
| `sensor.parcel_aggregator_outgoing_parcels` | `sensor.open_parcels_sending_parcels` | renamed; same state and attributes |
| `sensor.parcel_aggregator_delivered_parcels` | `sensor.open_parcels_delivered_parcels` | same |
| `sensor.parcel_aggregator_outgoing_delivered_parcels` | `sensor.open_parcels_sending_delivered_parcels` | renamed; same state and attributes |
| `sensor.parcel_aggregator_en_route_to_pickup_point` | `sensor.open_parcels_en_route_to_pickup_point` | same |
| `sensor.parcel_aggregator_awaiting_pickup` | `sensor.open_parcels_awaiting_pickup` | same |
| `sensor.parcel_aggregator_next_delivery` | `sensor.open_parcels_next_delivery` | adds `tracking_number`, `sender`, `receiver` beside `by_carrier` and `parcel` |
| `calendar.parcel_aggregator_deliveries` | `calendar.open_parcels_deliveries` | entries come from the server's calendar: title is the server's (label, shop or sender), delivered parcels show as `Delivered: …`, whole-day windows are all-day events |
| `sensor.<carrier device>_incoming_parcels` | `sensor.<connection device>_receiving_parcels` | renamed; same state and attributes; per connection |
| `sensor.<carrier device>_en_route_to_pickup_point` | `sensor.<connection device>_en_route_to_pickup_point` | now on every connection (GLS named it "En route to ParcelShop"; it is "En route to pickup point" here) |
| `sensor.<carrier device>_awaiting_pickup` | `sensor.<connection device>_awaiting_pickup` | same |
| `sensor.<carrier device>_delivered_parcels` | `sensor.<connection device>_delivered_parcels` | same |
| `sensor.<carrier device>_outgoing_parcels`, `_outgoing_delivered_parcels` | `_sending_parcels`, `_sending_delivered_parcels` on the connection device | renamed; now on every connection |
| `sensor.<carrier device>_next_delivery` | `sensor.<connection device>_next_delivery` | same `tracking_number`, `sender`, `receiver`; adds `by_carrier` and `parcel` |
| `sensor.<carrier device>_last_successful_update` | `sensor.<connection device>_last_successful_update` | now the connection's last successful run on the server, not Home Assistant's last poll; the hub's has the last resync |
| `sensor.<carrier device>_parcel_<barcode>` | `device_tracker.<connection device>_parcel_<tracking_number>` | open parcels only, same state (the status) and suite attributes, plus a position for the map; delivered parcels have no entity and are listed in `sensor.*_delivered_parcels` (`parcel_ids`, `parcels`) |
| `button.<carrier device>_refresh` | `button.<connection device>_refresh` | asks the server to run the connection; `button.open_parcels_refresh` resyncs everything |
| `calendar.<carrier device>_deliveries` | `calendar.open_parcels_deliveries` | one calendar for all connections |
| (none) | `sensor.<connection device>_connection_state`, `binary_sensor.<connection device>_problem` | new: the connection's state, including "needs sign-in" |
| (none) | `binary_sensor.open_parcels_event_stream` | new: connection to the server's event stream |

## Parcel attributes

| Old | New |
|---|---|
| `carrier` (display name such as "DHL") | unchanged; the server's id is in the new `carrier_id` |
| `status` | unchanged values. A manual status set on the server wins; the carrier's own is in the new `carrier_status` |
| `weight`, `dimensions` | always `null` (the server does not publish them) |
| `raw` | always `null` (the server does not publish raw payloads to clients) |
| `history` entries `{timestamp, status, raw_status}` | the same keys plus `location` |
| (none) | `id`, `direction`, `label`, `manual_status`, `items`, `order`, `live` (no coordinates), `lookup`, `connection_id`, `first_seen_at`, `updated_at` |

## Events

| Old | New |
|---|---|
| `<carrier>_parcel_registered`, `parcel_aggregator_parcel_registered` | `open_parcels_parcel_registered` |
| `<carrier>_parcel_status_changed`, `parcel_aggregator_parcel_status_changed` | `open_parcels_parcel_status_changed` |
| `<carrier>_parcel_delivered`, `parcel_aggregator_parcel_delivered` | `open_parcels_parcel_delivered` |
| `<carrier>_parcel_delivery_time_changed`, `parcel_aggregator_parcel_delivery_time_changed` | `open_parcels_parcel_delivery_time_changed` |
| `<carrier>_outgoing_parcel_delivered`, `parcel_aggregator_outgoing_parcel_delivered` | `open_parcels_sending_parcel_delivered` |
| `<carrier>_outgoing_parcel_status_changed`, `parcel_aggregator_outgoing_parcel_status_changed` | no equivalent: the server has no status-change event for sending parcels |
| (none) | `open_parcels_sending_parcel_registered`, `open_parcels_parcel_live_updated`, `open_parcels_parcel_removed`, `open_parcels_connection_state_changed` |

Event data:
- It is the parcel's attributes plus `device_id`, as before. The per-carrier events included
  `raw`; these never do, as with the aggregator.
- `hub_device_id` is new.
- To filter on a carrier, use `carrier` (display name) or `carrier_id`. To filter on one
  account, use `device_id`.

Device triggers keep their type names (`parcel_registered`, `parcel_status_changed`, …). They exist
on every connection device and on the hub.

## Services

| Old | New |
|---|---|
| `<carrier>.track_parcel` (`tracking_code`) | `open_parcels.track_parcel` (`tracking_number`, optional `carrier`, `label`, `direction`) |
| `<carrier>.untrack_parcel` (`tracking_code`) | `open_parcels.archive_parcel` (`tracking_number` or `parcel_id`) |
| per-carrier refresh button | `open_parcels.refresh` (optionally for one parcel) |

## Not carried over

- **Options:**
  - Per-carrier polling options and tracking-code lists: connections and codes are managed on the
    server.
  - The delivered-parcels filter and the history option stay, on this integration.
- **The aggregator's Assist sentences and LLM tools.** Not part of this integration.
- **The aggregator's "no carrier integrations found" repair issue.** Not needed: the connections come
  from the server.
