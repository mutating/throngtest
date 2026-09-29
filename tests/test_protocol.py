import base64
from typing import Optional

import pytest

from throngtest.protocol import Request, WorkerError, decode, encode, read_response


@pytest.mark.parametrize('fingerprint', [None, 'fingerprint'])
def test_round_trip_and_unrelated_stdout(fingerprint: Optional[str]) -> None:
    """Round-trip requests and extract Unicode responses from unrelated process output."""
    request = Request(['-k', 'русский "test"'], fingerprint, 3, 'files', 1, 'unique:', '.')
    assert Request.unpack(request.pack()) == request
    response = {'version': 1, 'reports': [], 'message': '🌏'}
    output = 'unrelated\nunique:' + encode(response) + '\nmore unrelated output\n'
    assert read_response(output, 'unique:') == response


def test_decoder_rejects_non_object() -> None:
    """Reject valid JSON that is not a protocol object."""
    with pytest.raises(ValueError, match='JSON object'):
        decode(base64.b64encode(b'[]').decode())


@pytest.mark.parametrize(('text', 'message'), [
    ('no result', 'without a result'),
    ('marker:broken!', 'invalid worker response'),
    ('marker:' + base64.b64encode(b'\xff').decode('ascii'), 'invalid worker response'),
    ('marker:' + encode({'version': 2}), 'protocol version'),
])
def test_invalid_response(text: str, message: str) -> None:
    """Diagnose missing responses, malformed encoding, and unsupported protocol versions."""
    with pytest.raises(WorkerError, match=message):
        read_response(text, 'marker:')
