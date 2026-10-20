# Example plugins

Event, assertion and cockpit panel plugins built against the plugin SDK (P2-T03, P7-T04), kept
small enough to copy as a starting point.

| Directory | Contents | Arrives with |
| --- | --- | --- |
| `example_events/` | an event plugin: the timeline action `example.announce` (publish a `std_msgs/msg/String` once from the vehicle's container), registered through the `fieldproof.events` entry point group and installed by the engine's development environment ([events.md](../../docs/reference/events.md#writing-a-plugin)) | P2-T03 |
