"""DynamoDB persistence.

One table, composite key:
  pk = entity name ("transactions", "income", "accounts", ...)   sk = zero-padded row id
  pk = "fx_rates"                                                sk = "GBP#2026-10-01"
  pk = "file#<stored name>"                                      sk = chunk number (uploaded screenshots)

Credentials come from the standard AWS environment variables (AWS_ACCESS_KEY_ID /
AWS_SECRET_ACCESS_KEY of an IAM user); this module never reads or logs them.
"""
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

CHUNK = 300 * 1024          # DynamoDB items are limited to 400 KB
TX_LIMIT = 100              # max operations per TransactWriteItems call


class StoreError(Exception):
    """DynamoDB could not be reached or rejected a request."""


class DynamoStore:
    def __init__(self):
        self.table = os.environ.get("DYNAMODB_TABLE") or "household-finance"
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if not region:
            raise RuntimeError("Set AWS_REGION (e.g. eu-central-2) to use DynamoDB storage")
        self.region = region
        self.client = boto3.client(
            "dynamodb", region_name=region,
            endpoint_url=os.environ.get("DYNAMODB_ENDPOINT_URL") or None,
            config=Config(retries={"max_attempts": 6, "mode": "standard"}, connect_timeout=5, read_timeout=20),
        )
        self._ser, self._de = TypeSerializer(), TypeDeserializer()

    # ------------------------------------------------------------ helpers
    def _call(self, fn, **kwargs):
        try:
            return fn(**kwargs)
        except (ClientError, BotoCoreError) as e:
            raise StoreError(str(e)) from e

    def _item(self, row: dict) -> dict:
        clean = {k: (Decimal(str(v)) if isinstance(v, float) else v) for k, v in row.items()}
        return {k: self._ser.serialize(v) for k, v in clean.items()}

    def _key(self, pk: str, sk: str) -> dict:
        return {"pk": {"S": pk}, "sk": {"S": sk}}

    def _query(self, pk: str, projection: str | None = None):
        kwargs = dict(TableName=self.table, KeyConditionExpression="pk = :p",
                      ExpressionAttributeValues={":p": {"S": pk}}, ConsistentRead=True)
        if projection:
            kwargs["ProjectionExpression"] = projection
        while True:
            page = self._call(self.client.query, **kwargs)
            yield from page["Items"]
            if "LastEvaluatedKey" not in page:
                return
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    # ------------------------------------------------------------ table
    def ensure_table(self):
        try:
            self.client.describe_table(TableName=self.table)
            return
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise StoreError(str(e)) from e
        except BotoCoreError as e:
            raise StoreError(str(e)) from e
        if os.environ.get("DYNAMODB_CREATE_TABLE", "true").lower() not in ("1", "true", "yes"):
            raise StoreError(f"DynamoDB table '{self.table}' does not exist in {self.region}")
        self._call(self.client.create_table, TableName=self.table, BillingMode="PAY_PER_REQUEST",
                   AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                         {"AttributeName": "sk", "AttributeType": "S"}],
                   KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                              {"AttributeName": "sk", "KeyType": "RANGE"}])
        self._call(self.client.get_waiter("table_exists").wait, TableName=self.table)

    # ------------------------------------------------------------ rows
    def load(self, entity: str) -> list[dict]:
        """All rows of one entity as plain dicts (numbers come back as Decimal)."""
        return [{k: self._de.deserialize(v) for k, v in item.items()} for item in self._query(entity)]

    def apply(self, puts: list[tuple[str, str, dict]], deletes: list[tuple[str, str]]):
        """Write a set of changes. Up to 100 changes are atomic; larger sets go in atomic groups of 100."""
        ops = [{"Put": {"TableName": self.table, "Item": self._item({**row, "pk": pk, "sk": sk})}}
               for pk, sk, row in puts]
        ops += [{"Delete": {"TableName": self.table, "Key": self._key(pk, sk)}} for pk, sk in deletes]
        for i in range(0, len(ops), TX_LIMIT):
            self._call(self.client.transact_write_items, TransactItems=ops[i:i + TX_LIMIT])

    # ------------------------------------------------------------ files
    def put_file(self, name: str, data: bytes):
        for n, i in enumerate(range(0, max(len(data), 1), CHUNK)):
            self._call(self.client.put_item, TableName=self.table,
                       Item={**self._key(f"file#{name}", f"{n:05d}"), "data": {"B": data[i:i + CHUNK]}})

    def get_file(self, name: str) -> bytes | None:
        parts = [bytes(item["data"]["B"]) for item in self._query(f"file#{name}")]
        return b"".join(parts) if parts else None

    def delete_file(self, name: str):
        keys = [(f"file#{name}", item["sk"]["S"]) for item in self._query(f"file#{name}", "pk, sk")]
        self.apply([], keys)
