#!/usr/bin/env python3
"""Fail before publishing if the feed points at the wrong build or archive."""
import base64
import plistlib
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def verify(app: Path, feed: Path, archive: Path) -> None:
    with (app / 'Contents/Info.plist').open('rb') as stream:
        info = plistlib.load(stream)
    namespace = '{http://www.andymatuschak.org/xml-namespaces/sparkle}'
    items = ET.parse(feed).findall('./channel/item')
    assert len(items) == 1, 'Expected exactly one release in the feed'
    item = items[0]
    assert item.findtext(namespace + 'version') == info['CFBundleVersion'], 'Build version mismatch'
    assert item.findtext(namespace + 'shortVersionString') == info['CFBundleShortVersionString'], 'Version mismatch'
    enclosure = item.find('enclosure')
    assert enclosure is not None, 'Missing update archive'
    url = urlparse(enclosure.attrib['url'])
    assert url.scheme == 'https' and url.hostname == 'github.com', 'Unexpected update host'
    assert unquote(url.path).endswith('/' + archive.name), 'Archive filename mismatch'
    assert int(enclosure.attrib['length']) == archive.stat().st_size, 'Archive size mismatch'
    signature = base64.b64decode(enclosure.attrib[namespace + 'edSignature'], validate=True)
    public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(info['SUPublicEDKey'], validate=True))
    public_key.verify(signature, archive.read_bytes())
    print('Verified: version, build, HTTPS download URL, archive size and Ed25519 signature')


if __name__ == '__main__':
    verify(*(Path(arg) for arg in sys.argv[1:]))
