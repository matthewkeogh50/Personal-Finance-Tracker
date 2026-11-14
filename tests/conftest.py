"""Test setup. Runs against local SQLite by default; set TEST_STORAGE=dynamodb to run the same
tests against an in-process DynamoDB mock (moto)."""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["FX_AUTO_FETCH"] = "false"
os.environ.pop("API_KEY", None)

if os.environ.get("TEST_STORAGE") == "dynamodb":
    from moto import mock_aws

    os.environ.update(STORAGE="dynamodb", DYNAMODB_TABLE="finance-test", AWS_REGION="eu-central-2",
                      AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing")
    os.environ.pop("DYNAMODB_ENDPOINT_URL", None)
    mock_aws().start()
else:
    os.environ["STORAGE"] = "sqlite"
