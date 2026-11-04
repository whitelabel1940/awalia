"""Write a CA bundle = certifi + Windows ROOT/CA stores (covers a local HTTPS-scanning root such as Norton's)."""
import ssl
import sys
from pathlib import Path

import certifi

out = Path(sys.argv[1])
parts = [Path(certifi.where()).read_text(encoding='ascii')]
for store in ('ROOT', 'CA'):
    for cert, encoding, trust in ssl.enum_certificates(store):
        if encoding == 'x509_asn':
            parts.append(ssl.DER_cert_to_PEM_cert(cert))
out.write_text('\n'.join(parts), encoding='ascii')
print(out, len(parts))
