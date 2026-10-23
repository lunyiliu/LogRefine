"""Input hashes and template-family keys for comparing source records."""
import hashlib
import re

from .assets import canon


def mask_family(s):
    """Normalize volatile tokens to obtain a diagnostic template-family key."""
    t = canon(s)
    t = re.sub(r'\b(?:0x)?[0-9a-f]{6,}\b', '<H>', t, flags=re.IGNORECASE)
    t = re.sub(r'\b\d{1,3}(?:\.\d{1,3}){3}\b', '<IP>', t)
    t = re.sub(r'\d+', '<D>', t)
    return t


def family_hash(s):
    return hashlib.sha256(mask_family(s).encode('utf-8')).hexdigest()


def input_hash(s):
    return hashlib.sha256(canon(s).encode('utf-8')).hexdigest()
