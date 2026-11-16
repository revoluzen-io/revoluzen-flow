"""Kafka-compatible services, and Amazon MSK's IAM sign-in (2026-11-13).

One Kafka connection reaches every service that speaks the Kafka protocol; the
connection's Service only sets defaults and, for two of them, how to sign in:

=====================  ==========================================================
Apache Kafka           any protocol and SASL mechanism the cluster uses
Redpanda               the same Kafka protocol; Redpanda Cloud uses SASL_SSL with
                       SCRAM-SHA-256 or SCRAM-SHA-512
Confluent Cloud        SASL_SSL, PLAIN, the API key as username and its secret
                       as password
Amazon MSK             SASL_SSL with SCRAM-SHA-512, TLS client certificates, or
                       IAM (``AWS-MSK-IAM``, below)
Azure Event Hubs       the namespace's Kafka endpoint (Standard tier and above):
                       SASL_SSL, PLAIN, user ``$ConnectionString``, the
                       connection string as password, port 9093
Aiven                  SASL_SSL with SCRAM, or TLS client certificates
=====================  ==========================================================

Amazon MSK IAM
--------------
MSK's IAM sign-in is an OAUTHBEARER token: a presigned AWS request for the
action ``kafka-cluster:Connect``, signed with SigV4 for the service
``kafka-cluster`` and valid for 15 minutes, as AWS's own
``aws-msk-iam-sasl-signer`` makes it. botocore, which ships with Flow for S3,
does the signing. With no keys in the connection, botocore's usual chain is
used (environment, profile, the instance or pod role).
"""

from __future__ import annotations

import base64
import datetime as _dt
from urllib.parse import parse_qs, urlencode, urlparse

SERVICE_LABELS = {
    "apache_kafka": "Apache Kafka",
    "redpanda": "Redpanda",
    "confluent_cloud": "Confluent Cloud",
    "amazon_msk": "Amazon MSK",
    "azure_event_hubs": "Azure Event Hubs (Kafka endpoint)",
    "aiven": "Aiven for Apache Kafka",
    "other": "Another Kafka-compatible service",
}

_TOKEN_SECONDS = 900
_USER_AGENT = "revoluzen-flow-msk-iam/1.0"


def msk_iam_token(region: str, access_key_id: str = "", secret_access_key: str = "",
                  session_token: str = "") -> tuple[str, float]:
    """``(token, ends at)`` for Amazon MSK IAM, ``ends at`` in seconds since 1970."""
    try:
        from botocore.auth import SigV4QueryAuth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials
        from botocore.session import Session
    except ImportError:
        from revoluzen_flow.connections.addons import driver_message

        raise ValueError(f"Kafka: {driver_message('Amazon MSK IAM sign-in', 'botocore')}") from None
    if access_key_id and secret_access_key:
        credentials = Credentials(access_key_id, secret_access_key, session_token or None)
    else:
        credentials = Session().get_credentials()
        if credentials is None:
            raise ValueError(
                "Kafka: Amazon MSK IAM sign-in found no AWS keys: none in the connection, and "
                "none on the computer Flow runs on. Add an access key to the connection, or give "
                "the server an AWS role."
            )
        credentials = credentials.get_frozen_credentials()
    url = f"https://kafka.{region}.amazonaws.com/?" + urlencode({"Action": "kafka-cluster:Connect"})
    request = AWSRequest(method="GET", url=url)
    SigV4QueryAuth(credentials, "kafka-cluster", region, expires=_TOKEN_SECONDS).add_auth(request)
    signed = f"{request.url}&{urlencode({'User-Agent': _USER_AGENT})}"
    query = parse_qs(urlparse(request.url).query)
    issued = _dt.datetime.strptime(query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=_dt.timezone.utc)
    ends = issued.timestamp() + int(query["X-Amz-Expires"][0])
    token = base64.urlsafe_b64encode(signed.encode("utf-8")).decode("ascii").rstrip("=")
    return token, ends
