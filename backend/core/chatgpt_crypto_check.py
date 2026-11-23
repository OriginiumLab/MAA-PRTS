"""Exercise the packaged OAuth verifier with a local, disposable RSA key."""
from __future__ import annotations

import json
import time

from . import chatgpt_auth as auth


def verify():
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid='packaging-test', use='sig', alg='RS256')
    client_id = 'oaiapp_packaging_test'
    nonce = 'packaging-test-nonce'
    subject = 'packaging-test-user'
    claims = {
        'iss': auth.ISSUER, 'aud': client_id, 'sub': subject,
        'exp': int(time.time()) + 60, 'nonce': nonce,
    }
    headers = {'kid': jwk['kid']}
    keys = {'keys': [jwk]}
    token = jwt.encode(claims, key, algorithm='RS256', headers=headers)
    if auth._verify_identity(token, client_id, nonce, '', keys)['sub'] != subject:
        raise RuntimeError('RS256 self-test returned the wrong subject')

    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    invalid = jwt.encode(claims, wrong_key, algorithm='RS256', headers=headers)
    try:
        auth._verify_identity(invalid, client_id, nonce, '', keys)
    except auth.ChatGPTError:
        return
    raise RuntimeError('RS256 self-test accepted a token signed by the wrong key')
