"""Reading and writing streams of messages and database changes (2026-11-13).

Revoluzen Flow runs pipelines in batches. A streaming source reads the messages
or database changes that are waiting when its run starts, up to its Max
Messages and Timeout, and the run carries on with them as rows. The next run
starts after the last message the previous successful run read.

Two ways of remembering where a source is
-----------------------------------------
Log sources (Kafka and Redpanda, Amazon Kinesis, Azure Event Hubs, NATS
JetStream, Apache Pulsar, MySQL and MariaDB change logs) keep every message for
a while and let a reader start at any place. Flow keeps the place itself, for
each pipeline step (``positions.py``), in the metastore's ``sync_state`` table:
SQLite on one server, PostgreSQL when several servers and worker pods share one.
The place is saved only after the whole run has succeeded, and only if no other
run moved it in the meantime.

Queue sources (Amazon SQS, Google Pub/Sub, RabbitMQ, MQTT) hand a message to
one reader and forget it once the reader confirms it. Flow holds the messages
without confirming them while the run goes on (``holds.py``) and confirms them
only after the whole run has succeeded. A failed, stopped or Sample run gives
them back, so the queue delivers them again.

PostgreSQL change capture is a log source whose place is kept by the database
itself, in a replication slot; Flow moves the slot forward only after the whole
run has succeeded (``pg_cdc.py``).

Either way a message can arrive twice after a failure, but none is skipped.
"""
