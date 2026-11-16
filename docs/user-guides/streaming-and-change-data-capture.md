# Streams, queues and change data capture

Revoluzen Flow reads and writes message streams and queues, and reads the changes made in a database, as part of an ordinary pipeline. This guide explains what that means in practice, how to set each one up, and what Flow promises about every message and every change.

## How Flow reads a stream

A pipeline runs in batches. Each time it runs (by hand, on a schedule or from a trigger), a stream or queue source reads the messages that are waiting, up to its **Max Messages** and within its **Timeout**, and the rest of the pipeline works on them as rows. The next run carries on after the last message the previous successful run read.

Flow does not keep a pipeline running all the time. To keep data moving, schedule the pipeline often (every minute, for example).

### What Flow promises

- **No message is skipped.** Flow moves its place in a stream, or confirms the messages it took from a queue, only after the whole run has succeeded, destinations included.
- **After a failure a message can arrive twice.** A run that fails or is stopped leaves its messages for the next run. Use a destination that tolerates a second copy (the Database destination's **Merge** or **Apply changes** mode, for example).
- **Sample runs and Test Node change nothing.** They read what is waiting, show it to you, and leave the stream or queue exactly as it was.
- **Several servers share one place.** Flow keeps each step's place in its own database. When several Flow Enterprise servers or worker pods share one PostgreSQL database, they all read and move the same place, and a run never puts back a place another run moved further.

## Connections

Create the connection on the **Connections** page (**New Connection**), then pick it in a pipeline's **Source** or **Destination** step. The form shows only the settings the choices you made need.

| Connection | What it reaches | Notes |
|---|---|---|
| **Kafka (Redpanda, Confluent, MSK)** | Apache Kafka, Redpanda, Confluent Cloud, Amazon MSK, Azure Event Hubs' Kafka endpoint, Aiven and other Kafka-compatible services | Choose the **Service** first. Sign-in: PLAIN, SCRAM-SHA-256/512, Amazon MSK IAM; TLS with your company's certificate authority and client certificates. Add a **Schema Registry** address for Avro messages. Needs the Kafka add-on. |
| **Amazon Kinesis** | Kinesis Data Streams | AWS region and keys, or the AWS role of the computer Flow runs on. |
| **Amazon SQS** | Standard and FIFO queues | As Kinesis. |
| **Google Pub/Sub** | Subscriptions (to read) and topics (to write) | A service account key with the Pub/Sub Subscriber or Publisher role. Works with the Pub/Sub emulator too. |
| **Azure Event Hubs** | Event hubs, any tier | The namespace's connection string. Needs the azure-eventhub add-on. (Standard tier and above can also be reached through the Kafka connection.) |
| **MQTT** | Any MQTT 3.1.1 broker | Host, port, sign-in, TLS. Needs the paho-mqtt add-on. |
| **RabbitMQ** | Queues and exchanges | Host, virtual host, sign-in, TLS. Needs the pika add-on. |
| **NATS JetStream** | Streams, by subject | Servers and a user, token or NKey. Needs the nats-py add-on. |
| **Apache Pulsar** | Topics | Service URL and token. Needs the pulsar-client add-on. |

An add-on is a client library that a standard install leaves out. An administrator installs it on **Settings > Add-ons**; until then, Test connection and every run say which one is missing.

## Reading

In a pipeline, add a **Source** step and pick the connection. Then:

- **Topic, stream, queue or subscription**: what to read. For Kinesis, SQS and Event Hubs it can come from the connection.
- **Message Format**: **JSON** (a JSON object's fields become columns), **Text** (the whole message in one column), and for Kafka also **Avro** (with the connection's Schema Registry) and **Debezium change events**.
- **Include message details**: adds the message's key, time and attributes as columns (`_kafka_key`, `_kafka_headers`, `_kafka_timestamp` for Kafka; `_message_id`, `_message_key`, `_message_time`, `_message_attributes` for the others). Times are in UTC.
- **First run starts at** (Kafka, Kinesis, Event Hubs, NATS, Pulsar): the oldest message the stream keeps, or only messages that arrive after the first run.
- **Hold Time** (SQS, Pub/Sub, RabbitMQ, MQTT): how long the queue keeps the run's messages from other readers. If a run takes longer than this, the queue may hand those messages out again.

### Logs and queues

Kafka, Kinesis, Event Hubs, NATS and Pulsar keep every message for a while and let a reader start anywhere. For these, Flow remembers each step's place. The step's form shows it under **Read position**, and **Reset** sends the next run back to its first-run starting point.

SQS, Pub/Sub, RabbitMQ and MQTT hand each message to one reader. Flow takes the messages without confirming them, and confirms them (so the queue removes them) only after the run succeeds; after a failure, a stop or a Sample run it gives them back. For MQTT, Flow keeps a session for each pipeline step on the broker, and messages that were not confirmed come again with the next run.

## Writing

Add a **Destination** step and pick the connection. Each row becomes one message: a JSON object, or with **Text** one column's text. For Kafka, **Avro** registers the rows' schema in the Schema Registry under `<topic>-value`.

- **Key Column**: the message key. For Kafka and Kinesis, messages with one key stay in order; for an SQS FIFO queue it is the message group.
- **Headers / Attributes Column**: a column holding a JSON object, such as `{"source": "flow"}`; each field becomes a header or attribute.

Flow checks that every message was accepted. If the service refuses any, the run fails and says how many.

## Change data capture

Change data capture reads the inserts, updates and deletes made in a database since the last run, so a copy elsewhere can be kept up to date without reloading whole tables. It is a Revoluzen Flow Enterprise feature; the open-source edition shows the step but does not run it.

1. Add a **Source** step and pick a **PostgreSQL**, **MySQL** or **MariaDB** connection.
2. Under **Read**, choose **Changes: inserts, updates and deletes**.
3. Name the **Tables** (`schema.table` on PostgreSQL, `database.table` on MySQL and MariaDB).
4. Choose where the first run starts: **only changes from now on**, or **a copy of every row, then the changes**.

Each change becomes a row with the table's columns and:

| Column | Meaning |
|---|---|
| `_op` | `insert`, `update`, `delete`, `truncate`, or `snapshot` for rows of the first copy |
| `_table` | the table it happened in |
| `_committed_at` | when the database committed it (UTC) |
| `_seq` / `_lsn` / `_position` | its order in the database's change log |
| `_unchanged` | (PostgreSQL) columns whose large value an update did not send |

A database transaction is never split between runs.

### Keeping a copy of a table

Add a **Destination** with a database connection, choose **Write Mode: Apply changes**, and name the **Key Columns** (the table's primary key). Inserts and updates replace the key's row, deletes remove it, and only the last change of each key counts. Applying the same changes twice leaves the same table, which is what makes the "a change can arrive twice" rule harmless. One destination writes one table: if the step captures several tables, add a **Filter** on `_table` before each destination.

### What the database needs

**PostgreSQL** (11 or later)

- `wal_level = logical` (Amazon RDS / Aurora: `rds.logical_replication = 1`; Google Cloud SQL: `cloudsql.logical_decoding = on`).
- A user with the `REPLICATION` right (Amazon RDS: `GRANT rds_replication`) who may create a publication for the tables, or a publication your administrator created (enter its name in the step).
- Each table needs a primary key or `REPLICA IDENTITY FULL`. Flow refuses a table without one, because adding it would stop updates and deletes on that table in the database.

Flow creates one replication slot per pipeline step. The database keeps its change log for that slot until Flow reads it, so a pipeline that stops running makes the database keep log files: delete the pipeline (which removes the slot) or press **Reset** if you no longer need it.

**MySQL and MariaDB**

- The binary log on, `binlog_format = ROW`, `binlog_row_image = FULL`; for ENUM and SET columns also `binlog_row_metadata = FULL`.
- A user with `REPLICATION SLAVE` and `REPLICATION CLIENT` (MariaDB 10.5+: also `BINLOG MONITOR`).
- The server keeps its log files for `binlog_expire_logs_seconds`. If a pipeline does not run within that time, Flow says the changes in between can no longer be read; press **Reset** to start again.
- Needs the mysql-replication add-on.

**SQL Server, Oracle, MongoDB, Db2 and others**: use Debezium to write the changes to Kafka, then read that topic with a Kafka **Source** and **Message Format: Debezium change events**. The rows have the same `_op`, `_table` and `_committed_at` columns.

## Plain answers to common questions

- **Is this real-time?** Messages arrive with each run, so the delay is the pipeline's schedule plus its run time: seconds to minutes, not milliseconds.
- **Can two pipelines read the same topic?** Yes. Each step keeps its own place; Flow joins no Kafka consumer group, so they do not take messages from each other. On a queue (SQS, Pub/Sub, RabbitMQ), two readers share the messages, as a queue always does.
- **What does a failed run cost?** Nothing is lost. The next run reads the same messages or changes again.
