"""Flyer images over Claude's size limit are shrunk, not skipped."""

import base64
import io
import os

from PIL import Image

from app.classify import MAX_IMAGE_BYTES, MAX_IMAGE_EDGE, download_image


class Client:
    def __init__(self, body, ctype):
        self.body, self.ctype = body, ctype

    def get(self, url):
        body, ctype = self.body, self.ctype

        class R:
            status_code, content, headers = 200, body, {"content-type": ctype}
        return R()


def noise_png(w, h):
    buf = io.BytesIO()
    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(buf, "PNG")   # noise doesn't compress
    return buf.getvalue()


def test_big_image_is_shrunk_to_a_jpeg_under_the_limit():
    big = noise_png(1800, 1200)
    assert len(big) > MAX_IMAGE_BYTES
    block = download_image("https://cdn.example/flyer.png", Client(big, "image/png"))
    data = base64.standard_b64decode(block["source"]["data"])
    assert block["source"]["media_type"] == "image/jpeg" and len(data) <= MAX_IMAGE_BYTES
    with Image.open(io.BytesIO(data)) as im:
        assert max(im.size) == MAX_IMAGE_EDGE and im.size[0] > im.size[1]       # aspect kept


def test_small_image_is_sent_as_it_is_and_junk_is_skipped():
    small = noise_png(40, 30)
    block = download_image("https://cdn.example/a.png", Client(small, "image/png"))
    assert block["source"]["media_type"] == "image/png" and base64.standard_b64decode(block["source"]["data"]) == small
    assert download_image("https://cdn.example/b.jpg", Client(b"x" * (MAX_IMAGE_BYTES + 1), "image/jpeg")) is None
