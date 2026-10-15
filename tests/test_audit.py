import io
import json

from app.audit import AuditLogger,redact

def test_secret_mapping_keys_are_redacted():
    assert redact({"api_key":"not-for-logs","message":"okay"})=={"api_key":"[REDACTED]","message":"okay"}

def test_structured_log_is_json_and_does_not_contain_secret():
    stream=io.StringIO()
    logger=AuditLogger(path="/path-that-should-not-be-created/events.jsonl",stream=stream)
    logger.emit("TEST",api_secret="highly-secret-value",status="ok")
    record=json.loads(stream.getvalue())
    assert "highly-secret-value" not in stream.getvalue()
    assert record["payload"]["api_secret"]=="[REDACTED]"
    logger.close()
