import base64

from app.kraken import sign_spot

def test_signature_is_deterministic_for_same_payload():
    secret=base64.b64encode(b"test-secret-key").decode("ascii")
    params={"nonce":"123","ordertype":"market","type":"buy","volume":"0.1","pair":"XBTUSD"}
    assert sign_spot("/0/private/AddOrder",params,secret)==sign_spot("/0/private/AddOrder",params,secret)

def test_signature_changes_when_nonce_changes():
    secret=base64.b64encode(b"test-secret-key").decode("ascii")
    assert sign_spot("/0/private/Balance",{"nonce":"123"},secret)!=sign_spot("/0/private/Balance",{"nonce":"124"},secret)
