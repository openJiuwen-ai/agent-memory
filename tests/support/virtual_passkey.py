"""A real ES256 authenticator for deterministic WebAuthn verification tests."""

import hashlib
import json
import secrets

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url


class VirtualPasskey:
    def __init__(self, rp_id="localhost", origin="http://localhost"):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.rp_id, self.origin = rp_id, origin

    def response(self, options, *, enroll=False, uv=True, counter=1, origin=None):
        client = json.dumps(
            {
                "type": "webauthn.create" if enroll else "webauthn.get",
                "challenge": options["publicKey"]["challenge"],
                "origin": self.origin if origin is None else origin,
                "crossOrigin": False,
            }
        ).encode()
        flags = 1 | (4 if uv else 0) | (64 if enroll else 0)
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags])
        auth_data += counter.to_bytes(4, "big")
        if enroll:
            public = self.key.public_key().public_numbers()
            cose = cbor2.dumps(
                {
                    1: 2,
                    3: -7,
                    -1: 1,
                    -2: public.x.to_bytes(32, "big"),
                    -3: public.y.to_bytes(32, "big"),
                }
            )
            auth_data += b"\0" * 16 + len(self.credential_id).to_bytes(2, "big")
            auth_data += self.credential_id + cose
            response = {
                "clientDataJSON": bytes_to_base64url(client),
                "attestationObject": bytes_to_base64url(
                    cbor2.dumps(
                        {
                            "fmt": "none",
                            "attStmt": {},
                            "authData": auth_data,
                        }
                    )
                ),
            }
        else:
            signature = self.key.sign(
                auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256())
            )
            response = {
                "clientDataJSON": bytes_to_base64url(client),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": None,
            }
        cid = bytes_to_base64url(self.credential_id)
        if base64url_to_bytes(cid) != self.credential_id:
            raise ValueError("virtual credential encoding mismatch")
        return {
            "id": cid,
            "rawId": cid,
            "type": "public-key",
            "response": response,
            "clientExtensionResults": {},
        }
