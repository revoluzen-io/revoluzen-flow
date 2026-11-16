/**
 * The number of connectors the product quotes must be the number the New
 * Connection page offers.
 *
 * "76 connectors" is typed by hand in the Dashboard's new-workspace card
 * (DashboardPage.tsx), in About (SettingsPage.tsx), in README.md and in the
 * docs (docs/connectors.md, docs/product_facts/08_connectors.md). The Connections
 * gallery counts the real list, and Help → Node Reference counts the real list
 * too (help/ConnectorCoverage.tsx). If this test fails, a connector was added,
 * retired or moved to Coming soon: update those places along with this number.
 */
import { describe, expect, it } from 'vitest';
import { CONNECTION_TYPES, connectorStatus } from '../components/pages/ConnectionsPage';

describe('connectors on offer', () => {
  const offered = CONNECTION_TYPES.filter((t) => connectorStatus(t.type) !== 'roadmap');

  // 2026-11-11: 66 -> 67 with Windows File Share (SMB), 67 -> 68 with ODBC database.
  // 2026-11-13: 68 -> 76. RabbitMQ, Pulsar, Event Hubs and Kinesis got source
  // and destination steps and left Coming soon; SQS, Pub/Sub, NATS and MQTT
  // were added. Redpanda, Confluent Cloud and Amazon MSK are part of the Kafka
  // card, not cards of their own (the founders' choice).
  it('is the 76 the Dashboard, About and the docs quote (86 declared, 10 marked Coming soon)', () => {
    expect(CONNECTION_TYPES.length).toBe(86);
    expect(CONNECTION_TYPES.length - offered.length).toBe(10);
    expect(offered.length).toBe(76);
  });

  it('lists each connection type once', () => {
    expect(new Set(CONNECTION_TYPES.map((t) => t.type)).size).toBe(CONNECTION_TYPES.length);
  });
});
